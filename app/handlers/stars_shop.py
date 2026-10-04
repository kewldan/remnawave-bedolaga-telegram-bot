"""Магазин звёзд Telegram в боте: получатель → количество → подтверждение → оплата с баланса."""

from __future__ import annotations

import html
import uuid

import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import StarsOrderStatus, User
from app.keyboards.inline import get_insufficient_balance_keyboard
from app.localization.texts import get_texts
from app.services import stars_shop_service as shop
from app.services.user_cart_service import user_cart_service
from app.states import StarsShopStates


logger = structlog.get_logger(__name__)

MENU_CALLBACK = 'menu_stars_shop'
RETURN_TO_CART_CALLBACK = 'stars_shop_return_to_cart'

_STATUS_ICONS = {
    StarsOrderStatus.PAID.value: '🕓',
    StarsOrderStatus.PROCESSING.value: '⏳',
    StarsOrderStatus.BROADCASTING.value: '⏳',
    StarsOrderStatus.COMPLETED.value: '✅',
    StarsOrderStatus.FAILED.value: '⚠️',
    StarsOrderStatus.REFUNDED.value: '↩️',
    StarsOrderStatus.NEEDS_REVIEW.value: '🔎',
}


def _back_button(texts) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=texts.BACK, callback_data=MENU_CALLBACK)


async def _show(callback_or_message, text: str, keyboard: InlineKeyboardMarkup) -> None:
    """Отредактировать сообщение с кнопкой или ответить новым на текстовый ввод."""
    if isinstance(callback_or_message, types.CallbackQuery):
        message = callback_or_message.message
        if isinstance(message, InaccessibleMessage) or message is None:
            await callback_or_message.answer()
            return
        try:
            await message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
        except TelegramBadRequest:
            await message.answer(text, reply_markup=keyboard, parse_mode='HTML')
        await callback_or_message.answer()
    else:
        await callback_or_message.answer(text, reply_markup=keyboard, parse_mode='HTML')


async def _shop_unavailable(callback: types.CallbackQuery, texts) -> bool:
    if shop.is_shop_available():
        return False
    await callback.answer(
        texts.t('STARS_SHOP_UNAVAILABLE', '⭐ Покупка звёзд сейчас недоступна'),
        show_alert=True,
    )
    return True


# ── Старт ───────────────────────────────────────────────────────────────────


async def handle_stars_menu(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)
    if await _shop_unavailable(callback, texts):
        return
    await state.clear()
    config = shop.get_shop_config()
    text = texts.t(
        'STARS_SHOP_TITLE',
        '⭐ <b>Звёзды Telegram</b>\n\n'
        'Звёзды приходят на аккаунт получателя за пару минут.\n'
        'Цена: <b>{price}</b> за звезду, от {min} до {max} шт.\n\n'
        'Кому отправить звёзды?',
    ).format(
        price=texts.format_price(config.price_per_star_kopeks),
        min=config.min_quantity,
        max=config.max_quantity,
    )
    rows: list[list[InlineKeyboardButton]] = []
    if db_user.username:
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t('STARS_SHOP_TO_SELF_BUTTON', '🙋 Себе (@{username})').format(
                        username=db_user.username
                    ),
                    callback_data='stars_shop_self',
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=texts.t('STARS_SHOP_TO_OTHER_BUTTON', '🎁 Другому'), callback_data='stars_shop_other'
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=texts.t('STARS_SHOP_MY_ORDERS_BUTTON', '📜 Мои заказы'), callback_data='stars_shop_orders'
            )
        ]
    )
    rows.append([InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])
    await _show(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


# ── Получатель ──────────────────────────────────────────────────────────────


async def handle_stars_to_self(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)
    if await _shop_unavailable(callback, texts):
        return
    try:
        recipient = shop.normalize_recipient(db_user.username)
    except shop.StarsRecipientError:
        await callback.answer(
            texts.t('STARS_SHOP_NO_USERNAME', 'У вашего аккаунта нет ника — укажите получателя вручную'),
            show_alert=True,
        )
        return
    await state.update_data(stars_recipient=recipient)
    await _show_quantity(callback, db_user, state)


async def handle_stars_to_other(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)
    if await _shop_unavailable(callback, texts):
        return
    await state.set_state(StarsShopStates.waiting_recipient)
    text = texts.t(
        'STARS_SHOP_ASK_RECIPIENT',
        '🎁 Отправьте ник получателя в Telegram — например, <code>@durov</code>.\n\n'
        'Звёзды придут на этот аккаунт, отменить отправку будет нельзя.',
    )
    await _show(callback, text, InlineKeyboardMarkup(inline_keyboard=[[_back_button(texts)]]))


async def handle_recipient_input(message: types.Message, db_user: User, state: FSMContext) -> None:
    texts = get_texts(db_user.language)
    try:
        recipient = shop.normalize_recipient(message.text)
    except shop.StarsRecipientError:
        await message.answer(
            texts.t(
                'STARS_SHOP_BAD_RECIPIENT',
                '❌ Не похоже на ник Telegram. Нужны латинские буквы, цифры и «_», от 4 до 32 символов.',
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back_button(texts)]]),
        )
        return
    await state.update_data(stars_recipient=recipient)
    await state.set_state(None)
    await _show_quantity(message, db_user, state)


