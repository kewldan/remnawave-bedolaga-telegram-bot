"""Выдача оплаченных заказов звёзд через Fragment.

Обработчик берёт заказы ``paid`` по одному (``FOR UPDATE SKIP LOCKED``), переводит в
``processing``, а прямо перед отправкой TON — в ``broadcasting`` (коммитом). Поэтому
после падения процесса понятно, могли ли уйти деньги:

* ``processing`` → перевода не было, заказ возвращается в очередь;
* ``broadcasting`` → перевод мог пройти, заказ уходит на ручную проверку (``needs_review``).

Повтор — только для ошибок до отправки; после ``STARS_SHOP_MAX_ATTEMPTS`` — возврат на баланс.
"""

from __future__ import annotations

import asyncio
import html
from datetime import UTC, datetime, timedelta

import structlog
from aiogram import Bot
from sqlalchemy import or_, select, update
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import StarsOrder, StarsOrderStatus, User
from app.external.fragment import (
    FragmentBroadcastUncertainError,
    FragmentConfigurationError,
    FragmentRecipientNotFoundError,
    FragmentRetryableError,
    FragmentStarsClient,
    StarsPurchaseReceipt,
)
from app.services.stars_notifications import (
    WalletSnapshot,
    build_completed_message,
    build_order_keyboard,
    build_vpn_offer,
)
from app.services.ton_rate_service import TonRate, get_ton_rate


logger = structlog.get_logger(__name__)

_DRY_RUN_PREFIX = 'dry-run'


def build_fragment_client() -> FragmentStarsClient:
    """Клиент Fragment из настроек; ``FragmentConfigurationError``, если чего-то не хватает."""
    if not settings.is_fragment_configured():
        raise FragmentConfigurationError(
            'Fragment не настроен: FRAGMENT_COOKIES, FRAGMENT_WALLET_MNEMONIC, ключ TON API'
        )
    return FragmentStarsClient(
        cookies=settings.FRAGMENT_COOKIES,
        seed=settings.FRAGMENT_WALLET_MNEMONIC,
        api_key=settings.FRAGMENT_TON_API_KEY,
        api_provider=settings.FRAGMENT_TON_API_PROVIDER,
        wallet_version=settings.FRAGMENT_WALLET_VERSION,
        proxy=settings.FRAGMENT_PROXY or None,
        ton_api_rps=settings.FRAGMENT_TON_API_RPS,
    )


