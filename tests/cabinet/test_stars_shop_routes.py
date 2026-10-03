"""API магазина звёзд в кабинете.

По образцу остальных тестов админ-маршрутов: проверка регистрации путей на общем
роутере и прямой вызов обработчиков с заранее подготовленными аргументами
(``require_permission`` и ``get_cabinet_db`` обходятся).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.config import settings
from app.services import stars_shop_service as shop


@pytest.fixture(autouse=True)
def _shop(monkeypatch):
    monkeypatch.setattr(settings, 'STARS_SHOP_ENABLED', True)
    monkeypatch.setattr(settings, 'STARS_SHOP_DRY_RUN', True)
    monkeypatch.setattr(settings, 'STARS_SHOP_PRICE_PER_STAR_KOPEKS', 160)
    monkeypatch.setattr(settings, 'STARS_SHOP_MIN_QUANTITY', 50)
    monkeypatch.setattr(settings, 'STARS_SHOP_MAX_QUANTITY', 10000)


def _user(**overrides) -> SimpleNamespace:
    base = {'id': 7, 'balance_kopeks': 50_000, 'username': 'buyer', 'email': None, 'telegram_id': 42}
    base.update(overrides)
    return SimpleNamespace(**base)


def _order(**overrides) -> SimpleNamespace:
    base = {
        'id': 11,
        'status': 'paid',
        'quantity': 100,
        'amount_kopeks': 16000,
        'recipient_username': 'durov',
        'recipient_name': None,
        'created_at': datetime.now(UTC),
        'completed_at': None,
        'refunded_at': None,
        'user_id': 7,
        'source': 'cabinet',
        'attempts': 0,
        'last_error': None,
        'fragment_req_id': None,
        'ton_tx_hash': None,
        'cost_nanoton': None,
        'next_attempt_at': None,
        'updated_at': datetime.now(UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _purchase_request(**overrides):
    from app.cabinet.schemas.stars_shop import StarsPurchaseRequest

    data = {
        'quantity': 100,
        'recipient_username': '@durov',
        'expected_total_kopeks': 16000,
        'idempotency_key': 'idem-key-123',
    }
    data.update(overrides)
    return StarsPurchaseRequest(**data)


# ── Регистрация ─────────────────────────────────────────────────────────────


def test_stars_routes_registered(registered_paths) -> None:
    assert registered_paths.get('/cabinet/stars/config') == {'GET'}
    assert registered_paths.get('/cabinet/stars/quote') == {'GET'}
    assert registered_paths.get('/cabinet/stars/purchase') == {'POST'}
    assert registered_paths.get('/cabinet/stars/orders') == {'GET'}
    assert registered_paths.get('/cabinet/stars/orders/{order_id}') == {'GET'}
    assert registered_paths.get('/cabinet/admin/stars/status') == {'GET'}
    assert registered_paths.get('/cabinet/admin/stars/stats') == {'GET'}
    assert registered_paths.get('/cabinet/admin/stars/orders') == {'GET'}
    assert registered_paths.get('/cabinet/admin/stars/wallet') == {'GET'}
    for action in ('retry', 'refund', 'complete'):
        assert registered_paths.get(f'/cabinet/admin/stars/orders/{{order_id}}/{action}') == {'POST'}


def test_stars_permissions_registered() -> None:
    from app.services.permission_service import get_all_permissions
    from app.services.rbac_bootstrap_service import _PRESET_ROLES

    assert {'stars_shop:read', 'stars_shop:manage'} <= set(get_all_permissions())
    admin = next(role for role in _PRESET_ROLES if role['name'] == 'Admin')
    assert 'stars_shop:*' in admin['permissions']


def test_fragment_secrets_are_never_editable_from_settings_api() -> None:
    from app.services.system_settings_service import bot_configuration_service

    for key in ('FRAGMENT_COOKIES', 'FRAGMENT_WALLET_MNEMONIC', 'FRAGMENT_TON_API_KEY'):
        assert key in bot_configuration_service.EXCLUDED_KEYS
    assert bot_configuration_service._resolve_category_key('STARS_SHOP_PRICE_PER_STAR_KOPEKS') == 'STARS_SHOP'


# ── Клиент ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_config_hides_shop_without_fulfillment(monkeypatch) -> None:
    from app.cabinet.routes import stars_shop as routes

    config = await routes.get_stars_config(user=_user())
    assert config.available is True and config.presets
    monkeypatch.setattr(settings, 'STARS_SHOP_DRY_RUN', False)
    monkeypatch.setattr(settings, 'FRAGMENT_COOKIES', '')
    assert (await routes.get_stars_config(user=_user())).available is False
    with pytest.raises(HTTPException) as exc:
        await routes.get_stars_quote(quantity=100, user=_user())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_purchase_success_maps_order() -> None:
    from app.cabinet.routes import stars_shop as routes

    result = shop.StarsPurchaseResult(
        order=_order(),
        transaction=None,
        quote=shop.StarsQuote(100, 160, 16000),
        remaining_balance_kopeks=34000,
        is_idempotent_replay=False,
    )
    with (
        patch.object(routes.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(routes.shop, 'purchase_stars_from_balance', AsyncMock(return_value=result)) as purchase,
    ):
        response = await routes.purchase_stars(request=_purchase_request(), user=_user(), db=MagicMock())

    assert (response.order.id, response.balance_kopeks, response.is_replay) == (11, 34000, False)
    assert purchase.await_args.kwargs['source'] == 'cabinet'


@pytest.mark.asyncio
async def test_insufficient_balance_saves_cart_and_returns_402() -> None:
    from app.cabinet.routes import stars_shop as routes

    error = shop.StarsInsufficientBalanceError(required_kopeks=16000, available_kopeks=1000)
    with (
        patch.object(routes.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(routes.shop, 'purchase_stars_from_balance', AsyncMock(side_effect=error)),
        patch.object(routes.user_cart_service, 'save_user_cart', AsyncMock(return_value=True)) as save_cart,
    ):
        with pytest.raises(HTTPException) as exc:
            await routes.purchase_stars(request=_purchase_request(), user=_user(), db=MagicMock())

    assert exc.value.status_code == 402
    assert exc.value.detail['missing_kopeks'] == 15000
    cart = save_cart.await_args.args[1]
    assert cart['cart_mode'] == 'stars_purchase'
    assert (cart['recipient_username'], cart['stars_checkout_id']) == ('durov', 'idem-key-123')
    assert cart['return_to_cart'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('error', 'code'),
    [
        (shop.StarsPriceChangedError(16000, shop.StarsQuote(100, 170, 17000)), 409),
        (shop.StarsQuantityError(50, 10000), 422),
        (shop.StarsRecipientError('bad'), 422),
        (shop.StarsIdempotencyConflictError('x'), 409),
        (shop.StarsPurchaseRestrictedError('x'), 403),
        (shop.StarsShopDisabledError('x'), 404),
    ],
)
async def test_purchase_errors_map_to_http(error, code) -> None:
    from app.cabinet.routes import stars_shop as routes

    with (
        patch.object(routes.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(routes.shop, 'purchase_stars_from_balance', AsyncMock(side_effect=error)),
    ):
        with pytest.raises(HTTPException) as exc:
            await routes.purchase_stars(request=_purchase_request(), user=_user(), db=MagicMock())
    assert exc.value.status_code == code


@pytest.mark.asyncio
async def test_purchase_is_rate_limited() -> None:
    from app.cabinet.routes import stars_shop as routes

    with patch.object(routes.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as exc:
            await routes.purchase_stars(request=_purchase_request(), user=_user(), db=MagicMock())
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_user_sees_only_own_order() -> None:
    from app.cabinet.routes import stars_shop as routes

    with patch.object(routes.shop, 'get_user_order', AsyncMock(return_value=None)) as get_order:
        with pytest.raises(HTTPException) as exc:
            await routes.get_stars_order(order_id=99, user=_user(id=7), db=MagicMock())
    assert exc.value.status_code == 404
    assert get_order.await_args.args[1:] == (7, 99)


# ── Админка ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_state_errors_become_409() -> None:
    from app.cabinet.routes import admin_stars_shop as routes
    from app.cabinet.schemas.stars_shop import AdminRefundRequest

    with patch.object(routes.shop, 'refund_order', AsyncMock(side_effect=shop.StarsOrderStateError('final'))):
        with pytest.raises(HTTPException) as exc:
            await routes.refund_order(order_id=1, request=AdminRefundRequest(), admin=_user(), db=MagicMock())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_admin_list_rejects_unknown_status() -> None:
    from app.cabinet.routes import admin_stars_shop as routes

    with pytest.raises(HTTPException) as exc:
        await routes.list_orders(status_filter='weird', search=None, limit=10, offset=0, _=_user(), db=MagicMock())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_admin_wallet_reports_missing_configuration(monkeypatch) -> None:
    from app.cabinet.routes import admin_stars_shop as routes

    monkeypatch.setattr(settings, 'FRAGMENT_COOKIES', '')
    with pytest.raises(HTTPException) as exc:
        await routes.get_wallet(_=_user())
    assert exc.value.status_code == 503
