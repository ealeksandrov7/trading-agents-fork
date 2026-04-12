from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from langchain_core.messages import ToolMessage

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.dataflows.stockstats_utils import load_ohlcv
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
from .candidate import CandidateSnapshot, detect_candidate
from .journal import BotJournal
from .regime import (
    RegimeSnapshot,
    allowed_strategies_for_regime,
    classify_regime,
    classify_regime_from_data,
)
from .replay import evaluate_replay_observation, summarize_replay
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
        self.journal = BotJournal(Path(self.config["bot_journal_path"]))
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
        analysis = self._analyze_decision(state, snapshot, decision_timestamp)
        final_state = analysis["final_state"]
        tool_errors = analysis["tool_errors"]
        action = analysis["action"]
        diagnostics = analysis["diagnostics"]
        analysis_timestamp = datetime.now(timezone.utc).isoformat()
        state.regime_snapshot = diagnostics["regime"]
        state.candidate_snapshot = diagnostics["candidate"]
        state.last_decision_diagnostics = diagnostics
        events = self.store.append_event(
            state,
            events,
            event_type="regime",
            message=diagnostics["regime"]["reason"],
            payload=diagnostics["regime"],
        )
        events = self.store.append_event(
            state,
            events,
            event_type="candidate",
            message=diagnostics["candidate"]["reason"],
            payload=diagnostics["candidate"],
        )
        if diagnostics["quality_filter_reasons"]:
            events = self.store.append_event(
                state,
                events,
                event_type="decision_rejected",
                message="Structured decision rejected by deterministic quality filters.",
                payload={
                    "reasons": diagnostics["quality_filter_reasons"],
                    "raw_action": diagnostics["raw_action"],
                    "final_action": diagnostics["final_action"],
                },
            )
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
            self._journal_cycle(
                state,
                snapshot,
                decision_timestamp,
                analysis_timestamp,
                diagnostics,
                {},
                None,
                tool_errors,
                "decision_error",
                final_state.get("final_trade_action_error", "missing structured decision"),
            )
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
            self._journal_cycle(
                state,
                snapshot,
                decision_timestamp,
                analysis_timestamp,
                diagnostics,
                action,
                plan,
                tool_errors,
                "entry_blocked_tool_failure",
                "Live entry blocked because required analysis tools failed.",
            )
            self.store.save(state, events)
            return

        if plan.action == "NO_ACTION":
            self._emit(f"[bot] no action: {plan.reason}")
            state.last_decision_timestamp = decision_timestamp
            state.last_decision_action = action
            self._journal_cycle(
                state,
                snapshot,
                decision_timestamp,
                analysis_timestamp,
                diagnostics,
                action,
                plan,
                tool_errors,
                "no_action",
                plan.reason,
            )
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
            self._journal_cycle(
                state,
                snapshot,
                decision_timestamp,
                analysis_timestamp,
                diagnostics,
                action,
                plan,
                tool_errors,
                "cancel_pending",
                plan.reason,
            )
            self.store.save(state, events)
            return

        if plan.action == "CLOSE_POSITION":
            close_intent = self.risk_engine.build_order_intent(
                action,
                reference_price=snapshot.mark_prices[state.symbol],
                mode=ExecutionMode.LIVE,
                open_position=self._position_from_state(state),
            )
            try:
                preview = self.exchange.execute(close_intent)
            except Exception as exc:
                self._journal_cycle(
                    state,
                    snapshot,
                    decision_timestamp,
                    analysis_timestamp,
                    diagnostics,
                    action,
                    plan,
                    tool_errors,
                    "close_error",
                    str(exc),
                    order_intent=close_intent.model_dump(),
                )
                raise
            if preview.status == "rejected":
                self._emit(f"[bot] close rejected for {close_intent.symbol}: {preview.message}")
                events = self.store.append_event(
                    state,
                    events,
                    event_type="close_rejected",
                    message=preview.message,
                    payload=preview.model_dump(),
                )
                self._journal_cycle(
                    state,
                    snapshot,
                    decision_timestamp,
                    analysis_timestamp,
                    diagnostics,
                    action,
                    plan,
                    tool_errors,
                    "close_rejected",
                    preview.message,
                    order_intent=close_intent.model_dump(),
                    order_preview=preview.model_dump(),
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
            self._journal_cycle(
                state,
                snapshot,
                decision_timestamp,
                analysis_timestamp,
                diagnostics,
                action,
                plan,
                tool_errors,
                "close_submitted",
                preview.message,
                order_intent=close_intent.model_dump(),
                order_preview=preview.model_dump(),
            )
            self.store.save(state, events)
            return

        intent = plan.order_intent
        if intent is None:
            state.last_decision_timestamp = decision_timestamp
            state.last_decision_action = action
            self._journal_cycle(
                state,
                snapshot,
                decision_timestamp,
                analysis_timestamp,
                diagnostics,
                action,
                plan,
                tool_errors,
                "missing_intent",
                "Plan returned no executable order intent.",
            )
            self.store.save(state, events)
            return

        try:
            preview = self.exchange.execute(intent)
        except Exception as exc:
            self._journal_cycle(
                state,
                snapshot,
                decision_timestamp,
                analysis_timestamp,
                diagnostics,
                action,
                plan,
                tool_errors,
                "entry_error",
                str(exc),
                order_intent=intent.model_dump(),
            )
            raise
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
            self._journal_cycle(
                state,
                snapshot,
                decision_timestamp,
                analysis_timestamp,
                diagnostics,
                action,
                plan,
                tool_errors,
                "entry_rejected",
                preview.message,
                order_intent=intent.model_dump(),
                order_preview=preview.model_dump(),
            )
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
        self._journal_cycle(
            state,
            snapshot,
            decision_timestamp,
            analysis_timestamp,
            diagnostics,
            action,
            plan,
            tool_errors,
            "entry_submitted",
            preview.message,
            order_intent=intent.model_dump(),
            order_preview=preview.model_dump(),
        )
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
        regime_snapshot = state.regime_snapshot or {}
        init_state["regime_summary"] = regime_snapshot.get("summary", "")
        init_state["regime_context"] = regime_snapshot
        candidate_snapshot = state.candidate_snapshot or {}
        init_state["candidate_summary"] = candidate_snapshot.get("summary", "")
        init_state["candidate_context"] = candidate_snapshot
        init_state["allowed_setup_families"] = regime_snapshot.get("allowed_setup_families", [])
        init_state["setup_family"] = candidate_snapshot.get("setup_family") or regime_snapshot.get(
            "setup_family",
            self.config.get("bot_strategy_setup_family", "trend_pullback"),
        )
        graph_args = graph.propagator.get_graph_args()
        final_state = graph.graph.invoke(init_state, config=graph_args["config"])
        return final_state

    def _analyze_decision(
        self,
        state: BotState,
        snapshot: ExchangeStateSnapshot,
        decision_timestamp: str,
        replay_bars: Optional[pd.DataFrame] = None,
        strategy_family: Optional[str] = None,
    ) -> dict[str, Any]:
        if replay_bars is None:
            regime = self._classify_regime(state.symbol, decision_timestamp)
        else:
            try:
                regime = self._classify_regime(
                    state.symbol,
                    decision_timestamp,
                    replay_bars=replay_bars,
                )
            except TypeError:
                regime = self._classify_regime(state.symbol, decision_timestamp)
        state.regime_snapshot = {**regime.to_dict(), "summary": regime.summary()}
        selected_strategy = strategy_family or self._select_strategy_for_regime(regime)
        candidate = self._candidate_with_fallback(
            state.symbol,
            decision_timestamp,
            regime,
            setup_family=selected_strategy,
            replay_bars=replay_bars,
        )
        state.candidate_snapshot = {**candidate.to_dict(), "summary": candidate.summary()}
        self._emit(
            f"[bot] regime {regime.label} | allowed={regime.trade_allowed} | "
            f"preferred={regime.preferred_action} | strategies={','.join(regime.allowed_setup_families) or 'none'}"
        )
        self._emit(
            f"[bot] candidate present={candidate.candidate_setup_present} | strategy={candidate.setup_family} | direction={candidate.direction}"
        )

        if not regime.trade_allowed:
            final_state = self._build_blocked_regime_decision(state, decision_timestamp, regime)
        elif not candidate.candidate_setup_present:
            final_state = self._build_blocked_candidate_decision(state, decision_timestamp, regime, candidate)
        else:
            final_state = self._run_decision(state, snapshot, decision_timestamp)

        tool_errors = self._extract_tool_errors(final_state)
        raw_action = final_state.get("final_trade_action") or {}
        if not raw_action:
            return {
                "final_state": final_state,
                "tool_errors": tool_errors,
                "action": raw_action,
                "diagnostics": {
                    "regime": state.regime_snapshot,
                    "candidate": state.candidate_snapshot,
                    "raw_action": raw_action,
                    "quality_filter_reasons": [],
                    "final_action": raw_action,
                },
            }

        normalized_action, quality_filter_reasons = self._apply_quality_filters(
            raw_action,
            snapshot,
            regime,
            candidate,
        )
        diagnostics = {
            "regime": state.regime_snapshot,
            "candidate": state.candidate_snapshot,
            "raw_action": raw_action,
            "quality_filter_reasons": quality_filter_reasons,
            "final_action": normalized_action,
        }
        return {
            "final_state": final_state,
            "tool_errors": tool_errors,
            "action": normalized_action,
            "diagnostics": diagnostics,
        }

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

    def _classify_regime(
        self,
        symbol: str,
        decision_timestamp: str,
        *,
        replay_bars: Optional[pd.DataFrame] = None,
    ) -> RegimeSnapshot:
        try:
            if replay_bars is not None:
                frame = replay_bars.copy()
                frame["Date"] = pd.to_datetime(frame["Date"], utc=True, errors="coerce")
                cutoff = pd.to_datetime(decision_timestamp, utc=True, errors="coerce")
                frame = frame[frame["Date"] <= cutoff]
                return classify_regime_from_data(
                    frame,
                    self.config,
                    timeframe=self.bot_config.timeframe,
                )
            vendor_symbol = self.bot_config.symbol
            return classify_regime(vendor_symbol, decision_timestamp, self.config)
        except Exception as exc:
            return RegimeSnapshot(
                label="low_quality",
                trade_allowed=False,
                preferred_action="FLAT",
                setup_family="",
                allowed_setup_families=[],
                current_price=0.0,
                ema20=0.0,
                ema50=0.0,
                atr14=0.0,
                atr_pct=0.0,
                ema20_slope_pct=0.0,
                trend_spread_pct=0.0,
                realized_vol_24h=0.0,
                bar_change_pct=0.0,
                pullback_distance_atr=0.0,
                pullback_zone_low=None,
                pullback_zone_high=None,
                reason=f"Regime classification failed: {exc}",
            )

    def _detect_candidate(
        self,
        symbol: str,
        decision_timestamp: str,
        regime: RegimeSnapshot,
        *,
        setup_family: Optional[str] = None,
        replay_bars: Optional[pd.DataFrame] = None,
    ) -> CandidateSnapshot:
        try:
            if replay_bars is not None:
                frame = replay_bars.copy()
                frame["Date"] = pd.to_datetime(frame["Date"], utc=True, errors="coerce")
                cutoff = pd.to_datetime(decision_timestamp, utc=True, errors="coerce")
                frame = frame[frame["Date"] <= cutoff]
            else:
                frame = load_ohlcv(self.bot_config.symbol, decision_timestamp, timeframe_override=self.bot_config.timeframe)
            return detect_candidate(frame, regime, self.config, setup_family=setup_family)
        except Exception as exc:
            return CandidateSnapshot(
                candidate_setup_present=False,
                setup_family=str(setup_family or regime.setup_family or self.config.get("bot_strategy_setup_family", "trend_pullback")),
                direction=regime.preferred_action,
                entry_zone_low=regime.pullback_zone_low,
                entry_zone_high=regime.pullback_zone_high,
                invalidation_level=None,
                target_reference=None,
                reward_risk_estimate=None,
                reclaim_confirmed=False,
                reason=f"Candidate detection failed: {exc}",
            )

    def _candidate_with_fallback(
        self,
        symbol: str,
        decision_timestamp: str,
        regime: RegimeSnapshot,
        *,
        setup_family: Optional[str] = None,
        replay_bars: Optional[pd.DataFrame] = None,
    ) -> CandidateSnapshot:
        try:
            return self._detect_candidate(
                symbol,
                decision_timestamp,
                regime,
                setup_family=setup_family,
                replay_bars=replay_bars,
            )
        except TypeError:
            return self._detect_candidate(symbol, decision_timestamp, regime, replay_bars=replay_bars)

    def _build_blocked_regime_decision(
        self,
        state: BotState,
        decision_timestamp: str,
        regime: RegimeSnapshot,
    ) -> dict[str, Any]:
        reason = regime.reason
        payload = {
            "symbol": state.symbol,
            "timestamp": decision_timestamp,
            "action": "FLAT",
            "entry_mode": "MARKET",
            "entry_price": None,
            "entry_zone_low": None,
            "entry_zone_high": None,
            "confidence": 0.0,
            "thesis_summary": reason,
            "time_horizon": self.bot_config.timeframe,
            "stop_loss": None,
            "take_profit": None,
            "invalidation": reason,
            "size_hint": "small",
            "setup_expiry_bars": None,
            "position_instruction": "NO_ACTION",
        }
        return {
            "messages": [],
            "market_report": f"No-trade regime: {regime.summary()}",
            "final_trade_decision": (
                "STRUCTURED_DECISION\n```json\n"
                f"{json.dumps(payload, indent=2)}\n```\n"
                f"EXECUTIVE_SUMMARY\nStand aside. {reason}"
            ),
            "final_trade_action": payload,
            "final_trade_action_error": "",
        }

    def _build_blocked_candidate_decision(
        self,
        state: BotState,
        decision_timestamp: str,
        regime: RegimeSnapshot,
        candidate: CandidateSnapshot,
    ) -> dict[str, Any]:
        reason = candidate.reason
        payload = {
            "symbol": state.symbol,
            "timestamp": decision_timestamp,
            "action": "FLAT",
            "entry_mode": "MARKET",
            "entry_price": None,
            "entry_zone_low": None,
            "entry_zone_high": None,
            "confidence": 0.0,
            "thesis_summary": reason,
            "time_horizon": self.bot_config.timeframe,
            "stop_loss": None,
            "take_profit": None,
            "invalidation": reason,
            "size_hint": "small",
            "setup_expiry_bars": None,
            "position_instruction": "NO_ACTION",
        }
        return {
            "messages": [],
            "market_report": (
                f"Eligible regime but no deterministic {regime.setup_family} candidate. "
                f"{candidate.summary()}"
            ),
            "final_trade_decision": (
                "STRUCTURED_DECISION\n```json\n"
                f"{json.dumps(payload, indent=2)}\n```\n"
                f"EXECUTIVE_SUMMARY\nStand aside. {reason}"
            ),
            "final_trade_action": payload,
            "final_trade_action_error": "",
        }

    def _apply_quality_filters(
        self,
        action: dict[str, Any],
        snapshot: ExchangeStateSnapshot,
        regime: RegimeSnapshot,
        candidate: CandidateSnapshot,
    ) -> tuple[dict[str, Any], list[str]]:
        reasons: list[str] = []
        action_side = str(action.get("action", "")).upper()
        if action_side == TradeAction.FLAT.value:
            return action, []

        if str(action.get("symbol", "")).upper() != self._market_symbol(snapshot).upper():
            reasons.append("structured decision symbol does not match active market snapshot")

        setup_family = str(candidate.setup_family or regime.setup_family or self.config.get("bot_strategy_setup_family", "trend_pullback"))

        expected_direction = regime.preferred_action
        if setup_family == "range_fade" and candidate.direction in {TradeAction.LONG.value, TradeAction.SHORT.value}:
            expected_direction = candidate.direction

        if not regime.trade_allowed:
            reasons.append(f"regime {regime.label} is blocked for new entries")
        elif expected_direction in {TradeAction.LONG.value, TradeAction.SHORT.value} and action_side != expected_direction:
            reasons.append(
                f"direction {action_side} conflicts with expected direction {expected_direction}"
            )
        if regime.allowed_setup_families and setup_family not in regime.allowed_setup_families:
            reasons.append(f"strategy {setup_family} is not allowed in regime {regime.label}")

        planned_entry = self._planned_entry_price(action, snapshot)
        if planned_entry is None:
            reasons.append("unable to infer entry price for quality validation")
        else:
            if not self._entry_in_candidate_zone(planned_entry, candidate):
                reasons.append(f"planned entry is outside the allowed {setup_family} zone")
            distance_limit = self._entry_distance_limit_for_timeframe(self.bot_config.timeframe)
            mark_price = snapshot.mark_prices.get(self._market_symbol(snapshot), 0.0)
            if distance_limit is not None and mark_price > 0:
                entry_distance_pct = abs(planned_entry - mark_price) / mark_price
                if entry_distance_pct > distance_limit:
                    reasons.append(
                        f"planned entry distance {entry_distance_pct:.4f} exceeds limit {distance_limit:.4f}"
                    )
            if not self._entry_orientation_is_valid(action, planned_entry, mark_price, setup_family):
                reasons.append(f"entry orientation does not behave like {setup_family}")

        reward_risk = self._reward_risk_ratio(action, planned_entry)
        if reward_risk is None:
            reasons.append("reward-to-risk could not be computed")
        elif reward_risk < float(self.config.get("bot_min_reward_risk", 1.8)):
            reasons.append(
                f"reward-to-risk {reward_risk:.2f} is below minimum {self.config.get('bot_min_reward_risk', 1.8):.2f}"
            )

        if reasons:
            return self._build_rejected_action(action, regime, reasons, setup_family), reasons
        return action, reasons

    def _build_rejected_action(
        self,
        action: dict[str, Any],
        regime: RegimeSnapshot,
        reasons: list[str],
        setup_family: str,
    ) -> dict[str, Any]:
        reason_text = "; ".join(reasons)
        return {
            "symbol": action.get("symbol", self.bot_config.symbol.replace("-USD", "")),
            "timestamp": action.get("timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")),
            "action": "FLAT",
            "entry_mode": "MARKET",
            "entry_price": None,
            "entry_zone_low": None,
            "entry_zone_high": None,
            "confidence": 0.0,
            "thesis_summary": f"Rejected {setup_family}: {reason_text}",
            "time_horizon": action.get("time_horizon", self.bot_config.timeframe),
            "stop_loss": None,
            "take_profit": None,
            "invalidation": reason_text,
            "size_hint": action.get("size_hint", "small"),
            "setup_expiry_bars": None,
            "position_instruction": "NO_ACTION",
        }

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
            f"Regime: {(state.regime_snapshot or {}).get('label', 'unknown')} | "
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

    def run_replay(
        self,
        start_timestamp: str,
        end_timestamp: str,
        *,
        data_source: str = "vendor",
        mode: str = "full-llm",
        strategy_filter: Optional[str] = None,
    ) -> dict[str, Any]:
        replay_mode = mode.strip().lower()
        if replay_mode not in {"regime-only", "candidate-only", "full-llm"}:
            raise ValueError("replay mode must be one of: regime-only, candidate-only, full-llm")
        bars = self._load_replay_bars(
            self.bot_config.symbol,
            start_timestamp,
            end_timestamp,
            data_source=data_source,
        )
        timestamps = self._replay_analysis_timestamps(bars, start_timestamp, end_timestamp)
        observations: list[dict[str, Any]] = []
        for ts in timestamps:
            state = BotState(
                symbol=self.bot_config.symbol.replace("-USD", ""),
                timeframe=self.bot_config.timeframe,
                signal_interval_minutes=self.bot_config.signal_interval_minutes,
                analysis_interval_minutes=self.bot_config.analysis_interval_minutes,
            )
            snapshot = self._historical_snapshot_for_timestamp(bars, ts)
            if replay_mode == "regime-only":
                regime = self._replay_regime(state.symbol, ts.strftime("%Y-%m-%d %H:%M"), bars)
                observations.append(
                    self._build_replay_observation(
                        bars,
                        state.symbol,
                        ts,
                        replay_mode,
                        regime,
                        CandidateSnapshot(
                            candidate_setup_present=False,
                            setup_family="",
                            direction=regime.preferred_action,
                            entry_zone_low=None,
                            entry_zone_high=None,
                            invalidation_level=None,
                            target_reference=None,
                            reward_risk_estimate=None,
                            reclaim_confirmed=False,
                            reason="Regime-only replay skipped deterministic candidate detection.",
                        ),
                        self._build_replay_flat_action(
                            state.symbol,
                            ts.strftime("%Y-%m-%d %H:%M"),
                            "Regime-only replay mode does not invoke candidate or LLM evaluation.",
                        ),
                        quality_filter_reasons=[],
                        tool_errors=[],
                        llm_evaluated=False,
                    )
                )
                continue
            elif replay_mode == "candidate-only":
                regime = self._replay_regime(state.symbol, ts.strftime("%Y-%m-%d %H:%M"), bars)
                strategies = [
                    strategy
                    for strategy in (regime.allowed_setup_families or [])
                    if strategy_filter is None or strategy == strategy_filter
                ]
                if not strategies:
                    observations.append(
                        self._build_replay_observation(
                            bars,
                            state.symbol,
                            ts,
                            replay_mode,
                            regime,
                            CandidateSnapshot(
                                candidate_setup_present=False,
                                setup_family="",
                                direction=regime.preferred_action,
                                entry_zone_low=None,
                                entry_zone_high=None,
                                invalidation_level=None,
                                target_reference=None,
                                reward_risk_estimate=None,
                                reclaim_confirmed=False,
                                reason="No strategy is eligible for this regime in candidate-only replay.",
                            ),
                            self._build_replay_flat_action(
                                state.symbol,
                                ts.strftime("%Y-%m-%d %H:%M"),
                                "Candidate-only replay mode found no eligible strategy for this bar.",
                            ),
                            quality_filter_reasons=[],
                            tool_errors=[],
                            llm_evaluated=False,
                        )
                    )
                    continue
                for strategy in strategies:
                    candidate = self._candidate_with_fallback(
                        state.symbol,
                        ts.strftime("%Y-%m-%d %H:%M"),
                        regime,
                        setup_family=strategy,
                        replay_bars=bars,
                    )
                    action = self._build_replay_flat_action(
                        state.symbol,
                        ts.strftime("%Y-%m-%d %H:%M"),
                        "Candidate-only replay mode does not invoke LLM evaluation.",
                    )
                    if candidate.candidate_setup_present:
                        action = {
                            "symbol": state.symbol,
                            "timestamp": ts.strftime("%Y-%m-%d %H:%M"),
                            "action": candidate.direction,
                            "entry_mode": "LIMIT_ZONE",
                            "entry_price": None,
                            "entry_zone_low": candidate.entry_zone_low,
                            "entry_zone_high": candidate.entry_zone_high,
                            "confidence": 0.5,
                            "thesis_summary": candidate.reason,
                            "time_horizon": self.bot_config.timeframe,
                            "stop_loss": candidate.invalidation_level,
                            "take_profit": candidate.target_reference,
                            "invalidation": candidate.reason,
                            "size_hint": "small",
                            "setup_expiry_bars": self.bot_config.setup_expiry_bars_default,
                            "position_instruction": "OPEN",
                        }
                    observations.append(
                        self._build_replay_observation(
                            bars,
                            state.symbol,
                            ts,
                            replay_mode,
                            regime,
                            candidate,
                            action,
                            quality_filter_reasons=[],
                            tool_errors=[],
                            llm_evaluated=False,
                        )
                    )
                continue
            else:
                regime = self._replay_regime(state.symbol, ts.strftime("%Y-%m-%d %H:%M"), bars)
                strategy = self._select_strategy_for_regime(regime)
                if strategy_filter is not None and strategy != strategy_filter:
                    observations.append(
                        self._build_replay_observation(
                            bars,
                            state.symbol,
                            ts,
                            replay_mode,
                            regime,
                            CandidateSnapshot(
                                candidate_setup_present=False,
                                setup_family=strategy or "",
                                direction=regime.preferred_action,
                                entry_zone_low=None,
                                entry_zone_high=None,
                                invalidation_level=None,
                                target_reference=None,
                                reward_risk_estimate=None,
                                reclaim_confirmed=False,
                                reason="Bar skipped because routed strategy did not match replay strategy filter.",
                            ),
                            self._build_replay_flat_action(
                                state.symbol,
                                ts.strftime("%Y-%m-%d %H:%M"),
                                "Replay strategy filter skipped this routed bar.",
                            ),
                            quality_filter_reasons=[],
                            tool_errors=[],
                            llm_evaluated=False,
                        )
                    )
                    continue
                analysis = self._analyze_decision(
                    state,
                    snapshot,
                    ts.strftime("%Y-%m-%d %H:%M"),
                    replay_bars=bars,
                    strategy_family=strategy,
                )
                observations.append(
                    self._build_replay_observation(
                        bars,
                        state.symbol,
                        ts,
                        replay_mode,
                        RegimeSnapshot(**{k: v for k, v in analysis["diagnostics"]["regime"].items() if k != "summary"}),
                        CandidateSnapshot(**{k: v for k, v in analysis["diagnostics"]["candidate"].items() if k != "summary"}),
                        analysis["action"],
                        quality_filter_reasons=analysis["diagnostics"]["quality_filter_reasons"],
                        tool_errors=analysis["tool_errors"],
                        llm_evaluated=bool(
                            analysis["diagnostics"]["candidate"]["candidate_setup_present"]
                            and analysis["diagnostics"]["regime"]["trade_allowed"]
                        ),
                    )
                )
        return {
            "symbol": self.bot_config.symbol,
            "timeframe": self.bot_config.timeframe,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "data_source": data_source,
            "mode": replay_mode,
            "strategy_filter": strategy_filter,
            "observations": observations,
            "summary": summarize_replay(observations),
        }

    def _load_replay_bars(
        self,
        symbol: str,
        start_timestamp: str,
        end_timestamp: str,
        *,
        data_source: str = "vendor",
    ) -> pd.DataFrame:
        timeframe = self.bot_config.timeframe.lower()
        if data_source == "hyperliquid":
            hl_symbol = symbol.replace("-USD", "")
            if not hasattr(self.exchange, "get_historical_ohlcv"):
                raise ValueError("hyperliquid replay requires an executor with get_historical_ohlcv")
            return self.exchange.get_historical_ohlcv(
                hl_symbol,
                start_time=start_timestamp,
                end_time=end_timestamp,
                timeframe=timeframe,
            )
        return load_ohlcv(symbol, end_timestamp, timeframe_override=timeframe)

    def _replay_analysis_timestamps(
        self,
        bars: pd.DataFrame,
        start_timestamp: str,
        end_timestamp: str,
    ) -> list[pd.Timestamp]:
        frame = bars.sort_values("Date").copy()
        frame["Date"] = pd.to_datetime(frame["Date"], utc=True, errors="coerce")
        start_ts = pd.to_datetime(start_timestamp, utc=True)
        end_ts = pd.to_datetime(end_timestamp, utc=True)
        timestamps: list[pd.Timestamp] = []
        for ts in frame["Date"]:
            if pd.isna(ts) or ts < start_ts or ts > end_ts:
                continue
            minutes_since_midnight = ts.hour * 60 + ts.minute
            if minutes_since_midnight % self.bot_config.analysis_interval_minutes == 0:
                timestamps.append(ts)
        return timestamps

    def _historical_snapshot_for_timestamp(
        self,
        bars: pd.DataFrame,
        timestamp: pd.Timestamp,
    ) -> ExchangeStateSnapshot:
        frame = bars.copy()
        frame["Date"] = pd.to_datetime(frame["Date"], utc=True, errors="coerce")
        matching = frame[frame["Date"] == timestamp]
        if matching.empty:
            raise ValueError(f"historical snapshot timestamp not found: {timestamp}")
        bar = matching.iloc[-1]
        symbol = self.bot_config.symbol.replace("-USD", "")
        bankroll = float(self.config.get("bot_replay_initial_equity", 1000.0))
        return ExchangeStateSnapshot(
            wallet_address="replay",
            equity=bankroll,
            available_balance=bankroll,
            spot_usdc_balance=bankroll,
            mark_prices={symbol: float(bar["Close"])},
            positions=[],
            open_orders=[],
            fetched_at=timestamp.isoformat(),
        )

    def _build_replay_flat_action(
        self,
        symbol: str,
        timestamp: str,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "timestamp": timestamp,
            "action": "FLAT",
            "entry_mode": "MARKET",
            "entry_price": None,
            "entry_zone_low": None,
            "entry_zone_high": None,
            "confidence": 0.0,
            "thesis_summary": reason,
            "time_horizon": self.bot_config.timeframe,
            "stop_loss": None,
            "take_profit": None,
            "invalidation": reason,
            "size_hint": "small",
            "setup_expiry_bars": None,
            "position_instruction": "NO_ACTION",
        }

    def _replay_regime(
        self,
        symbol: str,
        decision_timestamp: str,
        replay_bars: pd.DataFrame,
    ) -> RegimeSnapshot:
        try:
            return self._classify_regime(symbol, decision_timestamp, replay_bars=replay_bars)
        except TypeError:
            return self._classify_regime(symbol, decision_timestamp)

    def _build_replay_observation(
        self,
        bars: pd.DataFrame,
        symbol: str,
        timestamp: pd.Timestamp,
        replay_mode: str,
        regime: RegimeSnapshot,
        candidate: CandidateSnapshot,
        action: dict[str, Any],
        *,
        quality_filter_reasons: list[str],
        tool_errors: list[str],
        llm_evaluated: bool,
    ) -> dict[str, Any]:
        observation = {
            "decision_timestamp": timestamp.isoformat(),
            "symbol": symbol,
            "timeframe": self.bot_config.timeframe,
            "replay_mode": replay_mode,
            "allowed_setup_families": regime.allowed_setup_families,
            "setup_family": candidate.setup_family or regime.setup_family,
            "regime_label": regime.label,
            "regime_trade_allowed": regime.trade_allowed,
            "regime_reason": regime.reason,
            "candidate_setup_present": candidate.candidate_setup_present,
            "candidate_reason": candidate.reason,
            "quality_filter_reasons": quality_filter_reasons,
            "tool_errors": tool_errors,
            "raw_action": action,
            "final_action": action,
            "llm_evaluated": llm_evaluated,
        }
        observation.update(
            evaluate_replay_observation(
                bars,
                timestamp.isoformat(),
                action,
                setup_expiry_bars_default=self.bot_config.setup_expiry_bars_default,
            )
        )
        return observation

    def _market_symbol(self, snapshot: ExchangeStateSnapshot) -> str:
        if snapshot.mark_prices:
            return next(iter(snapshot.mark_prices.keys()))
        return self.bot_config.symbol.replace("-USD", "")

    def _planned_entry_price(
        self,
        action: dict[str, Any],
        snapshot: ExchangeStateSnapshot,
    ) -> Optional[float]:
        entry_mode = str(action.get("entry_mode") or "MARKET").upper()
        if entry_mode == "MARKET":
            mark_symbol = self._market_symbol(snapshot)
            return float(snapshot.mark_prices.get(mark_symbol, 0.0) or 0.0)
        if entry_mode == "LIMIT":
            price = action.get("entry_price")
            return float(price) if price is not None else None
        low = action.get("entry_zone_low")
        high = action.get("entry_zone_high")
        if low is None or high is None:
            return None
        return (float(low) + float(high)) / 2.0

    def _reward_risk_ratio(
        self,
        action: dict[str, Any],
        planned_entry: Optional[float],
    ) -> Optional[float]:
        if planned_entry is None:
            return None
        stop_loss = action.get("stop_loss")
        take_profit = action.get("take_profit")
        if stop_loss is None or take_profit is None:
            return None
        stop_loss = float(stop_loss)
        take_profit = float(take_profit)
        side = str(action.get("action", "")).upper()
        if side == TradeAction.LONG.value:
            risk = planned_entry - stop_loss
            reward = take_profit - planned_entry
        else:
            risk = stop_loss - planned_entry
            reward = planned_entry - take_profit
        if risk <= 0 or reward <= 0:
            return None
        return reward / risk

    def _entry_in_candidate_zone(self, planned_entry: float, candidate: CandidateSnapshot) -> bool:
        if candidate.entry_zone_low is None or candidate.entry_zone_high is None:
            return False
        return candidate.entry_zone_low <= planned_entry <= candidate.entry_zone_high

    def _entry_orientation_is_valid(
        self,
        action: dict[str, Any],
        planned_entry: float,
        mark_price: float,
        setup_family: str,
    ) -> bool:
        side = str(action.get("action", "")).upper()
        entry_mode = str(action.get("entry_mode") or "MARKET").upper()
        tolerance = mark_price * 0.002
        if entry_mode == "MARKET":
            return True
        if setup_family == "range_fade":
            if side == TradeAction.LONG.value:
                return planned_entry <= mark_price + tolerance
            if side == TradeAction.SHORT.value:
                return planned_entry >= mark_price - tolerance
            return False
        if side == TradeAction.LONG.value:
            return planned_entry <= mark_price + tolerance
        if side == TradeAction.SHORT.value:
            return planned_entry >= mark_price - tolerance
        return False

    def _select_strategy_for_regime(self, regime: RegimeSnapshot) -> Optional[str]:
        if regime.allowed_setup_families:
            return regime.allowed_setup_families[0]
        allowed = allowed_strategies_for_regime(regime.label, self.config)
        return allowed[0] if allowed else None

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

    def _journal_cycle(
        self,
        state: BotState,
        snapshot: ExchangeStateSnapshot,
        decision_timestamp: str,
        analysis_timestamp: str,
        diagnostics: dict[str, Any],
        action: dict[str, Any],
        plan: Optional[BotActionPlan],
        tool_errors: list[str],
        outcome: str,
        outcome_message: str,
        *,
        order_intent: Optional[dict[str, Any]] = None,
        order_preview: Optional[dict[str, Any]] = None,
    ) -> None:
        self.journal.insert_cycle(
            mode="live",
            symbol=state.symbol,
            timeframe=state.timeframe,
            decision_timestamp=decision_timestamp,
            analysis_timestamp=analysis_timestamp,
            regime_snapshot=diagnostics.get("regime"),
            candidate_snapshot=diagnostics.get("candidate"),
            allowed_setup_families=(diagnostics.get("regime") or {}).get("allowed_setup_families", []),
            selected_setup_family=(diagnostics.get("candidate") or {}).get("setup_family"),
            raw_action=diagnostics.get("raw_action"),
            final_action=action or diagnostics.get("final_action"),
            quality_filter_reasons=diagnostics.get("quality_filter_reasons"),
            tool_errors=tool_errors,
            plan_action=plan.action if plan else None,
            outcome=outcome,
            outcome_message=outcome_message,
            exchange_snapshot=snapshot.model_dump(),
            order_intent=order_intent,
            order_preview=order_preview,
        )


def build_bot_runtime_config() -> dict:
    return DEFAULT_CONFIG.copy()
