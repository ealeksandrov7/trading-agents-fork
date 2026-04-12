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


def detect_trend_pullback_candidate(
    data: pd.DataFrame,
    regime: RegimeSnapshot,
    config: dict,
) -> CandidateSnapshot:
    setup_family = str(config.get("bot_strategy_setup_family", "trend_pullback"))
    if not regime.trade_allowed:
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
