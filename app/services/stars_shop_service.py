"""Магазин звёзд Telegram: цена, покупка с баланса, корзина, возврат, операции админа.

Деньги списываются с баланса сразу при покупке, заказ ставится в очередь в статусе
``paid``; звёзды отправляет ``stars_fulfillment_service``. Схема повторяет
подарочные подписки (``gift_purchase_service``): блокировка покупателя, пересчёт цены
под блокировкой, идемпотентность по ключу, транзакция и заказ — в одном коммите.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.transaction import create_transaction, emit_transaction_side_effects
from app.database.crud.user import add_user_balance, lock_user_for_pricing, subtract_user_balance
from app.database.models import (
    PaymentMethod,
    StarsOrder,
    StarsOrderStatus,
    Transaction,
    TransactionType,
    User,
)
from app.services.ton_rate_service import TonRate


logger = structlog.get_logger(__name__)

STARS_CART_MODE = 'stars_purchase'
SOURCES = frozenset({'bot', 'cabinet', 'auto_cart'})
# Ник Telegram: 4–32 символа, латиница, цифры, подчёркивание, начинается с буквы.
_USERNAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{3,31}$')

ACTIVE_STATUSES = (
    StarsOrderStatus.PAID.value,
    StarsOrderStatus.PROCESSING.value,
    StarsOrderStatus.BROADCASTING.value,
)
REFUNDABLE_STATUSES = (
    StarsOrderStatus.PAID.value,
    StarsOrderStatus.FAILED.value,
    StarsOrderStatus.NEEDS_REVIEW.value,
)


# ── Ошибки ──────────────────────────────────────────────────────────────────


class StarsShopError(Exception):
    """Базовая ошибка магазина звёзд."""


class StarsShopDisabledError(StarsShopError):
    """Магазин выключен."""


class StarsQuantityError(StarsShopError):
    """Количество вне допустимых пределов."""

    def __init__(self, min_quantity: int, max_quantity: int) -> None:
        super().__init__(f'Количество звёзд должно быть от {min_quantity} до {max_quantity}')
        self.min_quantity = min_quantity
        self.max_quantity = max_quantity


class StarsRecipientError(StarsShopError):
    """Некорректный ник получателя."""


class StarsPurchaseRestrictedError(StarsShopError):
    """Покупки для пользователя запрещены."""


class StarsInsufficientBalanceError(StarsShopError):
    """Не хватает денег на балансе."""

    def __init__(self, required_kopeks: int, available_kopeks: int) -> None:
        super().__init__('Недостаточно средств на балансе')
        self.required_kopeks = required_kopeks
        self.available_kopeks = available_kopeks

    @property
    def missing_kopeks(self) -> int:
        return max(0, self.required_kopeks - self.available_kopeks)


class StarsPriceChangedError(StarsShopError):
    """Цена изменилась после показа пользователю."""

    def __init__(self, expected_kopeks: int, fresh_quote: StarsQuote) -> None:
        super().__init__('Цена изменилась')
        self.expected_kopeks = expected_kopeks
        self.fresh_quote = fresh_quote


class StarsIdempotencyConflictError(StarsShopError):
    """Ключ идемпотентности уже использован с другими параметрами."""


class StarsOrderStateError(StarsShopError):
    """Операция недоступна в текущем статусе заказа."""


# ── Модели чтения ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StarsShopConfig:
    enabled: bool
    price_per_star_kopeks: int
    min_quantity: int
    max_quantity: int
    presets: list[int]
    fulfillment_ready: bool


@dataclass(frozen=True)
class StarsQuote:
    quantity: int
    price_per_star_kopeks: int
    total_kopeks: int


@dataclass(frozen=True)
class StarsPurchaseResult:
    order: StarsOrder
    transaction: Transaction | None
    quote: StarsQuote
    remaining_balance_kopeks: int
    is_idempotent_replay: bool


@dataclass(frozen=True)
class StarsShopStats:
    orders_total: int
    orders_completed: int
    stars_sold: int
    revenue_kopeks: int
    refunded_kopeks: int
    cost_nanoton: int
    margin_kopeks: int | None
    by_status: dict[str, int]
    cost_kopeks: int | None = None
    ton_rate_kopeks: int | None = None
    ton_rate_source: str | None = None


# ── Конфигурация и цена ─────────────────────────────────────────────────────


def get_shop_config() -> StarsShopConfig:
    return StarsShopConfig(
        enabled=bool(settings.STARS_SHOP_ENABLED),
        price_per_star_kopeks=int(settings.STARS_SHOP_PRICE_PER_STAR_KOPEKS),
        min_quantity=int(settings.STARS_SHOP_MIN_QUANTITY),
        max_quantity=int(settings.STARS_SHOP_MAX_QUANTITY),
        presets=settings.get_stars_shop_presets(),
        fulfillment_ready=bool(settings.STARS_SHOP_DRY_RUN) or settings.is_fragment_configured(),
    )


def is_shop_available() -> bool:
    """Магазин виден пользователям: включён и выдача настроена (или тестовый режим)."""
    config = get_shop_config()
    return config.enabled and config.fulfillment_ready and config.price_per_star_kopeks > 0


def normalize_recipient(raw: str | None) -> str:
    """Ник получателя без ``@`` и ссылки ``t.me/``; бросает ``StarsRecipientError``."""
    value = (raw or '').strip()
    for prefix in ('https://t.me/', 'http://t.me/', 't.me/'):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
    value = value.lstrip('@').strip()
    if not _USERNAME_RE.fullmatch(value):
        raise StarsRecipientError('Укажите ник Telegram: латиница, цифры и «_», от 4 до 32 символов')
    return value


def quote_stars(quantity: int) -> StarsQuote:
    """Цена за ``quantity`` звёзд по текущим настройкам."""
    config = get_shop_config()
    if not isinstance(quantity, int) or not (config.min_quantity <= quantity <= config.max_quantity):
        raise StarsQuantityError(config.min_quantity, config.max_quantity)
    return StarsQuote(
        quantity=quantity,
        price_per_star_kopeks=config.price_per_star_kopeks,
        total_kopeks=quantity * config.price_per_star_kopeks,
    )


def _external_id(idempotency_key: str) -> str:
    return f'stars:{idempotency_key}'


def _quote_from_order(order: StarsOrder) -> StarsQuote:
    per_star = order.amount_kopeks // order.quantity if order.quantity else 0
    return StarsQuote(quantity=order.quantity, price_per_star_kopeks=per_star, total_kopeks=order.amount_kopeks)


async def _find_order_by_key(db: AsyncSession, idempotency_key: str) -> StarsOrder | None:
    result = await db.execute(select(StarsOrder).where(StarsOrder.idempotency_key == idempotency_key))
    return result.scalars().first()


async def _replay(
    db: AsyncSession,
    order: StarsOrder,
    *,
    buyer_id: int,
    quantity: int,
    recipient_username: str,
) -> StarsPurchaseResult:
    if (
        order.user_id != buyer_id
        or order.quantity != quantity
        or order.recipient_username.lower() != recipient_username.lower()
    ):
        raise StarsIdempotencyConflictError('Ключ уже использован для другого заказа')
    transaction = await db.get(Transaction, order.transaction_id) if order.transaction_id else None
    buyer = await db.get(User, buyer_id)
    return StarsPurchaseResult(
        order=order,
        transaction=transaction,
        quote=_quote_from_order(order),
        remaining_balance_kopeks=buyer.balance_kopeks if buyer else 0,
        is_idempotent_replay=True,
    )


# ── Покупка ─────────────────────────────────────────────────────────────────


async def purchase_stars_from_balance(
    db: AsyncSession,
    *,
    buyer_id: int,
    quantity: int,
    recipient_username: str,
    expected_total_kopeks: int,
    idempotency_key: str,
    source: str = 'bot',
) -> StarsPurchaseResult:
    """Списывает деньги и создаёт заказ в статусе ``paid`` атомарно и идемпотентно."""
    if not idempotency_key or not idempotency_key.strip():
        raise ValueError('idempotency_key is required')
    if source not in SOURCES:
        raise ValueError(f'unknown source: {source}')
    recipient = normalize_recipient(recipient_username)

    existing = await _find_order_by_key(db, idempotency_key)
    if existing is not None:
        return await _replay(db, existing, buyer_id=buyer_id, quantity=quantity, recipient_username=recipient)

    if not is_shop_available():
        raise StarsShopDisabledError('Магазин звёзд недоступен')

    buyer = await lock_user_for_pricing(db, buyer_id)
    if buyer is None:
        raise StarsPurchaseRestrictedError('Пользователь не найден')
    if getattr(buyer, 'restriction_subscription', False):
        raise StarsPurchaseRestrictedError('Покупки для этого аккаунта ограничены')

    fresh_quote = quote_stars(quantity)
    if fresh_quote.total_kopeks != expected_total_kopeks:
        raise StarsPriceChangedError(expected_total_kopeks, fresh_quote)
    if buyer.balance_kopeks < fresh_quote.total_kopeks:
        raise StarsInsufficientBalanceError(fresh_quote.total_kopeks, buyer.balance_kopeks)

    description = f'Звёзды Telegram: {quantity} ⭐ → @{recipient}'
    external_id = _external_id(idempotency_key)
    now = datetime.now(UTC)
    try:
        order = StarsOrder(
            user_id=buyer.id,
            recipient_username=recipient,
            quantity=quantity,
            amount_kopeks=fresh_quote.total_kopeks,
            status=StarsOrderStatus.PAID.value,
            source=source,
            idempotency_key=idempotency_key,
            next_attempt_at=now,
        )
        db.add(order)

        debited = await subtract_user_balance(
            db,
            buyer,
            fresh_quote.total_kopeks,
            description=description,
            create_transaction=False,
            commit=False,
        )
        if not debited:
            await db.rollback()
            raise StarsInsufficientBalanceError(fresh_quote.total_kopeks, buyer.balance_kopeks)

        transaction = await create_transaction(
            db,
            user_id=buyer.id,
            type=TransactionType.STARS_PAYMENT,
            amount_kopeks=fresh_quote.total_kopeks,
            description=description,
            payment_method=PaymentMethod.BALANCE,
            external_id=external_id,
            commit=False,
        )
        await db.flush()
        order.transaction_id = transaction.id
        await db.commit()
        await db.refresh(order)
        await db.refresh(transaction)
        await db.refresh(buyer)
    except IntegrityError:
        await db.rollback()
        winner = await _find_order_by_key(db, idempotency_key)
        if winner is None:
            raise
        return await _replay(db, winner, buyer_id=buyer_id, quantity=quantity, recipient_username=recipient)

    await emit_transaction_side_effects(
        db,
        transaction,
        amount_kopeks=fresh_quote.total_kopeks,
        user_id=buyer.id,
        type=TransactionType.STARS_PAYMENT,
        payment_method=PaymentMethod.BALANCE,
        external_id=external_id,
        description=description,
    )
    logger.info(
        'Звёзды: заказ оплачен',
        order_id=order.id,
        user_id=buyer.id,
        quantity=quantity,
        amount_kopeks=fresh_quote.total_kopeks,
        source=source,
    )

    from app.services.stars_fulfillment_service import stars_fulfillment_service

    stars_fulfillment_service.wake()
    return StarsPurchaseResult(
        order=order,
        transaction=transaction,
        quote=fresh_quote,
        remaining_balance_kopeks=buyer.balance_kopeks,
        is_idempotent_replay=False,
    )


def build_stars_cart(
    *,
    user_id: int,
    quantity: int,
    recipient_username: str,
    total_kopeks: int,
    missing_kopeks: int,
    idempotency_key: str,
    source: str,
) -> dict:
    """Корзина для «пополнить недостающее и купить автоматически»."""
    return {
        'cart_mode': STARS_CART_MODE,
        'stars_checkout_id': idempotency_key,
        'quantity': quantity,
        'recipient_username': recipient_username,
        'total_price': total_kopeks,
        'missing_amount': missing_kopeks,
        'source': source,
        'saved_cart': True,
        'return_to_cart': True,
        'user_id': user_id,
    }


# ── Возврат и операции админа ───────────────────────────────────────────────


async def _lock_order(db: AsyncSession, order_id: int) -> StarsOrder | None:
    result = await db.execute(
        select(StarsOrder).where(StarsOrder.id == order_id).with_for_update().execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def refund_order(db: AsyncSession, order_id: int, *, reason: str) -> StarsOrder:
    """Возвращает деньги на баланс. Допустимо из ``paid``, ``failed``, ``needs_review``."""
    order = await _lock_order(db, order_id)
    if order is None:
        raise StarsOrderStateError('Заказ не найден')
    if order.status not in REFUNDABLE_STATUSES:
        raise StarsOrderStateError(f'Возврат недоступен в статусе {order.status}')
    if order.user_id is None:
        raise StarsOrderStateError('У заказа нет покупателя')

    buyer = await db.get(User, order.user_id)
    description = f'Возврат за звёзды: заказ #{order.id}'
    credited = await add_user_balance(
        db, buyer, order.amount_kopeks, description, create_transaction=False, commit=False
    )
    if not credited:
        await db.rollback()
        raise StarsOrderStateError('Не удалось вернуть деньги на баланс')
    external_id = f'stars_refund:{order.id}'
    transaction = await create_transaction(
        db,
        user_id=order.user_id,
        type=TransactionType.REFUND,
        amount_kopeks=order.amount_kopeks,
        description=description,
        payment_method=PaymentMethod.BALANCE,
        external_id=external_id,
        commit=False,
    )
    await db.flush()
    order.refund_transaction_id = transaction.id
    order.status = StarsOrderStatus.REFUNDED.value
    order.refunded_at = datetime.now(UTC)
    order.next_attempt_at = None
    order.last_error = reason
    await db.commit()
    await db.refresh(order)
    await emit_transaction_side_effects(
        db,
        transaction,
        amount_kopeks=order.amount_kopeks,
        user_id=order.user_id,
        type=TransactionType.REFUND,
        payment_method=PaymentMethod.BALANCE,
        external_id=external_id,
        description=description,
    )
    logger.info('Звёзды: деньги возвращены', order_id=order.id, amount_kopeks=order.amount_kopeks, reason=reason)
    return order


async def admin_retry_order(db: AsyncSession, order_id: int) -> StarsOrder:
    """Снова поставить в очередь заказ из ``failed`` или после ручной проверки ``needs_review``.

    Для ``needs_review`` админ подтверждает, что перевод НЕ ушёл (проверил кошелёк),
    иначе звёзды будут оплачены второй раз.
    """
    order = await _lock_order(db, order_id)
    if order is None:
        raise StarsOrderStateError('Заказ не найден')
    if order.status not in (StarsOrderStatus.FAILED.value, StarsOrderStatus.NEEDS_REVIEW.value):
        raise StarsOrderStateError(f'Повтор недоступен в статусе {order.status}')
    order.status = StarsOrderStatus.PAID.value
    order.next_attempt_at = datetime.now(UTC)
    order.attempts = 0
    order.fragment_req_id = None
    await db.commit()
    await db.refresh(order)

    from app.services.stars_fulfillment_service import stars_fulfillment_service

    stars_fulfillment_service.wake()
    return order


async def admin_mark_completed(db: AsyncSession, order_id: int, *, ton_tx_hash: str | None = None) -> StarsOrder:
    """Закрыть ``needs_review``: админ убедился, что звёзды дошли."""
    order = await _lock_order(db, order_id)
    if order is None:
        raise StarsOrderStateError('Заказ не найден')
    if order.status != StarsOrderStatus.NEEDS_REVIEW.value:
        raise StarsOrderStateError(f'Отметить выполненным можно только заказ на проверке, сейчас: {order.status}')
    order.status = StarsOrderStatus.COMPLETED.value
    order.completed_at = datetime.now(UTC)
    if ton_tx_hash:
        order.ton_tx_hash = ton_tx_hash.strip()[:128]
    await db.commit()
    await db.refresh(order)
    return order


# ── Чтение ──────────────────────────────────────────────────────────────────


async def list_user_orders(db: AsyncSession, user_id: int, *, limit: int = 20, offset: int = 0) -> list[StarsOrder]:
    result = await db.execute(
        select(StarsOrder)
        .where(StarsOrder.user_id == user_id)
        .order_by(StarsOrder.created_at.desc(), StarsOrder.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def get_user_order(db: AsyncSession, user_id: int, order_id: int) -> StarsOrder | None:
    result = await db.execute(select(StarsOrder).where(StarsOrder.id == order_id, StarsOrder.user_id == user_id))
    return result.scalars().first()


async def admin_list_orders(
    db: AsyncSession,
    *,
    status: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[StarsOrder], int]:
    query = select(StarsOrder)
    if status:
        query = query.where(StarsOrder.status == status)
    if search:
        term = search.strip().lstrip('@#')
        conditions = [StarsOrder.recipient_username.ilike(f'%{term}%')]
        if term.isdigit():
            conditions.append(StarsOrder.id == int(term))
            conditions.append(StarsOrder.user_id == int(term))
        query = query.where(or_(*conditions))
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.order_by(StarsOrder.id.desc()).limit(limit).offset(offset))
    return list(result.scalars().all()), int(total)


async def admin_stats(
    db: AsyncSession,
    *,
    since: datetime | None = None,
    ton_rate: TonRate | None = None,
) -> StarsShopStats:
    """Сводка за период. Себестоимость — по курсу на момент выдачи; у заказов без него —
    по текущему курсу ``ton_rate``. Нет ни того, ни другого — маржа ``None``."""
    base = select(StarsOrder)
    if since is not None:
        base = base.where(StarsOrder.created_at >= since)
    sub = base.subquery()
    by_status_rows = (await db.execute(select(sub.c.status, func.count()).group_by(sub.c.status))).all()
    by_status = {status: int(count) for status, count in by_status_rows}
    is_completed = sub.c.status == StarsOrderStatus.COMPLETED.value
    completed = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(func.sum(sub.c.quantity), 0),
                func.coalesce(func.sum(sub.c.amount_kopeks), 0),
                func.coalesce(func.sum(sub.c.cost_nanoton), 0),
                func.coalesce(func.sum(sub.c.cost_kopeks), 0),
                func.coalesce(func.sum(case((sub.c.cost_kopeks.is_(None), sub.c.cost_nanoton), else_=0)), 0),
            ).where(is_completed)
        )
    ).one()
    refunded = (
        await db.execute(
            select(func.coalesce(func.sum(sub.c.amount_kopeks), 0)).where(
                sub.c.status == StarsOrderStatus.REFUNDED.value
            )
        )
    ).scalar_one()
    revenue = int(completed[2])
    cost_nanoton = int(completed[3])
    known_cost_kopeks = int(completed[4])
    unpriced_nanoton = int(completed[5])
    if unpriced_nanoton == 0:
        cost_kopeks: int | None = known_cost_kopeks
    elif ton_rate is not None:
        cost_kopeks = known_cost_kopeks + ton_rate.nanoton_to_kopeks(unpriced_nanoton)
    else:
        cost_kopeks = None
    return StarsShopStats(
        orders_total=sum(by_status.values()),
        orders_completed=int(completed[0]),
        stars_sold=int(completed[1]),
        revenue_kopeks=revenue,
        refunded_kopeks=int(refunded),
        cost_nanoton=cost_nanoton,
        margin_kopeks=revenue - cost_kopeks if cost_kopeks is not None else None,
        by_status=by_status,
        cost_kopeks=cost_kopeks,
        ton_rate_kopeks=ton_rate.kopeks if ton_rate else None,
        ton_rate_source=ton_rate.source if ton_rate else None,
    )
