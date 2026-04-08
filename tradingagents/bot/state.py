from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import BotEvent, BotState


class BotStateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(BotState(), [])

    def load(self) -> tuple[BotState, list[BotEvent]]:
        payload = json.loads(self.path.read_text())
        state = BotState.model_validate(payload.get("state", {}))
        events = [BotEvent.model_validate(event) for event in payload.get("events", [])]
        return state, events

    def save(self, state: BotState, events: list[BotEvent]) -> None:
        payload = {
            "state": state.model_dump(),
            "events": [event.model_dump() for event in events[-500:]],
        }
        self.path.write_text(json.dumps(payload, indent=2))

    def append_event(self, state: BotState, events: list[BotEvent], *, event_type: str, message: str, payload: dict | None = None) -> list[BotEvent]:
        events = list(events)
        events.append(
            BotEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type=event_type,
                message=message,
                payload=payload or {},
            )
        )
        self.save(state, events)
        return events
