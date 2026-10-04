"""Курс TON в рублях для расчёта себестоимости и маржи магазина звёзд.

``STARS_SHOP_TON_RATE_KOPEKS`` > 0 — ручной курс. Иначе курс берётся с tonapi.io
(без ключа), запасной источник — CoinGecko. Значение кешируется на 5 минут; если оба
источника недоступны, до часа отдаётся последний полученный курс.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

_TONAPI_URL = 'https://tonapi.io/v2/rates?tokens=ton&currencies=rub'
_COINGECKO_URL = 'https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=rub'
_CACHE_TTL_SECONDS = 300
_STALE_TTL_SECONDS = 3600
_TIMEOUT = aiohttp.ClientTimeout(total=5)


@dataclass(frozen=True, slots=True)
class TonRate:
    kopeks: int
    """Цена 1 TON в копейках."""
    source: str
    """manual, tonapi или coingecko."""

    def nanoton_to_kopeks(self, nanoton: int) -> int:
        return nanoton * self.kopeks // 1_000_000_000


_cached: TonRate | None = None
_cached_at = 0.0


def _rub_to_kopeks(value: object) -> int | None:
    try:
        kopeks = round(float(value) * 100)
    except (TypeError, ValueError):
        return None
    return kopeks if kopeks > 0 else None


async def _fetch_tonapi(session: aiohttp.ClientSession) -> int | None:
    async with session.get(_TONAPI_URL) as response:
        response.raise_for_status()
        data = await response.json()
    return _rub_to_kopeks(data['rates']['TON']['prices']['RUB'])


async def _fetch_coingecko(session: aiohttp.ClientSession) -> int | None:
    async with session.get(_COINGECKO_URL) as response:
        response.raise_for_status()
        data = await response.json()
    return _rub_to_kopeks(data['the-open-network']['rub'])


async def get_ton_rate() -> TonRate | None:
    """Текущий курс TON или ``None``, если его негде взять."""
    global _cached, _cached_at

    manual = int(settings.STARS_SHOP_TON_RATE_KOPEKS or 0)
    if manual > 0:
        return TonRate(kopeks=manual, source='manual')

    now = time.monotonic()
    if _cached is not None and now - _cached_at < _CACHE_TTL_SECONDS:
        return _cached

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        for source, fetch in (('tonapi', _fetch_tonapi), ('coingecko', _fetch_coingecko)):
            try:
                kopeks = await fetch(session)
            except Exception as exc:
                logger.warning('Курс TON: источник недоступен', source=source, error=str(exc))
                continue
            if kopeks:
                _cached, _cached_at = TonRate(kopeks=kopeks, source=source), now
                return _cached

    if _cached is not None and now - _cached_at < _STALE_TTL_SECONDS:
        return _cached
    return None


def reset_cache() -> None:
    global _cached, _cached_at
    _cached, _cached_at = None, 0.0
