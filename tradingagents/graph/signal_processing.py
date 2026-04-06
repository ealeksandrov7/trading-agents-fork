# TradingAgents/graph/signal_processing.py

from typing import Optional

from tradingagents.execution import DecisionParseError, DecisionParser


class SignalProcessor:
    """Processes trading signals into validated machine-readable actions."""

    def __init__(self, quick_thinking_llm=None):
        self.quick_thinking_llm = quick_thinking_llm

    def process_signal(
        self,
        full_signal: str,
        *,
        symbol: Optional[str] = None,
        trade_date: Optional[str] = None,
    ) -> dict:
        try:
            return DecisionParser.parse(
                full_signal,
                fallback_symbol=symbol,
                fallback_timestamp=trade_date,
            ).model_dump()
        except DecisionParseError:
            return {}
