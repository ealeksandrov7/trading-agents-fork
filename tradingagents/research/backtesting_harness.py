from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any, Optional

import pandas as pd

from tradingagents.bot import BotConfig, BotRunner
from tradingagents.bot.regime import apply_higher_timeframe_filter
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.execution import HyperliquidExecutionError


SUPPORTED_BACKTEST_STRATEGIES = {"trend_pullback", "range_fade"}


class BacktestingUnavailableError(RuntimeError):
    """Raised when the optional backtesting.py dependency is unavailable."""


@dataclass
class PreparedBacktest:
    frame: pd.DataFrame
    summary: dict[str, Any]


def build_backtesting_frame(
    *,
    symbol: str,
    timeframe: str,
    start_timestamp: str,
    end_timestamp: str,
    strategy_name: str,
    data_source: str = "vendor",
    config: Optional[dict[str, Any]] = None,
    executor: Any = None,
    analysis_interval_minutes: int = 60,
    testnet: bool = True,
) -> PreparedBacktest:
    """Load historical data and precompute deterministic strategy actions per bar."""
    strategy = _normalize_strategy_name(strategy_name)
    merged_config = DEFAULT_CONFIG.copy()
    if config:
        merged_config.update(config)
    merged_config["analysis_timeframe"] = timeframe
    merged_config["decision_timeframe"] = timeframe
    merged_config["hyperliquid_testnet"] = testnet
    merged_config["bot_analysis_interval_minutes"] = analysis_interval_minutes

    runner = _build_runner(
        symbol=symbol,
        timeframe=timeframe,
        config=merged_config,
        executor=executor,
        analysis_interval_minutes=analysis_interval_minutes,
        testnet=testnet,
    )
    bars = _load_backtesting_bars(
        runner,
        symbol=symbol,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        data_source=data_source,
    )
    return build_backtesting_frame_from_bars(
        bars,
        symbol=symbol,
        timeframe=timeframe,
        strategy_name=strategy,
        config=merged_config,
        runner=runner,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )


