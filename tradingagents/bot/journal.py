from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class BotJournal:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def insert_cycle(
        self,
        *,
        mode: str,
        symbol: str,
        timeframe: str,
        decision_timestamp: str,
        analysis_timestamp: str,
        regime_snapshot: dict[str, Any] | None,
        candidate_snapshot: dict[str, Any] | None,
        raw_action: dict[str, Any] | None,
        final_action: dict[str, Any] | None,
        quality_filter_reasons: list[str] | None,
        tool_errors: list[str] | None,
        plan_action: str | None,
        outcome: str,
        outcome_message: str,
        exchange_snapshot: dict[str, Any] | None,
        order_intent: dict[str, Any] | None = None,
        order_preview: dict[str, Any] | None = None,
    ) -> None:
        payload = (
            mode,
            symbol,
            timeframe,
            decision_timestamp,
            analysis_timestamp,
            (regime_snapshot or {}).get("label"),
            1 if (regime_snapshot or {}).get("trade_allowed") else 0,
            (candidate_snapshot or {}).get("direction"),
            1 if (candidate_snapshot or {}).get("candidate_setup_present") else 0,
            (final_action or {}).get("action"),
            plan_action,
            outcome,
            outcome_message,
            json.dumps(regime_snapshot or {}),
            json.dumps(candidate_snapshot or {}),
            json.dumps(raw_action or {}),
            json.dumps(final_action or {}),
            json.dumps(quality_filter_reasons or []),
            json.dumps(tool_errors or []),
            json.dumps(exchange_snapshot or {}),
            json.dumps(order_intent or {}),
            json.dumps(order_preview or {}),
            datetime.now(timezone.utc).isoformat(),
        )
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO bot_cycle_journal (
                    mode,
                    symbol,
                    timeframe,
                    decision_timestamp,
                    analysis_timestamp,
                    regime_label,
                    regime_trade_allowed,
                    candidate_direction,
                    candidate_setup_present,
                    final_action,
                    plan_action,
                    outcome,
                    outcome_message,
                    regime_snapshot,
                    candidate_snapshot,
                    raw_action_payload,
                    final_action_payload,
                    quality_filter_reasons,
                    tool_errors,
                    exchange_snapshot,
                    order_intent,
                    order_preview,
                    recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            conn.commit()

    def _initialize(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_cycle_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    decision_timestamp TEXT NOT NULL,
                    analysis_timestamp TEXT NOT NULL,
                    regime_label TEXT,
                    regime_trade_allowed INTEGER NOT NULL DEFAULT 0,
                    candidate_direction TEXT,
                    candidate_setup_present INTEGER NOT NULL DEFAULT 0,
                    final_action TEXT,
                    plan_action TEXT,
                    outcome TEXT NOT NULL,
                    outcome_message TEXT NOT NULL,
                    regime_snapshot TEXT NOT NULL,
                    candidate_snapshot TEXT NOT NULL,
                    raw_action_payload TEXT NOT NULL,
                    final_action_payload TEXT NOT NULL,
                    quality_filter_reasons TEXT NOT NULL,
                    tool_errors TEXT NOT NULL,
                    exchange_snapshot TEXT NOT NULL,
                    order_intent TEXT NOT NULL,
                    order_preview TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