# ── Количество ──────────────────────────────────────────────────────────────


async def _show_quantity(target, db_user: User, state: FSMContext) -> None:
    texts = get_texts(db_user.language)
    data = await state.get_data()
    config = shop.get_shop_config()
    text = texts.t(
        'STARS_SHOP_ASK_QUANTITY',
        '⭐ Получатель: <b>@{recipient}</b>\n\nСколько звёзд отправить?',
    ).format(recipient=html.escape(data.get('stars_recipient', '')))
    buttons = [
        InlineKeyboardButton(
            text=texts.t('STARS_SHOP_PRESET_BUTTON', '{quantity} ⭐ — {price}').format(
                quantity=quantity, price=texts.format_price(quantity * config.price_per_star_kopeks)
            ),
            callback_data=f'stars_shop_qty:{quantity}',
        )
        for quantity in config.presets
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton(
                text=texts.t('STARS_SHOP_CUSTOM_QUANTITY_BUTTON', '✏️ Другое количество'),
                callback_data='stars_shop_qty_custom',
            )
        ]
    )
    rows.append([_back_button(texts)])
    await _show(target, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def handle_quantity_preset(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)
    if await _shop_unavailable(callback, texts):
        return
    try:
        quantity = int(callback.data.split(':', 1)[1])
    except (IndexError, ValueError):
        await callback.answer()
        return
    await _show_confirmation(callback, db_user, state, quantity)


async def handle_quantity_custom(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)
    config = shop.get_shop_config()
    await state.set_state(StarsShopStates.waiting_quantity)
    text = texts.t('STARS_SHOP_ASK_CUSTOM_QUANTITY', '✏️ Напишите количество звёзд — от {min} до {max}.').format(
        min=config.min_quantity, max=config.max_quantity
    )
    await _show(callback, text, InlineKeyboardMarkup(inline_keyboard=[[_back_button(texts)]]))


async def handle_quantity_input(message: types.Message, db_user: User, state: FSMContext) -> None:
    texts = get_texts(db_user.language)
    config = shop.get_shop_config()
    raw = (message.text or '').strip().replace(' ', '')
    if not raw.isdigit() or not (config.min_quantity <= int(raw) <= config.max_quantity):
        await message.answer(
            texts.t('STARS_SHOP_BAD_QUANTITY', '❌ Нужно число от {min} до {max}.').format(
                min=config.min_quantity, max=config.max_quantity
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_back_button(texts)]]),
        )
        return
    await state.set_state(None)
    await _show_confirmation(message, db_user, state, int(raw))


# ── Подтверждение и оплата ──────────────────────────────────────────────────


