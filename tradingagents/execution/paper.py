from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import (
    EntryMode,
    ExecutionMode,
    OrderIntent,
    OrderPreview,
    OrderStatus,
    Position,
    TradeAction,
)


class PaperBroker:
    def __init__(self, ledger_path: Path):
        self.ledger_path = ledger_path
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.ledger_path.exists():
            self._save({"positions": {}, "executions": []})

    def get_open_position(self) -> Optional[Position]:
        positions = self._load()["positions"]
        for payload in positions.values():
            return Position.model_validate(payload)
        return None

    def execute(self, intent: OrderIntent) -> OrderPreview:
        ledger = self._load()
        positions = ledger["positions"]

        if intent.action == TradeAction.FLAT:
            positions.pop(intent.symbol, None)
            preview = OrderPreview(
                status=OrderStatus.FILLED,
                mode=ExecutionMode.PAPER,
                symbol=intent.symbol,
                action=intent.action,
                message="Paper position closed.",
                reference_price=intent.reference_price,
                size=intent.size,
                leverage=intent.leverage,
            )
        else:
            if intent.entry_mode in (EntryMode.LIMIT, EntryMode.LIMIT_ZONE):
                if not self._can_fill_limit(intent):
                    preview = OrderPreview(
                        status=OrderStatus.PREVIEW,
                        mode=ExecutionMode.PAPER,
                        symbol=intent.symbol,
                        action=intent.action,
                        message="Paper limit order staged but not filled at the current price.",
                        reference_price=intent.reference_price,
                        size=intent.size,
                        leverage=intent.leverage,
                        stop_loss=intent.stop_loss,
                        take_profit=intent.take_profit,
                    )
                    ledger["executions"].append(
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "intent": intent.model_dump(),
                            "result": preview.model_dump(),
                        }
                    )
                    self._save(ledger)
                    return preview
            position = Position(
                symbol=intent.symbol,
                side=intent.action,
                size=intent.size,
                entry_price=self._fill_price(intent),
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
                opened_at=datetime.now(timezone.utc).isoformat(),
                mode=ExecutionMode.PAPER,
            )
            positions = {intent.symbol: position.model_dump()}
            ledger["positions"] = positions
            preview = OrderPreview(
                status=OrderStatus.FILLED,
                mode=ExecutionMode.PAPER,
                symbol=intent.symbol,
                action=intent.action,
                message="Paper position opened.",
                reference_price=position.entry_price,
                size=intent.size,
                leverage=intent.leverage,
                stop_loss=intent.stop_loss,
                take_profit=intent.take_profit,
            )

        ledger["executions"].append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "intent": intent.model_dump(),
                "result": preview.model_dump(),
            }
        )
        self._save(ledger)
        return preview

    def _load(self) -> dict:
        return json.loads(self.ledger_path.read_text())

    def _save(self, payload: dict) -> None:
        self.ledger_path.write_text(json.dumps(payload, indent=2))

    def _fill_price(self, intent: OrderIntent) -> float:
        if intent.entry_mode == EntryMode.LIMIT and intent.limit_price is not None:
            return intent.limit_price
        if (
            intent.entry_mode == EntryMode.LIMIT_ZONE
            and intent.limit_zone_low is not None
            and intent.limit_zone_high is not None
        ):
            return max(min(intent.reference_price, intent.limit_zone_high), intent.limit_zone_low)
        return intent.reference_price

    def _can_fill_limit(self, intent: OrderIntent) -> bool:
        if intent.entry_mode == EntryMode.LIMIT and intent.limit_price is not None:
            if intent.action == TradeAction.LONG:
                return intent.reference_price <= intent.limit_price
            return intent.reference_price >= intent.limit_price
        if (
            intent.entry_mode == EntryMode.LIMIT_ZONE
            and intent.limit_zone_low is not None
            and intent.limit_zone_high is not None
        ):
            return intent.limit_zone_low <= intent.reference_price <= intent.limit_zone_high
        return True
