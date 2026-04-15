"""Structured progress events for frontend integration.

Every interesting pipeline event is emitted as a JSON line to a
configurable sink (stdout, file, callback). This lets dashboards, CI
bots, and TUIs consume pipeline progress without parsing Rich markup.

Event schema:

    {
        "type": "stage_start" | "stage_end" | "fn_start" | "fn_iter" |
                "fn_pass" | "fn_fail" | "gate_result" | "error",
        "timestamp": "2026-04-15T12:34:56Z",
        "stage": "analyze" | "extract" | "architect" | "implement" | "verify" | "report",
        "data": { ... event-specific payload }
    }
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

@dataclass
class Event:
    type: str
    stage: str = ""
    data: dict = field(default_factory=dict)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


# ---------------------------------------------------------------------------
# Sink protocol + implementations
# ---------------------------------------------------------------------------

class EventSink(Protocol):
    def emit(self, event: Event) -> None: ...


class StdoutSink:
    """Emit JSON lines to stdout."""
    def emit(self, event: Event) -> None:
        try:
            print(event.to_json(), flush=True)
        except Exception:
            pass


class FileSink:
    """Append JSON lines to a file."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: Event) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(event.to_json() + "\n")
        except Exception:
            pass


class CallbackSink:
    """Forward events to a user-provided callback."""
    def __init__(self, callback: Callable[[Event], None]):
        self.callback = callback

    def emit(self, event: Event) -> None:
        try:
            self.callback(event)
        except Exception:
            pass


class NullSink:
    """Discard all events (default when no sink is configured)."""
    def emit(self, event: Event) -> None:
        pass


# ---------------------------------------------------------------------------
# Emitter — global singleton the pipeline modules call
# ---------------------------------------------------------------------------

class EventEmitter:
    """Central event bus. Pipeline modules call `emitter.emit(event)`."""

    def __init__(self):
        self._sinks: list[EventSink] = []

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def clear_sinks(self) -> None:
        self._sinks.clear()

    def emit(self, event: Event) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:
                pass

    # --- Convenience methods ---

    def stage_start(self, stage: str, **data) -> None:
        self.emit(Event(type="stage_start", stage=stage, data=data))

    def stage_end(self, stage: str, ok: bool, **data) -> None:
        self.emit(Event(type="stage_end", stage=stage, data={"ok": ok, **data}))

    def fn_start(self, stage: str, fn_name: str, crate: str = "", **data) -> None:
        self.emit(Event(type="fn_start", stage=stage,
                        data={"fn": fn_name, "crate": crate, **data}))

    def fn_iter(self, stage: str, fn_name: str, iteration: int, **data) -> None:
        self.emit(Event(type="fn_iter", stage=stage,
                        data={"fn": fn_name, "iteration": iteration, **data}))

    def fn_pass(self, stage: str, fn_name: str, iteration: int, **data) -> None:
        self.emit(Event(type="fn_pass", stage=stage,
                        data={"fn": fn_name, "iteration": iteration, **data}))

    def fn_fail(self, stage: str, fn_name: str, reason: str = "", **data) -> None:
        self.emit(Event(type="fn_fail", stage=stage,
                        data={"fn": fn_name, "reason": reason, **data}))

    def gate_result(self, gate: str, ok: bool, summary: str = "", **data) -> None:
        self.emit(Event(type="gate_result", stage="verify",
                        data={"gate": gate, "ok": ok, "summary": summary, **data}))

    def error(self, stage: str, message: str, **data) -> None:
        self.emit(Event(type="error", stage=stage,
                        data={"message": message, **data}))


# Module-level singleton. Import and use directly:
#   from alchemist.events import emitter
#   emitter.fn_pass("implement", "adler32", iteration=2)
emitter = EventEmitter()
