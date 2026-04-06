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
    PaperBroker,
    RiskEngine,
    RiskEvaluationError,
    TradeAction,
)
from tradingagents.dataflows.stockstats_utils import _clean_dataframe, resample_ohlcv


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


class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = RiskEngine(
            bankroll=1000,
            max_risk_per_trade_pct=0.01,
            max_leverage=2,
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

    def test_rejects_wrong_time_horizon(self):
        decision = self.decision.model_copy(update={"time_horizon": "1h"})
        with self.assertRaises(RiskEvaluationError):
            self.engine.build_order_intent(
                decision,
                reference_price=85000,
                mode=ExecutionMode.PAPER,
            )


class PaperBrokerTests(unittest.TestCase):
    def test_paper_broker_open_and_close(self):
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


if __name__ == "__main__":
    unittest.main()
