import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from tradingagents.execution import (
    DecisionParseError,
    DecisionParser,
    EntryMode,
    ExecutionMode,
    HyperliquidExecutor,
    OrderIntent,
    PaperBroker,
    Position,
    RiskEngine,
    RiskEvaluationError,
    TradeAction,
)
from tradingagents.dataflows.stockstats_utils import (
    _clean_dataframe,
    get_cutoff_timestamp,
    get_indicator_analysis_window_days,
    get_indicator_compute_window_days,
    resample_ohlcv,
)


RAW_DECISION = """
STRUCTURED_DECISION
```json
{
  "symbol": "BTC-USD",
  "timestamp": "2026-04-02",
  "action": "LONG",
  "confidence": 0.73,
  "thesis_summary": "Momentum and sentiment align to the upside.",
  "time_horizon": "4h",
  "stop_loss": 82000,
  "take_profit": 90000,
  "invalidation": "Breakdown below recent support.",
  "size_hint": "small"
}
```
EXECUTIVE_SUMMARY
Take the trade.
"""

PROSE_ONLY_FLAT = """
As an AI model, I must synthesize the conflicting advice provided by the various expert perspectives while respecting the immediate operational constraint: adopt a defensive posture and prioritize capital preservation.

Actionable Recommendation:
Maintain high liquidity, avoid speculative bets, and prioritize capital retention over aggressive growth.
"""

OBSERVE_HOLD_FLAT = """
Investment Strategy Report: Bitcoin (BTC/USD)

Recommendation: Observe/Hold (Await Confirmation Signal)

Our core strategy is therefore to adopt a defensive, observation-based stance. We recommend maintaining current exposure or exiting short-term speculative positions to wait for a clear breakout or breakdown signal.

Actionable Takeaway: Avoid aggressive directional bets until market participants confirm agreement on the next macro move.

Based on the synthesis of technical stagnation, elevated uncertainty, and the need for confirmation, our primary recommendation is Observation (Wait and Watch).
"""

MALFORMED_JSON_DECISION = """
STRUCTURED_DECISION
```json
{
  'symbol': 'BTC-USD',
  'timestamp': '2026-04-02 11:00',
  'action': 'SHORT',
  'entry_mode': 'LIMIT_ZONE',
  'entry_price': null,
  'entry_zone_low': 67500,
  'entry_zone_high': 68000,
  'confidence': 0.52,
  'thesis_summary': 'Fade the bounce.',
  'time_horizon': '1h',
  'stop_loss': 68500,
  'take_profit': 64000,
  'invalidation': 'Hourly close above 68500.',
  'size_hint': 'small',
}
```
"""


class DecisionParserTests(unittest.TestCase):
    def test_parse_structured_decision(self):
        decision = DecisionParser.parse(RAW_DECISION)
        self.assertEqual(decision.symbol, "BTC")
        self.assertEqual(decision.action, TradeAction.LONG)
        self.assertEqual(decision.time_horizon, "4h")
        self.assertEqual(decision.entry_mode, EntryMode.MARKET)

    def test_parse_rejects_missing_json(self):
        with self.assertRaises(DecisionParseError):
            DecisionParser.parse("No JSON here")

    def test_parse_recovers_from_prose_only_flat(self):
        decision = DecisionParser.parse(
            PROSE_ONLY_FLAT,
            fallback_symbol="BTC-USD",
            fallback_timestamp="2026-04-06 11:00",
            fallback_time_horizon="4h",
        )
        self.assertEqual(decision.symbol, "BTC")
        self.assertEqual(decision.action, TradeAction.FLAT)
        self.assertEqual(decision.entry_mode, EntryMode.MARKET)
        self.assertIsNone(decision.stop_loss)
        self.assertIsNone(decision.take_profit)
        self.assertEqual(decision.time_horizon, "4h")

    def test_parse_recovers_from_malformed_json(self):
        decision = DecisionParser.parse(MALFORMED_JSON_DECISION)
        self.assertEqual(decision.symbol, "BTC")
        self.assertEqual(decision.action, TradeAction.SHORT)
        self.assertEqual(decision.entry_mode, EntryMode.LIMIT_ZONE)

    def test_parse_recovers_from_observe_hold_language(self):
        decision = DecisionParser.parse(
            OBSERVE_HOLD_FLAT,
            fallback_symbol="BTC-USD",
            fallback_timestamp="2026-04-06 11:00",
            fallback_time_horizon="1h",
        )
        self.assertEqual(decision.action, TradeAction.FLAT)
        self.assertEqual(decision.entry_mode, EntryMode.MARKET)
        self.assertEqual(decision.time_horizon, "1h")


