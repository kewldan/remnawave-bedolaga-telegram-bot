# Vendored from fragment-api-py 12.1.0 (https://github.com/S1qwy/fragment-api-py),
# MIT License, (c) S1qwy. Modified for Bedolaga: see ../NOTICE.md.
"""
Pydantic v2 models for Fragment API responses.

All API methods return strongly-typed Pydantic model instances.
Backward-compatible with previous dataclass-based results via re-exports.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ConfigDict


class FragmentBaseModel(BaseModel):
    """Base model with shared config for all Fragment API models."""

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )


class TransactionResult(FragmentBaseModel):
    """Result of a TON transaction with confirmation details."""

    tx_hash: str
    boc: str | None = None
    seqno_before: int | None = None
    seqno_after: int | None = None
    balance_before: float | None = None
    balance_after: float | None = None
    confirmed: bool = False

    def __repr__(self) -> str:
        return (
            f"TransactionResult("
            f"tx='{self.tx_hash[:16]}...', "
            f"confirmed={self.confirmed}, "
            f"seqno={self.seqno_before}->{self.seqno_after}"
            f")"
        )


class WalletInfo(FragmentBaseModel):
    """Wallet state information with GRAM and USDT balances."""

    address: str
    state: str
    gram_balance: float
    usdt_balance: float

    @property
    def balance_ton(self) -> float:
        """Alias for gram_balance for backward compatibility."""
        return self.gram_balance

    @property
    def balance_usdt(self) -> float:
        """Alias for usdt_balance for consistency."""
        return self.usdt_balance

    def __repr__(self) -> str:
        return (
            f"WalletInfo("
            f"address='{self.address}', "
            f"state='{self.state}', "
            f"gram_balance={self.gram_balance}, "
            f"usdt_balance={self.usdt_balance}"
            f")"
        )


class RecipientInfo(FragmentBaseModel):
    """Resolved recipient from Fragment search."""

    recipient: str
    name: str
    photo_url: str | None = None
    myself: bool = False

    def __repr__(self) -> str:
        return (
            f"RecipientInfo("
            f"name='{self.name}', "
            f"recipient='{self.recipient[:24]}...', "
            f"myself={self.myself}"
            f")"
        )


class PurchaseResult(FragmentBaseModel):
    """Result of a successful purchase operation."""

    transaction_id: str
    type: str
    username: str
    amount: int
    payment_method: str = "gram"

    def __repr__(self) -> str:
        unit = "months" if self.type == "premium" else ("GRAM" if self.type in ("gram", "ton") else "stars")
        return (
            f"PurchaseResult("
            f"type='{self.type}', "
            f"username='{self.username}', "
            f"amount={self.amount} {unit}, "
            f"payment='{self.payment_method}', "
            f"tx='{self.transaction_id}'"
            f")"
        )


class StarsPrice(FragmentBaseModel):
    """Price for a specific stars amount."""

    stars: int
    gram_price: str
    usd_price: str

    @property
    def ton_price(self) -> str:
        """Alias for gram_price for backward compatibility."""
        return self.gram_price


class StarsPrices(FragmentBaseModel):
    """All available stars package prices."""

    packages: list[StarsPrice]
    gram_rate: float

    @property
    def ton_rate(self) -> float:
        """Alias for gram_rate for backward compatibility."""
        return self.gram_rate
