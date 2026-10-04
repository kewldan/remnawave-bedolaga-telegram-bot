"""Клиент Fragment для звёзд: разбор cookies, классификация ошибок по моменту отправки денег."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tonutils.exceptions import ProviderResponseError

from app.external.fragment import client as fc
from app.external.fragment._vendor import wallet as vendor_wallet
from app.external.fragment._vendor.exceptions import ConfirmationTimeout, TransactionError, WalletError


_COOKIES = 'stel_ssid=a; stel_dt=b; stel_token=c; stel_ton_token=d'
_SEED = ' '.join(['word'] * 24)
# Любой валидный адрес TON (здесь — контракт USDT), сеть не вызывается.
_ADDRESS = 'EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs'


def _client(ton_api_rps: float = 0) -> fc.FragmentStarsClient:
    # Лимит запросов проверяется отдельными тестами; в остальных он только замедлял бы прогон.
    return fc.FragmentStarsClient(cookies=_COOKIES, seed=_SEED, api_key='key', ton_api_rps=ton_api_rps)


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fragment_api(*, recipient: str | None = 'R123', link: dict | None = None, confirm_error: bool = False):
    """Поддельный post_fragment_api: отвечает по полю method."""

    async def post(session, fragment_hash, headers, payload):
        method = payload['method']
        if method == 'searchStarsRecipient':
            return {'found': {'recipient': recipient, 'name': 'Pavel'}} if recipient else {'found': {}}
        if method == 'initBuyStarsRequest':
            return {'req_id': 'REQ-1'}
        if method == 'getBuyStarsLink':
            return link or {'transaction': {'messages': [{'address': 'EQ', 'amount': '420000000', 'payload': ''}]}}
        if method == 'confirmReq':
            if confirm_error:
                raise RuntimeError('confirm failed')
            return {'ok': True}
        raise AssertionError(method)

    return post


@pytest.fixture
def fragment_io():
    with (
        patch.object(fc, 'fetch_fragment_hash', AsyncMock(return_value='hash')),
        patch.object(fc, 'build_account_info', AsyncMock(return_value={'address': 'A'})),
        patch.object(fc.FragmentStarsClient, '_session', lambda self: _Session()),
    ):
        yield


@pytest.mark.parametrize(
    'raw',
    [_COOKIES, '{"stel_ssid":"a","stel_dt":"b","stel_token":"c","stel_ton_token":"d"}'],
)
def test_parse_cookies_string_and_json(raw):
    assert fc.parse_cookies(raw)['stel_ton_token'] == 'd'


def test_configuration_errors():
    with pytest.raises(fc.FragmentConfigurationError):
        fc.parse_cookies('stel_ssid=a; stel_dt=b')
    with pytest.raises(fc.FragmentConfigurationError):
        fc.FragmentStarsClient(cookies=_COOKIES, seed='only five words here now', api_key='k')
    with pytest.raises(fc.FragmentConfigurationError):
        fc.FragmentStarsClient(cookies=_COOKIES, seed=_SEED, api_key=' ')
    with pytest.raises(fc.FragmentConfigurationError):
        fc.FragmentStarsClient(cookies=_COOKIES, seed=_SEED, api_key='k', wallet_version='V1')


@pytest.mark.asyncio
async def test_purchase_calls_before_broadcast_before_sending(fragment_io):
    order: list[str] = []

    async def before(req_id, cost):
        order.append(f'mark:{req_id}:{cost}')

    async def execute(client, transaction):
        order.append('send')
        return SimpleNamespace(tx_hash='TX', boc='BOC')

    with (
        patch.object(fc, 'post_fragment_api', _fragment_api()),
        patch.object(fc, 'execute_transaction', execute),
    ):
        receipt = await _client().purchase_stars('@durov', 100, before_broadcast=before)

    assert order == ['mark:REQ-1:420000000', 'send']
    assert (receipt.req_id, receipt.tx_hash, receipt.cost_nanoton) == ('REQ-1', 'TX', 420_000_000)
    assert receipt.recipient_name == 'Pavel'
    assert receipt.fragment_confirmed is True


@pytest.mark.asyncio
async def test_unknown_recipient_and_kyc(fragment_io):
    with patch.object(fc, 'post_fragment_api', _fragment_api(recipient=None)):
        with pytest.raises(fc.FragmentRecipientNotFoundError):
            await _client().purchase_stars('ghost_user', 100)
    with patch.object(fc, 'post_fragment_api', _fragment_api(link={'need_verify': True})):
        with pytest.raises(fc.FragmentConfigurationError):
            await _client().purchase_stars('durov', 100)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('error', 'expected'),
    [
        (WalletError('low balance'), fc.FragmentRetryableError),
        (ConfirmationTimeout('no confirm'), fc.FragmentBroadcastUncertainError),
        (TransactionError('provider 500'), fc.FragmentBroadcastUncertainError),
        (RuntimeError('socket closed'), fc.FragmentBroadcastUncertainError),
    ],
)
async def test_send_errors_are_classified_by_money_risk(fragment_io, error, expected):
    with (
        patch.object(fc, 'post_fragment_api', _fragment_api()),
        patch.object(fc, 'execute_transaction', AsyncMock(side_effect=error)),
    ):
        with pytest.raises(expected):
            await _client().purchase_stars('durov', 100)


@pytest.mark.asyncio
async def test_failed_confirm_req_does_not_fail_paid_purchase(fragment_io):
    with (
        patch.object(fc, 'post_fragment_api', _fragment_api(confirm_error=True)),
        patch.object(fc, 'execute_transaction', AsyncMock(return_value=SimpleNamespace(tx_hash='TX', boc='B'))),
    ):
        receipt = await _client().purchase_stars('durov', 100)
    assert receipt.fragment_confirmed is False


@pytest.mark.asyncio
async def test_quantity_bounds_checked_before_any_request():
    with pytest.raises(fc.FragmentConfigurationError):
        await _client().purchase_stars('durov', fc.STARS_MIN - 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [500, 406, 400])
async def test_vendored_broadcast_never_resends_after_ambiguous_error(monkeypatch, code):
    """Патч к fragment-api-py 12.1.0: повтор отправки только при 429 (см. NOTICE.md)."""
    monkeypatch.setattr(vendor_wallet.asyncio, 'sleep', AsyncMock())
    wallet = MagicMock()
    wallet.refresh = AsyncMock()
    # tonutils 2.x отправляет через transfer_message — путь, который работает в проде.
    wallet.transfer_message = AsyncMock(
        side_effect=ProviderResponseError(code=code, message='seqno duplicate message', endpoint='send')
    )
    with pytest.raises((ProviderResponseError, TransactionError)):
        await vendor_wallet._broadcast_with_retry(wallet, [_ADDRESS], [1], [None])
    assert wallet.transfer_message.await_count == 1


@pytest.mark.asyncio
async def test_vendored_broadcast_retries_rate_limit(monkeypatch):
    monkeypatch.setattr(vendor_wallet.asyncio, 'sleep', AsyncMock())
    wallet = MagicMock()
    wallet.refresh = AsyncMock()
    # tonutils 2.x отправляет через transfer_message — путь, который работает в проде.
    wallet.transfer_message = AsyncMock(
        side_effect=[ProviderResponseError(code=429, message='rate', endpoint='send'), 'ok'],
    )
    assert await vendor_wallet._broadcast_with_retry(wallet, [_ADDRESS], [1], [None]) == 'ok'
    assert wallet.transfer_message.await_count == 2


@pytest.mark.asyncio
async def test_ton_operations_are_spaced_by_rate_limit(monkeypatch):
    """tonapi без платного тарифа — 1 запрос в секунду: операции идут по одной и с паузой."""
    clock = [100.0]
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(round(seconds, 2))
        clock[0] += seconds

    monkeypatch.setattr(fc.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(fc.asyncio, 'sleep', fake_sleep)
    monkeypatch.setattr(fc, '_ton_last_done', 0.0)
    wallet = SimpleNamespace(address='A', state='active', gram_balance=1.0, usdt_balance=0.0)
    monkeypatch.setattr(fc, 'fetch_wallet_info', AsyncMock(return_value=wallet))

    client = _client(ton_api_rps=1.0)
    await client.get_wallet()
    await client.get_wallet()
    assert sleeps == [1.1]  # первая — сразу, вторая ждёт окно лимита


@pytest.mark.asyncio
async def test_wallet_read_retries_rate_limit(monkeypatch):
    monkeypatch.setattr(fc, '_ton_last_done', 0.0)
    monkeypatch.setattr(fc.asyncio, 'sleep', AsyncMock())
    wallet = SimpleNamespace(address='A', state='active', gram_balance=1.0, usdt_balance=0.0)
    fetch = AsyncMock(side_effect=[RuntimeError('429 rate limit: limit for tier'), wallet])
    monkeypatch.setattr(fc, 'fetch_wallet_info', fetch)
    assert (await _client().get_wallet()) is wallet
    assert fetch.await_count == 2


def test_vendored_ton_client_gets_rate_limit():
    client = SimpleNamespace(api_provider='tonapi', api_key='k', ton_api_rps=1.0)
    with patch.object(vendor_wallet, 'TonapiClient') as tonapi:
        vendor_wallet._make_ton_client(client)
    assert tonapi.call_args.kwargs['rps_limit'] == 1
    assert tonapi.call_args.kwargs['rps_period'] == pytest.approx(1.1)
