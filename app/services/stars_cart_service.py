"""Корзина заказа звёзд: «не хватило денег → пополнил → куплено автоматически».

Корзину кладут бот и кабинет при ``StarsInsufficientBalanceError``. После пополнения
``auto_purchase_saved_cart_after_topup`` передаёт её сюда. Покупка идёт по тому же
ключу идемпотентности, что был у исходной попытки, поэтому повторное пополнение не
создаст второй заказ.
"""

from __future__ import annotations

import structlog
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.stars_shop_service import (
    STARS_CART_MODE,
    StarsInsufficientBalanceError,
    StarsPriceChangedError,
    StarsShopError,
    purchase_stars_from_balance,
)
from app.services.user_cart_service import user_cart_service


logger = structlog.get_logger(__name__)


async def auto_purchase_stars_cart(
    db: AsyncSession,
    user: User,
    cart: dict,
    *,
    bot: Bot | None = None,
) -> bool:
    """Купить звёзды из корзины после пополнения. ``True`` — заказ оплачен."""
    if cart.get('cart_mode') != STARS_CART_MODE or cart.get('user_id') != user.id:
        return False
    if not await user_cart_service.has_topup_intent(user.id):
        logger.info('Звёзды: корзина есть, но пополнение было не ради неё', user_id=user.id)
        return False

    checkout_id = str(cart.get('stars_checkout_id') or '')
    quantity = int(cart.get('quantity') or 0)
    recipient = str(cart.get('recipient_username') or '')
    expected = int(cart.get('total_price') or 0)
    if not checkout_id or quantity <= 0 or not recipient:
        await user_cart_service.delete_user_cart(user.id)
        return False

    try:
        result = await purchase_stars_from_balance(
            db,
            buyer_id=user.id,
            quantity=quantity,
            recipient_username=recipient,
            expected_total_kopeks=expected,
            idempotency_key=checkout_id,
            source='auto_cart',
        )
    except StarsInsufficientBalanceError as exc:
        # Пополнили не на всю сумму — корзина и метка остаются до следующего пополнения.
        cart['missing_amount'] = exc.missing_kopeks
        await user_cart_service.save_user_cart(user.id, cart)
        logger.info('Звёзды: после пополнения всё ещё не хватает', user_id=user.id, missing=exc.missing_kopeks)
        return False
    except StarsPriceChangedError:
        # Цена изменилась: молча списывать другую сумму нельзя — ждём подтверждения.
        await _notify(bot, user, 'price_changed', quantity=quantity, recipient=recipient)
        await user_cart_service.clear_topup_intent(user.id)
        return False
    except StarsShopError as exc:
        logger.warning('Звёзды: автопокупка из корзины не прошла', user_id=user.id, error=str(exc))
        await user_cart_service.clear_topup_intent(user.id)
        await user_cart_service.delete_user_cart(user.id)
        return False

    await user_cart_service.clear_topup_intent(user.id)
    await user_cart_service.delete_user_cart(user.id)
    if not result.is_idempotent_replay:
        await _notify(bot, user, 'paid', quantity=quantity, recipient=recipient)
    return True


async def _notify(bot: Bot | None, user: User, kind: str, *, quantity: int, recipient: str) -> None:
    if bot is None or not user.telegram_id:
        return
    from app.localization.texts import get_texts

    texts = get_texts(user.language)
    if kind == 'paid':
        text = texts.t(
            'STARS_SHOP_AUTO_PURCHASED',
            '✅ Баланс пополнен — заказ оплачен: {quantity} ⭐ для @{recipient}. Отправляем звёзды.',
        )
    else:
        text = texts.t(
            'STARS_SHOP_AUTO_PRICE_CHANGED',
            '⚠️ Цена звёзд изменилась, поэтому заказ {quantity} ⭐ для @{recipient} не оплачен автоматически. '
            'Откройте «Купить звёзды» и подтвердите заново.',
        )
    try:
        await bot.send_message(user.telegram_id, text.format(quantity=quantity, recipient=recipient))
    except Exception as exc:
        logger.warning('Звёзды: не удалось отправить уведомление о корзине', user_id=user.id, error=str(exc))
