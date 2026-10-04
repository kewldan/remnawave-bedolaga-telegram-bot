"""Диалог покупки звёзд в боте."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from app.config import settings
from app.handlers import stars_shop as handlers
from app.services import stars_shop_service as shop
from app.states import StarsShopStates


@pytest.fixture(autouse=True)
def _shop(monkeypatch):
    monkeypatch.setattr(settings, 'STARS_SHOP_ENABLED', True)
    monkeypatch.setattr(settings, 'STARS_SHOP_DRY_RUN', True)
    monkeypatch.setattr(settings, 'STARS_SHOP_PRICE_PER_STAR_KOPEKS', 160)
    monkeypatch.setattr(settings, 'STARS_SHOP_MIN_QUANTITY', 50)
    monkeypatch.setattr(settings, 'STARS_SHOP_MAX_QUANTITY', 10000)
    monkeypatch.setattr(settings, 'STARS_SHOP_PRESETS', '50,100')


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


def _user(**overrides) -> SimpleNamespace:
    base = {'id': 5, 'language': 'ru', 'username': 'buyer_nick', 'balance_kopeks': 100_000}
    base.update(overrides)
    return SimpleNamespace(**base)


def _callback(data: str) -> MagicMock:
    callback = MagicMock(spec=CallbackQuery)
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock(spec=Message)
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback


def _shown_text(callback) -> str:
    return callback.message.edit_text.await_args.args[0]


def _buttons(callback) -> list[str]:
    markup = callback.message.edit_text.await_args.kwargs['reply_markup']
    return [button.callback_data for row in markup.inline_keyboard for button in row]


@pytest.mark.asyncio
async def test_menu_offers_self_only_with_username():
    callback = _callback(handlers.MENU_CALLBACK)
    await handlers.handle_stars_menu(callback, _user(), MagicMock(), _state())
    assert 'stars_shop_self' in _buttons(callback)

    callback = _callback(handlers.MENU_CALLBACK)
    await handlers.handle_stars_menu(callback, _user(username=None), MagicMock(), _state())
    assert 'stars_shop_self' not in _buttons(callback)
    assert 'stars_shop_other' in _buttons(callback)


@pytest.mark.asyncio
async def test_unavailable_shop_shows_alert(monkeypatch):
    monkeypatch.setattr(settings, 'STARS_SHOP_ENABLED', False)
    callback = _callback(handlers.MENU_CALLBACK)
    await handlers.handle_stars_menu(callback, _user(), MagicMock(), _state())
    callback.message.edit_text.assert_not_awaited()
    assert callback.answer.await_args.kwargs.get('show_alert') is True


@pytest.mark.asyncio
async def test_recipient_input_is_validated():
    state = _state()
    await state.set_state(StarsShopStates.waiting_recipient)
    message = MagicMock(spec=Message)
    message.answer = AsyncMock()

    message.text = 'not a nick'
    await handlers.handle_recipient_input(message, _user(), state)
    assert await state.get_state() == StarsShopStates.waiting_recipient.state

    message.text = 'https://t.me/Durov'
    await handlers.handle_recipient_input(message, _user(), state)
    assert (await state.get_data())['stars_recipient'] == 'Durov'
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_preset_opens_confirmation_with_total():
    state = _state()
    await state.update_data(stars_recipient='durov')
    callback = _callback('stars_shop_qty:100')
    await handlers.handle_quantity_preset(callback, _user(), MagicMock(), state)
    data = await state.get_data()
    assert (data['stars_quantity'], data['stars_total']) == (100, 16000)
    assert data['stars_checkout_id']
    assert 'stars_shop_pay' in _buttons(callback)


@pytest.mark.asyncio
async def test_double_pay_tap_reuses_checkout_key():
    state = _state()
    await state.update_data(stars_recipient='durov')
    await handlers.handle_quantity_preset(_callback('stars_shop_qty:100'), _user(), MagicMock(), state)

    result = shop.StarsPurchaseResult(
        order=SimpleNamespace(id=9),
        transaction=None,
        quote=shop.StarsQuote(100, 160, 16000),
        remaining_balance_kopeks=84000,
        is_idempotent_replay=False,
    )
    keys = []

    async def purchase(db, **kwargs):
        keys.append(kwargs['idempotency_key'])
        return result

    with patch.object(handlers.shop, 'purchase_stars_from_balance', purchase):
        first = _callback('stars_shop_pay')
        await handlers.handle_pay(first, _user(), MagicMock(), state)
    assert 'Заказ #9 оплачен' in _shown_text(first)
    assert len(keys) == 1
    # После оплаты состояние очищено: повторное нажатие на старый экран не создаст заказ.
    second = _callback('stars_shop_pay')
    await handlers.handle_pay(second, _user(), MagicMock(), state)
    assert len(keys) == 1
    assert second.answer.await_args.kwargs.get('show_alert') is True


@pytest.mark.asyncio
async def test_insufficient_balance_saves_cart_with_same_key():
    state = _state()
    await state.update_data(stars_recipient='durov')
    await handlers.handle_quantity_preset(_callback('stars_shop_qty:100'), _user(), MagicMock(), state)
    checkout_id = (await state.get_data())['stars_checkout_id']

    error = shop.StarsInsufficientBalanceError(required_kopeks=16000, available_kopeks=1000)
    with (
        patch.object(handlers.shop, 'purchase_stars_from_balance', AsyncMock(side_effect=error)),
        patch.object(handlers.user_cart_service, 'save_user_cart', AsyncMock(return_value=True)) as save_cart,
        patch.object(handlers, 'get_insufficient_balance_keyboard', MagicMock(return_value=MagicMock())) as keyboard,
    ):
        callback = _callback('stars_shop_pay')
        await handlers.handle_pay(callback, _user(), MagicMock(), state)

    cart = save_cart.await_args.args[1]
    assert cart['cart_mode'] == 'stars_purchase'
    assert cart['stars_checkout_id'] == checkout_id
    assert keyboard.call_args.kwargs['amount_kopeks'] == 15000
    assert keyboard.call_args.kwargs['resume_callback'] == handlers.RETURN_TO_CART_CALLBACK


@pytest.mark.asyncio
async def test_return_to_cart_keeps_original_key():
    state = _state()
    cart = {'cart_mode': 'stars_purchase', 'recipient_username': 'durov', 'quantity': 100, 'stars_checkout_id': 'orig'}
    with patch.object(handlers.user_cart_service, 'get_user_cart', AsyncMock(return_value=cart)):
        await handlers.handle_return_to_cart(_callback(handlers.RETURN_TO_CART_CALLBACK), _user(), MagicMock(), state)
    assert (await state.get_data())['stars_checkout_id'] == 'orig'


def test_legacy_main_menu_shows_button_only_when_available(monkeypatch):
    from app.services.menu_layout.service import MenuLayoutService

    context = MagicMock()
    assert MenuLayoutService._evaluate_conditions({'stars_shop_available': True}, context) is True
    monkeypatch.setattr(settings, 'STARS_SHOP_ENABLED', False)
    assert MenuLayoutService._evaluate_conditions({'stars_shop_available': True}, context) is False