async def _show_confirmation(target, db_user: User, state: FSMContext, quantity: int) -> None:
    texts = get_texts(db_user.language)
    data = await state.get_data()
    recipient = data.get('stars_recipient')
    if not recipient:
        await _show(
            target,
            texts.t('STARS_SHOP_SESSION_EXPIRED', '⌛ Выбор устарел, начните заново.'),
            InlineKeyboardMarkup(inline_keyboard=[[_back_button(texts)]]),
        )
        return
    try:
        quote = shop.quote_stars(quantity)
    except shop.StarsQuantityError as exc:
        await _show(
            target,
            texts.t('STARS_SHOP_BAD_QUANTITY', '❌ Нужно число от {min} до {max}.').format(
                min=exc.min_quantity, max=exc.max_quantity
            ),
            InlineKeyboardMarkup(inline_keyboard=[[_back_button(texts)]]),
        )
        return
    # Новый ключ на каждый показ подтверждения: повторное нажатие «Оплатить»
    # на том же экране — тот же заказ, новый экран — новая покупка.
    await state.update_data(
        stars_quantity=quantity,
        stars_total=quote.total_kopeks,
        stars_checkout_id=uuid.uuid4().hex,
    )
    text = texts.t(
        'STARS_SHOP_CONFIRM',
        '⭐ <b>Проверьте заказ</b>\n\n'
        'Получатель: <b>@{recipient}</b>\n'
        'Количество: <b>{quantity} ⭐</b>\n'
        'К оплате: <b>{total}</b>\n'
        'На балансе: {balance}',
    ).format(
        recipient=html.escape(recipient),
        quantity=quantity,
        total=texts.format_price(quote.total_kopeks),
        balance=texts.format_price(db_user.balance_kopeks),
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('STARS_SHOP_PAY_BUTTON', '✅ Оплатить'), callback_data='stars_shop_pay'
                )
            ],
            [_back_button(texts)],
        ]
    )
    await _show(target, text, keyboard)


async def handle_pay(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)
    if await _shop_unavailable(callback, texts):
        return
    data = await state.get_data()
    recipient = data.get('stars_recipient')
    quantity = data.get('stars_quantity')
    total = data.get('stars_total')
    checkout_id = data.get('stars_checkout_id')
    if not (recipient and quantity and checkout_id) or total is None:
        await callback.answer(
            texts.t('STARS_SHOP_SESSION_EXPIRED', '⌛ Выбор устарел, начните заново.'), show_alert=True
        )
        return
    await _purchase(callback, db_user, db, state, recipient, int(quantity), int(total), str(checkout_id))


async def _purchase(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    recipient: str,
    quantity: int,
    total: int,
    checkout_id: str,
) -> None:
    texts = get_texts(db_user.language)
    try:
        result = await shop.purchase_stars_from_balance(
            db,
            buyer_id=db_user.id,
            quantity=quantity,
            recipient_username=recipient,
            expected_total_kopeks=total,
            idempotency_key=checkout_id,
            source='bot',
        )
    except shop.StarsInsufficientBalanceError as exc:
        await user_cart_service.save_user_cart(
            db_user.id,
            shop.build_stars_cart(
                user_id=db_user.id,
                quantity=quantity,
                recipient_username=recipient,
                total_kopeks=exc.required_kopeks,
                missing_kopeks=exc.missing_kopeks,
                idempotency_key=checkout_id,
                source='bot',
            ),
        )
        text = texts.t(
            'STARS_SHOP_INSUFFICIENT',
            '💰 Не хватает <b>{missing}</b>.\n\n'
            'Пополните баланс — заказ {quantity} ⭐ для @{recipient} оплатится автоматически.',
        ).format(
            missing=texts.format_price(exc.missing_kopeks),
            quantity=quantity,
            recipient=html.escape(recipient),
        )
        keyboard = get_insufficient_balance_keyboard(
            db_user.language,
            resume_callback=RETURN_TO_CART_CALLBACK,
            amount_kopeks=exc.missing_kopeks,
            has_saved_cart=True,
            resume_text=texts.t('STARS_SHOP_RETURN_TO_CART_BUTTON', '⭐ Вернуться к заказу звёзд'),
        )
        await _show(callback, text, keyboard)
        return
    except shop.StarsPriceChangedError as exc:
        await callback.answer(
            texts.t('STARS_SHOP_PRICE_CHANGED', '⚠️ Цена изменилась — проверьте заказ ещё раз.'), show_alert=True
        )
        await _show_confirmation(callback, db_user, state, exc.fresh_quote.quantity)
        return
    except shop.StarsPurchaseRestrictedError:
        await callback.answer(
            texts.t('STARS_SHOP_RESTRICTED', '⛔ Покупки для вашего аккаунта ограничены.'), show_alert=True
        )
        return
    except shop.StarsShopError as exc:
        logger.warning('Звёзды: покупка в боте не прошла', user_id=db_user.id, error=str(exc))
        await callback.answer(texts.t('STARS_SHOP_UNAVAILABLE', '⭐ Покупка звёзд сейчас недоступна'), show_alert=True)
        return

    await state.clear()
    text = texts.t(
        'STARS_SHOP_PAID',
        '✅ <b>Заказ #{order_id} оплачен</b>\n\n'
        '{quantity} ⭐ для @{recipient} — отправляем, обычно это пара минут. Мы напишем, когда звёзды придут.\n\n'
        'Остаток на балансе: {balance}',
    ).format(
        order_id=result.order.id,
        quantity=quantity,
        recipient=html.escape(recipient),
        balance=texts.format_price(result.remaining_balance_kopeks),
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('STARS_SHOP_MY_ORDERS_BUTTON', '📜 Мои заказы'), callback_data='stars_shop_orders'
                )
            ],
            [InlineKeyboardButton(text=texts.MAIN_MENU_BUTTON, callback_data='back_to_menu')],
        ]
    )
    await _show(callback, text, keyboard)


