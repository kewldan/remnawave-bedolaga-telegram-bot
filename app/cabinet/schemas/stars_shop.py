"""Схемы API магазина звёзд (кабинет: клиент и админка)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class StarsShopConfigResponse(BaseModel):
    available: bool
    price_per_star_kopeks: int
    min_quantity: int
    max_quantity: int
    presets: list[int]
    balance_kopeks: int


class StarsQuoteResponse(BaseModel):
    quantity: int
    price_per_star_kopeks: int
    total_kopeks: int


class StarsPurchaseRequest(BaseModel):
    quantity: int = Field(..., ge=1)
    recipient_username: str = Field(..., min_length=1, max_length=64)
    expected_total_kopeks: int = Field(..., ge=0)
    idempotency_key: str = Field(..., min_length=8, max_length=100)


class StarsOrderResponse(BaseModel):
    id: int
    status: str
    quantity: int
    amount_kopeks: int
    recipient_username: str
    recipient_name: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    refunded_at: datetime | None = None


class StarsPurchaseResponse(BaseModel):
    order: StarsOrderResponse
    balance_kopeks: int
    is_replay: bool


class StarsOrdersListResponse(BaseModel):
    items: list[StarsOrderResponse]


# ── Админка ─────────────────────────────────────────────────────────────────


class AdminStarsOrderResponse(StarsOrderResponse):
    user_id: int | None = None
    user_display: str | None = None
    source: str
    attempts: int
    last_error: str | None = None
    fragment_req_id: str | None = None
    ton_tx_hash: str | None = None
    cost_nanoton: int | None = None
    next_attempt_at: datetime | None = None
    updated_at: datetime | None = None


class AdminStarsOrdersListResponse(BaseModel):
    items: list[AdminStarsOrderResponse]
    total: int
    limit: int
    offset: int


class AdminStarsStatsResponse(BaseModel):
    orders_total: int
    orders_completed: int
    stars_sold: int
    revenue_kopeks: int
    refunded_kopeks: int
    cost_nanoton: int
    margin_kopeks: int | None
    by_status: dict[str, int]
    needs_review: int


class AdminStarsStatusResponse(BaseModel):
    enabled: bool
    dry_run: bool
    fragment_configured: bool
    price_per_star_kopeks: int
    min_quantity: int
    max_quantity: int
    presets: list[int]
    ton_rate_kopeks: int


class AdminStarsWalletResponse(BaseModel):
    address: str
    state: str
    balance_ton: float
    fragment_price_ton_per_100: str | None = None


class AdminMarkCompletedRequest(BaseModel):
    ton_tx_hash: str | None = Field(default=None, max_length=128)


class AdminRefundRequest(BaseModel):
    reason: str = Field(default='Возврат администратором', max_length=500)
