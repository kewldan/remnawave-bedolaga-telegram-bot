"""Магазин звёзд в кабинете: витрина, цена, покупка с баланса, история заказов."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import StarsOrder, User
from app.services import stars_shop_service as shop
from app.services.user_cart_service import user_cart_service
from app.utils.cache import RateLimitCache

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.stars_shop import (
    StarsOrderResponse,
    StarsOrdersListResponse,
    StarsPurchaseRequest,
    StarsPurchaseResponse,
    StarsQuoteResponse,
    StarsShopConfigResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/stars', tags=['Cabinet Stars Shop'])


def order_to_response(order: StarsOrder) -> StarsOrderResponse:
    return StarsOrderResponse(
        id=order.id,
        status=order.status,
        quantity=order.quantity,
        amount_kopeks=order.amount_kopeks,
        recipient_username=order.recipient_username,
        recipient_name=order.recipient_name,
        created_at=order.created_at,
        completed_at=order.completed_at,
        refunded_at=order.refunded_at,
    )


def _require_available() -> None:
    if not shop.is_shop_available():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Stars shop is not available')


@router.get('/config', response_model=StarsShopConfigResponse)
async def get_stars_config(user: User = Depends(get_current_cabinet_user)) -> StarsShopConfigResponse:
    config = shop.get_shop_config()
    return StarsShopConfigResponse(
        available=shop.is_shop_available(),
        price_per_star_kopeks=config.price_per_star_kopeks,
        min_quantity=config.min_quantity,
        max_quantity=config.max_quantity,
        presets=config.presets,
        balance_kopeks=user.balance_kopeks,
    )


@router.get('/quote', response_model=StarsQuoteResponse)
async def get_stars_quote(
    quantity: int = Query(..., ge=1),
    user: User = Depends(get_current_cabinet_user),
) -> StarsQuoteResponse:
    _require_available()
    try:
        quote = shop.quote_stars(quantity)
    except shop.StarsQuantityError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={'code': 'invalid_quantity', 'min': exc.min_quantity, 'max': exc.max_quantity},
        ) from exc
    return StarsQuoteResponse(
        quantity=quote.quantity,
        price_per_star_kopeks=quote.price_per_star_kopeks,
        total_kopeks=quote.total_kopeks,
    )


@router.post('/purchase', response_model=StarsPurchaseResponse)
async def purchase_stars(
    request: StarsPurchaseRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> StarsPurchaseResponse:
    _require_available()
    if await RateLimitCache.is_rate_limited(user.id, 'stars_purchase', limit=5, window=60):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Too many purchase attempts')

    try:
        result = await shop.purchase_stars_from_balance(
            db,
            buyer_id=user.id,
            quantity=request.quantity,
            recipient_username=request.recipient_username,
            expected_total_kopeks=request.expected_total_kopeks,
            idempotency_key=request.idempotency_key,
            source='cabinet',
        )
    except shop.StarsInsufficientBalanceError as exc:
        # Корзина: после пополнения на недостающую сумму заказ оплатится автоматически.
        recipient = shop.normalize_recipient(request.recipient_username)
        await user_cart_service.save_user_cart(
            user.id,
            shop.build_stars_cart(
                user_id=user.id,
                quantity=request.quantity,
                recipient_username=recipient,
                total_kopeks=exc.required_kopeks,
                missing_kopeks=exc.missing_kopeks,
                idempotency_key=request.idempotency_key,
                source='cabinet',
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                'code': 'insufficient_balance',
                'required_kopeks': exc.required_kopeks,
                'available_kopeks': exc.available_kopeks,
                'missing_kopeks': exc.missing_kopeks,
            },
        ) from exc
    except shop.StarsPriceChangedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={'code': 'price_changed', 'total_kopeks': exc.fresh_quote.total_kopeks},
        ) from exc
    except shop.StarsQuantityError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={'code': 'invalid_quantity', 'min': exc.min_quantity, 'max': exc.max_quantity},
        ) from exc
    except shop.StarsRecipientError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail={'code': 'invalid_recipient'}
        ) from exc
    except shop.StarsIdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={'code': 'idempotency_conflict'}) from exc
    except shop.StarsPurchaseRestrictedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={'code': 'restricted'}) from exc
    except shop.StarsShopDisabledError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Stars shop is not available') from exc

    return StarsPurchaseResponse(
        order=order_to_response(result.order),
        balance_kopeks=result.remaining_balance_kopeks,
        is_replay=result.is_idempotent_replay,
    )


@router.get('/orders', response_model=StarsOrdersListResponse)
async def list_stars_orders(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> StarsOrdersListResponse:
    orders = await shop.list_user_orders(db, user.id, limit=limit, offset=offset)
    return StarsOrdersListResponse(items=[order_to_response(order) for order in orders])


@router.get('/orders/{order_id}', response_model=StarsOrderResponse)
async def get_stars_order(
    order_id: int,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> StarsOrderResponse:
    order = await shop.get_user_order(db, user.id, order_id)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Order not found')
    return order_to_response(order)
