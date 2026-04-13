from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

import pandas as pd

from tradingagents.dataflows.stockstats_utils import load_ohlcv


@dataclass
class RegimeSnapshot:
    label: str
    trade_allowed: bool
    preferred_action: str
    setup_family: str
    allowed_setup_families: list[str]
    current_price: float
    ema20: float
    ema50: float
    atr14: float
    atr_pct: float
    ema20_slope_pct: float
    trend_spread_pct: float
    realized_vol_24h: float
    bar_change_pct: float
    pullback_distance_atr: float
    pullback_zone_low: Optional[float]
    pullback_zone_high: Optional[float]
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        zone = "n/a"
        if self.pullback_zone_low is not None and self.pullback_zone_high is not None:
            zone = f"{self.pullback_zone_low:.2f}-{self.pullback_zone_high:.2f}"
        return (
            f"Regime={self.label} | trade_allowed={self.trade_allowed} | preferred_action={self.preferred_action} | "
            f"setup_family={self.setup_family} | allowed={','.join(self.allowed_setup_families) or 'none'} | "
            f"price={self.current_price:.2f} | ema20={self.ema20:.2f} | "
            f"ema50={self.ema50:.2f} | atr14={self.atr14:.2f} ({self.atr_pct:.4f}) | "
            f"ema20_slope_pct={self.ema20_slope_pct:.4f} | trend_spread_pct={self.trend_spread_pct:.4f} | "
            f"realized_vol_24h={self.realized_vol_24h:.4f} | bar_change_pct={self.bar_change_pct:.4f} | "
            f"pullback_distance_atr={self.pullback_distance_atr:.2f} | pullback_zone={zone} | reason={self.reason}"
        )


@dataclass
class HigherTimeframeTrendSnapshot:
    timeframe: str
    label: str
    preferred_action: str
    current_price: float
    ema20: float
    ema50: float
    ema20_slope_pct: float
    trend_spread_pct: float
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return (
            f"HigherTimeframe={self.timeframe} | label={self.label} | preferred_action={self.preferred_action} | "
            f"price={self.current_price:.2f} | ema20={self.ema20:.2f} | ema50={self.ema50:.2f} | "
            f"ema20_slope_pct={self.ema20_slope_pct:.4f} | trend_spread_pct={self.trend_spread_pct:.4f} | "
            f"reason={self.reason}"
        )


def allowed_strategies_for_regime(label: str, config: dict) -> list[str]:
    enabled = set(_enabled_strategy_families(config))
    route_map = _strategy_route_map(config)
    return [strategy for strategy in route_map.get(label, []) if strategy in enabled]


def classify_regime(symbol: str, trade_timestamp: str, config: dict) -> RegimeSnapshot:
    timeframe = str(config.get("analysis_timeframe", "1d")).lower()
    if timeframe != "1h":
        return _unsupported_timeframe_snapshot(config, timeframe)

    data = load_ohlcv(symbol, trade_timestamp, timeframe_override=timeframe).copy()
    return classify_regime_from_data(data, config, timeframe=timeframe)


def classify_higher_timeframe_trend(
    symbol: str,
    trade_timestamp: str,
    config: dict,
) -> HigherTimeframeTrendSnapshot:
    timeframe = str(config.get("bot_higher_timeframe_anchor_timeframe", "4h")).lower()
    data = load_ohlcv(symbol, trade_timestamp, timeframe_override=timeframe).copy()
    return classify_higher_timeframe_trend_from_data(data, config, timeframe=timeframe)


