import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langchain_core.messages import ToolMessage

from tradingagents.bot.models import BotConfig, BotState
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


if __name__ == "__main__":
    unittest.main()
