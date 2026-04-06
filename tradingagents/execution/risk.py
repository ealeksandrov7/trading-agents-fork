from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import (
    EntryMode,
    ExecutionMode,
    OrderIntent,
    Position,
    StructuredTradeDecision,
    TradeAction,
    canonical_symbol,
)


class RiskEvaluationError(ValueError):
    """Raised when a structured decision is not tradable."""


@dataclass
class RiskEngine:
    bankroll: float
    max_risk_per_trade_pct: float
    max_leverage: int
    allowed_symbols: tuple[str, ...] = ("BTC", "ETH")
    single_position_mode: bool = True
    decision_timeframe: str = "4h"

    def build_order_intent(
        self,
        decision: StructuredTradeDecision | dict,
        *,
        reference_price: float,
        mode: ExecutionMode,
        open_position: Optional[Position] = None,
    ) -> OrderIntent:
        if isinstance(decision, dict):
            decision = StructuredTradeDecision.model_validate(decision)
        symbol = canonical_symbol(decision.symbol)
        if symbol not in self.allowed_symbols:
            raise RiskEvaluationError(f"symbol {symbol} is not allowed")
        if reference_price <= 0:
            raise RiskEvaluationError("reference price must be positive")
        if self.bankroll <= 0:
            raise RiskEvaluationError("bankroll must be positive")
        if decision.time_horizon.strip().lower() != self.decision_timeframe.lower():
            raise RiskEvaluationError(
                f"decision time horizon must be {self.decision_timeframe}"
            )

        if decision.action == TradeAction.FLAT:
            return self._build_flat_intent(decision, reference_price, mode, open_position)

        if self.single_position_mode and open_position and open_position.symbol != symbol:
            raise RiskEvaluationError(
                f"single-position mode blocks a new {symbol} trade while {open_position.symbol} is open"
            )

        stop_loss = decision.stop_loss
        take_profit = decision.take_profit
        if stop_loss is None or take_profit is None:
            raise RiskEvaluationError("directional trades require stop loss and take profit")

        is_long = decision.action == TradeAction.LONG
        entry_reference_price = self._resolve_entry_reference_price(decision, reference_price)

        if is_long:
            if stop_loss >= entry_reference_price:
                raise RiskEvaluationError("long trade stop loss must be below entry price")
            if take_profit <= entry_reference_price:
                raise RiskEvaluationError("long trade take profit must be above entry price")
            stop_distance = entry_reference_price - stop_loss
        else:
            if stop_loss <= entry_reference_price:
                raise RiskEvaluationError("short trade stop loss must be above entry price")
            if take_profit >= entry_reference_price:
                raise RiskEvaluationError("short trade take profit must be below entry price")
            stop_distance = stop_loss - entry_reference_price

        if stop_distance <= 0:
            raise RiskEvaluationError("stop distance must be positive")

        risk_budget = self.bankroll * self.max_risk_per_trade_pct
        size = risk_budget / stop_distance
        notional = size * entry_reference_price
        implied_leverage = max(1, int(-(-notional // self.bankroll)))
        if implied_leverage > self.max_leverage:
            raise RiskEvaluationError(
                f"trade would require leverage {implied_leverage}x above configured cap {self.max_leverage}x"
            )

        return OrderIntent(
            mode=mode,
            symbol=symbol,
            action=decision.action,
            size=round(size, 6),
            reference_price=reference_price,
            entry_mode=decision.entry_mode,
            limit_price=decision.entry_price,
            limit_zone_low=decision.entry_zone_low,
            limit_zone_high=decision.entry_zone_high,
            leverage=implied_leverage,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=decision.confidence,
            thesis_summary=decision.thesis_summary,
            time_horizon=decision.time_horizon,
            invalidation=decision.invalidation,
            decision_timestamp=decision.timestamp,
            rationale="Accepted by deterministic risk engine.",
            reduce_only=False,
        )

    def _build_flat_intent(
        self,
        decision: StructuredTradeDecision,
        reference_price: float,
        mode: ExecutionMode,
        open_position: Optional[Position],
    ) -> OrderIntent:
        if not open_position:
            raise RiskEvaluationError("received FLAT but there is no open position to close")
        if open_position.symbol != decision.symbol:
            raise RiskEvaluationError(
                f"received FLAT for {decision.symbol} but open position is {open_position.symbol}"
            )

        return OrderIntent(
            mode=mode,
            symbol=decision.symbol,
            action=TradeAction.FLAT,
            size=open_position.size,
            reference_price=reference_price,
            entry_mode=EntryMode.MARKET,
            leverage=1,
            stop_loss=None,
            take_profit=None,
            confidence=decision.confidence,
            thesis_summary=decision.thesis_summary,
            time_horizon=decision.time_horizon,
            invalidation=decision.invalidation,
            decision_timestamp=decision.timestamp,
            rationale="Closing the current position.",
            reduce_only=True,
        )

    def _resolve_entry_reference_price(
        self,
        decision: StructuredTradeDecision,
        market_price: float,
    ) -> float:
        if decision.entry_mode == EntryMode.MARKET:
            return market_price
        if decision.entry_mode == EntryMode.LIMIT:
            assert decision.entry_price is not None
            return decision.entry_price
        if decision.entry_mode == EntryMode.LIMIT_ZONE:
            assert decision.entry_zone_low is not None
            assert decision.entry_zone_high is not None
            return (decision.entry_zone_low + decision.entry_zone_high) / 2
        return market_price
