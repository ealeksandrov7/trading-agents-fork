import unittest
from unittest.mock import patch

import pandas as pd

from tradingagents.research.backtesting_harness import (
    BacktestingUnavailableError,
    PreparedBacktest,
    build_backtesting_frame_from_bars,
    optimize_backtesting_strategy,
    run_backtesting_strategy,
)


def _sample_ohlcv_bars(num_bars: int = 96) -> pd.DataFrame:
    rows = []
    base = 70_000.0
    timestamps = pd.date_range("2026-03-01 00:00", periods=num_bars, freq="1h", tz="UTC")
    for idx, timestamp in enumerate(timestamps):
        drift = idx * 18.0
        pullback = -140.0 if 40 <= idx <= 44 else 0.0
        close = base + drift + pullback
        rows.append(
            {
                "Date": timestamp,
                "Open": close - 20.0,
                "High": close + 35.0,
                "Low": close - 45.0,
                "Close": close,
                "Volume": 1000.0 + idx,
            }
        )
    return pd.DataFrame(rows)


class BacktestingHarnessTests(unittest.TestCase):
    def test_build_backtesting_frame_from_bars_adds_expected_columns(self):
        prepared = build_backtesting_frame_from_bars(
            _sample_ohlcv_bars(),
            symbol="BTC-USD",
            timeframe="1h",
            strategy_name="trend_pullback",
            start_timestamp="2026-03-01 00:00",
            end_timestamp="2026-03-04 23:00",
        )

        self.assertIsInstance(prepared, PreparedBacktest)
        self.assertIn("analysis_bar", prepared.frame.columns)
        self.assertIn("deterministic_action_generated", prepared.frame.columns)
        self.assertIn("signal", prepared.frame.columns)
        self.assertIn("entry_price", prepared.frame.columns)
        self.assertGreater(prepared.summary["bars"], 0)
        self.assertGreater(prepared.summary["analysis_bars"], 0)
        self.assertTrue(prepared.summary["higher_timeframe_filter_enabled"])

    def test_range_fade_summary_disables_higher_timeframe_filter(self):
        prepared = build_backtesting_frame_from_bars(
            _sample_ohlcv_bars(),
            symbol="BTC-USD",
            timeframe="1h",
            strategy_name="range_fade",
            start_timestamp="2026-03-01 00:00",
            end_timestamp="2026-03-04 23:00",
        )

        self.assertFalse(prepared.summary["higher_timeframe_filter_enabled"])

    def test_run_backtesting_strategy_surfaces_missing_dependency_cleanly(self):
        prepared = PreparedBacktest(
            frame=build_backtesting_frame_from_bars(
                _sample_ohlcv_bars(),
                symbol="BTC-USD",
                timeframe="1h",
                strategy_name="trend_pullback",
                start_timestamp="2026-03-01 00:00",
                end_timestamp="2026-03-04 23:00",
            ).frame,
            summary={"bars": 96, "analysis_bars": 96, "candidate_bars": 0, "deterministic_actions": 0, "higher_timeframe_filter_enabled": True},
        )
        with patch(
            "tradingagents.research.backtesting_harness._import_backtesting_classes",
            side_effect=BacktestingUnavailableError("missing"),
        ):
            with self.assertRaises(BacktestingUnavailableError):
                run_backtesting_strategy(
                    symbol="BTC-USD",
                    timeframe="1h",
                    start_timestamp="2026-03-01 00:00",
                    end_timestamp="2026-03-04 23:00",
                    strategy_name="trend_pullback",
                    prepared=prepared,
                )

    def test_optimize_backtesting_strategy_ranks_parameter_grid(self):
        sample_bars = _sample_ohlcv_bars()

        def fake_run_backtesting_strategy(**kwargs):
            cfg = kwargs["config"]
            entry_style = cfg["bot_deterministic_trend_pullback_entry_style"]
            target_r = cfg["bot_deterministic_trend_pullback_target_r_multiple"]
            expiry = cfg["bot_deterministic_trend_pullback_expiry_bars"]
            score = 10.0
            if entry_style == "near_price":
                score += 3.0
            score -= abs(target_r - 1.5)
            score -= abs(expiry - 5) * 0.25
            return {
                "stats": {
                    "Return [%]": score,
                    "# Trades": 12,
                    "Win Rate [%]": 55.0,
                    "Profit Factor": 1.4,
                    "Max. Drawdown [%]": -4.0,
                    "Buy & Hold Return [%]": 7.0,
                },
                "prepared_summary": {
                    "candidate_bars": 10,
                    "deterministic_actions": 8,
                },
            }

        with patch(
            "tradingagents.research.backtesting_harness.run_backtesting_strategy",
            side_effect=fake_run_backtesting_strategy,
        ):
            result = optimize_backtesting_strategy(
                symbol="BTC-USD",
                timeframe="1h",
                start_timestamp="2026-03-01 00:00",
                end_timestamp="2026-03-04 23:00",
                strategy_name="trend_pullback",
                bars=sample_bars,
                parameter_grid={
                    "target_r": [1.0, 1.5],
                    "expiry_bars": [5],
                    "entry_style": ["midpoint", "near_price"],
                },
            )

        self.assertEqual(result["evaluated"], 4)
        self.assertEqual(result["ranked_results"][0]["params"]["entry_style"], "near_price")
        self.assertEqual(result["ranked_results"][0]["params"]["target_r"], 1.5)


if __name__ == "__main__":
    unittest.main()