def classify_higher_timeframe_trend_from_data(
    data: pd.DataFrame,
    config: dict,
    *,
    timeframe: str = "4h",
) -> HigherTimeframeTrendSnapshot:
    timeframe = str(timeframe).lower()
    if timeframe != "4h":
        return HigherTimeframeTrendSnapshot(
            timeframe=timeframe,
            label="neutral",
            preferred_action="FLAT",
            current_price=0.0,
            ema20=0.0,
            ema50=0.0,
            ema20_slope_pct=0.0,
            trend_spread_pct=0.0,
            reason=f"Higher-timeframe trend filter currently supports 4h only; got {timeframe}.",
        )
    if data.empty or len(data) < 60:
        return HigherTimeframeTrendSnapshot(
            timeframe=timeframe,
            label="neutral",
            preferred_action="FLAT",
            current_price=0.0,
            ema20=0.0,
            ema50=0.0,
            ema20_slope_pct=0.0,
            trend_spread_pct=0.0,
            reason="Insufficient 4h OHLCV history to classify higher-timeframe trend.",
        )

    frame = _build_feature_frame(data)
    latest = frame.iloc[-1]
    current_price = _safe_float(latest["Close"])
    ema20 = _safe_float(latest["ema20"])
    ema50 = _safe_float(latest["ema50"])
    ema20_slope_pct = (
        (ema20 - _safe_float(frame.iloc[-7]["ema20"])) / current_price
        if len(frame) >= 7 and current_price > 0
        else 0.0
    )
    trend_spread_pct = abs(ema20 - ema50) / current_price if current_price > 0 else 0.0
    trend_spread_min = float(
        config.get("bot_higher_timeframe_trend_spread_min_pct", config.get("bot_regime_trend_spread_min_pct", 0.003))
    )
    trend_slope_min = float(
        config.get("bot_higher_timeframe_slope_min_pct", config.get("bot_regime_slope_min_pct", 0.0015))
    )

    if current_price > ema20 > ema50 and trend_spread_pct >= trend_spread_min and ema20_slope_pct >= trend_slope_min:
        return HigherTimeframeTrendSnapshot(
            timeframe=timeframe,
            label="trend_up",
            preferred_action="LONG",
            current_price=current_price,
            ema20=ema20,
            ema50=ema50,
            ema20_slope_pct=ema20_slope_pct,
            trend_spread_pct=trend_spread_pct,
            reason="4h uptrend confirmed by EMA stack, spread, and positive EMA slope.",
        )
    if current_price < ema20 < ema50 and trend_spread_pct >= trend_spread_min and ema20_slope_pct <= -trend_slope_min:
        return HigherTimeframeTrendSnapshot(
            timeframe=timeframe,
            label="trend_down",
            preferred_action="SHORT",
            current_price=current_price,
            ema20=ema20,
            ema50=ema50,
            ema20_slope_pct=ema20_slope_pct,
            trend_spread_pct=trend_spread_pct,
            reason="4h downtrend confirmed by EMA stack, spread, and negative EMA slope.",
        )
    return HigherTimeframeTrendSnapshot(
        timeframe=timeframe,
        label="neutral",
        preferred_action="FLAT",
        current_price=current_price,
        ema20=ema20,
        ema50=ema50,
        ema20_slope_pct=ema20_slope_pct,
        trend_spread_pct=trend_spread_pct,
        reason="4h structure is neutral or transitional; no higher-timeframe trend bias.",
    )


def apply_higher_timeframe_filter(
    regime: RegimeSnapshot,
    higher_timeframe: HigherTimeframeTrendSnapshot,
    config: dict,
) -> RegimeSnapshot:
    if not bool(config.get("bot_higher_timeframe_filter_enabled", True)):
        return regime
    if regime.setup_family != "trend_pullback":
        return regime
    if regime.label not in {"trend_up", "trend_down"}:
        return regime
    aligned = (
        (regime.preferred_action == "LONG" and higher_timeframe.label == "trend_up")
        or (regime.preferred_action == "SHORT" and higher_timeframe.label == "trend_down")
    )
    if aligned:
        return regime
    return RegimeSnapshot(
        **{
            **regime.to_dict(),
            "trade_allowed": False,
            "setup_family": "",
            "allowed_setup_families": [],
            "reason": (
                f"{regime.reason} Blocked by higher timeframe filter: "
                f"1h {regime.label} requires aligned 4h bias, but got {higher_timeframe.label}."
            ),
        }
    )


