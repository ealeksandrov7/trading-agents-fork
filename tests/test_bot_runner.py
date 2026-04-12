import tempfile
import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from langchain_core.messages import ToolMessage

from tradingagents.bot.candidate import CandidateSnapshot
from tradingagents.bot.models import BotConfig, BotState
from tradingagents.bot.regime import RegimeSnapshot
from tradingagents.bot.runner import BotRunner
from tradingagents.bot.state import BotStateStore
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.execution import (
    ExchangeOrder,
    ExchangeStateSnapshot,
    ExecutionMode,
    OrderPreview,
    OrderStatus,
    Position,
    TradeAction,
)


class BotStateStoreTests(unittest.TestCase):
    def test_store_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BotStateStore(Path(tmpdir) / "bot_state.json")
            state, events = store.load()
            self.assertEqual(state.symbol, "BTC")
            self.assertEqual(events, [])


class BotRunnerPlannerTests(unittest.TestCase):
    def setUp(self):
        class StubExecutor:
            def cancel_order(self, symbol, order_id):
                return {"status": "ok", "symbol": symbol, "order_id": order_id}

        config = DEFAULT_CONFIG.copy()
        config["bot_state_path"] = str(Path(tempfile.gettempdir()) / "bot_runner_test_state.json")
        config["hyperliquid_wallet_address"] = "0xabc"
        self.runner = BotRunner(
            config=config,
            bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", once=True),
            executor=StubExecutor(),
        )

    @staticmethod
    def _tradable_regime():
        return RegimeSnapshot(
            label="trend_down",
            trade_allowed=True,
            preferred_action="SHORT",
            setup_family="trend_pullback",
            current_price=70000,
            ema20=70100,
            ema50=70600,
            atr14=400,
            atr_pct=0.0057,
            ema20_slope_pct=-0.003,
            trend_spread_pct=0.008,
            realized_vol_24h=0.007,
            bar_change_pct=-0.001,
            pullback_distance_atr=0.25,
            pullback_zone_low=69950,
            pullback_zone_high=70350,
            reason="Downtrend confirmed.",
        )

    @staticmethod
    def _positive_candidate(direction: str = "SHORT"):
        return CandidateSnapshot(
            candidate_setup_present=True,
            setup_family="trend_pullback",
            direction=direction,
            entry_zone_low=69950,
            entry_zone_high=70350,
            invalidation_level=70600 if direction == "SHORT" else 69400,
            target_reference=68800 if direction == "SHORT" else 71200,
            reward_risk_estimate=2.0,
            reclaim_confirmed=True,
            reason="Deterministic pullback candidate confirmed.",
        )

    def test_flat_without_position_is_no_action(self):
        state = BotState(symbol="BTC", timeframe="1h", signal_interval_minutes=60, analysis_interval_minutes=240)
        snapshot = ExchangeStateSnapshot(
            wallet_address="0xabc",
            equity=1000,
            available_balance=1000,
            mark_prices={"BTC": 70000},
            positions=[],
            open_orders=[],
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
        plan = self.runner._build_action_plan(
            state,
            snapshot,
            {"symbol": "BTC", "action": "FLAT", "confidence": 0.4, "thesis_summary": "stay flat", "time_horizon": "1h", "invalidation": "n/a"},
        )
        self.assertEqual(plan.action, "NO_ACTION")

    def test_pending_order_kept_when_not_expired(self):
        created_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        state = BotState(
            symbol="BTC",
            timeframe="1h",
            signal_interval_minutes=60,
            analysis_interval_minutes=240,
            active_order_id="123",
            active_order_intent={"action": "SHORT", "setup_expiry_bars": 3},
            active_order_created_at=created_at.isoformat(),
        )
        snapshot = ExchangeStateSnapshot(
            wallet_address="0xabc",
            equity=1000,
            available_balance=1000,
            mark_prices={"BTC": 70000},
            positions=[],
            open_orders=[
                ExchangeOrder(symbol="BTC", order_id="123", side=TradeAction.SHORT, size=0.01)
            ],
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
        plan = self.runner._build_action_plan(
            state,
            snapshot,
            {
                "symbol": "BTC",
                "action": "SHORT",
                "entry_mode": "LIMIT_ZONE",
                "entry_zone_low": 70500,
                "entry_zone_high": 71000,
                "confidence": 0.5,
                "thesis_summary": "wait for retrace",
                "time_horizon": "1h",
                "stop_loss": 71500,
                "take_profit": 69000,
                "invalidation": "breakout",
            },
        )
        self.assertEqual(plan.action, "KEEP_PENDING")

    def test_open_entry_created_when_flat(self):
        state = BotState(symbol="BTC", timeframe="1h", signal_interval_minutes=60, analysis_interval_minutes=240)
        snapshot = ExchangeStateSnapshot(
            wallet_address="0xabc",
            equity=1000,
            available_balance=1000,
            mark_prices={"BTC": 70000},
            positions=[],
            open_orders=[],
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
        plan = self.runner._build_action_plan(
            state,
            snapshot,
            {
                "symbol": "BTC",
                "action": "SHORT",
                "entry_mode": "LIMIT",
                "entry_price": 70200,
                "confidence": 0.55,
                "thesis_summary": "fade resistance",
                "time_horizon": "1h",
                "stop_loss": 71000,
                "take_profit": 68500,
                "invalidation": "hourly breakout",
            },
        )
        self.assertEqual(plan.action, "OPEN_ENTRY")
        self.assertIsNotNone(plan.order_intent)
        self.assertEqual(plan.order_intent.mode, ExecutionMode.LIVE)

    def test_latest_analysis_bar_only_triggers_every_four_hours_for_1h_signals(self):
        class FixedRunner(BotRunner):
            def _latest_completed_signal_bar(self):
                return datetime(2026, 4, 7, 11, 0, tzinfo=timezone.utc)

        runner = FixedRunner(
            config=DEFAULT_CONFIG.copy(),
            bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", analysis_interval_minutes=240, once=True),
            executor=type("StubExecutor", (), {"cancel_order": lambda *args, **kwargs: {}})(),
        )
        self.assertIsNone(runner._latest_analysis_bar())

        class FixedRunnerAligned(BotRunner):
            def _latest_completed_signal_bar(self):
                return datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc)

        aligned_runner = FixedRunnerAligned(
            config=DEFAULT_CONFIG.copy(),
            bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", analysis_interval_minutes=240, once=True),
            executor=type("StubExecutor", (), {"cancel_order": lambda *args, **kwargs: {}})(),
        )
        self.assertEqual(aligned_runner._latest_analysis_bar(), "2026-04-07 12:00")

    def test_intraday_bot_defaults_to_market_only_analysts(self):
        self.assertEqual(self.runner._bot_analysts(), ["market"])

    def test_failed_live_submit_does_not_mark_analysis_bar_complete(self):
        class FailingExecutor:
            def __init__(self):
                self.calls = 0

            def get_exchange_state_snapshot(self, symbol):
                return ExchangeStateSnapshot(
                    wallet_address="0xabc",
                    equity=1000,
                    available_balance=1000,
                    spot_usdc_balance=1000,
                    mark_prices={"BTC": 70000},
                    positions=[],
                    open_orders=[],
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                )

            def execute(self, intent):
                self.calls += 1
                raise RuntimeError("post-submit parse failure")

            def cancel_order(self, symbol, order_id):
                return {"status": "ok"}

        class FixedRunner(BotRunner):
            def _latest_analysis_bar(self):
                return "2026-04-07 12:00"

            def _classify_regime(self, symbol, decision_timestamp):
                return BotRunnerPlannerTests._tradable_regime()

            def _detect_candidate(self, symbol, decision_timestamp, regime, *, replay_bars=None):
                return BotRunnerPlannerTests._positive_candidate("SHORT")

            def _run_decision(self, state, snapshot, decision_timestamp):
                return {
                    "final_trade_action": {
                        "symbol": "BTC",
                        "action": "SHORT",
                        "entry_mode": "LIMIT",
                        "entry_price": 70200,
                        "confidence": 0.55,
                        "thesis_summary": "fade resistance",
                        "time_horizon": "1h",
                        "stop_loss": 71000,
                        "take_profit": 68500,
                        "invalidation": "hourly breakout",
                        "position_instruction": "OPEN",
                    }
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            config = DEFAULT_CONFIG.copy()
            config["bot_state_path"] = str(Path(tmpdir) / "bot_state.json")
            executor = FailingExecutor()
            runner = FixedRunner(
                config=config,
                bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", once=True),
                executor=executor,
            )
            with self.assertRaises(RuntimeError):
                runner.run_once()

            state, _ = BotStateStore(Path(config["bot_state_path"])).load()
            self.assertIsNone(state.last_decision_timestamp)
            self.assertEqual(executor.calls, 1)

    def test_rejected_live_submit_does_not_create_active_order_state(self):
        class RejectingExecutor:
            def __init__(self):
                self.calls = 0

            def get_exchange_state_snapshot(self, symbol):
                return ExchangeStateSnapshot(
                    wallet_address="0xabc",
                    equity=1000,
                    available_balance=1000,
                    spot_usdc_balance=1000,
                    mark_prices={"BTC": 70000},
                    positions=[],
                    open_orders=[],
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                )

            def execute(self, intent):
                self.calls += 1
                return OrderPreview(
                    status=OrderStatus.REJECTED,
                    mode=ExecutionMode.LIVE,
                    symbol="BTC",
                    action=TradeAction.SHORT,
                    message="Live bracket order rejected: Order has invalid size.",
                    reference_price=70200,
                    size=0.024849,
                    leverage=2,
                )

            def cancel_order(self, symbol, order_id):
                return {"status": "ok"}

        class FixedRunner(BotRunner):
            def _latest_analysis_bar(self):
                return "2026-04-07 12:00"

            def _classify_regime(self, symbol, decision_timestamp):
                return BotRunnerPlannerTests._tradable_regime()

            def _detect_candidate(self, symbol, decision_timestamp, regime, *, replay_bars=None):
                return BotRunnerPlannerTests._positive_candidate("SHORT")

            def _run_decision(self, state, snapshot, decision_timestamp):
                return {
                    "final_trade_action": {
                        "symbol": "BTC",
                        "action": "SHORT",
                        "entry_mode": "LIMIT",
                        "entry_price": 70200,
                        "confidence": 0.55,
                        "thesis_summary": "fade resistance",
                        "time_horizon": "1h",
                        "stop_loss": 71000,
                        "take_profit": 68500,
                        "invalidation": "hourly breakout",
                        "position_instruction": "OPEN",
                    }
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            config = DEFAULT_CONFIG.copy()
            config["bot_state_path"] = str(Path(tmpdir) / "bot_state.json")
            executor = RejectingExecutor()
            runner = FixedRunner(
                config=config,
                bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", once=True),
                executor=executor,
            )

            runner.run_once()

            state, events = BotStateStore(Path(config["bot_state_path"])).load()
            self.assertEqual(state.last_decision_timestamp, "2026-04-07 12:00")
            self.assertIsNone(state.active_order_id)
            self.assertIsNone(state.active_order_intent)
            self.assertEqual(executor.calls, 1)
            self.assertEqual(events[-1].event_type, "entry_rejected")

    def test_tool_failure_blocks_live_entry_submission(self):
        class StubExecutor:
            def __init__(self):
                self.calls = 0

            def get_exchange_state_snapshot(self, symbol):
                return ExchangeStateSnapshot(
                    wallet_address="0xabc",
                    equity=1000,
                    available_balance=1000,
                    spot_usdc_balance=1000,
                    mark_prices={"BTC": 70000},
                    positions=[],
                    open_orders=[],
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                )

            def execute(self, intent):
                self.calls += 1
                raise AssertionError("execute should not be called when tool failures are present")

            def cancel_order(self, symbol, order_id):
                return {"status": "ok"}

        class FixedRunner(BotRunner):
            def _latest_analysis_bar(self):
                return "2026-04-07 12:00"

            def _classify_regime(self, symbol, decision_timestamp):
                return BotRunnerPlannerTests._tradable_regime()

            def _detect_candidate(self, symbol, decision_timestamp, regime, *, replay_bars=None):
                return BotRunnerPlannerTests._positive_candidate("SHORT")

            def _run_decision(self, state, snapshot, decision_timestamp):
                return {
                    "messages": [
                        ToolMessage(
                            content="[TOOL_ERROR] tool=get_stock_data symbol=BTC-USD detail=No data found for symbol 'BTC-USD'",
                            tool_call_id="call-1",
                        )
                    ],
                    "final_trade_action": {
                        "symbol": "BTC",
                        "action": "SHORT",
                        "entry_mode": "LIMIT",
                        "entry_price": 70200,
                        "confidence": 0.55,
                        "thesis_summary": "fade resistance",
                        "time_horizon": "1h",
                        "stop_loss": 71000,
                        "take_profit": 68500,
                        "invalidation": "hourly breakout",
                        "position_instruction": "OPEN",
                    },
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            config = DEFAULT_CONFIG.copy()
            config["bot_state_path"] = str(Path(tmpdir) / "bot_state.json")
            executor = StubExecutor()
            runner = FixedRunner(
                config=config,
                bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", once=True),
                executor=executor,
            )

            runner.run_once()

            state, events = BotStateStore(Path(config["bot_state_path"])).load()
            self.assertEqual(state.last_decision_timestamp, "2026-04-07 12:00")
            self.assertEqual(executor.calls, 0)
            self.assertEqual(events[-1].event_type, "entry_blocked_tool_failure")

    def test_blocked_regime_short_circuits_to_no_action(self):
        class StubExecutor:
            def __init__(self):
                self.calls = 0

            def get_exchange_state_snapshot(self, symbol):
                return ExchangeStateSnapshot(
                    wallet_address="0xabc",
                    equity=1000,
                    available_balance=1000,
                    spot_usdc_balance=1000,
                    mark_prices={"BTC": 70000},
                    positions=[],
                    open_orders=[],
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                )

            def execute(self, intent):
                self.calls += 1
                raise AssertionError("execute should not be called for blocked regimes")

            def cancel_order(self, symbol, order_id):
                return {"status": "ok"}

        class FixedRunner(BotRunner):
            def _latest_analysis_bar(self):
                return "2026-04-07 12:00"

            def _classify_regime(self, symbol, decision_timestamp):
                return RegimeSnapshot(
                    label="range",
                    trade_allowed=False,
                    preferred_action="FLAT",
                    setup_family="trend_pullback",
                    current_price=70000,
                    ema20=70000,
                    ema50=70010,
                    atr14=1200,
                    atr_pct=0.017,
                    ema20_slope_pct=0.0,
                    trend_spread_pct=0.0002,
                    realized_vol_24h=0.01,
                    bar_change_pct=0.001,
                    pullback_distance_atr=0.0,
                    pullback_zone_low=None,
                    pullback_zone_high=None,
                    reason="Trend spread and slope are too weak.",
                )

            def _run_decision(self, state, snapshot, decision_timestamp):
                raise AssertionError("graph should not run when regime is blocked")

        with tempfile.TemporaryDirectory() as tmpdir:
            config = DEFAULT_CONFIG.copy()
            config["bot_state_path"] = str(Path(tmpdir) / "bot_state.json")
            executor = StubExecutor()
            runner = FixedRunner(
                config=config,
                bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", once=True),
                executor=executor,
            )

            runner.run_once()

            state, events = BotStateStore(Path(config["bot_state_path"])).load()
            self.assertEqual(state.last_decision_timestamp, "2026-04-07 12:00")
            self.assertEqual(state.last_decision_action["action"], "FLAT")
            self.assertEqual(executor.calls, 0)
            self.assertEqual(state.regime_snapshot["label"], "range")
            self.assertEqual(events[-1].event_type, "plan")

    def test_quality_filter_rejects_countertrend_trade(self):
        class StubExecutor:
            def __init__(self):
                self.calls = 0

            def get_exchange_state_snapshot(self, symbol):
                return ExchangeStateSnapshot(
                    wallet_address="0xabc",
                    equity=1000,
                    available_balance=1000,
                    spot_usdc_balance=1000,
                    mark_prices={"BTC": 70000},
                    positions=[],
                    open_orders=[],
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                )

            def execute(self, intent):
                self.calls += 1
                raise AssertionError("execute should not be called for rejected decisions")

            def cancel_order(self, symbol, order_id):
                return {"status": "ok"}

        class FixedRunner(BotRunner):
            def _latest_analysis_bar(self):
                return "2026-04-07 12:00"

            def _classify_regime(self, symbol, decision_timestamp):
                return RegimeSnapshot(
                    label="trend_up",
                    trade_allowed=True,
                    preferred_action="LONG",
                    setup_family="trend_pullback",
                    current_price=70000,
                    ema20=69900,
                    ema50=69200,
                    atr14=400,
                    atr_pct=0.0057,
                    ema20_slope_pct=0.003,
                    trend_spread_pct=0.01,
                    realized_vol_24h=0.008,
                    bar_change_pct=0.001,
                    pullback_distance_atr=0.25,
                    pullback_zone_low=69600,
                    pullback_zone_high=70040,
                    reason="Uptrend confirmed.",
                )

            def _detect_candidate(self, symbol, decision_timestamp, regime, *, replay_bars=None):
                return BotRunnerPlannerTests._positive_candidate("LONG")

            def _run_decision(self, state, snapshot, decision_timestamp):
                return {
                    "final_trade_action": {
                        "symbol": "BTC",
                        "action": "SHORT",
                        "entry_mode": "LIMIT",
                        "entry_price": 70020,
                        "confidence": 0.7,
                        "thesis_summary": "countertrend fade",
                        "time_horizon": "1h",
                        "stop_loss": 70400,
                        "take_profit": 69400,
                        "invalidation": "break above highs",
                        "position_instruction": "OPEN",
                    }
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            config = DEFAULT_CONFIG.copy()
            config["bot_state_path"] = str(Path(tmpdir) / "bot_state.json")
            executor = StubExecutor()
            runner = FixedRunner(
                config=config,
                bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", once=True),
                executor=executor,
            )

            runner.run_once()

            state, events = BotStateStore(Path(config["bot_state_path"])).load()
            self.assertEqual(state.last_decision_action["action"], "FLAT")
            self.assertEqual(executor.calls, 0)
            rejection_events = [event for event in events if event.event_type == "decision_rejected"]
            self.assertEqual(len(rejection_events), 1)
            self.assertIn("conflicts with regime preferred action", rejection_events[0].payload["reasons"][0])

    def test_missing_candidate_skips_graph_even_in_tradable_regime(self):
        class StubExecutor:
            def __init__(self):
                self.calls = 0

            def get_exchange_state_snapshot(self, symbol):
                return ExchangeStateSnapshot(
                    wallet_address="0xabc",
                    equity=1000,
                    available_balance=1000,
                    spot_usdc_balance=1000,
                    mark_prices={"BTC": 70000},
                    positions=[],
                    open_orders=[],
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                )

            def execute(self, intent):
                self.calls += 1
                raise AssertionError("execute should not be called when no candidate is present")

            def cancel_order(self, symbol, order_id):
                return {"status": "ok"}

        class FixedRunner(BotRunner):
            def _latest_analysis_bar(self):
                return "2026-04-07 12:00"

            def _classify_regime(self, symbol, decision_timestamp):
                return RegimeSnapshot(
                    label="trend_up",
                    trade_allowed=True,
                    preferred_action="LONG",
                    setup_family="trend_pullback",
                    current_price=70000,
                    ema20=69900,
                    ema50=69200,
                    atr14=400,
                    atr_pct=0.0057,
                    ema20_slope_pct=0.003,
                    trend_spread_pct=0.01,
                    realized_vol_24h=0.008,
                    bar_change_pct=0.001,
                    pullback_distance_atr=0.25,
                    pullback_zone_low=69600,
                    pullback_zone_high=70040,
                    reason="Uptrend confirmed.",
                )

            def _detect_candidate(self, symbol, decision_timestamp, regime, *, replay_bars=None):
                return CandidateSnapshot(
                    candidate_setup_present=True,
                    setup_family="trend_pullback",
                    direction="LONG",
                    entry_zone_low=69600,
                    entry_zone_high=71000,
                    invalidation_level=69800,
                    target_reference=71600,
                    reward_risk_estimate=2.2,
                    reclaim_confirmed=True,
                    reason="Deterministic pullback candidate confirmed.",
                )

            def _detect_candidate(self, symbol, decision_timestamp, regime, *, replay_bars=None):
                return CandidateSnapshot(
                    candidate_setup_present=False,
                    setup_family="trend_pullback",
                    direction="LONG",
                    entry_zone_low=69600,
                    entry_zone_high=70040,
                    invalidation_level=None,
                    target_reference=None,
                    reward_risk_estimate=None,
                    reclaim_confirmed=False,
                    reason="Pullback reached the zone but has not reclaimed in the trend direction.",
                )

            def _run_decision(self, state, snapshot, decision_timestamp):
                raise AssertionError("graph should not run when no deterministic candidate is present")

        with tempfile.TemporaryDirectory() as tmpdir:
            config = DEFAULT_CONFIG.copy()
            config["bot_state_path"] = str(Path(tmpdir) / "bot_state.json")
            runner = FixedRunner(
                config=config,
                bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", once=True),
                executor=StubExecutor(),
            )

            runner.run_once()

            state, events = BotStateStore(Path(config["bot_state_path"])).load()
            self.assertEqual(state.last_decision_action["action"], "FLAT")
            self.assertFalse(state.candidate_snapshot["candidate_setup_present"])
            candidate_events = [event for event in events if event.event_type == "candidate"]
            self.assertEqual(len(candidate_events), 1)

    def test_run_replay_returns_summary(self):
        class ReplayRunner(BotRunner):
            def _load_replay_bars(self, symbol, start_timestamp, end_timestamp, *, data_source="vendor"):
                base = pd.Timestamp("2026-04-07 00:00", tz="UTC")
                rows = []
                price = 70000.0
                for idx in range(12):
                    ts = base + pd.Timedelta(hours=idx)
                    close = price + idx * 50.0
                    rows.append(
                        {
                            "Date": ts,
                            "Open": close - 25.0,
                            "High": close + 75.0,
                            "Low": close - 75.0,
                            "Close": close,
                            "Volume": 1000 + idx,
                        }
                    )
                return pd.DataFrame(rows)

            def _classify_regime(self, symbol, decision_timestamp):
                return RegimeSnapshot(
                    label="trend_up",
                    trade_allowed=True,
                    preferred_action="LONG",
                    setup_family="trend_pullback",
                    current_price=70000,
                    ema20=69900,
                    ema50=69200,
                    atr14=400,
                    atr_pct=0.0057,
                    ema20_slope_pct=0.003,
                    trend_spread_pct=0.01,
                    realized_vol_24h=0.008,
                    bar_change_pct=0.001,
                    pullback_distance_atr=0.25,
                    pullback_zone_low=69600,
                    pullback_zone_high=71000,
                    reason="Uptrend confirmed.",
                )

            def _detect_candidate(self, symbol, decision_timestamp, regime, *, replay_bars=None):
                return CandidateSnapshot(
                    candidate_setup_present=True,
                    setup_family="trend_pullback",
                    direction="LONG",
                    entry_zone_low=69600,
                    entry_zone_high=71000,
                    invalidation_level=69800,
                    target_reference=71600,
                    reward_risk_estimate=2.2,
                    reclaim_confirmed=True,
                    reason="Deterministic pullback candidate confirmed.",
                )

            def _run_decision(self, state, snapshot, decision_timestamp):
                return {
                    "final_trade_action": {
                        "symbol": "BTC",
                        "action": "LONG",
                        "entry_mode": "MARKET",
                        "confidence": 0.7,
                        "thesis_summary": "trend pullback continuation",
                        "time_horizon": "1h",
                        "stop_loss": 69800,
                        "take_profit": 71600,
                        "invalidation": "lose pullback low",
                        "position_instruction": "OPEN",
                    }
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            config = DEFAULT_CONFIG.copy()
            config["bot_state_path"] = str(Path(tmpdir) / "bot_state.json")
            runner = ReplayRunner(
                config=config,
                bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", analysis_interval_minutes=240, once=True),
                executor=type("StubExecutor", (), {"cancel_order": lambda *args, **kwargs: {}})(),
            )
            result = runner.run_replay("2026-04-07 00:00", "2026-04-07 11:00")

            self.assertEqual(result["summary"]["total_decisions"], 3)
            self.assertEqual(result["summary"]["executed"], 3)
            self.assertIn("trend_up", result["summary"]["by_regime"])

    def test_run_replay_uses_hyperliquid_source(self):
        class StubExecutor:
            def __init__(self):
                self.calls = []

            def get_historical_ohlcv(self, symbol, *, start_time, end_time, timeframe):
                self.calls.append((symbol, start_time, end_time, timeframe))
                base = pd.Timestamp("2026-04-07 00:00", tz="UTC")
                rows = []
                for idx in range(5):
                    ts = base + pd.Timedelta(hours=idx * 4)
                    close = 70000.0 + idx * 100.0
                    rows.append(
                        {
                            "Date": ts,
                            "Open": close - 25.0,
                            "High": close + 75.0,
                            "Low": close - 75.0,
                            "Close": close,
                            "Volume": 1000 + idx,
                        }
                    )
                return pd.DataFrame(rows)

            def cancel_order(self, symbol, order_id):
                return {"status": "ok"}

        class ReplayRunner(BotRunner):
            def _classify_regime(self, symbol, decision_timestamp):
                return RegimeSnapshot(
                    label="trend_up",
                    trade_allowed=True,
                    preferred_action="LONG",
                    setup_family="trend_pullback",
                    current_price=70000,
                    ema20=69900,
                    ema50=69200,
                    atr14=400,
                    atr_pct=0.0057,
                    ema20_slope_pct=0.003,
                    trend_spread_pct=0.01,
                    realized_vol_24h=0.008,
                    bar_change_pct=0.001,
                    pullback_distance_atr=0.25,
                    pullback_zone_low=69600,
                    pullback_zone_high=71000,
                    reason="Uptrend confirmed.",
                )

            def _run_decision(self, state, snapshot, decision_timestamp):
                return {
                    "final_trade_action": {
                        "symbol": "BTC",
                        "action": "LONG",
                        "entry_mode": "MARKET",
                        "confidence": 0.7,
                        "thesis_summary": "trend continuation",
                        "time_horizon": "1h",
                        "stop_loss": 69800,
                        "take_profit": 71600,
                        "invalidation": "lose pullback low",
                        "position_instruction": "OPEN",
                    }
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            config = DEFAULT_CONFIG.copy()
            config["bot_state_path"] = str(Path(tmpdir) / "bot_state.json")
            executor = StubExecutor()
            runner = ReplayRunner(
                config=config,
                bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", analysis_interval_minutes=240, once=True),
                executor=executor,
            )
            result = runner.run_replay("2026-04-07 00:00", "2026-04-07 16:00", data_source="hyperliquid")

            self.assertEqual(result["data_source"], "hyperliquid")
            self.assertEqual(executor.calls[0], ("BTC", "2026-04-07 00:00", "2026-04-07 16:00", "1h"))

    def test_replay_regime_uses_replay_bars_not_vendor_symbol_fetch(self):
        class StubExecutor:
            def get_historical_ohlcv(self, symbol, *, start_time, end_time, timeframe):
                base = pd.Timestamp("2026-04-01 00:00", tz="UTC")
                rows = []
                price = 70000.0
                for idx in range(80):
                    ts = base + pd.Timedelta(hours=idx)
                    close = price + idx * 25.0
                    rows.append(
                        {
                            "Date": ts,
                            "Open": close - 10.0,
                            "High": close + 40.0,
                            "Low": close - 40.0,
                            "Close": close,
                            "Volume": 1000 + idx,
                        }
                    )
                return pd.DataFrame(rows)

            def cancel_order(self, symbol, order_id):
                return {"status": "ok"}

        class ReplayRunner(BotRunner):
            def _run_decision(self, state, snapshot, decision_timestamp):
                return {
                    "final_trade_action": {
                        "symbol": "BTC",
                        "action": "LONG",
                        "entry_mode": "MARKET",
                        "confidence": 0.7,
                        "thesis_summary": "trend continuation",
                        "time_horizon": "1h",
                        "stop_loss": 69800,
                        "take_profit": 71600,
                        "invalidation": "lose pullback low",
                        "position_instruction": "OPEN",
                    }
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            config = DEFAULT_CONFIG.copy()
            config["bot_state_path"] = str(Path(tmpdir) / "bot_state.json")
            runner = ReplayRunner(
                config=config,
                bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", analysis_interval_minutes=240, once=True),
                executor=StubExecutor(),
            )
            result = runner.run_replay("2026-04-02 00:00", "2026-04-03 23:00", data_source="hyperliquid")

            regimes = {obs["regime_label"] for obs in result["observations"]}
            self.assertNotEqual(regimes, {"low_quality"})

    def test_regime_only_replay_skips_graph(self):
        class ReplayRunner(BotRunner):
            def _load_replay_bars(self, symbol, start_timestamp, end_timestamp, *, data_source="vendor"):
                base = pd.Timestamp("2026-04-07 00:00", tz="UTC")
                rows = []
                for idx in range(12):
                    ts = base + pd.Timedelta(hours=idx)
                    close = 70000.0 + idx * 50.0
                    rows.append(
                        {
                            "Date": ts,
                            "Open": close - 25.0,
                            "High": close + 75.0,
                            "Low": close - 75.0,
                            "Close": close,
                            "Volume": 1000 + idx,
                        }
                    )
                return pd.DataFrame(rows)

            def _run_decision(self, state, snapshot, decision_timestamp):
                raise AssertionError("graph should not run in regime-only replay")

        with tempfile.TemporaryDirectory() as tmpdir:
            config = DEFAULT_CONFIG.copy()
            config["bot_state_path"] = str(Path(tmpdir) / "bot_state.json")
            runner = ReplayRunner(
                config=config,
                bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", analysis_interval_minutes=240, once=True),
                executor=type("StubExecutor", (), {"cancel_order": lambda *args, **kwargs: {}})(),
            )
            result = runner.run_replay("2026-04-07 00:00", "2026-04-07 11:00", mode="regime-only")

            self.assertEqual(result["mode"], "regime-only")
            self.assertEqual(result["summary"]["llm_evaluated"], 0)

    def test_candidate_only_replay_skips_graph(self):
        class ReplayRunner(BotRunner):
            def _load_replay_bars(self, symbol, start_timestamp, end_timestamp, *, data_source="vendor"):
                base = pd.Timestamp("2026-04-07 00:00", tz="UTC")
                rows = []
                for idx in range(80):
                    ts = base + pd.Timedelta(hours=idx)
                    close = 70000.0 + idx * 20.0
                    rows.append(
                        {
                            "Date": ts,
                            "Open": close - 10.0,
                            "High": close + 40.0,
                            "Low": close - 40.0,
                            "Close": close,
                            "Volume": 1000 + idx,
                        }
                    )
                return pd.DataFrame(rows)

            def _run_decision(self, state, snapshot, decision_timestamp):
                raise AssertionError("graph should not run in candidate-only replay")

        with tempfile.TemporaryDirectory() as tmpdir:
            config = DEFAULT_CONFIG.copy()
            config["bot_state_path"] = str(Path(tmpdir) / "bot_state.json")
            runner = ReplayRunner(
                config=config,
                bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", analysis_interval_minutes=240, once=True),
                executor=type("StubExecutor", (), {"cancel_order": lambda *args, **kwargs: {}})(),
            )
            result = runner.run_replay("2026-04-08 00:00", "2026-04-09 23:00", mode="candidate-only")

            self.assertEqual(result["mode"], "candidate-only")
            self.assertEqual(result["summary"]["llm_evaluated"], 0)

    def test_sqlite_journal_records_live_cycle(self):
        class StubExecutor:
            def get_exchange_state_snapshot(self, symbol):
                return ExchangeStateSnapshot(
                    wallet_address="0xabc",
                    equity=1000,
                    available_balance=1000,
                    spot_usdc_balance=1000,
                    mark_prices={"BTC": 70000},
                    positions=[],
                    open_orders=[],
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                )

            def cancel_order(self, symbol, order_id):
                return {"status": "ok"}

        class FixedRunner(BotRunner):
            def _latest_analysis_bar(self):
                return "2026-04-07 12:00"

            def _classify_regime(self, symbol, decision_timestamp):
                return BotRunnerPlannerTests._tradable_regime()

            def _detect_candidate(self, symbol, decision_timestamp, regime, *, replay_bars=None):
                return BotRunnerPlannerTests._positive_candidate("SHORT")

            def _run_decision(self, state, snapshot, decision_timestamp):
                return {
                    "final_trade_action": {
                        "symbol": "BTC",
                        "action": "FLAT",
                        "entry_mode": "MARKET",
                        "confidence": 0.0,
                        "thesis_summary": "stand aside",
                        "time_horizon": "1h",
                        "stop_loss": None,
                        "take_profit": None,
                        "invalidation": "n/a",
                        "position_instruction": "NO_ACTION",
                    }
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            config = DEFAULT_CONFIG.copy()
            config["bot_state_path"] = str(Path(tmpdir) / "bot_state.json")
            journal_path = Path(tmpdir) / "bot_journal.sqlite"
            config["bot_journal_path"] = str(journal_path)
            runner = FixedRunner(
                config=config,
                bot_config=BotConfig(symbol="BTC-USD", timeframe="1h", once=True),
                executor=StubExecutor(),
            )

            runner.run_once()

            with sqlite3.connect(journal_path) as conn:
                row = conn.execute(
                    "SELECT symbol, timeframe, regime_label, candidate_setup_present, outcome, final_action "
                    "FROM bot_cycle_journal ORDER BY id DESC LIMIT 1"
                ).fetchone()

            self.assertEqual(row[0], "BTC")
            self.assertEqual(row[1], "1h")
            self.assertEqual(row[2], "trend_down")
            self.assertEqual(row[3], 1)
            self.assertEqual(row[4], "no_action")
            self.assertEqual(row[5], "FLAT")


if __name__ == "__main__":
    unittest.main()
