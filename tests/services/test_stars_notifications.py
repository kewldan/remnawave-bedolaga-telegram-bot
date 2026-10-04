"""Уведомление админам о проданных звёздах: деньги, кошелёк и ссылки на перевод."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import settings
from app.database.models import StarsOrder, User
from app.services.stars_notifications import WalletSnapshot, build_completed_message, build_order_keyboard
from app.services.ton_rate_service import TonRate


TX = 'ce15694ba402224da7033035dccc33f77ba4a9db690a3dd8b1c8442103dd1ffb'
WALLET = 'UQBSl6VKWo2-sSoKohxeikg3XeH9Goi86fY19nyh6usdchif'


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://lk.example.org/')
    monkeypatch.setattr(settings, 'STARS_SHOP_WALLET_LOW_STARS', 1000)


def _order(**overrides) -> StarsOrder:
    created = datetime(2026, 10, 4, 14, 23, 47, tzinfo=UTC)
    values = {
        'id': 1,
        'user_id': 5,
        'recipient_username': 'kewldan',
        'recipient_name': 'Даниил',
        'quantity': 50,
        'amount_kopeks': 7000,
        'source': 'cabinet',
        'attempts': 1,
        'ton_tx_hash': TX,
        'cost_nanoton': 491_500_000,
        'ton_rate_kopeks': 12774,
        'cost_kopeks': 6278,
        'created_at': created,
        'completed_at': created + timedelta(seconds=4),
        'last_error': None,
    }
    values.update(overrides)
    return StarsOrder(**values)


def _buyer(**overrides) -> User:
    values = {'id': 5, 'telegram_id': 787751346, 'username': 'kewldan', 'first_name': 'Даниил'}
    values.update(overrides)
    return User(**values)


def _urls(markup) -> list[str]:
    return [button.url for row in markup.inline_keyboard for button in row]


def test_completed_message_has_money_wallet_and_links():
    text, markup = build_completed_message(
        _order(),
        buyer=_buyer(),
        rate=TonRate(kopeks=12774, source='tonapi'),
        wallet=WalletSnapshot(address=WALLET, balance_ton=13.8466),
    )
    assert '<b>⭐ ЗВЁЗДЫ ПРОДАНЫ</b>' in text
    assert 'ID <code>787751346</code>' in text
    assert '<i>(себе)</i>' in text
    assert 'заказ #1 · кабинет' in text
    assert 'Оплачено: <b>70,00 ₽</b> (1,40 ₽ за ⭐)' in text
    assert 'Себестоимость: 0,4915 TON ≈ 62,78 ₽' in text
    assert 'Маржа: <b>+7,22 ₽</b> (10,3%)' in text
    assert 'Курс TON: 127,74 ₽ (tonapi)' in text
    assert 'Выдано за 4 с · попытка 1' in text
    assert 'Кошелёк: <b>13,8466 TON</b> ≈ 1 768,76 ₽' in text
    # 13.8466 TON / (0.4915 TON / 50 ⭐) ≈ 1408 ⭐ — запас выше порога, без тревоги.
    assert '🔋 Хватит примерно на 1 408 ⭐' in text
    assert f'<code>{TX}</code>' in text

    urls = _urls(markup)
    assert f'https://tonscan.org/tx/{TX}' in urls
    assert f'https://tonviewer.com/transaction/{TX}' in urls
    assert 'https://lk.example.org/admin/stars?order=1' in urls
    assert 'https://lk.example.org/admin/users/5' in urls
    assert f'https://tonscan.org/address/{WALLET}' in urls


def test_low_wallet_and_negative_margin_are_flagged():
    text, _ = build_completed_message(
        _order(cost_kopeks=7500),
        buyer=_buyer(username='someone'),
        rate=TonRate(kopeks=15259, source='coingecko'),
        wallet=WalletSnapshot(address=WALLET, balance_ton=5.0),
    )
    assert 'Маржа: <b>−5,00 ₽</b>' in text
    assert '⚠️ Хватит примерно на 508 ⭐ — <b>пора пополнить</b>' in text
    assert '(себе)' not in text


def test_dry_run_has_no_chain_links_and_escapes_html():
    text, markup = build_completed_message(
        _order(ton_tx_hash='dry-run-tx-1', cost_nanoton=0, cost_kopeks=None, recipient_name='<b>x</b>'),
        buyer=_buyer(username=None, first_name='<script>', email=None),
        rate=None,
        wallet=None,
        dry_run=True,
    )
    assert '(тестовый режим)' in text
    assert 'Себестоимость' not in text
    assert '&lt;script&gt;' in text and '<script>' not in text
    assert '&lt;b&gt;x&lt;/b&gt;' in text
    assert not any('tonscan' in url or 'tonviewer' in url for url in _urls(markup))


def test_no_cabinet_links_without_real_cabinet_url(monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://example.com/cabinet')
    _, markup = build_completed_message(_order(), buyer=_buyer(), rate=None, wallet=None)
    assert not any('example.com' in url for url in _urls(markup))
    assert build_order_keyboard(1) is None


# ── Предложение VPN после выдачи звёзд ─────────────────────────────────────


class _Texts:
    @staticmethod
    def t(_key: str, default: str) -> str:
        return default


def _sub(*, active: bool, trial: bool = False):
    from app.database.models import Subscription

    now = datetime.now(UTC)
    return Subscription(
        status='active' if active else 'expired',
        is_trial=trial,
        end_date=now + timedelta(days=5) if active else now - timedelta(days=5),
    )


@pytest.fixture
def _trial(monkeypatch):
    monkeypatch.setattr(settings, 'STARS_SHOP_VPN_OFFER_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3)
    monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')


def _callback(offer) -> str:
    return offer[1].inline_keyboard[0][0].callback_data


def test_vpn_offer_trial_for_newcomer(_trial):
    from app.services.stars_notifications import build_vpn_offer

    offer = build_vpn_offer(_buyer(subscriptions=[], has_had_paid_subscription=False), _Texts())
    assert _callback(offer) == 'menu_trial'
    assert '3 дн. бесплатно' in offer[0]


def test_vpn_offer_buy_after_trial_or_expired_paid(_trial):
    from app.services.stars_notifications import build_vpn_offer

    used_trial = _buyer(subscriptions=[_sub(active=False, trial=True)], has_had_paid_subscription=False)
    lapsed = _buyer(subscriptions=[_sub(active=False)], has_had_paid_subscription=True)
    assert _callback(build_vpn_offer(used_trial, _Texts())) == 'menu_buy'
    assert _callback(build_vpn_offer(lapsed, _Texts())) == 'menu_buy'


def test_no_vpn_offer_for_subscribers_or_when_disabled(_trial, monkeypatch):
    from app.services.stars_notifications import build_vpn_offer

    paid = _buyer(subscriptions=[_sub(active=True)], has_had_paid_subscription=True)
    on_trial = _buyer(subscriptions=[_sub(active=True, trial=True)], has_had_paid_subscription=False)
    assert build_vpn_offer(paid, _Texts()) is None
    assert build_vpn_offer(on_trial, _Texts()) is None

    monkeypatch.setattr(settings, 'STARS_SHOP_VPN_OFFER_ENABLED', False)
    assert build_vpn_offer(_buyer(subscriptions=[], has_had_paid_subscription=False), _Texts()) is None
