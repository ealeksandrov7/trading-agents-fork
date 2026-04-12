from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

import pandas as pd

from .regime import RegimeSnapshot


@dataclass
class CandidateSnapshot:
    candidate_setup_present: bool
    setup_family: str
    direction: str
    entry_zone_low: Optional[float]
    entry_zone_high: Optional[float]
    invalidation_level: Optional[float]
    target_reference: Optional[float]
    reward_risk_estimate: Optional[float]
    reclaim_confirmed: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        zone = "n/a"
        if self.entry_zone_low is not None and self.entry_zone_high is not None:
            zone = f"{self.entry_zone_low:.2f}-{self.entry_zone_high:.2f}"
        rr = "-" if self.reward_risk_estimate is None else f"{self.reward_risk_estimate:.2f}"
        return (
            f"candidate_setup_present={self.candidate_setup_present} | setup_family={self.setup_family} | "
            f"direction={self.direction} | entry_zone={zone} | invalidation={self.invalidation_level} | "
            f"target_reference={self.target_reference} | reclaim_confirmed={self.reclaim_confirmed} | "
            f"reward_risk_estimate={rr} | reason={self.reason}"
        )


def detect_candidate(
    data: pd.DataFrame,
    regime: RegimeSnapshot,
    config: dict,
    setup_family: Optional[str] = None,
) -> CandidateSnapshot:
    selected = str(setup_family or regime.setup_family or config.get("bot_strategy_setup_family", "trend_pullback"))
    if selected == "range_fade":
        return detect_range_fade_candidate(data, regime, config)
    return detect_trend_pullback_candidate(data, regime, config)


def detect_trend_pullback_candidate(
    data: pd.DataFrame,
    regime: RegimeSnapshot,
    config: dict,
) -> CandidateSnapshot:
    setup_family = "trend_pullback"
    if not regime.trade_allowed or setup_family not in regime.allowed_setup_families:
        return CandidateSnapshot(
            candidate_setup_present=False,
            setup_family=setup_family,
            direction=regime.preferred_action,
            entry_zone_low=regime.pullback_zone_low,
            entry_zone_high=regime.pullback_zone_high,
            invalidation_level=None,
            target_reference=None,
            reward_risk_estimate=None,
            reclaim_confirmed=False,
            reason=f"Regime {regime.label} does not allow new entries.",
        )
    if data.empty or len(data) < 55:
        return CandidateSnapshot(
            candidate_setup_present=False,
            setup_family=setup_family,
            direction=regime.preferred_action,
            entry_zone_low=regime.pullback_zone_low,
            entry_zone_high=regime.pullback_zone_high,
            invalidation_level=None,
            target_reference=None,
            reward_risk_estimate=None,
            reclaim_confirmed=False,
            reason="Insufficient bars for deterministic candidate detection.",
        )

    frame = _build_feature_frame(data)
    latest = frame.iloc[-1]
    recent = frame.tail(6)
    entry_zone_low = regime.pullback_zone_low
    entry_zone_high = regime.pullback_zone_high
    current_price = float(latest["Close"])
    atr14 = max(float(latest["atr14"]), 1e-9)
    direction = regime.preferred_action

    touched_zone = False
    if entry_zone_low is not None and entry_zone_high is not None:
        for _, bar in recent.iterrows():
            if float(bar["Low"]) <= entry_zone_high and float(bar["High"]) >= entry_zone_low:
                touched_zone = True
                break

    if not touched_zone:
        return CandidateSnapshot(
            candidate_setup_present=False,
            setup_family=setup_family,
            direction=direction,
            entry_zone_low=entry_zone_low,
            entry_zone_high=entry_zone_high,
            invalidation_level=None,
            target_reference=None,
            reward_risk_estimate=None,
            reclaim_confirmed=False,
            reason="Recent bars never retraced into the pullback zone.",
        )

    reclaim_confirmed = False
    invalidation_level = None
    target_reference = None
    reward_risk_estimate = None

    if direction == "LONG":
        prior_low = float(recent["Low"].min())
        invalidation_level = prior_low - atr14 * 0.15
        reclaim_confirmed = current_price >= float(latest["ema20"]) and current_price > float(frame.iloc[-2]["Close"])
        target_reference = current_price + max(atr14 * 2.4, current_price - invalidation_level)
        risk = current_price - invalidation_level
        reward = target_reference - current_price
    else:
        prior_high = float(recent["High"].max())
        invalidation_level = prior_high + atr14 * 0.15
        reclaim_confirmed = current_price <= float(latest["ema20"]) and current_price < float(frame.iloc[-2]["Close"])
        target_reference = current_price - max(atr14 * 2.4, invalidation_level - current_price)
        risk = invalidation_level - current_price
        reward = current_price - target_reference

    if risk > 0 and reward > 0:
        reward_risk_estimate = reward / risk

    minimum_rr = float(config.get("bot_min_reward_risk", 1.8))
    if not reclaim_confirmed:
        reason = "Pullback reached the zone but has not reclaimed in the trend direction."
    elif reward_risk_estimate is None or reward_risk_estimate < minimum_rr:
        reason = (
            f"Pullback candidate exists but estimated reward-to-risk "
            f"{0.0 if reward_risk_estimate is None else reward_risk_estimate:.2f} is below {minimum_rr:.2f}."
        )
    else:
        reason = "Trend pullback candidate detected with reclaim confirmation and acceptable estimated RR."

    return CandidateSnapshot(
        candidate_setup_present=bool(reclaim_confirmed and reward_risk_estimate is not None and reward_risk_estimate >= minimum_rr),
        setup_family=setup_family,
        direction=direction,
        entry_zone_low=entry_zone_low,
        entry_zone_high=entry_zone_high,
        invalidation_level=invalidation_level,
        target_reference=target_reference,
        reward_risk_estimate=reward_risk_estimate,
        reclaim_confirmed=reclaim_confirmed,
        reason=reason,
    )