async def handle_return_to_cart(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    """Вернуться к сохранённому заказу (например, после пополнения без автопокупки)."""
    texts = get_texts(db_user.language)
    cart = await user_cart_service.get_user_cart(db_user.id)
    if not cart or cart.get('cart_mode') != shop.STARS_CART_MODE:
        await callback.answer(
            texts.t('STARS_SHOP_SESSION_EXPIRED', '⌛ Выбор устарел, начните заново.'), show_alert=True
        )
        return
    await state.update_data(stars_recipient=cart.get('recipient_username'))
    await _show_confirmation(callback, db_user, state, int(cart.get('quantity') or 0))
    # Возврат к корзине — тот же заказ: оставляем исходный ключ, чтобы не создать дубль.
    await state.update_data(stars_checkout_id=cart.get('stars_checkout_id'))


# ── Мои заказы ──────────────────────────────────────────────────────────────


async def handle_my_orders(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
) -> None:
    texts = get_texts(db_user.language)
    orders = await shop.list_user_orders(db, db_user.id, limit=10)
    if not orders:
        text = texts.t('STARS_SHOP_NO_ORDERS', '📜 Заказов звёзд пока нет.')
    else:
        lines = [texts.t('STARS_SHOP_ORDERS_TITLE', '📜 <b>Ваши заказы звёзд</b>'), '']
        for order in orders:
            status = texts.t(f'STARS_SHOP_STATUS_{order.status.upper()}', order.status)
            lines.append(
                texts.t('STARS_SHOP_ORDER_LINE', '{icon} #{order_id} · {quantity} ⭐ → @{recipient} · {status}').format(
                    icon=_STATUS_ICONS.get(order.status, '•'),
                    order_id=order.id,
                    quantity=order.quantity,
                    recipient=html.escape(order.recipient_username),
                    status=status,
                )
            )
        text = '\n'.join(lines)
    await _show(callback, text, InlineKeyboardMarkup(inline_keyboard=[[_back_button(texts)]]))


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(handle_stars_menu, F.data == MENU_CALLBACK)
    dp.callback_query.register(handle_stars_to_self, F.data == 'stars_shop_self')
    dp.callback_query.register(handle_stars_to_other, F.data == 'stars_shop_other')
    dp.callback_query.register(handle_quantity_preset, F.data.startswith('stars_shop_qty:'))
    dp.callback_query.register(handle_quantity_custom, F.data == 'stars_shop_qty_custom')
    dp.callback_query.register(handle_pay, F.data == 'stars_shop_pay')
    dp.callback_query.register(handle_return_to_cart, F.data == RETURN_TO_CART_CALLBACK)
    dp.callback_query.register(handle_my_orders, F.data == 'stars_shop_orders')
    dp.message.register(handle_recipient_input, StarsShopStates.waiting_recipient)
    dp.message.register(handle_quantity_input, StarsShopStates.waiting_quantity)