def build_backtesting_frame_from_bars(
    bars: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    strategy_name: str,
    config: Optional[dict[str, Any]] = None,
    runner: Optional[BotRunner] = None,
    start_timestamp: Optional[str] = None,
    end_timestamp: Optional[str] = None,
) -> PreparedBacktest:
    """Precompute deterministic strategy actions from already-loaded OHLCV bars."""
    strategy = _normalize_strategy_name(strategy_name)
    merged_config = DEFAULT_CONFIG.copy()
    if config:
        merged_config.update(config)

    prepared_runner = runner or _build_runner(
        symbol=symbol,
        timeframe=timeframe,
        config=merged_config,
        executor=_NoopExecutor(),
        analysis_interval_minutes=60,
        testnet=bool(merged_config.get("hyperliquid_testnet", True)),
    )

    frame = _normalize_bars_for_backtest(bars)
    timestamps = prepared_runner._replay_analysis_timestamps(
        frame.reset_index().rename(columns={"index": "Date"}),
        start_timestamp or str(frame.index.min()),
        end_timestamp or str(frame.index.max()),
    )
    analysis_lookup = {_utc_timestamp(ts) for ts in timestamps}

    signal_frame = frame.copy()
    signal_frame["analysis_bar"] = signal_frame.index.isin(analysis_lookup)
    signal_frame["strategy_name"] = strategy
    signal_frame["regime_label"] = ""
    signal_frame["higher_timeframe_label"] = ""
    signal_frame["candidate_present"] = False
    signal_frame["deterministic_action_generated"] = False
    signal_frame["signal"] = 0
    signal_frame["entry_price"] = pd.NA
    signal_frame["entry_zone_low"] = pd.NA
    signal_frame["entry_zone_high"] = pd.NA
    signal_frame["stop_loss"] = pd.NA
    signal_frame["take_profit"] = pd.NA
    signal_frame["expiry_bars"] = pd.NA
    signal_frame["reason"] = ""
    signal_frame["reward_risk_estimate"] = pd.NA

    symbol_code = symbol.replace("-USD", "")
    summary = {
        "strategy_name": strategy,
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": int(len(signal_frame)),
        "analysis_bars": int(signal_frame["analysis_bar"].sum()),
        "candidate_bars": 0,
        "deterministic_actions": 0,
        "higher_timeframe_filter_enabled": bool(
            merged_config.get("bot_higher_timeframe_filter_enabled", True) and strategy == "trend_pullback"
        ),
    }

    for ts in timestamps:
        timestamp = _utc_timestamp(ts)
        bar_window = (
            frame.loc[:timestamp]
            .reset_index()
            .rename(columns={"index": "Date"})
        )
        if bar_window.empty or timestamp not in signal_frame.index:
            continue

        regime = prepared_runner._replay_regime(symbol_code, ts.strftime("%Y-%m-%d %H:%M"), bar_window)
        higher_timeframe = prepared_runner._higher_timeframe_with_fallback(
            symbol_code,
            ts.strftime("%Y-%m-%d %H:%M"),
            bar_window,
        )
        regime = apply_higher_timeframe_filter(regime, higher_timeframe, prepared_runner.config)

        candidate = prepared_runner._candidate_with_fallback(
            symbol_code,
            ts.strftime("%Y-%m-%d %H:%M"),
            regime,
            setup_family=strategy,
            replay_bars=bar_window,
        )
        signal_frame.at[timestamp, "regime_label"] = regime.label
        signal_frame.at[timestamp, "higher_timeframe_label"] = higher_timeframe.label
        signal_frame.at[timestamp, "candidate_present"] = bool(candidate.candidate_setup_present)
        signal_frame.at[timestamp, "reason"] = candidate.reason
        signal_frame.at[timestamp, "reward_risk_estimate"] = candidate.reward_risk_estimate
        signal_frame.at[timestamp, "entry_zone_low"] = candidate.entry_zone_low
        signal_frame.at[timestamp, "entry_zone_high"] = candidate.entry_zone_high
        if candidate.candidate_setup_present:
            summary["candidate_bars"] += 1

        action, generated, reason = prepared_runner._build_deterministic_replay_action(
            symbol_code,
            ts.strftime("%Y-%m-%d %H:%M"),
            candidate,
        )
        signal_frame.at[timestamp, "reason"] = reason
        signal_frame.at[timestamp, "deterministic_action_generated"] = generated
        if not generated:
            continue

        entry_mid = _entry_midpoint(action)
        if entry_mid is None:
            continue
        summary["deterministic_actions"] += 1
        signal_frame.at[timestamp, "signal"] = 1 if action["action"] == "LONG" else -1
        signal_frame.at[timestamp, "entry_price"] = entry_mid
        signal_frame.at[timestamp, "entry_zone_low"] = action.get("entry_zone_low")
        signal_frame.at[timestamp, "entry_zone_high"] = action.get("entry_zone_high")
        signal_frame.at[timestamp, "stop_loss"] = action.get("stop_loss")
        signal_frame.at[timestamp, "take_profit"] = action.get("take_profit")
        signal_frame.at[timestamp, "expiry_bars"] = action.get("setup_expiry_bars")

    return PreparedBacktest(frame=signal_frame, summary=summary)


def run_backtesting_strategy(
    *,
    symbol: str,
    timeframe: str,
    start_timestamp: str,
    end_timestamp: str,
    strategy_name: str,
    data_source: str = "vendor",
    config: Optional[dict[str, Any]] = None,
    executor: Any = None,
    analysis_interval_minutes: int = 60,
    testnet: bool = True,
    cash: float = 10_000.0,
    commission: float = 0.0,
    prepared: Optional[PreparedBacktest] = None,
) -> dict[str, Any]:
    """Run a deterministic backtest via backtesting.py using precomputed signals."""
    strategy = _normalize_strategy_name(strategy_name)
    bt_class, strategy_base = _import_backtesting_classes()
    prepared_backtest = prepared or build_backtesting_frame(
        symbol=symbol,
        timeframe=timeframe,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        strategy_name=strategy,
        data_source=data_source,
        config=config,
        executor=executor,
        analysis_interval_minutes=analysis_interval_minutes,
        testnet=testnet,
    )
    strategy_class = _make_precomputed_strategy_class(strategy_base, strategy)
    backtest = bt_class(
        prepared_backtest.frame,
        strategy_class,
        cash=cash,
        commission=commission,
        exclusive_orders=True,
        finalize_trades=True,
    )
    stats = backtest.run()
    trades = stats.get("_trades")
    equity_curve = stats.get("_equity_curve")
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_name": strategy,
        "data_source": data_source,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "cash": cash,
        "commission": commission,
        "prepared_summary": prepared_backtest.summary,
        "stats": _serialize_stats(stats),
        "trades": [] if trades is None else _serialize_frame(trades),
        "equity_curve": [] if equity_curve is None else _serialize_frame(equity_curve),
    }


