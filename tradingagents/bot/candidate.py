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
    candidate_score: float = 0.0
    candidate_threshold: float = 0.0
    candidate_tier: str = "none"
    stage_flags: dict[str, bool] | None = None

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
            f"reward_risk_estimate={rr} | candidate_score={self.candidate_score:.2f}/{self.candidate_threshold:.2f} | "
            f"candidate_tier={self.candidate_tier} | reason={self.reason}"
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
    threshold = float(config.get("bot_trend_pullback_candidate_score_min", 2.75))
    empty_stage_flags = {
        "trend_context_pass": False,
        "pullback_touch_pass": False,
        "reclaim_pass": False,
        "extension_pass": False,
        "rr_pass": False,
    }
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
            candidate_threshold=threshold,
            stage_flags=empty_stage_flags,
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
            candidate_threshold=threshold,
            stage_flags=empty_stage_flags,
        )

    frame = _build_feature_frame(data)
    latest = frame.iloc[-1]
    touch_lookback = max(int(config.get("bot_trend_pullback_touch_lookback_bars", 4)), 2)
    recent = frame.tail(max(touch_lookback + 2, 6))
    touch_window = frame.tail(touch_lookback)
    entry_zone_low = regime.pullback_zone_low
    entry_zone_high = regime.pullback_zone_high
    current_price = float(latest["Close"])
    atr14 = max(float(latest["atr14"]), 1e-9)
    direction = regime.preferred_action
    stage_flags = {
        "trend_context_pass": True,
        "pullback_touch_pass": False,
        "reclaim_pass": False,
        "extension_pass": False,
        "rr_pass": False,
    }

    touched_zone = False
    if entry_zone_low is not None and entry_zone_high is not None:
        for _, bar in touch_window.iterrows():
            if float(bar["Low"]) <= entry_zone_high and float(bar["High"]) >= entry_zone_low:
                touched_zone = True
                break
    stage_flags["pullback_touch_pass"] = touched_zone

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
            candidate_threshold=threshold,
            stage_flags=stage_flags,
        )

    reclaim_confirmed = False
    invalidation_level = None
    target_reference = None
    reward_risk_estimate = None
    latest_open = float(latest["Open"])
    latest_high = float(latest["High"])
    latest_low = float(latest["Low"])
    prev_close = float(frame.iloc[-2]["Close"])
    prev_high = float(frame.iloc[-2]["High"])
    prev_low = float(frame.iloc[-2]["Low"])
    close_range = max(latest_high - latest_low, 1e-9)
    close_location = (current_price - latest_low) / close_range
    min_close_location = float(config.get("bot_trend_pullback_reclaim_close_location_min", 0.55))
    max_extension_atr = float(config.get("bot_trend_pullback_max_extension_atr", 0.8))
    rr_research_floor = float(config.get("bot_trend_pullback_rr_research_floor", 0.8))

    if direction == "LONG":
        prior_low = float(recent["Low"].min())
        invalidation_level = prior_low - atr14 * 0.15
        extension_atr = 0.0 if entry_zone_high is None else max(0.0, current_price - float(entry_zone_high)) / atr14
        weak_reclaim = current_price >= float(latest["ema20"])
        medium_reclaim = (
            current_price >= float(latest["ema20"])
            and current_price > prev_close
            and current_price > latest_open
            and close_location >= min_close_location
        )
        strong_reclaim = medium_reclaim and current_price >= prev_high
        reclaim_tier = "strong" if strong_reclaim else "medium" if medium_reclaim else "weak" if weak_reclaim else "none"
        reclaim_confirmed = medium_reclaim
        target_reference = current_price + max(atr14 * 2.4, current_price - invalidation_level)
        risk = current_price - invalidation_level
        reward = target_reference - current_price
    else:
        prior_high = float(recent["High"].max())
        invalidation_level = prior_high + atr14 * 0.15
        close_location = (latest_high - current_price) / close_range
        extension_atr = 0.0 if entry_zone_low is None else max(0.0, float(entry_zone_low) - current_price) / atr14
        weak_reclaim = current_price <= float(latest["ema20"])
        medium_reclaim = (
            current_price <= float(latest["ema20"])
            and current_price < prev_close
            and current_price < latest_open
            and close_location >= min_close_location
        )
        strong_reclaim = medium_reclaim and current_price <= prev_low
        reclaim_tier = "strong" if strong_reclaim else "medium" if medium_reclaim else "weak" if weak_reclaim else "none"
        reclaim_confirmed = medium_reclaim
        target_reference = current_price - max(atr14 * 2.4, invalidation_level - current_price)
        risk = invalidation_level - current_price
        reward = current_price - target_reference

    if risk > 0 and reward > 0:
        reward_risk_estimate = reward / risk
    stage_flags["reclaim_pass"] = reclaim_confirmed
    stage_flags["extension_pass"] = extension_atr <= max_extension_atr
    stage_flags["rr_pass"] = reward_risk_estimate is not None and reward_risk_estimate >= rr_research_floor

    score = 0.5
    if stage_flags["pullback_touch_pass"]:
        score += 1.0
    if reclaim_tier == "weak":
        score += 0.5
    elif reclaim_tier == "medium":
        score += 1.25
    elif reclaim_tier == "strong":
        score += 1.75
    if stage_flags["extension_pass"]:
        score += 0.5
    if reward_risk_estimate is not None:
        if reward_risk_estimate >= 1.8:
            score += 0.75
        elif reward_risk_estimate >= 1.2:
            score += 0.5
        elif reward_risk_estimate >= rr_research_floor:
            score += 0.25

    candidate_present = bool(
        touched_zone
        and stage_flags["extension_pass"]
        and reclaim_tier in {"medium", "strong"}
        and score >= threshold
    )
    if not reclaim_confirmed:
        reason = f"Pullback reached the zone but reclaim quality is only {reclaim_tier}."
    elif not stage_flags["extension_pass"]:
        reason = f"Reclaim exists but price is too extended at {extension_atr:.2f} ATR beyond the zone."
    elif score < threshold:
        reason = f"Trend pullback score {score:.2f} is below threshold {threshold:.2f}."
    elif reward_risk_estimate is not None and reward_risk_estimate < 1.2:
        reason = f"Trend pullback candidate detected, but estimated RR is soft at {reward_risk_estimate:.2f}."
    else:
        reason = f"Trend pullback candidate detected with {reclaim_tier} reclaim and score {score:.2f}."

    return CandidateSnapshot(
        candidate_setup_present=candidate_present,
        setup_family=setup_family,
        direction=direction,
        entry_zone_low=entry_zone_low,
        entry_zone_high=entry_zone_high,
        invalidation_level=invalidation_level,
        target_reference=target_reference,
        reward_risk_estimate=reward_risk_estimate,
        reclaim_confirmed=reclaim_confirmed,
        reason=reason,
        candidate_score=score,
        candidate_threshold=threshold,
        candidate_tier=reclaim_tier,
        stage_flags=stage_flags,
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
