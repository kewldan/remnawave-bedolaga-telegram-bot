"""Gift claim must fail cleanly when the panel push rolled the session back."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database.models import Base, GuestPurchase, Subscription, Tariff, User
from app.services import guest_purchase_service as guest
from app.services.gift_claim_service import claim_gift_for_user
from app.services.subscription_service import SubscriptionService
from app.utils.cache import cache
from tests.fixtures.postgres_db import postgres_session


pytestmark = pytest.mark.postgres

TOKEN = 'g' * 64


@pytest.mark.parametrize('recipient', ['new_email', 'active_subscriber'])
async def test_gift_claim_panel_rollback_is_clean_error(postgres_database, monkeypatch, recipient):
    """Как на проде 09.11: почта уже закреплена за аккаунтом панели другого человека.

    create_remnawave_user ловит PanelAccountOwnedByAnotherUser, делает
    db.rollback() и возвращает None. Раньше activate_purchase после этого падал
    на db.refresh() отвязанной подписки и HTTP 500 уходил наружу необработанным.
    Теперь — GuestPurchaseError, подарок остаётся неполученным и не привязанным,
    созданный под получение аккаунт откатывается, а действующая подписка
    получателя не помечается продлённой.
    """
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(cache, 'get', AsyncMock(return_value=None))
    for name in ['send_guest_notification', '_send_admin_notification', '_create_nalogo_receipt_for_purchase']:
        monkeypatch.setattr(guest, name, AsyncMock())
    monkeypatch.setattr(SubscriptionService, 'is_configured', property(lambda self: True))

    async def failing_panel_push(self, session, subscription, **kwargs):
        await session.rollback()

    monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', failing_panel_push)

    now = datetime.now(UTC)
    end_date = now + timedelta(days=5)
    async with postgres_session(postgres_database, list(Base.metadata.sorted_tables)) as db:
        db.add(
            Tariff(
                id=1,
                name='Test',
                is_active=True,
                traffic_limit_gb=0,
                device_limit=10,
                period_prices={'30': 22900},
                allowed_squads=['test-squad'],
            )
        )
        db.add(User(id=10, telegram_id=1010, auth_type='telegram', language='ru', status='active'))
        if recipient == 'active_subscriber':
            db.add(
                User(
                    id=20,
                    email='friend@example.com',
                    auth_type='email',
                    password_hash='test-only-hash',
                    email_verified=True,
                    language='ru',
                    status='active',
                )
            )
        await db.commit()
        if recipient == 'active_subscriber':
            db.add(
                Subscription(
                    id=7,
                    user_id=20,
                    tariff_id=1,
                    is_trial=False,
                    status='active',
                    start_date=now - timedelta(days=25),
                    end_date=end_date,
                    traffic_limit_gb=0,
                    device_limit=10,
                    connected_squads=['test-squad'],
                )
            )
        db.add(
            GuestPurchase(
                token=TOKEN,
                tariff_id=1,
                period_days=30,
                amount_kopeks=22900,
                contact_type='email',
                contact_value='buyer@example.com',
                is_gift=True,
                source='cabinet',
                buyer_user_id=10,
                payment_method='yookassa_card',
                status='paid',
                paid_at=now,
            )
        )
        await db.commit()

        # Как роут claim_gift: аккаунт под почту заводится в той же транзакции.
        purchase = await db.scalar(select(GuestPurchase).where(GuestPurchase.token == TOKEN))
        user, _ = await guest._find_or_create_user(db, 'email', 'friend@example.com', purchase=purchase, tariff_id=1)
        claimant_id = user.id

        with pytest.raises(guest.GuestPurchaseError) as exc_info:
            await claim_gift_for_user(db, claimant_user_id=claimant_id, claim_input=TOKEN)
        assert exc_info.value.message == 'Activation failed, please try again'

        await db.rollback()
        db.expunge_all()
        purchase = await db.scalar(select(GuestPurchase).where(GuestPurchase.token == TOKEN))
        assert purchase.status == 'paid'
        assert purchase.user_id is None
        assert purchase.delivered_at is None

        emails = (await db.scalars(select(User.email).where(User.email.is_not(None)))).all()
        subscriptions = (await db.scalars(select(Subscription))).all()
        if recipient == 'new_email':
            assert emails == []
            assert subscriptions == []
        else:
            assert emails == ['friend@example.com']
            assert [s.id for s in subscriptions] == [7]
            assert subscriptions[0].end_date == end_date
