"""Cabinet trial must survive a panel failure that rolled the session back."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.cabinet.routes.subscription_modules import purchase
from app.config import settings
from app.database.models import Subscription, Tariff, User
from app.services import checkout_squad_validation as preflight
from app.services.remnawave_retry_queue import remnawave_retry_queue
from app.services.subscription_service import SubscriptionService
from tests.database.test_multi_tariff_switch_postgres import switch_data


pytestmark = pytest.mark.postgres


async def test_cabinet_trial_survives_panel_rollback(postgres_database, monkeypatch):
    """Как на проде: почта уже закреплена за аккаунтом панели другого человека.

    create_remnawave_user ловит PanelAccountOwnedByAnotherUser, делает
    db.rollback() и возвращает None. Триал к этому моменту закоммичен, поэтому
    роут должен ответить подпиской и поставить панель в очередь повтора, а не
    упасть с MissingGreenlet на протухшем user.id.
    """
    async with switch_data(postgres_database, monkeypatch) as (db, _existing):
        user = User(id=4, telegram_id=4, language='ru', balance_kopeks=10000)
        db.add(user)
        trial_tariff = await db.get(Tariff, 3)
        trial_tariff.is_trial_available = True
        trial_tariff.allowed_squads = ['masked-squad']
        await db.commit()
        await db.refresh(user, ['subscriptions'])
        monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
        monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
        monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', False)
        monkeypatch.setattr(settings, 'TRIAL_DISABLED_FOR', 'none')
        monkeypatch.setattr(preflight, 'validate_trial_checkout_squads', MagicMock(side_effect=_noop))
        monkeypatch.setattr(SubscriptionService, 'is_configured', property(lambda self: True))

        async def failing_panel_push(self, session, subscription, **kwargs):
            await session.rollback()

        monkeypatch.setattr(SubscriptionService, 'create_remnawave_user', failing_panel_push)
        enqueue = MagicMock()
        monkeypatch.setattr(remnawave_retry_queue, 'enqueue', enqueue)

        response = await purchase.activate_trial(None, user, db)

        subscription = await db.scalar(select(Subscription).where(Subscription.user_id == 4))
        assert subscription is not None
        assert subscription.is_trial
        assert response.id == subscription.id
        enqueue.assert_called_once_with(subscription_id=subscription.id, user_id=4, action='create')


async def _noop(*args, **kwargs):
    return None
