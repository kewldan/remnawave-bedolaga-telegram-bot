"""Магазин звёзд: цена, покупка с баланса, идемпотентность, возврат и операции админа."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.database.models import (
    PaymentMethod,
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
from app.services import stars_shop_service as shop
from tests.fixtures.sqlite_memory import memory_session


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


@pytest.fixture
def shop_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'STARS_SHOP_ENABLED', True)
    monkeypatch.setattr(settings, 'STARS_SHOP_DRY_RUN', True)
    monkeypatch.setattr(settings, 'STARS_SHOP_PRICE_PER_STAR_KOPEKS', 160)
    monkeypatch.setattr(settings, 'STARS_SHOP_MIN_QUANTITY', 50)
    monkeypatch.setattr(settings, 'STARS_SHOP_MAX_QUANTITY', 10000)
    monkeypatch.setattr(settings, 'STARS_SHOP_PRESETS', '1000, 50,100,50,20,99999')


@pytest.fixture
def quiet_side_effects():
    worker = MagicMock()
    with (
        patch('app.services.stars_shop_service.emit_transaction_side_effects', AsyncMock()) as emit,
        patch('app.services.stars_fulfillment_service.stars_fulfillment_service', worker),
    ):
        yield emit, worker


async def _buyer(db, balance: int = 100_000, **extra) -> User:
    user = User(telegram_id=555000, username='buyer', balance_kopeks=balance, **extra)
    db.add(user)
    await db.commit()
    return user


async def _orders_count(db) -> int:
    return (await db.execute(select(func.count()).select_from(StarsOrder))).scalar_one()


# ── Настройки, получатель, цена ─────────────────────────────────────────────


def test_presets_are_sorted_unique_and_within_bounds(shop_enabled):
    assert settings.get_stars_shop_presets() == [50, 100, 1000]


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [('@durov', 'durov'), ('durov', 'durov'), ('https://t.me/Some_User', 'Some_User'), ('  t.me/abcd ', 'abcd')],
)
def test_normalize_recipient_accepts_common_forms(raw, expected):
    assert shop.normalize_recipient(raw) == expected


@pytest.mark.parametrize('raw', ['', '@', 'ab', '1user', 'user name', 'юзер', 'a' * 33, None])
def test_normalize_recipient_rejects_invalid(raw):
    with pytest.raises(shop.StarsRecipientError):
        shop.normalize_recipient(raw)


def test_quote_uses_price_per_star_and_bounds(shop_enabled):
    quote = shop.quote_stars(250)
    assert (quote.quantity, quote.price_per_star_kopeks, quote.total_kopeks) == (250, 160, 40000)
    for bad in (49, 10001, 0, -5):
        with pytest.raises(shop.StarsQuantityError):
            shop.quote_stars(bad)


def test_shop_hidden_without_fragment_unless_dry_run(shop_enabled, monkeypatch):
    assert shop.is_shop_available() is True
    monkeypatch.setattr(settings, 'STARS_SHOP_DRY_RUN', False)
    monkeypatch.setattr(settings, 'FRAGMENT_COOKIES', '')
    assert shop.is_shop_available() is False


# ── Покупка ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_purchase_debits_balance_and_queues_order(monkeypatch, shop_enabled, quiet_side_effects):
    emit, worker = quiet_side_effects
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _buyer(db)
        result = await shop.purchase_stars_from_balance(
            db,
            buyer_id=buyer.id,
            quantity=100,
            recipient_username='@durov',
            expected_total_kopeks=16000,
            idempotency_key='key-1',
            source='cabinet',
        )

        assert result.is_idempotent_replay is False
        assert result.remaining_balance_kopeks == 84000
        order = result.order
        assert order.status == StarsOrderStatus.PAID.value
        assert (order.recipient_username, order.quantity, order.amount_kopeks) == ('durov', 100, 16000)
        assert order.source == 'cabinet'
        assert order.next_attempt_at is not None

        tx = result.transaction
        assert order.transaction_id == tx.id
        assert tx.type == TransactionType.STARS_PAYMENT.value
        assert tx.amount_kopeks == -16000
        assert tx.payment_method == PaymentMethod.BALANCE.value
        assert tx.external_id == 'stars:key-1'

        await db.refresh(buyer)
        assert buyer.balance_kopeks == 84000
        emit.assert_awaited_once()
        worker.wake.assert_called_once()


@pytest.mark.asyncio
async def test_repeat_with_same_key_does_not_charge_twice(monkeypatch, shop_enabled, quiet_side_effects):
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _buyer(db)
        kwargs = {
            'buyer_id': buyer.id,
            'quantity': 100,
            'recipient_username': 'durov',
            'expected_total_kopeks': 16000,
            'idempotency_key': 'same-key',
        }
        first = await shop.purchase_stars_from_balance(db, **kwargs)
        second = await shop.purchase_stars_from_balance(db, **{**kwargs, 'recipient_username': '@Durov'})

        assert second.is_idempotent_replay is True
        assert second.order.id == first.order.id
        assert await _orders_count(db) == 1
        await db.refresh(buyer)
        assert buyer.balance_kopeks == 84000

        with pytest.raises(shop.StarsIdempotencyConflictError):
            await shop.purchase_stars_from_balance(db, **{**kwargs, 'quantity': 500, 'expected_total_kopeks': 80000})


@pytest.mark.asyncio
async def test_insufficient_balance_creates_nothing(monkeypatch, shop_enabled, quiet_side_effects):
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _buyer(db, balance=10000)
        with pytest.raises(shop.StarsInsufficientBalanceError) as err:
            await shop.purchase_stars_from_balance(
                db,
                buyer_id=buyer.id,
                quantity=100,
                recipient_username='durov',
                expected_total_kopeks=16000,
                idempotency_key='poor',
            )
        assert err.value.missing_kopeks == 6000
        assert await _orders_count(db) == 0
        await db.refresh(buyer)
        assert buyer.balance_kopeks == 10000


@pytest.mark.asyncio
async def test_stale_price_is_rejected_with_fresh_quote(monkeypatch, shop_enabled, quiet_side_effects):
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _buyer(db)
        with pytest.raises(shop.StarsPriceChangedError) as err:
            await shop.purchase_stars_from_balance(
                db,
                buyer_id=buyer.id,
                quantity=100,
                recipient_username='durov',
                expected_total_kopeks=15000,
                idempotency_key='stale',
            )
        assert err.value.fresh_quote.total_kopeks == 16000
        assert await _orders_count(db) == 0


@pytest.mark.asyncio
async def test_disabled_shop_and_restricted_user_are_refused(monkeypatch, shop_enabled, quiet_side_effects):
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _buyer(db, restriction_subscription=True)
        kwargs = {
            'buyer_id': buyer.id,
            'quantity': 100,
            'recipient_username': 'durov',
            'expected_total_kopeks': 16000,
            'idempotency_key': 'x',
        }
        with pytest.raises(shop.StarsPurchaseRestrictedError):
            await shop.purchase_stars_from_balance(db, **kwargs)

        monkeypatch.setattr(settings, 'STARS_SHOP_ENABLED', False)
        with pytest.raises(shop.StarsShopDisabledError):
            await shop.purchase_stars_from_balance(db, **{**kwargs, 'idempotency_key': 'y'})
        assert await _orders_count(db) == 0


# ── Возврат и операции админа ───────────────────────────────────────────────


async def _paid_order(db, monkeypatch) -> tuple[User, StarsOrder]:
    buyer = await _buyer(db)
    result = await shop.purchase_stars_from_balance(
        db,
        buyer_id=buyer.id,
        quantity=100,
        recipient_username='durov',
        expected_total_kopeks=16000,
        idempotency_key='to-refund',
    )
    return buyer, result.order


@pytest.mark.asyncio
async def test_refund_returns_money_once(monkeypatch, shop_enabled, quiet_side_effects):
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer, order = await _paid_order(db, monkeypatch)
        order.status = StarsOrderStatus.FAILED.value
        await db.commit()

        refunded = await shop.refund_order(db, order.id, reason='fragment down')
        assert refunded.status == StarsOrderStatus.REFUNDED.value
        refund_tx = await db.get(Transaction, refunded.refund_transaction_id)
        assert refund_tx.type == TransactionType.REFUND.value
        assert refund_tx.amount_kopeks == 16000
        await db.refresh(buyer)
        assert buyer.balance_kopeks == 100_000

        with pytest.raises(shop.StarsOrderStateError):
            await shop.refund_order(db, order.id, reason='again')
        await db.refresh(buyer)
        assert buyer.balance_kopeks == 100_000


@pytest.mark.asyncio
async def test_completed_order_cannot_be_refunded_or_retried(monkeypatch, shop_enabled, quiet_side_effects):
    async with memory_session(monkeypatch, _TABLES) as db:
        _, order = await _paid_order(db, monkeypatch)
        order.status = StarsOrderStatus.COMPLETED.value
        await db.commit()
        with pytest.raises(shop.StarsOrderStateError):
            await shop.refund_order(db, order.id, reason='x')
        with pytest.raises(shop.StarsOrderStateError):
            await shop.admin_retry_order(db, order.id)


@pytest.mark.asyncio
async def test_admin_review_actions(monkeypatch, shop_enabled, quiet_side_effects):
    async with memory_session(monkeypatch, _TABLES) as db:
        _, order = await _paid_order(db, monkeypatch)
        with pytest.raises(shop.StarsOrderStateError):
            await shop.admin_mark_completed(db, order.id)

        order.status = StarsOrderStatus.NEEDS_REVIEW.value
        order.attempts = 3
        await db.commit()
        retried = await shop.admin_retry_order(db, order.id)
        assert (retried.status, retried.attempts) == (StarsOrderStatus.PAID.value, 0)

        retried.status = StarsOrderStatus.NEEDS_REVIEW.value
        await db.commit()
        done = await shop.admin_mark_completed(db, order.id, ton_tx_hash=' abc ')
        assert (done.status, done.ton_tx_hash) == (StarsOrderStatus.COMPLETED.value, 'abc')


@pytest.mark.asyncio
async def test_stats_count_completed_and_refunded(monkeypatch, shop_enabled, quiet_side_effects):
    monkeypatch.setattr(settings, 'STARS_SHOP_TON_RATE_KOPEKS', 30000)
    async with memory_session(monkeypatch, _TABLES) as db:
        buyer = await _buyer(db, balance=1_000_000)
        db.add_all(
            [
                StarsOrder(
                    user_id=buyer.id,
                    recipient_username='a1234',
                    quantity=100,
                    amount_kopeks=16000,
                    status='completed',
                    idempotency_key='s1',
                    cost_nanoton=400_000_000,
                ),
                StarsOrder(
                    user_id=buyer.id,
                    recipient_username='a1234',
                    quantity=50,
                    amount_kopeks=8000,
                    status='refunded',
                    idempotency_key='s2',
                ),
            ]
        )
        await db.commit()
        stats = await shop.admin_stats(db)
        assert (stats.orders_total, stats.orders_completed, stats.stars_sold) == (2, 1, 100)
        assert (stats.revenue_kopeks, stats.refunded_kopeks) == (16000, 8000)
        # 0.4 TON × 300 ₽ = 120 ₽ себестоимости → маржа 40 ₽
        assert stats.margin_kopeks == 4000
