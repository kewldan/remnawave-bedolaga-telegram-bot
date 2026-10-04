"""Уведомления админам о заказах звёзд: текст и кнопки.

Сборка отделена от отправки, чтобы проверять содержимое без бота.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import types

from app.config import settings
from app.database.models import StarsOrder, User
from app.services.ton_rate_service import TonRate
from app.utils.formatters import format_username_link
from app.utils.timezone import format_local_datetime


TONSCAN_TX_URL = 'https://tonscan.org/tx/{}'
TONVIEWER_TX_URL = 'https://tonviewer.com/transaction/{}'
TONSCAN_ADDRESS_URL = 'https://tonscan.org/address/{}'

_SOURCES = {'bot': 'бот', 'cabinet': 'кабинет', 'auto_cart': 'после пополнения'}


@dataclass(frozen=True, slots=True)
class WalletSnapshot:
    address: str
    balance_ton: float


def is_real_tx(tx_hash: str | None) -> bool:
    """Хеш настоящего перевода (а не тестового режима) — по нему есть что открыть."""
    return bool(tx_hash) and not tx_hash.startswith('dry-run')


def _rub(kopeks: int, *, signed: bool = False) -> str:
    sign = ('+' if kopeks > 0 else '−' if kopeks < 0 else '') if signed else ('−' if kopeks < 0 else '')
    rubles, rest = divmod(abs(kopeks), 100)
    return f'{sign}{rubles:,}'.replace(',', ' ') + f',{rest:02d} ₽'


def _ton(value: float) -> str:
    return f'{value:.4f}'.rstrip('0').rstrip('.').replace('.', ',') + ' TON'


def _duration(start: datetime | None, end: datetime | None) -> str | None:
    if not start or not end:
        return None
    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 60:
        return f'{seconds} с'
    minutes, seconds = divmod(seconds, 60)
    return f'{minutes} мин {seconds} с' if minutes < 60 else f'{minutes // 60} ч {minutes % 60} мин'


def _buyer_line(buyer: User | None) -> str:
    if buyer is None:
        return '👤 Покупатель: —'
    name = html.escape(buyer.full_name or '') if (buyer.first_name or buyer.last_name) else ''
    if buyer.username:
        who = format_username_link(buyer.username, f'@{html.escape(buyer.username)}')
        who = f'{name} ({who})' if name else who
    else:
        who = name or html.escape(buyer.email or f'#{buyer.id}')
    ident = f' · ID <code>{buyer.telegram_id}</code>' if buyer.telegram_id else f' · #{buyer.id}'
    return f'👤 Покупатель: {who}{ident}'


def _recipient_line(order: StarsOrder, buyer: User | None) -> str:
    link = format_username_link(order.recipient_username, f'@{html.escape(order.recipient_username)}')
    line = f'🎯 Получатель: {link}'
    name = (order.recipient_name or '').strip()
    if name and name.lower().lstrip('@') != order.recipient_username.lower():
        line += f' — {html.escape(name)}'
    if buyer and buyer.username and buyer.username.lower() == order.recipient_username.lower():
        line += ' <i>(себе)</i>'
    return line


def build_completed_message(
    order: StarsOrder,
    *,
    buyer: User | None,
    rate: TonRate | None,
    wallet: WalletSnapshot | None,
    dry_run: bool = False,
) -> tuple[str, types.InlineKeyboardMarkup | None]:
    """Уведомление о выданном заказе: кто, кому, сколько заработали и что с кошельком."""
    title = '⭐ ЗВЁЗДЫ ПРОДАНЫ' + (' (тестовый режим)' if dry_run else '')
    lines = [f'<b>{title}</b>', '', _buyer_line(buyer), _recipient_line(order, buyer)]

    price_per_star = order.amount_kopeks / order.quantity if order.quantity else 0
    deal = [
        '<blockquote>',
        f'⭐ <b>{order.quantity:,}</b> звёзд · заказ #{order.id} · {_SOURCES.get(order.source, order.source)}'.replace(
            ',', ' '
        ),
        f'💵 Оплачено: <b>{_rub(order.amount_kopeks)}</b> ({_rub(round(price_per_star))} за ⭐)',
    ]
    cost_nanoton = order.cost_nanoton or 0
    if cost_nanoton > 0:
        cost_line = f'💎 Себестоимость: {_ton(cost_nanoton / 1e9)}'
        if order.cost_kopeks is not None:
            cost_line += f' ≈ {_rub(order.cost_kopeks)}'
        deal.append(cost_line)
        if order.cost_kopeks is not None:
            margin = order.amount_kopeks - order.cost_kopeks
            percent = margin / order.amount_kopeks * 100 if order.amount_kopeks else 0
            deal.append(f'📈 Маржа: <b>{_rub(margin, signed=True)}</b> ({percent:.1f}%)'.replace('.', ','))
        if rate is not None:
            deal.append(f'💱 Курс TON: {_rub(rate.kopeks)} ({rate.source})')
    deal.append('</blockquote>')
    lines.extend(deal)

    timing = _duration(order.created_at, order.completed_at)
    attempt = f'попытка {order.attempts}' if order.attempts else None
    meta = ' · '.join(filter(None, [f'выдано за {timing}' if timing else None, attempt]))
    if meta:
        lines.append(f'⚡ {meta[0].upper()}{meta[1:]}')

    if wallet is not None:
        wallet_line = f'👛 Кошелёк: <b>{_ton(wallet.balance_ton)}</b>'
        if rate is not None:
            wallet_line += f' ≈ {_rub(round(wallet.balance_ton * rate.kopeks))}'
        lines.append(wallet_line)
        if cost_nanoton > 0 and order.quantity:
            stars_left = int(wallet.balance_ton * 1e9 / (cost_nanoton / order.quantity))
            low = stars_left < max(0, int(settings.STARS_SHOP_WALLET_LOW_STARS or 0))
            lines.append(
                f'{"⚠️" if low else "🔋"} Хватит примерно на {stars_left:,} ⭐'.replace(',', ' ')
                + (' — <b>пора пополнить</b>' if low else '')
            )

    if order.last_error:
        lines.append(f'⚠️ {html.escape(order.last_error)}')
    if is_real_tx(order.ton_tx_hash):
        lines.append(f'🧾 <code>{html.escape(order.ton_tx_hash)}</code>')

    lines.append(f'<i>{format_local_datetime(order.completed_at or datetime.now(UTC), "%d.%m.%Y %H:%M")}</i>')
    return '\n'.join(lines), _keyboard(order, buyer, wallet)


def _cabinet_url(path: str) -> str | None:
    base = (settings.CABINET_URL or '').rstrip('/')
    if not base.startswith('https://') or 'example.com' in base:
        return None
    return f'{base}{path}'


def _keyboard(
    order: StarsOrder, buyer: User | None, wallet: WalletSnapshot | None
) -> types.InlineKeyboardMarkup | None:
    rows: list[list[types.InlineKeyboardButton]] = []
    if is_real_tx(order.ton_tx_hash):
        rows.append(
            [
                types.InlineKeyboardButton(text='🔎 Tonscan', url=TONSCAN_TX_URL.format(order.ton_tx_hash)),
                types.InlineKeyboardButton(text='🔎 Tonviewer', url=TONVIEWER_TX_URL.format(order.ton_tx_hash)),
            ]
        )
    cabinet_row = []
    order_url = _cabinet_url(f'/admin/stars?order={order.id}')
    if order_url:
        cabinet_row.append(types.InlineKeyboardButton(text=f'⭐ Заказ #{order.id}', url=order_url))
    buyer_url = _cabinet_url(f'/admin/users/{buyer.id}') if buyer else None
    if buyer_url:
        cabinet_row.append(types.InlineKeyboardButton(text='👤 Покупатель', url=buyer_url))
    if cabinet_row:
        rows.append(cabinet_row)
    if wallet is not None:
        rows.append([types.InlineKeyboardButton(text='👛 Кошелёк', url=TONSCAN_ADDRESS_URL.format(wallet.address))])
    return types.InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def build_order_keyboard(order_id: int) -> types.InlineKeyboardMarkup | None:
    """Кнопка «открыть заказ в кабинете» для уведомлений о проблемах."""
    url = _cabinet_url(f'/admin/stars?order={order_id}')
    if not url:
        return None
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text=f'⭐ Заказ #{order_id} в кабинете', url=url)]]
    )
