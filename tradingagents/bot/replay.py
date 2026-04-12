from __future__ import annotations

from collections import defaultdict
from datetime import timezone
from typing import Any, Iterable, Optional

import pandas as pd

from tradingagents.execution import TradeAction


def evaluate_replay_observation(
    bars: pd.DataFrame,
    decision_timestamp: str,
    action: dict[str, Any],
    *,
    setup_expiry_bars_default: int,
) -> dict[str, Any]:
    frame = bars.sort_values("Date").reset_index(drop=True).copy()
    frame["Date"] = pd.to_datetime(frame["Date"], utc=True, errors="coerce")
    ts = pd.to_datetime(decision_timestamp, utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"invalid replay timestamp: {decision_timestamp}")

    current_idx = frame.index[frame["Date"] == ts]
    if len(current_idx) == 0:
        raise ValueError(f"timestamp {decision_timestamp} not found in replay bars")
    current_idx = int(current_idx[0])

    base = {
        "decision_timestamp": ts.isoformat(),
        "executed": False,
        "execution_status": "SKIPPED",
        "fill_timestamp": None,
        "fill_price": None,
        "mae_r": None,
        "mfe_r": None,
        "r_1": None,
        "r_2": None,
        "r_4": None,
        "r_8": None,
    }

    if not action or str(action.get("action", "")).upper() == TradeAction.FLAT.value:
        return base

    side = str(action["action"]).upper()
    expiry_bars = int(action.get("setup_expiry_bars") or setup_expiry_bars_default)
    fill = _find_fill(frame, current_idx, action, side, expiry_bars)
    if fill is None:
        return base

    fill_idx, fill_price = fill
    metrics = _compute_trade_metrics(frame, fill_idx, fill_price, action, side)
    return {
        **base,
        "executed": True,
        "execution_status": "FILLED",
        "fill_timestamp": frame.iloc[fill_idx]["Date"].isoformat(),
        "fill_price": fill_price,
        **metrics,
    }


def summarize_replay(observations: Iterable[dict[str, Any]]) -> dict[str, Any]:
    obs = list(observations)
    summary: dict[str, Any] = {
        "total_decisions": len(obs),
        "executed": 0,
        "skipped": 0,
        "llm_evaluated": 0,
        "by_regime": {},
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in obs:
        grouped[str(item.get("regime_label") or "unknown")].append(item)
        if item.get("llm_evaluated"):
            summary["llm_evaluated"] += 1
        if item.get("executed"):
            summary["executed"] += 1
        else:
            summary["skipped"] += 1

    for regime, items in grouped.items():
        executed = [item for item in items if item.get("executed")]
        summary["by_regime"][regime] = {
            "total": len(items),
            "executed": len(executed),
            "skipped": len(items) - len(executed),
            "avg_r_4": _average_metric(executed, "r_4"),
            "avg_r_8": _average_metric(executed, "r_8"),
            "avg_mfe_r": _average_metric(executed, "mfe_r"),
            "avg_mae_r": _average_metric(executed, "mae_r"),
        }
    return summary


def _find_fill(
    frame: pd.DataFrame,
    current_idx: int,
    action: dict[str, Any],
    side: str,
    expiry_bars: int,
) -> Optional[tuple[int, float]]:
    entry_mode = str(action.get("entry_mode") or "MARKET").upper()
    if entry_mode == "MARKET":
        return current_idx, float(frame.iloc[current_idx]["Close"])

    max_idx = min(len(frame) - 1, current_idx + max(expiry_bars, 1))
    window = frame.iloc[current_idx + 1 : max_idx + 1]
    if window.empty:
        return None

    if entry_mode == "LIMIT":
        limit_price = float(action["entry_price"])
        for idx, bar in window.iterrows():
            if side == TradeAction.LONG.value and float(bar["Low"]) <= limit_price:
                return int(idx), limit_price
            if side == TradeAction.SHORT.value and float(bar["High"]) >= limit_price:
                return int(idx), limit_price
        return None

    zone_low = float(action["entry_zone_low"])
    zone_high = float(action["entry_zone_high"])
    zone_mid = (zone_low + zone_high) / 2.0
    for idx, bar in window.iterrows():
        bar_low = float(bar["Low"])
        bar_high = float(bar["High"])
        if bar_low <= zone_high and bar_high >= zone_low:
            return int(idx), zone_mid
    return None


def _compute_trade_metrics(
    frame: pd.DataFrame,
    fill_idx: int,
    fill_price: float,
    action: dict[str, Any],
    side: str,
) -> dict[str, Any]:
    stop_loss = float(action["stop_loss"])
    risk = abs(fill_price - stop_loss)
    if risk <= 0:
        return {"mae_r": None, "mfe_r": None, "r_1": None, "r_2": None, "r_4": None, "r_8": None}

    future = frame.iloc[fill_idx + 1 : fill_idx + 9].copy()
    if future.empty:
        return {"mae_r": None, "mfe_r": None, "r_1": None, "r_2": None, "r_4": None, "r_8": None}

    if side == TradeAction.LONG.value:
        mae_r = (float(future["Low"].min()) - fill_price) / risk
        mfe_r = (float(future["High"].max()) - fill_price) / risk
        r_values = {
            f"r_{horizon}": _close_r(frame, fill_idx, fill_price, risk, horizon, long_side=True)
            for horizon in (1, 2, 4, 8)
        }
    else:
        mae_r = (fill_price - float(future["High"].max())) / risk
        mfe_r = (fill_price - float(future["Low"].min())) / risk
        r_values = {
            f"r_{horizon}": _close_r(frame, fill_idx, fill_price, risk, horizon, long_side=False)
            for horizon in (1, 2, 4, 8)
        }
    return {"mae_r": mae_r, "mfe_r": mfe_r, **r_values}


def _close_r(
    frame: pd.DataFrame,
    fill_idx: int,
    fill_price: float,
    risk: float,
    horizon: int,
    *,
    long_side: bool,
) -> Optional[float]:
    idx = fill_idx + horizon
    if idx >= len(frame):
        return None
    close_price = float(frame.iloc[idx]["Close"])
    if long_side:
        return (close_price - fill_price) / risk
    return (fill_price - close_price) / risk


def _average_metric(items: list[dict[str, Any]], key: str) -> Optional[float]:
    values = [float(item[key]) for item in items if item.get(key) is not None]
    if not values:
        return None
    return sum(values) / len(values)
