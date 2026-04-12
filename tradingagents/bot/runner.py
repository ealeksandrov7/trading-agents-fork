from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from langchain_core.messages import ToolMessage

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.execution import (
    ExchangeStateSnapshot,
    ExecutionMode,
    HyperliquidExecutionError,
    HyperliquidExecutor,
    OrderIntent,
    Position,
    RiskEngine,
    RiskEvaluationError,
    TradeAction,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.agents.utils.core_stock_tools import TOOL_ERROR_PREFIX

from .models import BotActionPlan, BotConfig, BotState
from .state import BotStateStore


class BotRunner:
    def __init__(
        self,
        config: dict,
        bot_config: BotConfig,
        executor: Optional[HyperliquidExecutor] = None,
        event_sink: Optional[Callable[[str], None]] = None,
    ):
        self.config = config.copy()
        self.bot_config = bot_config
        self.event_sink = event_sink
        self.config["analysis_timeframe"] = bot_config.timeframe
        self.config["decision_timeframe"] = bot_config.timeframe
        self.config["hyperliquid_testnet"] = bot_config.testnet
        self.store = BotStateStore(Path(self.config["bot_state_path"]))
        self.exchange = executor or HyperliquidExecutor(
            wallet_address=self.config.get("hyperliquid_wallet_address"),
            private_key=self.config.get("hyperliquid_private_key"),
            base_url=self.config.get("hyperliquid_base_url"),
            testnet=bot_config.testnet,
        )
        self.risk_engine = RiskEngine(
            bankroll=0.0,
            max_risk_per_trade_pct=self.config["max_risk_per_trade_pct"],
            max_leverage=self.config["max_leverage"],
            min_notional_usd=self.config["min_notional_usd"],
            allowed_symbols=tuple(self.config["allowed_symbols"]),
            single_position_mode=self.config["single_position_mode"],
            decision_timeframe=self.config["decision_timeframe"],
            max_entry_distance_pct=self._entry_distance_limit_for_timeframe(bot_config.timeframe),
        )

    def run_once(self) -> None:
        state, events = self.store.load()
        state.symbol = self.bot_config.symbol.replace("-USD", "")
        state.timeframe = self.bot_config.timeframe
        state.signal_interval_minutes = self.bot_config.signal_interval_minutes
        state.analysis_interval_minutes = self.bot_config.analysis_interval_minutes

        snapshot = self.exchange.get_exchange_state_snapshot(state.symbol)
        state.sync_from_exchange(snapshot)
        trading_balance = self._trading_balance(snapshot)
        risk_budget = trading_balance * self.config["max_risk_per_trade_pct"]
        self._emit(
            f"[bot] synced exchange state for {state.symbol} | wallet={snapshot.wallet_address} | "
            f"equity={snapshot.equity} | available_balance={snapshot.available_balance} | "
            f"spot_usdc={snapshot.spot_usdc_balance} | trading_balance={trading_balance} | "
            f"risk_budget={risk_budget:.2f} | "
            f"positions={len(snapshot.positions)} | orders={len(snapshot.open_orders)}"
        )
        events = self.store.append_event(
            state,
            events,
            event_type="sync",
            message="Synchronized exchange state.",
            payload=snapshot.model_dump(),
        )

        self._reconcile_local_order_state(state, snapshot, events)
        self.store.save(state, events)

        decision_timestamp = self._latest_analysis_bar()
        if decision_timestamp is None:
            next_analysis = self._next_analysis_bar()
            self._emit(
                f"[bot] reconciliation only | next full analysis at {next_analysis.strftime('%Y-%m-%d %H:%M UTC')}"
            )
            self.store.save(state, events)
            return
        if state.last_decision_timestamp == decision_timestamp:
            self._emit(f"[bot] analysis for {decision_timestamp} UTC already completed")
            return

        self._emit(f"[bot] running full analysis for {decision_timestamp} UTC")
        final_state = self._run_decision(state, snapshot, decision_timestamp)
        tool_errors = self._extract_tool_errors(final_state)
        if tool_errors:
            for tool_error in tool_errors:
                self._emit(f"[bot] tool failure: {tool_error}")
            events = self.store.append_event(
                state,
                events,
                event_type="analysis_degraded",
                message="Analysis completed with tool failures.",
                payload={"tool_errors": tool_errors},
            )
        else:
            self._emit("[bot] analysis completed cleanly")
        action = final_state.get("final_trade_action") or {}
        if not action:
            events = self.store.append_event(
                state,
                events,
                event_type="decision_error",
                message=final_state.get("final_trade_action_error", "Missing structured decision."),
            )
            self._emit(f"[bot] structured decision invalid: {final_state.get('final_trade_action_error', 'missing structured decision')}")
            state.last_decision_timestamp = decision_timestamp
            state.last_decision_action = {}
            self.store.save(state, events)
            return

        self._emit(
            f"[bot] decision {action.get('action')} {action.get('symbol')} "
            f"confidence={action.get('confidence')} instruction={action.get('position_instruction')}"
        )
        plan = self._build_action_plan(state, snapshot, action)
        events = self.store.append_event(
            state,
            events,
            event_type="plan",
            message=plan.reason,
            payload={"action": plan.action, "decision": action, "tool_errors": tool_errors},
        )

        if tool_errors and plan.action not in {"NO_ACTION", "CANCEL_PENDING", "CLOSE_POSITION"}:
            self._emit("[bot] live entry blocked due to tool failures in the analysis phase")
            events = self.store.append_event(
                state,
                events,
                event_type="entry_blocked_tool_failure",
                message="Live entry blocked because required analysis tools failed.",
                payload={"tool_errors": tool_errors, "decision": action},
            )
            state.last_decision_timestamp = decision_timestamp
            state.last_decision_action = action
            self.store.save(state, events)
            return

        if plan.action == "NO_ACTION":
            self._emit(f"[bot] no action: {plan.reason}")
            state.last_decision_timestamp = decision_timestamp
            state.last_decision_action = action
            self.store.save(state, events)
            return

        if plan.action == "CANCEL_PENDING":
            if state.active_order_id:
                self.exchange.cancel_order(state.symbol, state.active_order_id)
                self._emit(f"[bot] canceled pending order {state.active_order_id} for {state.symbol}")
            state.active_order_id = None
            state.active_order_intent = None
            state.active_order_created_at = None
            state.last_decision_timestamp = decision_timestamp
            state.last_decision_action = action
            self.store.save(state, events)
            return

        if plan.action == "CLOSE_POSITION":
            close_intent = self.risk_engine.build_order_intent(
                action,
                reference_price=snapshot.mark_prices[state.symbol],
                mode=ExecutionMode.LIVE,
                open_position=self._position_from_state(state),
            )
            preview = self.exchange.execute(close_intent)
            if preview.status == "rejected":
                self._emit(f"[bot] close rejected for {close_intent.symbol}: {preview.message}")
                events = self.store.append_event(
                    state,
                    events,
                    event_type="close_rejected",
                    message=preview.message,
                    payload=preview.model_dump(),
                )
                self.store.save(state, events)
                return
            self._emit(
                f"[bot] submitted close for {close_intent.symbol} size={close_intent.size:.6f} "
                f"at ref={close_intent.reference_price:.2f}"
            )
            state.active_order_intent = None
            state.active_order_id = preview.order_id
            state.active_order_created_at = datetime.now(timezone.utc).isoformat()
            events = self.store.append_event(
                state,
                events,
                event_type="close_submitted",
                message=preview.message,
                payload=preview.model_dump(),
            )
            state.last_decision_timestamp = decision_timestamp
            state.last_decision_action = action
            self.store.save(state, events)
            return

        intent = plan.order_intent
        if intent is None:
            state.last_decision_timestamp = decision_timestamp
            state.last_decision_action = action
            self.store.save(state, events)
            return

        preview = self.exchange.execute(intent)
        if preview.status == "rejected":
            self._emit(f"[bot] entry rejected for {intent.symbol}: {preview.message}")
            events = self.store.append_event(
                state,
                events,
                event_type="entry_rejected",
                message=preview.message,
                payload=preview.model_dump(),
            )
            state.last_decision_timestamp = decision_timestamp
            state.last_decision_action = action
            self.store.save(state, events)
            return
        self._emit(
            f"[bot] submitted {intent.action.value} {intent.symbol} size={intent.size:.6f} "
            f"entry_mode={intent.entry_mode.value} ref={intent.reference_price:.2f}"
        )
        state.active_order_intent = {
            **intent.model_dump(),
            "setup_expiry_bars": action.get("setup_expiry_bars")
            or self.bot_config.setup_expiry_bars_default,
        }
        state.active_order_id = preview.order_id
        state.active_order_created_at = datetime.now(timezone.utc).isoformat()
        events = self.store.append_event(
            state,
            events,
            event_type="entry_submitted",
            message=preview.message,
            payload=preview.model_dump(),
        )
        state.last_decision_timestamp = decision_timestamp
        state.last_decision_action = action
        self.store.save(state, events)

    def run_forever(self) -> None:
        self._emit(
            f"[bot] started | symbol={self.bot_config.symbol} | timeframe={self.bot_config.timeframe} | "
            f"analysis_every={self.bot_config.analysis_interval_minutes}m | reconcile_every={self.bot_config.reconcile_interval_seconds}s | "
            f"mode={'testnet' if self.bot_config.testnet else 'mainnet'} | analysts={','.join(self._bot_analysts())}"
        )
        while True:
            try:
                self.run_once()
            except Exception as exc:
                state, events = self.store.load()
                state.consecutive_failures += 1
                events = self.store.append_event(
                    state,
                    events,
                    event_type="error",
                    message=str(exc),
                )
                self.store.save(state, events)
                self._emit(f"[bot] error: {exc}")
            time.sleep(self.bot_config.reconcile_interval_seconds)

    def _run_decision(
        self,
        state: BotState,
        snapshot: ExchangeStateSnapshot,
        decision_timestamp: str,
    ) -> dict:
        graph = TradingAgentsGraph(
            self._bot_analysts(),
            debug=False,
            config=self.config,
        )
        init_state = graph.propagator.create_initial_state(self.bot_config.symbol, decision_timestamp)
        init_state["exchange_state_summary"] = self._exchange_summary(snapshot)
        init_state["bot_state_summary"] = self._bot_summary(state)
        graph_args = graph.propagator.get_graph_args()
        final_state = graph.graph.invoke(init_state, config=graph_args["config"])
        return final_state

    def _build_action_plan(
        self,
        state: BotState,
        snapshot: ExchangeStateSnapshot,
        action: dict,
    ) -> BotActionPlan:
        position_instruction = (action.get("position_instruction") or "").upper()
        active_order = state.active_order_id is not None
        active_position = self._position_from_state(state)
        if position_instruction in {"NO_ACTION", "HOLD"}:
            return BotActionPlan(action="NO_ACTION", reason="Decision explicitly keeps current state.")
        if position_instruction == "CANCEL_ENTRY":
            return BotActionPlan(action="CANCEL_PENDING", reason="Decision cancels pending entry.")
        if action["action"] == TradeAction.FLAT or action["action"] == "FLAT":
            if active_position:
                return BotActionPlan(action="CLOSE_POSITION", reason="Decision closes current position.")
            if active_order:
                return BotActionPlan(action="CANCEL_PENDING", reason="Decision stays flat and removes pending order.")
            return BotActionPlan(action="NO_ACTION", reason="Already flat with no pending orders.")
        if active_position:
            if active_position.side.value == action["action"]:
                return BotActionPlan(action="NO_ACTION", reason="Existing exchange position already matches the new directional thesis.")
            return BotActionPlan(action="CLOSE_POSITION", reason="Need to flatten before reversing.")
        if active_order and not state.setup_expired(
            as_of=datetime.now(timezone.utc),
            expiry_bars_default=self.bot_config.setup_expiry_bars_default,
        ):
            return BotActionPlan(action="KEEP_PENDING", reason="Existing pending entry remains fresh.")
        if active_order and state.active_order_id:
            self.exchange.cancel_order(state.symbol, state.active_order_id)
            state.active_order_id = None
            state.active_order_intent = None
            state.active_order_created_at = None
        reference_price = snapshot.mark_prices[state.symbol]
        self.risk_engine.bankroll = self._trading_balance(snapshot) or 1000.0
        normalized_action = {
            "timestamp": state.last_decision_timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            **action,
        }
        intent = self.risk_engine.build_order_intent(
            normalized_action,
            reference_price=reference_price,
            mode=ExecutionMode.LIVE,
            open_position=None,
        )
        return BotActionPlan(action="OPEN_ENTRY", reason="Submit new entry from latest hourly decision.", order_intent=intent)

    def _reconcile_local_order_state(
        self,
        state: BotState,
        snapshot: ExchangeStateSnapshot,
        events,
    ) -> None:
        matching_orders = [order for order in snapshot.open_orders if order.symbol.upper() == state.symbol.upper()]
        if not matching_orders:
            state.active_order_id = None
            state.active_order_intent = None
            state.active_order_created_at = None

    def _exchange_summary(self, snapshot: ExchangeStateSnapshot) -> str:
        position_bits = [
            f"{position.symbol} {position.side.value} size={position.size} entry={position.entry_price}"
            for position in snapshot.positions
        ] or ["no open positions"]
        order_bits = [
            f"{order.symbol} {order.side.value} oid={order.order_id} px={order.limit_price} reduce_only={order.reduce_only}"
            for order in snapshot.open_orders
        ] or ["no resting orders"]
        return (
            f"Wallet: {snapshot.wallet_address or 'unknown'} | Equity: {snapshot.equity} | "
            f"Available: {snapshot.available_balance} | Positions: {'; '.join(position_bits)} | "
            f"Orders: {'; '.join(order_bits)}"
        )

    def _bot_summary(self, state: BotState) -> str:
        return (
            f"Last decision timestamp: {state.last_decision_timestamp or 'none'} | "
            f"Signal interval: {state.signal_interval_minutes}m | "
            f"Analysis interval: {state.analysis_interval_minutes}m | "
            f"Active order id: {state.active_order_id or 'none'} | "
            f"Position: {state.current_position or 'none'} | "
            f"Failures: {state.consecutive_failures}"
        )

    def _latest_completed_signal_bar(self) -> datetime:
        now = datetime.now(timezone.utc)
        interval_minutes = self.bot_config.signal_interval_minutes
        total_minutes = now.hour * 60 + now.minute
        floored_minutes = (total_minutes // interval_minutes) * interval_minutes
        floored = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=floored_minutes)
        if floored >= now.replace(second=0, microsecond=0):
            floored -= timedelta(minutes=interval_minutes)
        return floored

    def _latest_analysis_bar(self) -> Optional[str]:
        completed_bar = self._latest_completed_signal_bar()
        minutes_since_midnight = completed_bar.hour * 60 + completed_bar.minute
        if minutes_since_midnight % self.bot_config.analysis_interval_minutes != 0:
            return None
        return completed_bar.strftime("%Y-%m-%d %H:%M")

    def _next_analysis_bar(self) -> datetime:
        completed_bar = self._latest_completed_signal_bar()
        step = timedelta(minutes=self.bot_config.signal_interval_minutes)
        candidate = completed_bar + step
        while (candidate.hour * 60 + candidate.minute) % self.bot_config.analysis_interval_minutes != 0:
            candidate += step
        return candidate

    def _bot_analysts(self) -> list[str]:
        timeframe = self.bot_config.timeframe.lower()
        if timeframe in {"1h", "4h"}:
            analysts = self.config.get("bot_default_intraday_analysts")
        else:
            analysts = self.config.get("bot_default_swing_analysts")
        return list(analysts or ["market"])

    def _entry_distance_limit_for_timeframe(self, timeframe: str) -> Optional[float]:
        limits = self.config.get("max_entry_distance_pct_by_timeframe", {})
        if not isinstance(limits, dict):
            return None
        value = limits.get(timeframe.lower())
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _emit(self, message: str) -> None:
        if self.event_sink:
            self.event_sink(message)

    def _position_from_state(self, state: BotState) -> Optional[Position]:
        if not state.current_position:
            return None
        return Position.model_validate(state.current_position)

    def _trading_balance(self, snapshot: ExchangeStateSnapshot) -> float:
        return float(
            snapshot.spot_usdc_balance
            or snapshot.available_balance
            or snapshot.equity
            or 0.0
        )

    def _extract_tool_errors(self, final_state: dict) -> list[str]:
        messages = final_state.get("messages") or []
        errors: list[str] = []
        for message in messages:
            if not isinstance(message, ToolMessage):
                continue
            content = message.content
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [str(item) for item in content]
            else:
                texts = [str(content)]
            for text in texts:
                if TOOL_ERROR_PREFIX in text:
                    for line in text.splitlines():
                        if TOOL_ERROR_PREFIX in line:
                            errors.append(line.strip())
        deduped: list[str] = []
        seen = set()
        for error in errors:
            if error not in seen:
                seen.add(error)
                deduped.append(error)
        return deduped


def build_bot_runtime_config() -> dict:
    return DEFAULT_CONFIG.copy()