class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = RiskEngine(
            bankroll=1000,
            max_risk_per_trade_pct=0.01,
            max_leverage=2,
            min_notional_usd=10.0,
            allowed_symbols=("BTC", "ETH"),
            single_position_mode=True,
            decision_timeframe="4h",
        )
        self.decision = DecisionParser.parse(RAW_DECISION)

    def test_build_order_intent(self):
        intent = self.engine.build_order_intent(
            self.decision,
            reference_price=85000,
            mode=ExecutionMode.PAPER,
        )
        self.assertEqual(intent.symbol, "BTC")
        self.assertEqual(intent.action, TradeAction.LONG)
        self.assertGreater(intent.size, 0)

    def test_floors_to_min_notional_instead_of_rejecting(self):
        engine = RiskEngine(
            bankroll=1000,
            max_risk_per_trade_pct=0.00001,
            max_leverage=2,
            min_notional_usd=10.0,
            allowed_symbols=("BTC", "ETH"),
            single_position_mode=True,
            decision_timeframe="4h",
        )
        intent = engine.build_order_intent(
            self.decision,
            reference_price=85000,
            mode=ExecutionMode.PAPER,
        )
        self.assertGreaterEqual(intent.size * 85000, 10.0)
        self.assertLess(intent.size * 85000, 10.1)
        self.assertIn("minimum notional", intent.rationale)

    def test_rejects_wrong_time_horizon(self):
        decision = self.decision.model_copy(update={"time_horizon": "1h"})
        with self.assertRaises(RiskEvaluationError):
            self.engine.build_order_intent(
                decision,
                reference_price=85000,
                mode=ExecutionMode.PAPER,
            )

    def test_rejects_entry_too_far_from_market_for_timeframe(self):
        engine = RiskEngine(
            bankroll=1000,
            max_risk_per_trade_pct=0.01,
            max_leverage=2,
            min_notional_usd=10.0,
            allowed_symbols=("BTC", "ETH"),
            single_position_mode=True,
            decision_timeframe="1h",
            max_entry_distance_pct=0.05,
        )
        decision = DecisionParser.parse(MALFORMED_JSON_DECISION).model_copy(
            update={
                "entry_zone_low": 79200.0,
                "entry_zone_high": 79650.0,
                "stop_loss": 80150.0,
                "take_profit": 78100.0,
            }
        )
        with self.assertRaises(RiskEvaluationError):
            engine.build_order_intent(
                decision,
                reference_price=71890.0,
                mode=ExecutionMode.PAPER,
            )

    def test_flat_ignores_time_horizon_mismatch_when_no_position_exists(self):
        decision = DecisionParser.parse(
            OBSERVE_HOLD_FLAT,
            fallback_symbol="BTC-USD",
            fallback_timestamp="2026-04-06 11:00",
            fallback_time_horizon="1h",
        )
        intent = self.engine.build_order_intent(
            decision,
            reference_price=85000,
            mode=ExecutionMode.PAPER,
            open_position=None,
        )
        self.assertEqual(intent.action, TradeAction.FLAT)
        self.assertEqual(intent.size, 0.0)

    def test_flat_with_open_position_does_not_require_positive_bankroll(self):
        engine = RiskEngine(
            bankroll=0.0,
            max_risk_per_trade_pct=0.01,
            max_leverage=2,
            min_notional_usd=10.0,
            allowed_symbols=("BTC", "ETH"),
            single_position_mode=True,
            decision_timeframe="1h",
        )
        decision = DecisionParser.parse(
            OBSERVE_HOLD_FLAT,
            fallback_symbol="BTC-USD",
            fallback_timestamp="2026-04-06 11:00",
            fallback_time_horizon="1h",
        )
        position = Position(
            symbol="BTC",
            side=TradeAction.LONG,
            size=0.01,
            entry_price=70000,
            stop_loss=None,
            take_profit=None,
            opened_at="2026-04-06T10:00:00+00:00",
            mode=ExecutionMode.LIVE,
        )
        intent = engine.build_order_intent(
            decision,
            reference_price=69900,
            mode=ExecutionMode.LIVE,
            open_position=position,
        )
        self.assertEqual(intent.action, TradeAction.FLAT)
        self.assertEqual(intent.size, 0.01)