def optimize_backtesting_strategy(
    *,
    symbol: str,
    timeframe: str,
    start_timestamp: str,
    end_timestamp: str,
    strategy_name: str,
    data_source: str = "vendor",
    config: Optional[dict[str, Any]] = None,
    executor: Any = None,
    analysis_interval_minutes: int = 60,
    testnet: bool = True,
    cash: float = 10_000.0,
    commission: float = 0.0,
    maximize: str = "Return [%]",
    parameter_grid: Optional[dict[str, list[Any]]] = None,
    bars: Optional[pd.DataFrame] = None,
) -> dict[str, Any]:
    """Grid-search deterministic strategy configs and rank runs by a chosen metric."""
    strategy = _normalize_strategy_name(strategy_name)
    merged_config = DEFAULT_CONFIG.copy()
    if config:
        merged_config.update(config)
    merged_config["analysis_timeframe"] = timeframe
    merged_config["decision_timeframe"] = timeframe
    merged_config["hyperliquid_testnet"] = testnet
    merged_config["bot_analysis_interval_minutes"] = analysis_interval_minutes

    grid = _normalize_parameter_grid(strategy, parameter_grid)
    runner = _build_runner(
        symbol=symbol,
        timeframe=timeframe,
        config=merged_config,
        executor=executor,
        analysis_interval_minutes=analysis_interval_minutes,
        testnet=testnet,
    )
    source_bars = bars if bars is not None else _load_backtesting_bars(
        runner,
        symbol=symbol,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        data_source=data_source,
    )

    results: list[dict[str, Any]] = []
    for params in _parameter_combinations(grid):
        run_config = merged_config.copy()
        run_config.update(_strategy_parameter_overrides(strategy, params))
        run_runner = _build_runner(
            symbol=symbol,
            timeframe=timeframe,
            config=run_config,
            executor=_NoopExecutor(),
            analysis_interval_minutes=analysis_interval_minutes,
            testnet=testnet,
        )
        prepared = build_backtesting_frame_from_bars(
            source_bars,
            symbol=symbol,
            timeframe=timeframe,
            strategy_name=strategy,
            config=run_config,
            runner=run_runner,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        result = run_backtesting_strategy(
            symbol=symbol,
            timeframe=timeframe,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            strategy_name=strategy,
            data_source=data_source,
            config=run_config,
            analysis_interval_minutes=analysis_interval_minutes,
            testnet=testnet,
            cash=cash,
            commission=commission,
            prepared=prepared,
        )
        score = _metric_value(result["stats"].get(maximize))
        results.append(
            {
                "params": params,
                "score": score,
                "result": result,
                "summary": _optimization_summary_row(result, params, maximize, score),
            }
        )

    ranked = sorted(results, key=_optimization_sort_key, reverse=True)
    best = ranked[0] if ranked else None
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_name": strategy,
        "data_source": data_source,
        "maximize": maximize,
        "parameter_grid": grid,
        "evaluated": len(ranked),
        "best_result": None if best is None else best["result"],
        "ranked_results": [item["summary"] for item in ranked],
    }


