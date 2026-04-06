from __future__ import annotations

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
    ) -> StructuredTradeDecision:
        payload = cls._extract_payload(raw_text)
        if fallback_symbol and "symbol" not in payload:
            payload["symbol"] = fallback_symbol
        if fallback_timestamp and "timestamp" not in payload:
            payload["timestamp"] = fallback_timestamp

        try:
            return StructuredTradeDecision.model_validate(payload)
        except ValidationError as exc:
            raise DecisionParseError(str(exc)) from exc

    @classmethod
    def _extract_payload(cls, raw_text: str) -> dict[str, Any]:
        if not raw_text or not raw_text.strip():
            raise DecisionParseError("empty portfolio manager output")

        for candidate in cls._candidate_json_blocks(raw_text):
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload

        raise DecisionParseError("no valid structured decision JSON block found")

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
