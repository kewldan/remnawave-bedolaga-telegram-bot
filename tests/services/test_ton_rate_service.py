"""Курс TON: ручной курс важнее автоматического, запасной источник и кеш."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import ton_rate_service as rates


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    rates.reset_cache()
    monkeypatch.setattr(settings, 'STARS_SHOP_TON_RATE_KOPEKS', 0)
    yield
    rates.reset_cache()


@pytest.mark.asyncio
async def test_manual_rate_wins_without_network(monkeypatch):
    monkeypatch.setattr(settings, 'STARS_SHOP_TON_RATE_KOPEKS', 15000)
    tonapi = AsyncMock()
    monkeypatch.setattr(rates, '_fetch_tonapi', tonapi)
    rate = await rates.get_ton_rate()
    assert (rate.kopeks, rate.source) == (15000, 'manual')
    tonapi.assert_not_awaited()


@pytest.mark.asyncio
async def test_tonapi_then_cache(monkeypatch):
    tonapi = AsyncMock(return_value=12774)
    monkeypatch.setattr(rates, '_fetch_tonapi', tonapi)
    first = await rates.get_ton_rate()
    second = await rates.get_ton_rate()
    assert (first.kopeks, first.source) == (12774, 'tonapi')
    assert second is first
    assert tonapi.await_count == 1


@pytest.mark.asyncio
async def test_falls_back_to_coingecko(monkeypatch):
    monkeypatch.setattr(rates, '_fetch_tonapi', AsyncMock(side_effect=RuntimeError('503')))
    monkeypatch.setattr(rates, '_fetch_coingecko', AsyncMock(return_value=12722))
    rate = await rates.get_ton_rate()
    assert (rate.kopeks, rate.source) == (12722, 'coingecko')


@pytest.mark.asyncio
async def test_stale_rate_survives_outage_then_expires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(rates.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(rates, '_fetch_tonapi', AsyncMock(return_value=12000))
    assert (await rates.get_ton_rate()).kopeks == 12000

    down = AsyncMock(side_effect=RuntimeError('down'))
    monkeypatch.setattr(rates, '_fetch_tonapi', down)
    monkeypatch.setattr(rates, '_fetch_coingecko', down)
    clock[0] += 600  # кеш истёк, источники лежат — отдаём последний курс
    assert (await rates.get_ton_rate()).kopeks == 12000
    clock[0] += 3600  # слишком старый — лучше без курса, чем с неверным
    assert await rates.get_ton_rate() is None


def test_rub_parsing_and_conversion():
    assert rates._rub_to_kopeks(127.744) == 12774
    assert rates._rub_to_kopeks('n/a') is None
    assert rates._rub_to_kopeks(0) is None
    assert rates.TonRate(kopeks=12000, source='x').nanoton_to_kopeks(491_500_000) == 5898