def classify_regime_from_data(
    data: pd.DataFrame,
    config: dict,
    *,
    timeframe: str = "1h",
) -> RegimeSnapshot:
    timeframe = str(timeframe).lower()
    if timeframe != "1h":
        return _unsupported_timeframe_snapshot(config, timeframe)

    if data.empty or len(data) < 60:
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
            reason="Insufficient 1h OHLCV history to classify market regime.",
        )

    frame = _build_feature_frame(data)
    latest = frame.iloc[-1]
    current_price = _safe_float(latest["Close"])
    ema20 = _safe_float(latest["ema20"])
    ema50 = _safe_float(latest["ema50"])
    atr14 = max(_safe_float(latest["atr14"]), 0.0)
    atr_pct = atr14 / current_price if current_price > 0 else 0.0
    ema20_slope_pct = (
        (ema20 - _safe_float(frame.iloc[-13]["ema20"])) / current_price
        if len(frame) >= 13 and current_price > 0
        else 0.0
    )
    trend_spread_pct = abs(ema20 - ema50) / current_price if current_price > 0 else 0.0
    realized_vol_24h = _safe_float(frame["return_pct"].tail(24).std(ddof=0))
    bar_change_pct = _safe_float(latest["return_pct"])
    pullback_distance_atr = abs(current_price - ema20) / atr14 if atr14 > 0 else 0.0

    setup_family = str(config.get("bot_strategy_setup_family", "trend_pullback"))
    trend_spread_min = float(config.get("bot_regime_trend_spread_min_pct", 0.003))
    trend_slope_min = float(config.get("bot_regime_slope_min_pct", 0.0015))
    range_spread_max = float(config.get("bot_regime_range_spread_max_pct", 0.0015))
    range_slope_max = float(config.get("bot_regime_range_slope_max_pct", 0.0007))
    volatility_event_atr_pct = float(config.get("bot_regime_volatility_event_atr_pct", 0.035))
    bar_shock_atr_multiple = float(config.get("bot_regime_bar_shock_atr_multiple", 1.8))
    pullback_atr_tolerance = float(config.get("bot_pullback_atr_tolerance", 0.75))

    def snapshot_for(
        label: str,
        preferred_action: str,
        reason: str,
        *,
        pullback_zone_low: Optional[float],
        pullback_zone_high: Optional[float],
    ) -> RegimeSnapshot:
        allowed = allowed_strategies_for_regime(label, config)
        selected = allowed[0] if allowed else ""
        return RegimeSnapshot(
            label=label,
            trade_allowed=bool(allowed),
            preferred_action=preferred_action,
            setup_family=selected or setup_family,
            allowed_setup_families=allowed,
            current_price=current_price,
            ema20=ema20,
            ema50=ema50,
            atr14=atr14,
            atr_pct=atr_pct,
            ema20_slope_pct=ema20_slope_pct,
            trend_spread_pct=trend_spread_pct,
            realized_vol_24h=realized_vol_24h,
            bar_change_pct=bar_change_pct,
            pullback_distance_atr=pullback_distance_atr,
            pullback_zone_low=pullback_zone_low,
            pullback_zone_high=pullback_zone_high,
            reason=reason,
        )

    if atr_pct >= volatility_event_atr_pct or (
        atr_pct > 0 and abs(bar_change_pct) >= atr_pct * bar_shock_atr_multiple
    ):
        return snapshot_for(
            "high_volatility_event",
            "FLAT",
            reason="Volatility shock detected; hard-skip new entries in event conditions.",
            pullback_zone_low=None,
            pullback_zone_high=None,
        )

    if trend_spread_pct <= range_spread_max and abs(ema20_slope_pct) <= range_slope_max:
        return snapshot_for(
            "range",
            "FLAT",
            reason="Trend spread and EMA slope are too weak; classify as chop/range.",
            pullback_zone_low=None,
            pullback_zone_high=None,
        )

    if (
        current_price > ema20 > ema50
        and trend_spread_pct >= trend_spread_min
        and ema20_slope_pct >= trend_slope_min
    ):
        return snapshot_for(
            "trend_up",
            "LONG",
            pullback_zone_low=max(0.0, ema20 - atr14 * pullback_atr_tolerance),
            pullback_zone_high=ema20 + atr14 * (pullback_atr_tolerance * 0.35),
            reason="Uptrend confirmed by EMA stack, spread, and positive EMA slope.",
        )

    if (
        current_price < ema20 < ema50
        and trend_spread_pct >= trend_spread_min
        and ema20_slope_pct <= -trend_slope_min
    ):
        return snapshot_for(
            "trend_down",
            "SHORT",
            pullback_zone_low=max(0.0, ema20 - atr14 * (pullback_atr_tolerance * 0.35)),
            pullback_zone_high=ema20 + atr14 * pullback_atr_tolerance,
            reason="Downtrend confirmed by EMA stack, spread, and negative EMA slope.",
        )

    return snapshot_for(
        "low_quality",
        "FLAT",
        reason="Market structure is ambiguous and does not qualify as trend pullback regime.",
        pullback_zone_low=None,
        pullback_zone_high=None,
    )


def _unsupported_timeframe_snapshot(config: dict, timeframe: str) -> RegimeSnapshot:
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
        reason=f"Regime gate v1 only supports BTC 1h; got timeframe={timeframe}.",
    )


def _build_feature_frame(data: pd.DataFrame) -> pd.DataFrame:
    frame = data.sort_values("Date").copy()
    frame["ema20"] = frame["Close"].ewm(span=20, adjust=False).mean()
    frame["ema50"] = frame["Close"].ewm(span=50, adjust=False).mean()
    prev_close = frame["Close"].shift(1)
    tr_components = pd.concat(
        [
            frame["High"] - frame["Low"],
            (frame["High"] - prev_close).abs(),
            (frame["Low"] - prev_close).abs(),
        ],
        axis=1,
    )
    frame["atr14"] = tr_components.max(axis=1).rolling(14).mean().bfill()
    frame["return_pct"] = frame["Close"].pct_change().fillna(0.0)
    return frame


def _safe_float(value) -> float:
    if pd.isna(value):
        return 0.0
    return float(value)


def _enabled_strategy_families(config: dict) -> list[str]:
    configured = config.get("bot_enabled_strategy_families")
    if isinstance(configured, (list, tuple)):
        values = [str(item).strip() for item in configured if str(item).strip()]
        if values:
            return values
    fallback = str(config.get("bot_strategy_setup_family", "trend_pullback")).strip()
    return [fallback] if fallback else ["trend_pullback"]


def _strategy_route_map(config: dict) -> dict[str, list[str]]:
    configured = config.get("bot_regime_strategy_map")
    if isinstance(configured, dict):
        route_map: dict[str, list[str]] = {}
        for label, families in configured.items():
            if isinstance(families, (list, tuple)):
                route_map[str(label)] = [str(item).strip() for item in families if str(item).strip()]
            elif families:
                route_map[str(label)] = [str(families).strip()]
            else:
                route_map[str(label)] = []
        return route_map
    return {
        "trend_up": ["trend_pullback"],
        "trend_down": ["trend_pullback"],
        "range": ["range_fade"],
        "high_volatility_event": [],
        "low_quality": [],
    }
