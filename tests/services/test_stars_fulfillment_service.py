"""Выдача звёзд: статусы, повторы, возврат и защита от двойной оплаты."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database.models import (
    Base,
    PromoGroup,
    StarsOrder,
    StarsOrderStatus,
    Subscription,
    Tariff,
    Transaction,
    TransactionType,
    User,
    UserPromoGroup,
    tariff_promo_groups,
)
from app.external.fragment import (
    FragmentBroadcastUncertainError,
    FragmentRecipientNotFoundError,
    FragmentRetryableError,
    StarsPurchaseReceipt,
)
from app.services import stars_fulfillment_service as fulfillment
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite


_TABLES = [
    Tariff.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
    UserPromoGroup.__table__,
    Subscription.__table__,
    User.__table__,
    Transaction.__table__,
    StarsOrder.__table__,
]


@contextlib.asynccontextmanager
async def _worker_db(monkeypatch) -> AsyncIterator[async_sessionmaker]:
    """Обработчик сам открывает сессии через AsyncSessionLocal — подменяем фабрику."""
    ensure_real_aiosqlite(monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=_TABLES))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(fulfillment, 'AsyncSessionLocal', maker)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, 'STARS_SHOP_ENABLED', True)
    monkeypatch.setattr(settings, 'STARS_SHOP_DRY_RUN', False)
    monkeypatch.setattr(settings, 'STARS_SHOP_MAX_ATTEMPTS', 3)
    monkeypatch.setattr(settings, 'STARS_SHOP_RETRY_DELAY_SECONDS', 60)
    monkeypatch.setattr(settings, 'STARS_SHOP_SHOW_SENDER', False)


_seq = iter(range(1, 10_000))


async def _seed(maker, *, status: str = StarsOrderStatus.PAID.value, attempts: int = 0) -> int:
    n = next(_seq)
    async with maker() as db:
        user = User(telegram_id=700_000 + n, username=f'buyer{n}', balance_kopeks=0)
        db.add(user)
        await db.flush()
        tx = Transaction(user_id=user.id, type=TransactionType.STARS_PAYMENT.value, amount_kopeks=-16000)
        db.add(tx)
        await db.flush()
        order = StarsOrder(
            user_id=user.id,
            recipient_username='durov',
            quantity=100,
            amount_kopeks=16000,
            status=status,
            idempotency_key=f'k-{n}',
            transaction_id=tx.id,
            attempts=attempts,
            next_attempt_at=datetime.now(UTC),
        )
        db.add(order)
        await db.commit()
        return order.id


async def _order(maker, order_id: int) -> StarsOrder:
    async with maker() as db:
        return await db.get(StarsOrder, order_id)


def _service() -> fulfillment.StarsFulfillmentService:
    service = fulfillment.StarsFulfillmentService()
    service._notify_user = AsyncMock()
    service._notify_admin = AsyncMock()
    return service


def _client(purchase) -> MagicMock:
    client = MagicMock()
    client.purchase_stars = purchase
    return client


@pytest.mark.asyncio
async def test_success_records_broadcasting_before_money_leaves(monkeypatch):
    async with _worker_db(monkeypatch) as maker:
        order_id = await _seed(maker)
        seen_before_send: list[str] = []

        async def purchase(username, quantity, *, show_sender, before_broadcast):
            assert (username, quantity, show_sender) == ('durov', 100, False)
            await before_broadcast('req-1', 420_000_000)
            # В момент «отправки» в базе уже должен быть зафиксирован broadcasting.
            seen_before_send.append((await _order(maker, order_id)).status)
            return StarsPurchaseReceipt('req-1', 'tx-hash', 420_000_000, 'Pavel', fragment_confirmed=True)

        service = _service()
        with patch.object(fulfillment, 'build_fragment_client', return_value=_client(purchase)):
            assert await service.process_next() is True

        assert seen_before_send == [StarsOrderStatus.BROADCASTING.value]
        order = await _order(maker, order_id)
        assert order.status == StarsOrderStatus.COMPLETED.value
        assert (order.fragment_req_id, order.ton_tx_hash, order.cost_nanoton) == ('req-1', 'tx-hash', 420_000_000)
        assert order.recipient_name == 'Pavel'
        assert order.attempts == 1
        service._notify_user.assert_awaited_once()
        assert await service.process_next() is False


@pytest.mark.asyncio
async def test_retryable_error_requeues_then_refunds_after_max_attempts(monkeypatch):
    async with _worker_db(monkeypatch) as maker:
        order_id = await _seed(maker)
        service = _service()
        failing = _client(AsyncMock(side_effect=FragmentRetryableError('fragment 502')))
        with (
            patch.object(fulfillment, 'build_fragment_client', return_value=failing),
            patch('app.services.stars_shop_service.emit_transaction_side_effects', AsyncMock()),
        ):
            await service.process_next()
            order = await _order(maker, order_id)
            assert (order.status, order.attempts) == (StarsOrderStatus.PAID.value, 1)
            assert order.next_attempt_at > datetime.now(UTC)
            assert order.last_error == 'fragment 502'
            # Отложенный заказ не берётся раньше времени.
            assert await service.process_next() is False

            for _ in range(2):
                async with maker() as db:
                    pending = await db.get(StarsOrder, order_id)
                    pending.next_attempt_at = datetime.now(UTC)
                    await db.commit()
                await service.process_next()

        order = await _order(maker, order_id)
        assert order.status == StarsOrderStatus.REFUNDED.value
        assert order.attempts == 3
        async with maker() as db:
            buyer = await db.get(User, order.user_id)
        assert buyer.balance_kopeks == 16000
        assert failing.purchase_stars.await_count == 3


@pytest.mark.asyncio
async def test_uncertain_broadcast_goes_to_review_without_refund(monkeypatch):
    async with _worker_db(monkeypatch) as maker:
        order_id = await _seed(maker)

        async def purchase(username, quantity, *, show_sender, before_broadcast):
            await before_broadcast('req-9', 1)
            raise FragmentBroadcastUncertainError('timeout', req_id='req-9')

        service = _service()
        with patch.object(fulfillment, 'build_fragment_client', return_value=_client(purchase)):
            await service.process_next()

        order = await _order(maker, order_id)
        assert order.status == StarsOrderStatus.NEEDS_REVIEW.value
        assert order.refund_transaction_id is None
        async with maker() as db:
            buyer = await db.get(User, order.user_id)
        assert buyer.balance_kopeks == 0
        assert await service.process_next() is False
        service._notify_admin.assert_awaited()


@pytest.mark.asyncio
async def test_unexpected_error_after_broadcast_is_never_retried(monkeypatch):
    async with _worker_db(monkeypatch) as maker:
        order_id = await _seed(maker)

        async def purchase(username, quantity, *, show_sender, before_broadcast):
            await before_broadcast('req-x', 1)
            raise RuntimeError('boom after send')

        with patch.object(fulfillment, 'build_fragment_client', return_value=_client(purchase)):
            await _service().process_next()

        assert (await _order(maker, order_id)).status == StarsOrderStatus.NEEDS_REVIEW.value


@pytest.mark.asyncio
async def test_unknown_recipient_is_refunded_immediately(monkeypatch):
    async with _worker_db(monkeypatch) as maker:
        order_id = await _seed(maker)
        missing = _client(AsyncMock(side_effect=FragmentRecipientNotFoundError('no such user')))
        with (
            patch.object(fulfillment, 'build_fragment_client', return_value=missing),
            patch('app.services.stars_shop_service.emit_transaction_side_effects', AsyncMock()),
        ):
            await _service().process_next()

        order = await _order(maker, order_id)
        assert (order.status, order.attempts) == (StarsOrderStatus.REFUNDED.value, 1)


@pytest.mark.asyncio
async def test_recover_after_crash(monkeypatch):
    async with _worker_db(monkeypatch) as maker:
        in_flight = await _seed(maker, status=StarsOrderStatus.PROCESSING.value)
        maybe_sent = await _seed(maker, status=StarsOrderStatus.BROADCASTING.value, attempts=1)

        service = _service()
        await service.recover_interrupted()

        assert (await _order(maker, in_flight)).status == StarsOrderStatus.PAID.value
        assert (await _order(maker, maybe_sent)).status == StarsOrderStatus.NEEDS_REVIEW.value
        service._notify_admin.assert_awaited_once()


@pytest.mark.asyncio
async def test_dry_run_completes_without_fragment(monkeypatch):
    monkeypatch.setattr(settings, 'STARS_SHOP_DRY_RUN', True)
    monkeypatch.setattr(fulfillment.asyncio, 'sleep', AsyncMock())
    async with _worker_db(monkeypatch) as maker:
        order_id = await _seed(maker)
        with patch.object(fulfillment, 'build_fragment_client', side_effect=AssertionError('no Fragment in dry-run')):
            await _service().process_next()
        order = await _order(maker, order_id)
        assert order.status == StarsOrderStatus.COMPLETED.value
        assert order.ton_tx_hash.startswith('dry-run')
