"""Клиент Fragment для покупки звёзд Telegram.

Работает только в режиме аккаунта с пройденным KYC (cookies fragment.com) и платит
со своего TON-кошелька. Протокол взят из fragment-api-py 12.1.0 (см. NOTICE.md).

Ошибки разделены по тому, ушли ли деньги в сеть:

* ``FragmentRetryableError`` — до отправки перевода. Заказ можно безопасно повторить.
* ``FragmentBroadcastUncertainError`` — перевод отправлялся, но результат неизвестен.
  Повторять нельзя (можно заплатить дважды), нужна ручная проверка.
* ``FragmentRecipientNotFoundError`` / ``FragmentConfigurationError`` — повтор не поможет.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import structlog
from curl_cffi import requests

from app.external.fragment._vendor import constants as fragment_constants
from app.external.fragment._vendor.exceptions import (
    ConfirmationTimeout,
    SeqnoError,
    VerificationError,
    WalletError,
)
from app.external.fragment._vendor.html import parse_stars_price_from_html
from app.external.fragment._vendor.http import build_headers, fetch_fragment_hash, post_fragment_api
from app.external.fragment._vendor.models import StarsPrice, WalletInfo
from app.external.fragment._vendor.proxy import build_curl_proxy_args, parse_proxy
from app.external.fragment._vendor.wallet import build_account_info, execute_transaction, fetch_wallet_info


logger = structlog.get_logger(__name__)

STARS_MIN = fragment_constants.STARS_PURCHASE_MIN
STARS_MAX = fragment_constants.STARS_PURCHASE_MAX
_IMPERSONATE = 'chrome120'

# Операции с TON API идут по одной на процесс и с паузой между ними: лимит провайдера общий
# на ключ, а клиент tonutils создаётся на каждую операцию и считает запросы только свои.
# Без этого проверка кошелька из админки во время покупки ловит 429 на подтверждении
# перевода — и заказ уходит на ручную проверку.
_ton_lock = asyncio.Lock()
_ton_last_done = 0.0
_CONFIRM_REFERER = 'stars/buy'


class FragmentStarsError(Exception):
    """Базовая ошибка покупки звёзд."""


class FragmentConfigurationError(FragmentStarsError):
    """Неверная настройка: cookies, кошелёк, ключ API, нужен KYC."""


class FragmentRecipientNotFoundError(FragmentStarsError):
    """Fragment не нашёл получателя по username."""


class FragmentRetryableError(FragmentStarsError):
    """Сбой до отправки перевода — деньги не ушли, повтор безопасен."""


class FragmentBroadcastUncertainError(FragmentStarsError):
    """Перевод отправлялся, итог неизвестен — повторять нельзя."""

    def __init__(self, message: str, *, req_id: str | None = None) -> None:
        super().__init__(message)
        self.req_id = req_id


@dataclass(frozen=True)
class StarsQuote:
    """Цена звёзд на Fragment."""

    quantity: int
    ton_price: str
    usd_price: str


@dataclass(frozen=True)
class StarsPurchaseReceipt:
    """Итог успешной покупки."""

    req_id: str
    tx_hash: str
    cost_nanoton: int
    recipient_name: str
    fragment_confirmed: bool


BeforeBroadcast = Callable[[str, int], Awaitable[None]]


def parse_cookies(raw: str | dict[str, str]) -> dict[str, str]:
    """Принимает cookies строкой ``k=v; k2=v2``, JSON-объектом или словарём."""
    if isinstance(raw, dict):
        cookies = {str(k): str(v) for k, v in raw.items()}
    else:
        text = (raw or '').strip()
        if text.startswith('{'):
            try:
                cookies = {str(k): str(v) for k, v in json.loads(text).items()}
            except (ValueError, AttributeError) as exc:
                raise FragmentConfigurationError('Cookies Fragment: некорректный JSON') from exc
        else:
            cookies = {}
            for item in text.split(';'):
                if '=' in item:
                    key, value = item.strip().split('=', 1)
                    cookies[key] = value
    missing = [key for key in fragment_constants.REQUIRED_COOKIE_KEYS_WALLET if not cookies.get(key, '').strip()]
    if missing:
        raise FragmentConfigurationError(f'Cookies Fragment: нет ключей {", ".join(missing)}')
    return cookies


class FragmentStarsClient:
    """Покупка звёзд через Fragment. Атрибуты seed/api_key/api_provider/wallet_version
    читает слой ``_vendor.wallet``."""

    def __init__(
        self,
        *,
        cookies: str | dict[str, str],
        seed: str,
        api_key: str,
        api_provider: str = 'toncenter',
        wallet_version: str = 'V5R1',
        timeout: float = fragment_constants.DEFAULT_TIMEOUT,
        proxy: str | None = None,
        ton_api_rps: float = 1.0,
    ) -> None:
        self.cookies = parse_cookies(cookies)

        words = (seed or '').split()
        if len(words) not in (12, 18, 24):
            raise FragmentConfigurationError('Seed-фраза кошелька должна содержать 12, 18 или 24 слова')
        self.seed = ' '.join(words)

        if not (api_key or '').strip():
            raise FragmentConfigurationError('Не задан ключ API TON (Toncenter или TonAPI)')
        self.api_key = api_key.strip()

        provider = (api_provider or '').strip().lower()
        if provider not in fragment_constants.SUPPORTED_API_PROVIDERS:
            raise FragmentConfigurationError(f'Провайдер TON API не поддерживается: {api_provider}')
        self.api_provider = provider

        version = (wallet_version or '').strip().upper()
        if version not in fragment_constants.SUPPORTED_WALLET_VERSIONS:
            raise FragmentConfigurationError(f'Версия кошелька не поддерживается: {wallet_version}')
        self.wallet_version = version

        self.timeout = float(timeout)
        self.ton_api_rps = max(0.0, float(ton_api_rps or 0))
        self.proxy = proxy.strip() if proxy else None
        if self.proxy:
            parse_proxy(self.proxy)

    @asynccontextmanager
    async def _ton_slot(self) -> AsyncIterator[None]:
        global _ton_last_done
        async with _ton_lock:
            if self.ton_api_rps > 0:
                wait = _ton_last_done + 1.1 / self.ton_api_rps - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            try:
                yield
            finally:
                _ton_last_done = time.monotonic()

    def _session(self) -> requests.AsyncSession:
        return requests.AsyncSession(
            cookies=self.cookies,
            timeout=self.timeout,
            impersonate=_IMPERSONATE,
            **build_curl_proxy_args(self.proxy),
        )

    async def _hash(self, page_url: str) -> tuple[str, dict[str, str]]:
        headers = build_headers(page_url)
        fragment_hash = await fetch_fragment_hash(self.cookies, headers, page_url, self.timeout, proxy=self.proxy)
        return fragment_hash, headers

    @staticmethod
    def _check_quantity(quantity: int) -> None:
        if not isinstance(quantity, int) or not (STARS_MIN <= quantity <= STARS_MAX):
            raise FragmentConfigurationError(f'Количество звёзд должно быть от {STARS_MIN} до {STARS_MAX}')

    async def get_stars_price(self, quantity: int) -> StarsQuote:
        """Текущая цена ``quantity`` звёзд на Fragment."""
        self._check_quantity(quantity)
        try:
            fragment_hash, headers = await self._hash(fragment_constants.STARS_PAGE)
            async with self._session() as session:
                result = await post_fragment_api(
                    session,
                    fragment_hash,
                    headers,
                    {'stars': '0', 'quantity': str(quantity), 'method': 'updateStarsPrices'},
                )
        except Exception as exc:
            raise FragmentRetryableError(f'Fragment: не удалось получить цену ({exc})') from exc
        ton_price, usd_price = parse_stars_price_from_html(result.get('cur_price', ''))
        price = StarsPrice(stars=quantity, gram_price=ton_price or '0', usd_price=usd_price or '0')
        return StarsQuote(quantity=quantity, ton_price=price.gram_price, usd_price=price.usd_price)

    async def get_wallet(self) -> WalletInfo:
        """Адрес и баланс платёжного кошелька."""
        for attempt in range(3):
            try:
                async with self._ton_slot():
                    return await fetch_wallet_info(self)
            except Exception as exc:
                if '429' in str(exc) and attempt < 2:
                    continue  # следующий слот очереди наступит не раньше чем через ~1 с
                raise FragmentRetryableError(f'Кошелёк: не удалось получить баланс ({exc})') from exc
        raise AssertionError('unreachable')

    async def purchase_stars(
        self,
        username: str,
        quantity: int,
        *,
        show_sender: bool = False,
        before_broadcast: BeforeBroadcast | None = None,
    ) -> StarsPurchaseReceipt:
        """Покупает ``quantity`` звёзд для ``username``.

        ``before_broadcast(req_id, cost_nanoton)`` вызывается непосредственно перед
        отправкой перевода — чтобы вызывающий успел сохранить, что деньги уходят.
        """
        self._check_quantity(quantity)
        target = (username or '').strip().lstrip('@')
        if not target:
            raise FragmentRecipientNotFoundError('Не указан получатель')

        try:
            fragment_hash, headers = await self._hash(fragment_constants.STARS_PAGE)
            async with self._session() as session:
                found = await post_fragment_api(
                    session,
                    fragment_hash,
                    headers,
                    {'method': 'searchStarsRecipient', 'query': target, 'quantity': ''},
                )
                recipient_info = (found or {}).get('found') or {}
                recipient = recipient_info.get('recipient')
                if not recipient:
                    raise FragmentRecipientNotFoundError(f'Получатель @{target} не найден на Fragment')

                initialized = await post_fragment_api(
                    session,
                    fragment_hash,
                    headers,
                    {
                        'method': 'initBuyStarsRequest',
                        'recipient': recipient,
                        'quantity': str(quantity),
                        'payment_method': 'ton',
                    },
                )
                if initialized.get('error'):
                    raise FragmentRetryableError(f'Fragment отклонил заявку: {initialized["error"]}')
                req_id = str(initialized.get('req_id') or '')
                if not req_id:
                    raise FragmentRetryableError('Fragment не вернул номер заявки')

                async with self._ton_slot():
                    account = await build_account_info(self)
                transaction = await post_fragment_api(
                    session,
                    fragment_hash,
                    headers,
                    {
                        'method': 'getBuyStarsLink',
                        'account': json.dumps(account),
                        'device': fragment_constants.DEVICE_FINGERPRINT,
                        'transaction': 1,
                        'id': req_id,
                        'show_sender': int(show_sender),
                    },
                )
        except FragmentStarsError:
            raise
        except VerificationError as exc:
            raise FragmentConfigurationError('Fragment требует KYC для этого аккаунта') from exc
        except Exception as exc:
            raise FragmentRetryableError(f'Fragment: заявка не создана ({exc})') from exc

        if transaction.get('need_verify'):
            raise FragmentConfigurationError('Fragment требует KYC для этого аккаунта')
        if transaction.get('error'):
            raise FragmentRetryableError(f'Fragment: {transaction["error"]}')
        messages = (transaction.get('transaction') or {}).get('messages') or []
        if not messages or transaction.get('evm'):
            raise FragmentRetryableError('Fragment вернул неожиданную форму оплаты')
        cost_nanoton = sum(int(message['amount']) for message in messages)

        if before_broadcast is not None:
            await before_broadcast(req_id, cost_nanoton)

        try:
            async with self._ton_slot():
                tx_result = await execute_transaction(self, transaction)
        except (WalletError, SeqnoError) as exc:
            # Обе ошибки возникают до отправки: проверка баланса и чтение seqno.
            raise FragmentRetryableError(f'Кошелёк: перевод не отправлен ({exc})') from exc
        except ConfirmationTimeout as exc:
            raise FragmentBroadcastUncertainError(
                f'Перевод отправлен, но подтверждение не дождались ({exc})', req_id=req_id
            ) from exc
        except Exception as exc:
            raise FragmentBroadcastUncertainError(f'Сбой при отправке перевода ({exc})', req_id=req_id) from exc

        fragment_confirmed = False
        if tx_result.boc:
            try:
                fragment_hash, headers = await self._hash(f'{fragment_constants.FRAGMENT_BASE_URL}/{_CONFIRM_REFERER}')
                async with self._session() as session:
                    await post_fragment_api(
                        session,
                        fragment_hash,
                        headers,
                        {'method': 'confirmReq', 'id': req_id, 'boc': tx_result.boc},
                    )
                fragment_confirmed = True
            except Exception as exc:
                # Перевод уже в сети: неподтверждённый confirmReq — не повод платить снова.
                logger.warning('Fragment: confirmReq не прошёл', req_id=req_id, error=str(exc))

        return StarsPurchaseReceipt(
            req_id=req_id,
            tx_hash=tx_result.tx_hash,
            cost_nanoton=cost_nanoton,
            recipient_name=str(recipient_info.get('name') or target),
            fragment_confirmed=fragment_confirmed,
        )
