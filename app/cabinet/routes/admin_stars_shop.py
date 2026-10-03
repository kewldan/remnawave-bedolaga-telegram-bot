"""Админка магазина звёзд: заказы, ручная проверка, возвраты, статистика, кошелёк.

Каждый вызов попадает в журнал аудита через ``require_permission``.
Настройки магазина редактируются в общем разделе настроек (категория STARS_SHOP).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import StarsOrder, StarsOrderStatus, User
from app.external.fragment import FragmentStarsError
from app.services import stars_shop_service as shop

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.stars_shop import (
    AdminMarkCompletedRequest,
    AdminRefundRequest,
    AdminStarsOrderResponse,
    AdminStarsOrdersListResponse,
    AdminStarsStatsResponse,
    AdminStarsStatusResponse,
    AdminStarsWalletResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/stars', tags=['Cabinet Admin Stars Shop'])

_STATUSES = {item.value for item in StarsOrderStatus}


async def _to_admin_response(db: AsyncSession, order: StarsOrder) -> AdminStarsOrderResponse:
    user_display = None
    if order.user_id is not None:
        user = await db.get(User, order.user_id)
        if user is not None:
            user_display = (
                f'@{user.username}' if user.username else (user.email or str(user.telegram_id or f'#{user.id}'))
            )
    return AdminStarsOrderResponse(
        id=order.id,
        status=order.status,
        quantity=order.quantity,
        amount_kopeks=order.amount_kopeks,
        recipient_username=order.recipient_username,
        recipient_name=order.recipient_name,
        created_at=order.created_at,
        completed_at=order.completed_at,
        refunded_at=order.refunded_at,
        user_id=order.user_id,
        user_display=user_display,
        source=order.source,
        attempts=order.attempts,
        last_error=order.last_error,
        fragment_req_id=order.fragment_req_id,
        ton_tx_hash=order.ton_tx_hash,
        cost_nanoton=order.cost_nanoton,
        next_attempt_at=order.next_attempt_at,
        updated_at=order.updated_at,
    )


@router.get('/status', response_model=AdminStarsStatusResponse)
async def get_status(_: User = Depends(require_permission('stars_shop:read'))) -> AdminStarsStatusResponse:
    config = shop.get_shop_config()
    return AdminStarsStatusResponse(
        enabled=config.enabled,
        dry_run=bool(settings.STARS_SHOP_DRY_RUN),
        fragment_configured=settings.is_fragment_configured(),
        price_per_star_kopeks=config.price_per_star_kopeks,
        min_quantity=config.min_quantity,
        max_quantity=config.max_quantity,
        presets=config.presets,
        ton_rate_kopeks=int(settings.STARS_SHOP_TON_RATE_KOPEKS or 0),
    )


@router.get('/stats', response_model=AdminStarsStatsResponse)
async def get_stats(
    days: int | None = Query(None, ge=1, le=3650),
    _: User = Depends(require_permission('stars_shop:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminStarsStatsResponse:
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    stats = await shop.admin_stats(db, since=since)
    return AdminStarsStatsResponse(
        orders_total=stats.orders_total,
        orders_completed=stats.orders_completed,
        stars_sold=stats.stars_sold,
        revenue_kopeks=stats.revenue_kopeks,
        refunded_kopeks=stats.refunded_kopeks,
        cost_nanoton=stats.cost_nanoton,
        margin_kopeks=stats.margin_kopeks,
        by_status=stats.by_status,
        needs_review=stats.by_status.get(StarsOrderStatus.NEEDS_REVIEW.value, 0),
    )


@router.get('/orders', response_model=AdminStarsOrdersListResponse)
async def list_orders(
    status_filter: str | None = Query(None, alias='status'),
    search: str | None = Query(None, max_length=64),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: User = Depends(require_permission('stars_shop:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminStarsOrdersListResponse:
    if status_filter and status_filter not in _STATUSES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Unknown status')
    orders, total = await shop.admin_list_orders(db, status=status_filter, search=search, limit=limit, offset=offset)
    return AdminStarsOrdersListResponse(
        items=[await _to_admin_response(db, order) for order in orders],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get('/orders/{order_id}', response_model=AdminStarsOrderResponse)
async def get_order(
    order_id: int,
    _: User = Depends(require_permission('stars_shop:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminStarsOrderResponse:
    order = await db.get(StarsOrder, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Order not found')
    return await _to_admin_response(db, order)


def _state_error(exc: shop.StarsOrderStateError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.post('/orders/{order_id}/retry', response_model=AdminStarsOrderResponse)
async def retry_order(
    order_id: int,
    _: User = Depends(require_permission('stars_shop:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminStarsOrderResponse:
    try:
        order = await shop.admin_retry_order(db, order_id)
    except shop.StarsOrderStateError as exc:
        raise _state_error(exc) from exc
    return await _to_admin_response(db, order)


@router.post('/orders/{order_id}/refund', response_model=AdminStarsOrderResponse)
async def refund_order(
    order_id: int,
    request: AdminRefundRequest,
    admin: User = Depends(require_permission('stars_shop:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminStarsOrderResponse:
    try:
        order = await shop.refund_order(db, order_id, reason=f'{request.reason} (админ #{admin.id})')
    except shop.StarsOrderStateError as exc:
        raise _state_error(exc) from exc
    return await _to_admin_response(db, order)


@router.post('/orders/{order_id}/complete', response_model=AdminStarsOrderResponse)
async def mark_completed(
    order_id: int,
    request: AdminMarkCompletedRequest,
    _: User = Depends(require_permission('stars_shop:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminStarsOrderResponse:
    try:
        order = await shop.admin_mark_completed(db, order_id, ton_tx_hash=request.ton_tx_hash)
    except shop.StarsOrderStateError as exc:
        raise _state_error(exc) from exc
    return await _to_admin_response(db, order)


@router.get('/wallet', response_model=AdminStarsWalletResponse)
async def get_wallet(_: User = Depends(require_permission('stars_shop:read'))) -> AdminStarsWalletResponse:
    from app.services.stars_fulfillment_service import build_fragment_client

    try:
        client = build_fragment_client()
        wallet = await client.get_wallet()
        try:
            price = (await client.get_stars_price(100)).ton_price
        except FragmentStarsError:
            price = None
    except FragmentStarsError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return AdminStarsWalletResponse(
        address=wallet.address,
        state=wallet.state,
        balance_ton=float(wallet.gram_balance),
        fragment_price_ton_per_100=price,
    )