class PaperBrokerTests(unittest.TestCase):
    def test_paper_broker_open_and_close(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            broker = PaperBroker(Path(tmpdir) / "ledger.json")
            engine = RiskEngine(
                bankroll=1000,
                max_risk_per_trade_pct=0.01,
                max_leverage=2,
                min_notional_usd=10.0,
                allowed_symbols=("BTC", "ETH"),
                single_position_mode=True,
                decision_timeframe="4h",
            )
            decision = DecisionParser.parse(RAW_DECISION)
            open_intent = engine.build_order_intent(
                decision,
                reference_price=85000,
                mode=ExecutionMode.PAPER,
            )
            open_preview = broker.execute(open_intent)
            self.assertEqual(open_preview.status.value, "filled")
            self.assertIsNotNone(broker.get_open_position())

            close_decision = decision.model_copy(
                update={
                    "action": TradeAction.FLAT,
                    "stop_loss": None,
                    "take_profit": None,
                }
            )
            close_intent = engine.build_order_intent(
                close_decision,
                reference_price=85100,
                mode=ExecutionMode.PAPER,
                open_position=broker.get_open_position(),
            )
            close_preview = broker.execute(close_intent)
            self.assertEqual(close_preview.status.value, "filled")
            self.assertIsNone(broker.get_open_position())

    def test_paper_limit_zone_stays_pending_until_price_reaches_zone(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            broker = PaperBroker(Path(tmpdir) / "ledger.json")
            engine = RiskEngine(
                bankroll=1000,
                max_risk_per_trade_pct=0.01,
                max_leverage=2,
                min_notional_usd=10.0,
                allowed_symbols=("BTC", "ETH"),
                single_position_mode=True,
                decision_timeframe="4h",
            )
            decision = DecisionParser.parse(RAW_DECISION).model_copy(
                update={
                    "action": TradeAction.SHORT,
                    "entry_mode": EntryMode.LIMIT_ZONE,
                    "entry_zone_low": 69000,
                    "entry_zone_high": 69500,
                    "stop_loss": 70500,
                    "take_profit": 64700,
                }
            )
            intent = engine.build_order_intent(
                decision,
                reference_price=66944.5,
                mode=ExecutionMode.PAPER,
            )
            preview = broker.execute(intent)
            self.assertEqual(preview.status.value, "preview")
            self.assertIsNone(broker.get_open_position())
            self.assertIsNotNone(broker.get_pending_order("BTC"))

    def test_paper_pending_limit_zone_fills_on_later_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            broker = PaperBroker(Path(tmpdir) / "ledger.json")
            engine = RiskEngine(
                bankroll=1000,
                max_risk_per_trade_pct=0.01,
                max_leverage=2,
                allowed_symbols=("BTC", "ETH"),
                single_position_mode=True,
                decision_timeframe="4h",
            )
            decision = DecisionParser.parse(RAW_DECISION).model_copy(
                update={
                    "action": TradeAction.SHORT,
                    "entry_mode": EntryMode.LIMIT_ZONE,
                    "entry_zone_low": 69000,
                    "entry_zone_high": 69500,
                    "stop_loss": 70500,
                    "take_profit": 64700,
                }
            )
            staged_intent = engine.build_order_intent(
                decision,
                reference_price=66944.5,
                mode=ExecutionMode.PAPER,
            )
            staged_preview = broker.execute(staged_intent)
            self.assertEqual(staged_preview.status.value, "preview")
            self.assertIsNotNone(broker.get_pending_order("BTC"))

            flat_decision = decision.model_copy(
                update={"action": TradeAction.FLAT, "stop_loss": None, "take_profit": None}
            )
            reconcile_intent = engine.build_order_intent(
                flat_decision,
                reference_price=69250,
                mode=ExecutionMode.PAPER,
                open_position=None,
            )
            reconcile_preview = broker.execute(reconcile_intent)
            self.assertEqual(reconcile_preview.status.value, "filled")
            self.assertIsNone(broker.get_open_position())
            self.assertIsNone(broker.get_pending_order("BTC"))
            ledger = json.loads((Path(tmpdir) / "ledger.json").read_text())
            messages = [entry["result"]["message"] for entry in ledger["executions"]]
            self.assertIn("Paper limit order filled from pending state.", messages)

    def test_paper_position_auto_closes_on_take_profit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            broker = PaperBroker(Path(tmpdir) / "ledger.json")
            engine = RiskEngine(
                bankroll=1000,
                max_risk_per_trade_pct=0.01,
                max_leverage=2,
                allowed_symbols=("BTC", "ETH"),
                single_position_mode=True,
                decision_timeframe="4h",
            )
            decision = DecisionParser.parse(RAW_DECISION)
            open_intent = engine.build_order_intent(
                decision,
                reference_price=85000,
                mode=ExecutionMode.PAPER,
            )
            broker.execute(open_intent)
            self.assertIsNotNone(broker.get_open_position())

            flat_decision = decision.model_copy(
                update={"action": TradeAction.FLAT, "stop_loss": None, "take_profit": None}
            )
            reconcile_intent = engine.build_order_intent(
                flat_decision,
                reference_price=90050,
                mode=ExecutionMode.PAPER,
                open_position=None,
            )
            preview = broker.execute(reconcile_intent)
            self.assertEqual(preview.status.value, "skipped")
            self.assertIsNone(broker.get_open_position())
            ledger = json.loads((Path(tmpdir) / "ledger.json").read_text())
            messages = [entry["result"]["message"] for entry in ledger["executions"]]
            self.assertTrue(
                any("take profit hit" in message for message in messages),
                messages,
            )


class HyperliquidExecutorTests(unittest.TestCase):
    def _build_executor(self, exchange):
        executor = HyperliquidExecutor.__new__(HyperliquidExecutor)
        executor.exchange = exchange
        executor.info = None
        executor.wallet_address = "0xabc"
        executor.private_key = "key"
        executor.testnet = True
        executor.base_url = "https://api.hyperliquid-testnet.xyz"
        return executor

    def test_live_limit_entry_uses_native_tpsl_grouping(self):
        class StubExchange:
            def __init__(self):
                self.updated_leverage = None
                self.bulk_request = None
                self.bulk_grouping = None

            def update_leverage(self, leverage, symbol, is_cross=True):
                self.updated_leverage = (leverage, symbol, is_cross)

            def bulk_orders(self, order_requests, grouping="na"):
                self.bulk_request = order_requests
                self.bulk_grouping = grouping
                return {
                    "response": {
                        "data": {
                            "statuses": [
                                {"resting": {"oid": 101}},
                                {"resting": {"oid": 102}},
                                {"resting": {"oid": 103}},
                            ]
                        }
                    }
                }

        exchange = StubExchange()
        executor = self._build_executor(exchange)
        intent = OrderIntent(
            mode=ExecutionMode.LIVE,
            symbol="BTC",
            action=TradeAction.SHORT,
            size=0.01,
            reference_price=68300,
            entry_mode=EntryMode.LIMIT_ZONE,
            limit_zone_low=69200,
            limit_zone_high=69400,
            leverage=1,
            stop_loss=70100,
            take_profit=67000,
            confidence=0.61,
            thesis_summary="Short the bounce.",
            time_horizon="1h",
            invalidation="Hourly close above resistance.",
            decision_timestamp="2026-04-07 16:00",
            rationale="test",
        )

        preview = executor.execute(intent)

        self.assertEqual(exchange.updated_leverage, (1, "BTC", True))
        self.assertEqual(exchange.bulk_grouping, "normalTpsl")
        self.assertEqual(len(exchange.bulk_request), 3)
        parent, tp, sl = exchange.bulk_request
        self.assertEqual(parent["coin"], "BTC")
        self.assertFalse(parent["is_buy"])
        self.assertEqual(parent["limit_px"], 69200)
        self.assertEqual(parent["order_type"], {"limit": {"tif": "Gtc"}})
        self.assertFalse(parent["reduce_only"])
        self.assertTrue(tp["is_buy"])
        self.assertTrue(tp["reduce_only"])
        self.assertEqual(tp["order_type"]["trigger"]["tpsl"], "tp")
        self.assertEqual(sl["order_type"]["trigger"]["tpsl"], "sl")
        self.assertEqual(preview.order_id, "101")
        self.assertEqual(preview.message, "Submitted live entry with native TP/SL bracket.")

    def test_live_market_entry_uses_ioc_parent_for_native_tpsl_grouping(self):
        class StubExchange:
            def __init__(self):
                self.bulk_request = None

            def update_leverage(self, leverage, symbol, is_cross=True):
                pass

            def _slippage_price(self, name, is_buy, slippage, px=None):
                return 68100.0

            def bulk_orders(self, order_requests, grouping="na"):
                self.bulk_request = order_requests
                return {"response": {"data": {"statuses": [{"filled": {"oid": 201}}]}}}

        exchange = StubExchange()
        executor = self._build_executor(exchange)
        intent = OrderIntent(
            mode=ExecutionMode.LIVE,
            symbol="BTC",
            action=TradeAction.LONG,
            size=0.01,
            reference_price=68000,
            entry_mode=EntryMode.MARKET,
            leverage=2,
            stop_loss=67000,
            take_profit=70000,
            confidence=0.7,
            thesis_summary="Breakout long.",
            time_horizon="1h",
            invalidation="Lose breakout level.",
            decision_timestamp="2026-04-07 16:00",
            rationale="test",
        )

        preview = executor.execute(intent)

        parent = exchange.bulk_request[0]
        self.assertEqual(parent["order_type"], {"limit": {"tif": "Ioc"}})
        self.assertEqual(parent["limit_px"], 68100.0)
        self.assertEqual(preview.order_id, "201")

    def test_live_orders_round_size_down_to_five_decimals(self):
        class StubExchange:
            def __init__(self):
                self.bulk_request = None

            def update_leverage(self, leverage, symbol, is_cross=True):
                pass

            def bulk_orders(self, order_requests, grouping="na"):
                self.bulk_request = order_requests
                return {"response": {"data": {"statuses": [{"resting": {"oid": 401}}]}}}

        exchange = StubExchange()
        executor = self._build_executor(exchange)
        intent = OrderIntent(
            mode=ExecutionMode.LIVE,
            symbol="BTC",
            action=TradeAction.LONG,
            size=0.019839,
            reference_price=72900,
            entry_mode=EntryMode.LIMIT,
            limit_price=72900,
            leverage=2,
            stop_loss=72400,
            take_profit=73900,
            confidence=0.62,
            thesis_summary="Buy the reclaim.",
            time_horizon="1h",
            invalidation="Lose support.",
            decision_timestamp="2026-04-10 20:00",
            rationale="test",
        )

        preview = executor.execute(intent)

        self.assertEqual(exchange.bulk_request[0]["sz"], 0.01983)
        self.assertEqual(exchange.bulk_request[1]["sz"], 0.01983)
        self.assertEqual(exchange.bulk_request[2]["sz"], 0.01983)
        self.assertEqual(preview.size, 0.01983)

    def test_limit_zone_price_uses_zone_high_for_longs_and_zone_low_for_shorts(self):
        engine = RiskEngine(
            bankroll=1000,
            max_risk_per_trade_pct=0.01,
            max_leverage=2,
            min_notional_usd=10.0,
            allowed_symbols=("BTC", "ETH"),
            single_position_mode=True,
            decision_timeframe="1h",
        )
        long_decision = DecisionParser.parse(MALFORMED_JSON_DECISION).model_copy(
            update={
                "action": TradeAction.LONG,
                "entry_zone_low": 72800.0,
                "entry_zone_high": 73000.0,
                "stop_loss": 72400.0,
                "take_profit": 73900.0,
                "time_horizon": "1h",
            }
        )
        short_decision = long_decision.model_copy(
            update={
                "action": TradeAction.SHORT,
                "entry_zone_low": 71400.0,
                "entry_zone_high": 71600.0,
                "stop_loss": 71900.0,
                "take_profit": 70500.0,
            }
        )

        long_intent = engine.build_order_intent(
            long_decision,
            reference_price=72900.0,
            mode=ExecutionMode.PAPER,
        )
        short_intent = engine.build_order_intent(
            short_decision,
            reference_price=71500.0,
            mode=ExecutionMode.PAPER,
        )

        self.assertEqual(engine._resolve_entry_reference_price(long_decision, 72900.0), 73000.0)
        self.assertEqual(engine._resolve_entry_reference_price(short_decision, 71500.0), 71400.0)
        self.assertEqual(long_intent.limit_zone_high, 73000.0)
        self.assertEqual(short_intent.limit_zone_low, 71400.0)

    def test_extract_order_ids_ignores_non_dict_status_entries(self):
        executor = self._build_executor(exchange=None)
        raw = {
            "response": {
                "data": {
                    "statuses": [
                        "ok",
                        {"resting": {"oid": 301}},
                        {"filled": {"oid": 302}},
                    ]
                }
            }
        }
        self.assertEqual(executor._extract_order_ids(raw), ["301", "302"])

    def test_live_bracket_order_returns_rejected_when_exchange_reports_error(self):
        class StubExchange:
            def update_leverage(self, leverage, symbol, is_cross=True):
                pass

            def bulk_orders(self, order_requests, grouping="na"):
                return {
                    "status": "ok",
                    "response": {
                        "data": {
                            "statuses": [
                                {"error": "Order has invalid size."},
                            ]
                        }
                    },
                }

        executor = self._build_executor(StubExchange())
        intent = OrderIntent(
            mode=ExecutionMode.LIVE,
            symbol="BTC",
            action=TradeAction.SHORT,
            size=0.024849,
            reference_price=71357.0,
            entry_mode=EntryMode.LIMIT_ZONE,
            limit_zone_low=71300,
            limit_zone_high=71400,
            leverage=2,
            stop_loss=71900,
            take_profit=70500,
            confidence=0.61,
            thesis_summary="Short the rejection.",
            time_horizon="1h",
            invalidation="Break back above the level.",
            decision_timestamp="2026-04-09 12:00",
            rationale="test",
        )

        preview = executor.execute(intent)

        self.assertEqual(preview.status, "rejected")
        self.assertIn("invalid size", preview.message.lower())
        self.assertIsNone(preview.order_id)


class TimeframeResampleTests(unittest.TestCase):
    def test_clean_dataframe_accepts_datetime_column(self):
        data = pd.DataFrame(
            {
                "Datetime": ["2026-04-03 12:00:00", "2026-04-03 13:00:00"],
                "Open": [1.0, 2.0],
                "High": [2.0, 3.0],
                "Low": [0.5, 1.5],
                "Close": [1.5, 2.5],
                "Volume": [10, 20],
            }
        )
        cleaned = _clean_dataframe(data)
        self.assertIn("Date", cleaned.columns)
        self.assertEqual(len(cleaned), 2)

    def test_resample_ohlcv_aggregates_to_4h(self):
        data = pd.DataFrame(
            {
                "Date": pd.to_datetime(
                    [
                        "2026-04-02 01:00:00",
                        "2026-04-02 02:00:00",
                        "2026-04-02 03:00:00",
                        "2026-04-02 04:00:00",
                    ]
                ),
                "Open": [1.0, 2.0, 3.0, 4.0],
                "High": [2.0, 3.0, 4.0, 5.0],
                "Low": [0.5, 1.5, 2.5, 3.5],
                "Close": [1.5, 2.5, 3.5, 4.5],
                "Volume": [10, 20, 30, 40],
            }
        )
        result = resample_ohlcv(data, "4h")
        self.assertEqual(len(result), 1)
        bar = result.iloc[0]
        self.assertEqual(bar["Open"], 1.0)
        self.assertEqual(bar["High"], 5.0)
        self.assertEqual(bar["Low"], 0.5)
        self.assertEqual(bar["Close"], 4.5)
        self.assertEqual(bar["Volume"], 100)

    def test_resample_ohlcv_1h_passthrough(self):
        data = pd.DataFrame(
            {
                "Date": pd.to_datetime(["2026-04-02 01:00:00", "2026-04-02 02:00:00"]),
                "Open": [1.0, 2.0],
                "High": [2.0, 3.0],
                "Low": [0.5, 1.5],
                "Close": [1.5, 2.5],
                "Volume": [10, 20],
            }
        )
        result = resample_ohlcv(data, "1h")
        self.assertEqual(len(result), 2)

    def test_clean_dataframe_strips_timezone_from_date_column(self):
        data = pd.DataFrame(
            {
                "Date": ["2026-03-24T00:00:00Z", "2026-03-25T00:00:00Z"],
                "Open": [1.0, 2.0],
                "High": [2.0, 3.0],
                "Low": [0.5, 1.5],
                "Close": [1.5, 2.5],
                "Volume": [10, 20],
            }
        )
        cleaned = _clean_dataframe(data)
        self.assertIsNone(cleaned["Date"].dt.tz)

    def test_cutoff_timestamp_returns_timezone_naive_timestamp(self):
        cutoff = get_cutoff_timestamp("2026-03-24T00:00:00Z")
        self.assertIsNone(cutoff.tzinfo)

    def test_timeframe_lookback_defaults_are_shorter_for_intraday(self):
        self.assertEqual(get_indicator_analysis_window_days("1h"), 5)
        self.assertEqual(get_indicator_analysis_window_days("4h"), 10)
        self.assertEqual(get_indicator_analysis_window_days("1d"), 30)
        self.assertEqual(get_indicator_compute_window_days("1h"), 21)
        self.assertEqual(get_indicator_compute_window_days("4h"), 60)


if __name__ == "__main__":
    unittest.main()
