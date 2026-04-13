from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from tradingagents.execution import ExchangeStateSnapshot, OrderIntent, Position


class BotConfig(BaseModel):
    symbol: str = "BTC-USD"
    timeframe: str = "1h"
    decision_mode: Literal["llm", "deterministic"] = "llm"
    analysis_interval_minutes: int = 240
    reconcile_interval_seconds: int = 60
    setup_expiry_bars_default: int = 3
    testnet: bool = True
    once: bool = False

    @property
    def signal_interval_minutes(self) -> int:
        timeframe = self.timeframe.lower()
        if timeframe == "1h":
            return 60
        if timeframe == "4h":
            return 240
        return 24 * 60


class BotEvent(BaseModel):
    timestamp: str
    event_type: str
    message: str
    payload: dict = Field(default_factory=dict)


class BotActionPlan(BaseModel):
    action: Literal[
        "OPEN_ENTRY",
        "KEEP_PENDING",
        "CANCEL_PENDING",
        "CLOSE_POSITION",
        "NO_ACTION",
    ]
    reason: str
    order_intent: Optional[OrderIntent] = None


class BotState(BaseModel):
    symbol: str = "BTC"
    timeframe: str = "1h"
    signal_interval_minutes: int = 60
    analysis_interval_minutes: int = 240
    last_decision_timestamp: Optional[str] = None
    last_decision_action: Optional[dict] = None
    active_order_intent: Optional[dict] = None
    active_order_id: Optional[str] = None
    active_order_created_at: Optional[str] = None
    current_position: Optional[dict] = None
    last_exchange_snapshot: Optional[dict] = None
    last_sync_at: Optional[str] = None
    cooldown_until: Optional[str] = None
    kill_switch_enabled: bool = False
    consecutive_failures: int = 0
    regime_snapshot: Optional[dict] = None
    higher_timeframe_snapshot: Optional[dict] = None
    candidate_snapshot: Optional[dict] = None
    last_decision_diagnostics: Optional[dict] = None

    def sync_from_exchange(self, snapshot: ExchangeStateSnapshot) -> None:
        self.last_exchange_snapshot = snapshot.model_dump()
        self.last_sync_at = snapshot.fetched_at
        matching_position = None
        for position in snapshot.positions:
            if position.symbol.upper() == self.symbol.upper():
                matching_position = position.model_dump()
                break
        self.current_position = matching_position
        matching_order = None
        for order in snapshot.open_orders:
            if order.symbol.upper() == self.symbol.upper():
                matching_order = order
                break
        if matching_order is None:
            self.active_order_id = None
        elif self.active_order_id is None:
            self.active_order_id = matching_order.order_id

    def setup_expired(self, *, as_of: datetime, expiry_bars_default: int) -> bool:
        if not self.active_order_intent or not self.active_order_created_at:
            return False
        created_at = datetime.fromisoformat(self.active_order_created_at)
        expires_after = self.active_order_intent.get("setup_expiry_bars") or expiry_bars_default
        expiry = created_at + timedelta(minutes=self.signal_interval_minutes * expires_after)
        return as_of.astimezone(timezone.utc) >= expiry.astimezone(timezone.utc)