class StarsFulfillmentService:
    def __init__(self) -> None:
        self._bot: Bot | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    def set_bot(self, bot: Bot | None) -> None:
        self._bot = bot

    def wake(self) -> None:
        """Разбудить обработчик сразу после оплаты, не дожидаясь интервала."""
        self._wake.set()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        await self.recover_interrupted()
        self._task = asyncio.create_task(self._run(), name='stars-fulfillment')
        logger.info('Звёзды: обработчик выдачи запущен')

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                if settings.STARS_SHOP_ENABLED:
                    while await self.process_next():
                        if self._stopping:
                            return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('Звёзды: сбой цикла выдачи', error=str(exc))
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(1, settings.STARS_SHOP_WORKER_INTERVAL_SECONDS))
            except TimeoutError:
                pass

    # ── Восстановление после рестарта ──────────────────────────────────────

    async def recover_interrupted(self) -> None:
        """Заказы, прерванные падением процесса: processing → paid, broadcasting → needs_review."""
        async with AsyncSessionLocal() as db:
            now = datetime.now(UTC)
            requeued = await db.execute(
                update(StarsOrder)
                .where(StarsOrder.status == StarsOrderStatus.PROCESSING.value)
                .values(status=StarsOrderStatus.PAID.value, next_attempt_at=now)
                .returning(StarsOrder.id)
            )
            requeued_ids = list(requeued.scalars())
            review = await db.execute(
                update(StarsOrder)
                .where(StarsOrder.status == StarsOrderStatus.BROADCASTING.value)
                .values(
                    status=StarsOrderStatus.NEEDS_REVIEW.value,
                    last_error='Процесс прервался во время отправки перевода — проверьте кошелёк',
                )
                .returning(StarsOrder.id)
            )
            review_ids = list(review.scalars())
            await db.commit()
        if requeued_ids or review_ids:
            logger.warning('Звёзды: восстановление после рестарта', requeued=requeued_ids, needs_review=review_ids)
        for order_id in review_ids:
            await self._notify_admin_review(order_id, 'Процесс прервался во время отправки перевода')

    # ── Обработка одного заказа ────────────────────────────────────────────

    async def _claim_next(self) -> int | None:
        async with AsyncSessionLocal() as db:
            now = datetime.now(UTC)
            result = await db.execute(
                select(StarsOrder)
                .where(
                    StarsOrder.status == StarsOrderStatus.PAID.value,
                    or_(StarsOrder.next_attempt_at.is_(None), StarsOrder.next_attempt_at <= now),
                )
                .order_by(StarsOrder.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            order = result.scalars().first()
            if order is None:
                return None
            order.status = StarsOrderStatus.PROCESSING.value
            order.processing_started_at = now
            order.attempts = (order.attempts or 0) + 1
            await db.commit()
            return order.id

    async def process_next(self) -> bool:
        """Обработать один заказ из очереди. ``True`` — заказ был."""
        order_id = await self._claim_next()
        if order_id is None:
            return False
        await self._fulfill(order_id)
        return True

    async def _set(self, order_id: int, **values: object) -> StarsOrder:
        async with AsyncSessionLocal() as db:
            order = await db.get(StarsOrder, order_id)
            for key, value in values.items():
                setattr(order, key, value)
            await db.commit()
            await db.refresh(order)
            return order

    async def _fulfill(self, order_id: int) -> None:
        async with AsyncSessionLocal() as db:
            order = await db.get(StarsOrder, order_id)
            recipient, quantity, attempts = order.recipient_username, order.quantity, order.attempts

        async def before_broadcast(req_id: str, cost_nanoton: int) -> None:
            await self._set(
                order_id,
                status=StarsOrderStatus.BROADCASTING.value,
                fragment_req_id=req_id,
                cost_nanoton=cost_nanoton,
            )

        try:
            if settings.STARS_SHOP_DRY_RUN:
                receipt = await self._dry_run(order_id, recipient, before_broadcast)
            else:
                client = build_fragment_client()
                receipt = await client.purchase_stars(
                    recipient,
                    quantity,
                    show_sender=bool(settings.STARS_SHOP_SHOW_SENDER),
                    before_broadcast=before_broadcast,
                )
        except FragmentBroadcastUncertainError as exc:
            await self._set(order_id, status=StarsOrderStatus.NEEDS_REVIEW.value, last_error=str(exc))
            logger.error('Звёзды: итог перевода неизвестен', order_id=order_id, error=str(exc))
            await self._notify_admin_review(order_id, str(exc))
            return
        except FragmentRecipientNotFoundError as exc:
            await self._fail_and_refund(order_id, str(exc), user_reason='recipient_not_found')
            return
        except (FragmentRetryableError, FragmentConfigurationError) as exc:
            await self._retry_or_refund(order_id, attempts, str(exc))
            if isinstance(exc, FragmentConfigurationError):
                await self._notify_admin(
                    f'<b>⚠️ ЗВЁЗДЫ: ПРОБЛЕМА С НАСТРОЙКОЙ FRAGMENT</b>\n\n<blockquote>{html.escape(str(exc))}</blockquote>',
                    order_id,
                )
            return
        except Exception as exc:
            # Неожиданная ошибка: статус показывает, успел ли уйти перевод.
            async with AsyncSessionLocal() as db:
                current = await db.get(StarsOrder, order_id)
                status = current.status
            if status == StarsOrderStatus.BROADCASTING.value:
                await self._set(order_id, status=StarsOrderStatus.NEEDS_REVIEW.value, last_error=repr(exc))
                await self._notify_admin_review(order_id, repr(exc))
            else:
                await self._retry_or_refund(order_id, attempts, repr(exc))
            logger.exception('Звёзды: непредвиденная ошибка выдачи', order_id=order_id)
            return

        rate = await get_ton_rate() if receipt.cost_nanoton else None
        order = await self._set(
            order_id,
            status=StarsOrderStatus.COMPLETED.value,
            ton_tx_hash=receipt.tx_hash,
            recipient_name=receipt.recipient_name[:255],
            completed_at=datetime.now(UTC),
            last_error=None if receipt.fragment_confirmed else 'confirmReq не подтверждён Fragment',
            next_attempt_at=None,
            ton_rate_kopeks=rate.kopeks if rate else None,
            cost_kopeks=rate.nanoton_to_kopeks(receipt.cost_nanoton) if rate else None,
        )
        logger.info('Звёзды: заказ выполнен', order_id=order_id, quantity=quantity, tx=receipt.tx_hash)
        await self._notify_user(order, 'completed')
        await self._notify_admin_completed(order, rate)

    async def _dry_run(self, order_id: int, recipient: str, before_broadcast) -> StarsPurchaseReceipt:
        req_id = f'{_DRY_RUN_PREFIX}-{order_id}'
        await before_broadcast(req_id, 0)
        await asyncio.sleep(0.5)
        return StarsPurchaseReceipt(
            req_id=req_id,
            tx_hash=f'{_DRY_RUN_PREFIX}-tx-{order_id}',
            cost_nanoton=0,
            recipient_name=recipient,
            fragment_confirmed=True,
        )

    async def _retry_or_refund(self, order_id: int, attempts: int, error: str) -> None:
        if attempts >= max(1, settings.STARS_SHOP_MAX_ATTEMPTS):
            await self._fail_and_refund(order_id, error, user_reason='failed')
            return
        delay = timedelta(seconds=max(1, settings.STARS_SHOP_RETRY_DELAY_SECONDS) * attempts)
        await self._set(
            order_id,
            status=StarsOrderStatus.PAID.value,
            next_attempt_at=datetime.now(UTC) + delay,
            last_error=error,
        )
        logger.warning('Звёзды: попытка не удалась, повторим', order_id=order_id, attempt=attempts, error=error)

    async def _fail_and_refund(self, order_id: int, error: str, *, user_reason: str) -> None:
        from app.services.stars_shop_service import refund_order

        await self._set(order_id, status=StarsOrderStatus.FAILED.value, last_error=error, next_attempt_at=None)
        try:
            async with AsyncSessionLocal() as db:
                order = await refund_order(db, order_id, reason=error)
        except Exception as exc:
            logger.error('Звёзды: автоматический возврат не прошёл', order_id=order_id, error=str(exc))
            await self._notify_admin(
                f'<b>🚨 ЗВЁЗДЫ: ЗАКАЗ #{order_id} НЕ ВЫДАН, ДЕНЬГИ НЕ ВОЗВРАЩЕНЫ</b>\n\n'
                f'<blockquote>{html.escape(error)}\n{html.escape(str(exc))}</blockquote>',
                order_id,
            )
            return
        await self._notify_user(order, user_reason)
        await self._notify_admin(
            f'<b>↩️ ЗВЁЗДЫ: ЗАКАЗ #{order_id} НЕ ВЫДАН</b>\n\n'
            f'{order.quantity} ⭐ → @{html.escape(order.recipient_username)}, '
            f'{order.amount_kopeks / 100:.2f} ₽ вернули на баланс покупателя.\n'
            f'<blockquote>{html.escape(error)}</blockquote>',
            order_id,
        )

    # ── Уведомления ────────────────────────────────────────────────────────

    async def _notify_user(self, order: StarsOrder, kind: str) -> None:
        if self._bot is None or order.user_id is None:
            return
        try:
            from app.localization.texts import get_texts

            async with AsyncSessionLocal() as db:
                user = await db.get(User, order.user_id, options=[selectinload(User.subscriptions)])
            if user is None or not user.telegram_id:
                return
            texts = get_texts(user.language)
            reply_markup = None
            if kind == 'completed':
                text = texts.t(
                    'STARS_SHOP_ORDER_COMPLETED',
                    '⭐ Готово! {quantity} звёзд отправлены пользователю @{recipient}.',
                ).format(quantity=order.quantity, recipient=order.recipient_username)
                offer = build_vpn_offer(user, texts)
                if offer is not None:
                    text = f'{text}\n\n{offer[0]}'
                    reply_markup = offer[1]
            elif kind == 'recipient_not_found':
                text = texts.t(
                    'STARS_SHOP_ORDER_REFUNDED_RECIPIENT',
                    '↩️ Не нашли пользователя @{recipient} на Fragment. {amount} вернули на баланс.',
                ).format(recipient=order.recipient_username, amount=texts.format_price(order.amount_kopeks))
            else:
                text = texts.t(
                    'STARS_SHOP_ORDER_REFUNDED',
                    '↩️ Не удалось отправить звёзды по заказу #{order_id}. {amount} вернули на баланс.',
                ).format(order_id=order.id, amount=texts.format_price(order.amount_kopeks))
            await self._bot.send_message(user.telegram_id, text, reply_markup=reply_markup)
        except Exception as exc:
            logger.warning('Звёзды: не удалось уведомить пользователя', order_id=order.id, error=str(exc))

    async def _notify_admin(self, text: str, order_id: int | None = None) -> None:
        if self._bot is None:
            return
        try:
            from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

            await AdminNotificationService(self._bot).send_admin_notification(
                text,
                reply_markup=build_order_keyboard(order_id) if order_id is not None else None,
                category=NotificationCategory.PURCHASES,
            )
        except Exception as exc:
            logger.warning('Звёзды: не удалось уведомить админов', error=str(exc))

    async def _notify_admin_completed(self, order: StarsOrder, rate: TonRate | None) -> None:
        if self._bot is None:
            return
        try:
            from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

            async with AsyncSessionLocal() as db:
                buyer = await db.get(User, order.user_id) if order.user_id is not None else None
            wallet = None
            if not settings.STARS_SHOP_DRY_RUN:
                try:
                    info = await asyncio.wait_for(build_fragment_client().get_wallet(), timeout=15)
                    wallet = WalletSnapshot(address=info.address, balance_ton=float(info.gram_balance))
                except Exception as exc:
                    logger.warning('Звёзды: баланс кошелька для уведомления недоступен', error=str(exc))
            text, markup = build_completed_message(
                order, buyer=buyer, rate=rate, wallet=wallet, dry_run=bool(settings.STARS_SHOP_DRY_RUN)
            )
            await AdminNotificationService(self._bot).send_admin_notification(
                text, reply_markup=markup, category=NotificationCategory.PURCHASES
            )
        except Exception as exc:
            logger.warning('Звёзды: не удалось уведомить админов о продаже', order_id=order.id, error=str(exc))

    async def _notify_admin_review(self, order_id: int, error: str) -> None:
        await self._notify_admin(
            f'<b>🚨 ЗВЁЗДЫ: ЗАКАЗ #{order_id} НА ПРОВЕРКЕ</b>\n\n'
            f'Перевод TON мог уйти, а итог неизвестен. Проверьте исходящие переводы кошелька и закройте заказ'
            f' в кабинете: «выполнен», «повторить» или «вернуть деньги».\n'
            f'<blockquote>{html.escape(error)}</blockquote>',
            order_id,
        )


stars_fulfillment_service = StarsFulfillmentService()