def _build_runner(
    *,
    symbol: str,
    timeframe: str,
    config: dict[str, Any],
    executor: Any,
    analysis_interval_minutes: int,
    testnet: bool,
) -> BotRunner:
    return BotRunner(
        config=config,
        bot_config=BotConfig(
            symbol=symbol,
            timeframe=timeframe,
            once=True,
            testnet=testnet,
            analysis_interval_minutes=analysis_interval_minutes,
            reconcile_interval_seconds=int(config.get("bot_reconcile_interval_seconds", 30)),
            setup_expiry_bars_default=int(config.get("bot_setup_expiry_bars_default", 3)),
        ),
        executor=executor or _NoopExecutor(),
    )


def _load_backtesting_bars(
    runner: BotRunner,
    *,
    symbol: str,
    start_timestamp: str,
    end_timestamp: str,
    data_source: str,
) -> pd.DataFrame:
    try:
        bars = runner._load_replay_bars(
            symbol,
            start_timestamp,
            end_timestamp,
            data_source=data_source,
        )
    except HyperliquidExecutionError as exc:
        raise RuntimeError(f"Failed to load {data_source} candles for backtest: {exc}") from exc
    if bars.empty:
        raise RuntimeError("No OHLCV data returned for backtest window.")
    return bars


def _normalize_bars_for_backtest(bars: pd.DataFrame) -> pd.DataFrame:
    frame = bars.copy()
    if "Date" not in frame.columns:
        raise ValueError("Backtest bars must contain a Date column.")
    frame["Date"] = pd.to_datetime(frame["Date"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["Date"]).sort_values("Date")
    frame = frame.set_index("Date")
    for column in ["Open", "High", "Low", "Close", "Volume"]:
        if column not in frame.columns:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"])
    return frame


def _import_backtesting_classes():
    try:
        from backtesting import Backtest, Strategy
        try:
            from backtesting.lib import FractionalBacktest
        except ImportError:
            FractionalBacktest = Backtest
    except ImportError as exc:
        raise BacktestingUnavailableError(
            "backtesting.py is not installed. Install the optional 'backtesting' package to use this research harness."
        ) from exc
    return FractionalBacktest, Strategy


def _make_precomputed_strategy_class(strategy_base, strategy_name: str):
    class PrecomputedStrategy(strategy_base):
        order_fraction = 0.95

        def init(self):
            self._pending_order = None
            self._pending_expiry_bar = None

        def next(self):
            current_bar = len(self.data.Close) - 1
            if self._pending_order is not None:
                pending_still_open = self._pending_order in self.orders
                if self.position or not pending_still_open:
                    self._pending_order = None
                    self._pending_expiry_bar = None
                elif self._pending_expiry_bar is not None and current_bar > self._pending_expiry_bar:
                    self._pending_order.cancel()
                    self._pending_order = None
                    self._pending_expiry_bar = None

            if self.position or self._pending_order is not None:
                return

            if not bool(self.data.analysis_bar[-1]):
                return
            if str(self.data.strategy_name[-1]) != strategy_name:
                return
            if not bool(self.data.deterministic_action_generated[-1]):
                return

            direction = int(self.data.signal[-1])
            entry_price = self.data.entry_price[-1]
            stop_loss = self.data.stop_loss[-1]
            take_profit = self.data.take_profit[-1]
            expiry_bars = self.data.expiry_bars[-1]
            if direction == 0 or pd.isna(entry_price) or pd.isna(stop_loss) or pd.isna(take_profit):
                return

            kwargs = {
                "size": self.order_fraction,
                "limit": float(entry_price),
                "sl": float(stop_loss),
                "tp": float(take_profit),
                "tag": str(self.data.reason[-1]),
            }
            order = self.buy(**kwargs) if direction > 0 else self.sell(**kwargs)
            self._pending_order = order
            expiry = int(expiry_bars) if not pd.isna(expiry_bars) else 0
            self._pending_expiry_bar = current_bar + max(expiry - 1, 0)

    PrecomputedStrategy.__name__ = f"{strategy_name.title().replace('_', '')}BacktestStrategy"
    return PrecomputedStrategy


def _normalize_strategy_name(strategy_name: str) -> str:
    strategy = str(strategy_name or "").strip().lower()
    if strategy not in SUPPORTED_BACKTEST_STRATEGIES:
        raise ValueError(
            f"Unsupported strategy '{strategy_name}'. Expected one of: {', '.join(sorted(SUPPORTED_BACKTEST_STRATEGIES))}."
        )
    return strategy


def _normalize_parameter_grid(strategy_name: str, parameter_grid: Optional[dict[str, list[Any]]]) -> dict[str, list[Any]]:
    defaults = {
        "trend_pullback": {
            "target_r": [1.0, 1.5, 2.0],
            "expiry_bars": [3, 5, 7, 9],
            "entry_style": ["midpoint", "near_price", "deep_pullback"],
        },
        "range_fade": {
            "target_mode": ["reference", "fixed_r"],
            "target_r": [1.5, 2.0, 2.5],
            "expiry_bars": [2, 3, 4],
        },
    }
    grid = defaults[strategy_name].copy()
    if parameter_grid:
        for key, values in parameter_grid.items():
            if values:
                grid[key] = list(values)
    return grid


def _parameter_combinations(parameter_grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(parameter_grid.keys())
    values = [parameter_grid[key] for key in keys]
    return [dict(zip(keys, combo, strict=False)) for combo in product(*values)]


def _strategy_parameter_overrides(strategy_name: str, params: dict[str, Any]) -> dict[str, Any]:
    if strategy_name == "range_fade":
        return {
            "bot_deterministic_range_fade_target_mode": params.get("target_mode", "reference"),
            "bot_deterministic_range_fade_target_r_multiple": float(params.get("target_r", 2.0)),
            "bot_deterministic_range_fade_expiry_bars": int(params.get("expiry_bars", 3)),
        }
    return {
        "bot_deterministic_trend_pullback_target_r_multiple": float(params.get("target_r", 2.0)),
        "bot_deterministic_trend_pullback_expiry_bars": int(params.get("expiry_bars", 5)),
        "bot_deterministic_trend_pullback_entry_style": str(params.get("entry_style", "midpoint")).lower(),
    }


def _metric_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optimization_summary_row(
    result: dict[str, Any],
    params: dict[str, Any],
    maximize: str,
    score: Optional[float],
) -> dict[str, Any]:
    stats = result["stats"]
    prepared = result["prepared_summary"]
    return {
        "params": params,
        "maximize_metric": maximize,
        "score": score,
        "return_pct": _metric_value(stats.get("Return [%]")),
        "buy_hold_return_pct": _metric_value(stats.get("Buy & Hold Return [%]")),
        "trades": _metric_value(stats.get("# Trades")),
        "win_rate_pct": _metric_value(stats.get("Win Rate [%]")),
        "profit_factor": _metric_value(stats.get("Profit Factor")),
        "max_drawdown_pct": _metric_value(stats.get("Max. Drawdown [%]")),
        "candidate_bars": prepared.get("candidate_bars"),
        "deterministic_actions": prepared.get("deterministic_actions"),
    }


def _optimization_sort_key(item: dict[str, Any]) -> tuple[float, float]:
    score = item.get("score")
    if score is None:
        return (float("-inf"), float("-inf"))
    trades = _metric_value(item["result"]["stats"].get("# Trades")) or 0.0
    return (float(score), trades)


def _serialize_stats(stats: pd.Series) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for key, value in stats.items():
        if key in {"_trades", "_equity_curve", "_strategy"}:
            continue
        if isinstance(value, pd.Timestamp):
            serialized[str(key)] = value.isoformat()
        elif hasattr(value, "item"):
            serialized[str(key)] = value.item()
        else:
            serialized[str(key)] = value
    return serialized


def _serialize_frame(frame: pd.DataFrame) -> list[dict[str, Any]]:
    safe = frame.copy()
    for column in safe.columns:
        if pd.api.types.is_datetime64_any_dtype(safe[column]):
            safe[column] = safe[column].astype(str)
    return safe.to_dict(orient="records")


def _entry_midpoint(action: dict[str, Any]) -> Optional[float]:
    entry_price = action.get("entry_price")
    if entry_price is not None:
        return float(entry_price)
    low = action.get("entry_zone_low")
    high = action.get("entry_zone_high")
    if low is None or high is None:
        return None
    return (float(low) + float(high)) / 2.0


def _utc_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


class _NoopExecutor:
    pass
