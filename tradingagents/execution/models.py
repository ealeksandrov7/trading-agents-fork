from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


def canonical_symbol(value: str) -> str:
    symbol = value.strip().upper()
    for suffix in ("-USD", "/USD", "USDT", "-PERP"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break
    return symbol


class TradeAction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class ExecutionMode(str, Enum):
    ANALYSIS = "analysis"
    PAPER = "paper"
    LIVE = "live"


class EntryMode(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    LIMIT_ZONE = "LIMIT_ZONE"


class StructuredTradeDecision(BaseModel):
    symbol: str
    timestamp: str
    action: TradeAction
    entry_mode: EntryMode = EntryMode.MARKET
    entry_price: Optional[float] = Field(default=None, gt=0.0)
    entry_zone_low: Optional[float] = Field(default=None, gt=0.0)
    entry_zone_high: Optional[float] = Field(default=None, gt=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    thesis_summary: str
    time_horizon: str
    stop_loss: Optional[float] = Field(default=None, gt=0.0)
    take_profit: Optional[float] = Field(default=None, gt=0.0)
    invalidation: str
    size_hint: Optional[str] = None
    setup_expiry_bars: Optional[int] = Field(default=None, ge=1)
    position_instruction: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        symbol = canonical_symbol(value)
        if not symbol:
            raise ValueError("symbol must not be empty")
        return symbol

    @field_validator("thesis_summary", "time_horizon", "invalidation")
    @classmethod
    def ensure_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("text field must not be empty")
        return text

    @field_validator("size_hint")
    @classmethod
    def normalize_size_hint(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @model_validator(mode="after")
    def validate_action_fields(self) -> "StructuredTradeDecision":
        if self.action == TradeAction.FLAT:
            return self

        if self.entry_mode == EntryMode.MARKET:
            pass
        elif self.entry_mode == EntryMode.LIMIT:
            if self.entry_price is None:
                raise ValueError("entry_price is required for LIMIT entries")
        elif self.entry_mode == EntryMode.LIMIT_ZONE:
            if self.entry_zone_low is None or self.entry_zone_high is None:
                raise ValueError("entry_zone_low and entry_zone_high are required for LIMIT_ZONE entries")
            if self.entry_zone_low >= self.entry_zone_high:
                raise ValueError("entry_zone_low must be less than entry_zone_high")

        if self.stop_loss is None:
            raise ValueError("stop_loss is required for directional trades")
        if self.take_profit is None:
            raise ValueError("take_profit is required for directional trades")
        return self


class Position(BaseModel):
    symbol: str
    side: TradeAction
    size: float = Field(gt=0.0)
    entry_price: float = Field(gt=0.0)
    stop_loss: Optional[float] = Field(default=None, gt=0.0)
    take_profit: Optional[float] = Field(default=None, gt=0.0)
    opened_at: str
    mode: ExecutionMode

    @field_validator("symbol")
    @classmethod
    def normalize_position_symbol(cls, value: str) -> str:
        return canonical_symbol(value)


class OrderIntent(BaseModel):
    mode: ExecutionMode
    symbol: str
    action: TradeAction
    size: float = Field(ge=0.0)
    reference_price: float = Field(gt=0.0)
    entry_mode: EntryMode = EntryMode.MARKET
    limit_price: Optional[float] = Field(default=None, gt=0.0)
    limit_zone_low: Optional[float] = Field(default=None, gt=0.0)
    limit_zone_high: Optional[float] = Field(default=None, gt=0.0)
    leverage: int = Field(ge=1)
    stop_loss: Optional[float] = Field(default=None, gt=0.0)
    take_profit: Optional[float] = Field(default=None, gt=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    thesis_summary: str
    time_horizon: str
    invalidation: str
    decision_timestamp: str
    rationale: str
    reduce_only: bool = False

    @field_validator("symbol")
    @classmethod
    def normalize_order_symbol(cls, value: str) -> str:
        return canonical_symbol(value)


class OrderStatus(str, Enum):
    PREVIEW = "preview"
    FILLED = "filled"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    ERROR = "error"


class OrderPreview(BaseModel):
    status: OrderStatus
    mode: ExecutionMode
    symbol: str
    action: TradeAction
    message: str
    reference_price: Optional[float] = None
    size: Optional[float] = None
    leverage: Optional[int] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    order_id: Optional[str] = None
    raw_response: Optional[dict] = None


class ExchangeOrder(BaseModel):
    symbol: str
    order_id: str
    side: TradeAction
    size: float = Field(ge=0.0)
    limit_price: Optional[float] = Field(default=None, gt=0.0)
    reduce_only: bool = False
    status: str = "open"

    @field_validator("symbol")
    @classmethod
    def normalize_exchange_order_symbol(cls, value: str) -> str:
        return canonical_symbol(value)


class ExchangeStateSnapshot(BaseModel):
    wallet_address: Optional[str] = None
    equity: Optional[float] = None
    available_balance: Optional[float] = None
    spot_usdc_balance: Optional[float] = None
    mark_prices: dict[str, float] = Field(default_factory=dict)
    positions: list[Position] = Field(default_factory=list)
    open_orders: list[ExchangeOrder] = Field(default_factory=list)
    fetched_at: str