def detect_range_fade_candidate(
    data: pd.DataFrame,
    regime: RegimeSnapshot,
    config: dict,
) -> CandidateSnapshot:
    setup_family = "range_fade"
    if regime.label != "range" or setup_family not in regime.allowed_setup_families:
        return CandidateSnapshot(
            candidate_setup_present=False,
            setup_family=setup_family,
            direction="FLAT",
            entry_zone_low=None,
            entry_zone_high=None,
            invalidation_level=None,
            target_reference=None,
            reward_risk_estimate=None,
            reclaim_confirmed=False,
            reason=f"Regime {regime.label} does not route to range_fade.",
        )
    if data.empty or len(data) < 55:
        return CandidateSnapshot(
            candidate_setup_present=False,
            setup_family=setup_family,
            direction="FLAT",
            entry_zone_low=None,
            entry_zone_high=None,
            invalidation_level=None,
            target_reference=None,
            reward_risk_estimate=None,
            reclaim_confirmed=False,
            reason="Insufficient bars for deterministic range_fade detection.",
        )

    frame = _build_feature_frame(data)
    latest = frame.iloc[-1]
    recent = frame.tail(24)
    current_price = float(latest["Close"])
    atr14 = max(float(latest["atr14"]), 1e-9)
    range_high = float(recent["High"].max())
    range_low = float(recent["Low"].min())
    range_width = range_high - range_low
    if range_width <= 0:
        return CandidateSnapshot(
            candidate_setup_present=False,
            setup_family=setup_family,
            direction="FLAT",
            entry_zone_low=None,
            entry_zone_high=None,
            invalidation_level=None,
            target_reference=None,
            reward_risk_estimate=None,
            reclaim_confirmed=False,
            reason="Range width is invalid for range_fade detection.",
        )

    min_width_atr = float(config.get("bot_range_fade_min_width_atr", 1.5))
    max_width_atr = float(config.get("bot_range_fade_max_width_atr", 5.5))
    if range_width / atr14 < min_width_atr or range_width / atr14 > max_width_atr:
        return CandidateSnapshot(
            candidate_setup_present=False,
            setup_family=setup_family,
            direction="FLAT",
            entry_zone_low=range_low,
            entry_zone_high=range_high,
            invalidation_level=None,
            target_reference=None,
            reward_risk_estimate=None,
            reclaim_confirmed=False,
            reason="Detected range width is outside the configured ATR bounds.",
        )

    edge_threshold = float(config.get("bot_range_fade_edge_atr_tolerance", 0.55)) * atr14
    stop_buffer = float(config.get("bot_range_fade_stop_buffer_atr", 0.2)) * atr14
    target_buffer = float(config.get("bot_range_fade_target_buffer_atr", 0.35)) * atr14
    minimum_rr = float(config.get("bot_min_reward_risk", 1.8))

    direction = "FLAT"
    entry_zone_low = None
    entry_zone_high = None
    invalidation_level = None
    target_reference = None
    reward_risk_estimate = None
    reclaim_confirmed = False
    reason = "Price is not near a range edge."

    prev_close = float(frame.iloc[-2]["Close"])
    prev_low = float(frame.iloc[-2]["Low"])
    prev_high = float(frame.iloc[-2]["High"])

    if current_price - range_low <= edge_threshold:
        direction = "LONG"
        entry_zone_low = range_low
        entry_zone_high = min(range_low + edge_threshold, range_high)
        invalidation_level = range_low - stop_buffer
        target_reference = range_high - target_buffer
        reclaim_confirmed = current_price > prev_close and float(latest["Low"]) >= prev_low
        risk = current_price - invalidation_level
        reward = target_reference - current_price
        if reclaim_confirmed:
            reason = "Range support rejection detected near the lower boundary."
        else:
            reason = "Price touched lower range support but rejection confirmation is missing."
    elif range_high - current_price <= edge_threshold:
        direction = "SHORT"
        entry_zone_low = max(range_low, range_high - edge_threshold)
        entry_zone_high = range_high
        invalidation_level = range_high + stop_buffer
        target_reference = range_low + target_buffer
        reclaim_confirmed = current_price < prev_close and float(latest["High"]) <= prev_high
        risk = invalidation_level - current_price
        reward = current_price - target_reference
        if reclaim_confirmed:
            reason = "Range resistance rejection detected near the upper boundary."
        else:
            reason = "Price touched upper range resistance but rejection confirmation is missing."
    else:
        risk = None
        reward = None

    if risk is not None and risk > 0 and reward is not None and reward > 0:
        reward_risk_estimate = reward / risk

    if direction == "FLAT":
        return CandidateSnapshot(
            candidate_setup_present=False,
            setup_family=setup_family,
            direction=direction,
            entry_zone_low=range_low,
            entry_zone_high=range_high,
            invalidation_level=None,
            target_reference=None,
            reward_risk_estimate=None,
            reclaim_confirmed=False,
            reason=reason,
        )

    if not reclaim_confirmed:
        final_reason = reason
    elif reward_risk_estimate is None or reward_risk_estimate < minimum_rr:
        final_reason = (
            f"Range fade candidate exists but estimated reward-to-risk "
            f"{0.0 if reward_risk_estimate is None else reward_risk_estimate:.2f} is below {minimum_rr:.2f}."
        )
    else:
        final_reason = "Range fade candidate detected with edge rejection and acceptable estimated RR."

    return CandidateSnapshot(
        candidate_setup_present=bool(reclaim_confirmed and reward_risk_estimate is not None and reward_risk_estimate >= minimum_rr),
        setup_family=setup_family,
        direction=direction,
        entry_zone_low=entry_zone_low,
        entry_zone_high=entry_zone_high,
        invalidation_level=invalidation_level,
        target_reference=target_reference,
        reward_risk_estimate=reward_risk_estimate,
        reclaim_confirmed=reclaim_confirmed,
        reason=final_reason,
    )


def _build_feature_frame(data: pd.DataFrame) -> pd.DataFrame:
    frame = data.sort_values("Date").copy()
    frame["Date"] = pd.to_datetime(frame["Date"], utc=True, errors="coerce")
    frame["ema20"] = frame["Close"].ewm(span=20, adjust=False).mean()
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
    return frame
