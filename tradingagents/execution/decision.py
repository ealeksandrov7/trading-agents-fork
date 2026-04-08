from __future__ import annotations

import ast
import json
import re
from typing import Any, Optional

from pydantic import ValidationError

from .models import StructuredTradeDecision


class DecisionParseError(ValueError):
    """Raised when the portfolio manager output cannot be parsed."""


class DecisionParser:
    MARKER = "STRUCTURED_DECISION"

    @classmethod
    def parse(
        cls,
        raw_text: str,
        *,
        fallback_symbol: Optional[str] = None,
        fallback_timestamp: Optional[str] = None,
        fallback_time_horizon: Optional[str] = None,
    ) -> StructuredTradeDecision:
        payload = cls._extract_payload(raw_text, fallback_time_horizon=fallback_time_horizon)
        if fallback_symbol and "symbol" not in payload:
            payload["symbol"] = fallback_symbol
        if fallback_timestamp and "timestamp" not in payload:
            payload["timestamp"] = fallback_timestamp
        if fallback_time_horizon and "time_horizon" not in payload:
            payload["time_horizon"] = fallback_time_horizon

        try:
            return StructuredTradeDecision.model_validate(payload)
        except ValidationError as exc:
            raise DecisionParseError(str(exc)) from exc

    @classmethod
    def _extract_payload(
        cls,
        raw_text: str,
        *,
        fallback_time_horizon: Optional[str] = None,
    ) -> dict[str, Any]:
        if not raw_text or not raw_text.strip():
            raise DecisionParseError("empty portfolio manager output")

        for candidate in cls._candidate_json_blocks(raw_text):
            payload = cls._load_candidate(candidate)
            if isinstance(payload, dict):
                return payload

        heuristic_payload = cls._extract_heuristic_payload(
            raw_text,
            fallback_time_horizon=fallback_time_horizon,
        )
        if heuristic_payload:
            return heuristic_payload

        raise DecisionParseError("no valid structured decision JSON block found")

    @classmethod
    def _load_candidate(cls, candidate: str) -> Optional[dict[str, Any]]:
        try:
            payload = json.loads(candidate)
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            pass

        normalized = candidate.strip()
        normalized = normalized.replace("“", '"').replace("”", '"').replace("’", "'")
        normalized = re.sub(r",\s*([}\]])", r"\1", normalized)
        try:
            payload = json.loads(normalized)
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            pass

        pythonish = (
            normalized.replace("null", "None")
            .replace("true", "True")
            .replace("false", "False")
        )
        try:
            payload = ast.literal_eval(pythonish)
            return payload if isinstance(payload, dict) else None
        except (ValueError, SyntaxError):
            return None

    @classmethod
    def _candidate_json_blocks(cls, raw_text: str) -> list[str]:
        candidates: list[str] = []

        marker_pattern = re.compile(
            rf"{cls.MARKER}\s*:?\s*```json\s*(\{{.*?\}})\s*```",
            re.IGNORECASE | re.DOTALL,
        )
        candidates.extend(match.group(1) for match in marker_pattern.finditer(raw_text))

        generic_fence_pattern = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
        candidates.extend(match.group(1) for match in generic_fence_pattern.finditer(raw_text))

        fallback = cls._extract_first_balanced_json(raw_text)
        if fallback:
            candidates.append(fallback)

        return candidates

    @staticmethod
    def _extract_first_balanced_json(raw_text: str) -> Optional[str]:
        start = raw_text.find("{")
        if start == -1:
            return None

        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(raw_text)):
            char = raw_text[idx]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return raw_text[start : idx + 1]
        return None

    @classmethod
    def _extract_heuristic_payload(
        cls,
        raw_text: str,
        *,
        fallback_time_horizon: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        action = cls._infer_action(raw_text)
        if not action:
            return None

        payload: dict[str, Any] = {
            "action": action,
            "confidence": cls._infer_confidence(raw_text),
            "thesis_summary": cls._infer_thesis_summary(raw_text),
            "time_horizon": cls._infer_time_horizon(
                raw_text,
                fallback_time_horizon=fallback_time_horizon,
            ),
            "invalidation": cls._infer_invalidation(raw_text),
            "size_hint": cls._infer_size_hint(raw_text),
            "setup_expiry_bars": cls._infer_setup_expiry_bars(raw_text),
            "position_instruction": cls._infer_position_instruction(raw_text, action),
        }

        if action == "FLAT":
            payload.update(
                {
                    "entry_mode": "MARKET",
                    "entry_price": None,
                    "entry_zone_low": None,
                    "entry_zone_high": None,
                    "stop_loss": None,
                    "take_profit": None,
                }
            )
            return payload

        entry_mode, entry_price, zone_low, zone_high = cls._infer_entry(raw_text, action)
        stop_loss = cls._extract_price_after_keywords(raw_text, ("stop", "stop loss", "invalidation"))
        take_profit = cls._extract_price_after_keywords(raw_text, ("target", "take profit", "primary target"))
        if stop_loss is None or take_profit is None:
            return None

        payload.update(
            {
                "entry_mode": entry_mode,
                "entry_price": entry_price,
                "entry_zone_low": zone_low,
                "entry_zone_high": zone_high,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            }
        )
        return payload

    @staticmethod
    def _infer_action(raw_text: str) -> Optional[str]:
        patterns = [
            r'"action"\s*:\s*"?(LONG|SHORT|FLAT)"?',
            r"\bDirection\s*:\s*(LONG|SHORT|FLAT)\b",
            r"\bRecommendation\s*:\s*(LONG|SHORT|FLAT)\b",
            r"\bActionable Recommendation\s*:?\s*(LONG|SHORT|FLAT)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, raw_text, re.IGNORECASE)
            if match:
                return match.group(1).upper()

        lower = raw_text.lower()
        flat_signals = (
            "maintain high liquidity",
            "stay in cash",
            "capital preservation",
            "avoid speculative bets",
            "defensive posture",
            "stand aside",
            "observe/hold",
            "observe / hold",
            "await confirmation",
            "wait and watch",
            "observation (wait and watch)",
            "maintaining current exposure",
            "hold (await confirmation signal)",
            "observation-based stance",
            "avoid aggressive directional bets",
            "no-action",
            "no action",
        )
        if any(signal in lower for signal in flat_signals):
            return "FLAT"
        if re.search(r"\bhold\b", lower) and re.search(
            r"\b(observe|watch|await|confirmation|wait)\b", lower
        ):
            return "FLAT"
        return None

    @staticmethod
    def _infer_confidence(raw_text: str) -> float:
        match = re.search(r'"confidence"\s*:\s*([0-9]*\.?[0-9]+)', raw_text)
        if match:
            value = float(match.group(1))
            if value > 1:
                return min(value / 100.0, 1.0)
            return value

        lower = raw_text.lower()
        if "low-confidence" in lower or "extremely defensive" in lower:
            return 0.2
        if "small size" in lower or "tactical" in lower:
            return 0.4
        return 0.35

    @staticmethod
    def _infer_thesis_summary(raw_text: str) -> str:
        lines = [line.strip(" -*#`") for line in raw_text.splitlines() if line.strip()]
        for line in lines:
            if len(line.split()) >= 6:
                return line[:400]
        return "Recovered from prose-only portfolio manager output."

    @staticmethod
    def _infer_setup_expiry_bars(raw_text: str) -> Optional[int]:
        match = re.search(r'"setup_expiry_bars"\s*:\s*(\d+)', raw_text)
        if match:
            return int(match.group(1))
        match = re.search(r"\bexpire(?:s|d)?\s+after\s+(\d+)\s+bars?\b", raw_text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _infer_position_instruction(raw_text: str, action: str) -> str:
        match = re.search(r'"position_instruction"\s*:\s*"([A-Z_]+)"', raw_text)
        if match:
            return match.group(1)
        if action == "FLAT":
            lower = raw_text.lower()
            if "cancel" in lower and "order" in lower:
                return "CANCEL_ENTRY"
            if "close" in lower or "exit" in lower:
                return "CLOSE"
            return "NO_ACTION"
        return "OPEN"

    @staticmethod
    def _infer_time_horizon(raw_text: str, fallback_time_horizon: Optional[str] = None) -> str:
        horizon_aliases = {
            "1h": ("1h", "h1", "hourly", "1-hour", "1 hour"),
            "4h": ("4h", "h4", "4-hour", "4 hour"),
            "1d": ("1d", "daily", "1-day", "1 day"),
        }
        lower = raw_text.lower()
        for normalized, aliases in horizon_aliases.items():
            if any(re.search(rf"\b{re.escape(alias)}\b", lower) for alias in aliases):
                return normalized
        for token in ("1h", "4h", "1d"):
            if re.search(rf"\b{re.escape(token)}\b", raw_text, re.IGNORECASE):
                return token
        return fallback_time_horizon or "1d"

    @staticmethod
    def _infer_invalidation(raw_text: str) -> str:
        patterns = [
            r"\b(?:invalidation|invalidates?|negates?)\b[:\s-]*(.+)",
            r"\b(?:hard exit|exit)\b[:\s-]*(.+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, raw_text, re.IGNORECASE)
            if match:
                return match.group(1).strip()[:400]
        return "No explicit invalidation provided in the prose output."

    @staticmethod
    def _infer_size_hint(raw_text: str) -> str:
        lower = raw_text.lower()
        if "small" in lower or "half normal" in lower or "defensive" in lower:
            return "small"
        if "large" in lower or "aggressive" in lower:
            return "large"
        return "medium"

    @classmethod
    def _infer_entry(cls, raw_text: str, action: str) -> tuple[str, Optional[float], Optional[float], Optional[float]]:
        zone_match = re.search(
            r"(?:zone|toward|into|between)\s+\$?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:–|-|to)\s*\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
            raw_text,
            re.IGNORECASE,
        )
        if zone_match:
            low = float(zone_match.group(1).replace(",", ""))
            high = float(zone_match.group(2).replace(",", ""))
            return "LIMIT_ZONE", None, min(low, high), max(low, high)

        entry_match = re.search(
            r"\b(?:entry|enter|short at|long at)\b[:\s-]*\$?([0-9][0-9,]*(?:\.[0-9]+)?)",
            raw_text,
            re.IGNORECASE,
        )
        if entry_match:
            return "LIMIT", float(entry_match.group(1).replace(",", "")), None, None

        return "MARKET", None, None, None

    @staticmethod
    def _extract_price_after_keywords(raw_text: str, keywords: tuple[str, ...]) -> Optional[float]:
        for keyword in keywords:
            pattern = rf"\b{re.escape(keyword)}\b[^0-9$]{{0,30}}\$?([0-9][0-9,]*(?:\.[0-9]+)?)"
            match = re.search(pattern, raw_text, re.IGNORECASE)
            if match:
                return float(match.group(1).replace(",", ""))
        return None
