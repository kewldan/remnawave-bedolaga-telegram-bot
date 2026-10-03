"""Покупка звёзд Telegram через Fragment (см. NOTICE.md)."""

from app.external.fragment.client import (
    STARS_MAX,
    STARS_MIN,
    FragmentBroadcastUncertainError,
    FragmentConfigurationError,
    FragmentRecipientNotFoundError,
    FragmentRetryableError,
    FragmentStarsClient,
    FragmentStarsError,
    StarsPurchaseReceipt,
    StarsQuote,
)


__all__ = [
    'STARS_MAX',
    'STARS_MIN',
    'FragmentBroadcastUncertainError',
    'FragmentConfigurationError',
    'FragmentRecipientNotFoundError',
    'FragmentRetryableError',
    'FragmentStarsClient',
    'FragmentStarsError',
    'StarsPurchaseReceipt',
    'StarsQuote',
]
