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
            self._save({"positions": {}, "pending_orders": {}, "executions": []})

    def get_open_position(self) -> Optional[Position]:
        positions = self._load()["positions"]
        for payload in positions.values():
            return Position.model_validate(payload)
        return None

    def get_pending_order(self, symbol: str) -> Optional[OrderIntent]:
        pending_orders = self._load().get("pending_orders", {})
        payload = pending_orders.get(symbol)
        if not payload:
            return None
        return OrderIntent.model_validate(payload)

    def execute(self, intent: OrderIntent) -> OrderPreview:
        ledger = self._load()
        positions = ledger["positions"]
        pending_orders = ledger.setdefault("pending_orders", {})

        self._reconcile_symbol(ledger, intent.symbol, intent.reference_price)
        positions = ledger["positions"]
        pending_orders = ledger.setdefault("pending_orders", {})

        if intent.action == TradeAction.FLAT:
            had_position = positions.pop(intent.symbol, None) is not None
            had_pending = pending_orders.pop(intent.symbol, None) is not None
            preview = OrderPreview(
                status=OrderStatus.FILLED if had_position else OrderStatus.SKIPPED,
                mode=ExecutionMode.PAPER,
                symbol=intent.symbol,
                action=intent.action,
                message=(
                    "Paper position closed."
                    if had_position
                    else "No open paper position. Pending order canceled."
                    if had_pending
                    else "No open paper position to close."
                ),
                reference_price=intent.reference_price,
                size=intent.size,
                leverage=intent.leverage,
            )
        else:
            if intent.entry_mode in (EntryMode.LIMIT, EntryMode.LIMIT_ZONE):
                if not self._can_fill_limit(intent):
                    pending_orders[intent.symbol] = intent.model_dump()
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
                pending_orders.pop(intent.symbol, None)
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

        self._append_execution(ledger, intent, preview)
        self._save(ledger)
        return preview

    def _load(self) -> dict:
        payload = json.loads(self.ledger_path.read_text())
        payload.setdefault("positions", {})
        payload.setdefault("pending_orders", {})
        payload.setdefault("executions", [])
        return payload

    def _save(self, payload: dict) -> None:
        self.ledger_path.write_text(json.dumps(payload, indent=2))

    def _append_execution(
        self,
        ledger: dict,
        intent: OrderIntent | dict,
        preview: OrderPreview,
    ) -> None:
        ledger["executions"].append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "intent": intent.model_dump() if isinstance(intent, OrderIntent) else intent,
                "result": preview.model_dump(),
            }
        )

    def _reconcile_symbol(self, ledger: dict, symbol: str, reference_price: float) -> None:
        positions = ledger.setdefault("positions", {})
        pending_orders = ledger.setdefault("pending_orders", {})

        pending_payload = pending_orders.get(symbol)
        if pending_payload:
            pending_intent = OrderIntent.model_validate(pending_payload)
            pending_intent.reference_price = reference_price
            if self._can_fill_limit(pending_intent):
                position = Position(
                    symbol=pending_intent.symbol,
                    side=pending_intent.action,
                    size=pending_intent.size,
                    entry_price=self._fill_price(pending_intent),
                    stop_loss=pending_intent.stop_loss,
                    take_profit=pending_intent.take_profit,
                    opened_at=datetime.now(timezone.utc).isoformat(),
                    mode=ExecutionMode.PAPER,
                )
                positions[symbol] = position.model_dump()
                pending_orders.pop(symbol, None)
                self._append_execution(
                    ledger,
                    pending_intent,
                    OrderPreview(
                        status=OrderStatus.FILLED,
                        mode=ExecutionMode.PAPER,
                        symbol=pending_intent.symbol,
                        action=pending_intent.action,
                        message="Paper limit order filled from pending state.",
                        reference_price=position.entry_price,
                        size=pending_intent.size,
                        leverage=pending_intent.leverage,
                        stop_loss=pending_intent.stop_loss,
                        take_profit=pending_intent.take_profit,
                    ),
                )

        position_payload = positions.get(symbol)
        if not position_payload:
            return

        position = Position.model_validate(position_payload)
        exit_reason = self._exit_reason(position, reference_price)
        if exit_reason is None:
            return

        positions.pop(symbol, None)
        self._append_execution(
            ledger,
            {
                "symbol": symbol,
                "action": "FLAT",
                "reference_price": reference_price,
                "rationale": exit_reason,
                "reduce_only": True,
            },
            OrderPreview(
                status=OrderStatus.FILLED,
                mode=ExecutionMode.PAPER,
                symbol=symbol,
                action=TradeAction.FLAT,
                message=f"Paper position closed automatically: {exit_reason}.",
                reference_price=reference_price,
                size=position.size,
                leverage=1,
            ),
        )

    def _exit_reason(self, position: Position, reference_price: float) -> Optional[str]:
        if position.side == TradeAction.LONG:
            if position.stop_loss is not None and reference_price <= position.stop_loss:
                return "stop loss hit"
            if position.take_profit is not None and reference_price >= position.take_profit:
                return "take profit hit"
            return None

        if position.stop_loss is not None and reference_price >= position.stop_loss:
            return "stop loss hit"
        if position.take_profit is not None and reference_price <= position.take_profit:
            return "take profit hit"
        return None

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
