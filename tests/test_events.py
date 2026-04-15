"""Tests for alchemist.events."""

from __future__ import annotations

import json

from alchemist.events import (
    CallbackSink,
    Event,
    EventEmitter,
    FileSink,
    NullSink,
    StdoutSink,
)


def test_event_to_json_includes_all_fields():
    e = Event(type="stage_start", stage="analyze", data={"files": 10})
    j = json.loads(e.to_json())
    assert j["type"] == "stage_start"
    assert j["stage"] == "analyze"
    assert j["data"]["files"] == 10
    assert j["timestamp"]


def test_event_auto_timestamps():
    e1 = Event(type="x")
    e2 = Event(type="x")
    assert e1.timestamp
    assert e2.timestamp


def test_callback_sink_receives_events():
    received = []
    sink = CallbackSink(lambda e: received.append(e))
    emitter = EventEmitter()
    emitter.add_sink(sink)
    emitter.stage_start("analyze", files=5)
    assert len(received) == 1
    assert received[0].type == "stage_start"
    assert received[0].data["files"] == 5


def test_file_sink_appends_json_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = FileSink(path)
    emitter = EventEmitter()
    emitter.add_sink(sink)
    emitter.fn_pass("implement", "adler32", iteration=2)
    emitter.fn_fail("implement", "crc32", reason="compile error")
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "fn_pass"
    assert json.loads(lines[1])["data"]["reason"] == "compile error"


def test_null_sink_swallows_events():
    emitter = EventEmitter()
    emitter.add_sink(NullSink())
    emitter.error("x", "y")  # should not raise


def test_emitter_convenience_methods():
    collected = []
    emitter = EventEmitter()
    emitter.add_sink(CallbackSink(lambda e: collected.append(e.type)))
    emitter.stage_start("analyze")
    emitter.stage_end("analyze", ok=True)
    emitter.fn_start("implement", "adler32")
    emitter.fn_iter("implement", "adler32", iteration=1)
    emitter.fn_pass("implement", "adler32", iteration=2)
    emitter.fn_fail("implement", "crc32", reason="bad")
    emitter.gate_result("anti-stub", ok=False, summary="3 violations")
    emitter.error("extract", "server down")
    assert collected == [
        "stage_start", "stage_end", "fn_start", "fn_iter",
        "fn_pass", "fn_fail", "gate_result", "error",
    ]


def test_clear_sinks():
    collected = []
    emitter = EventEmitter()
    emitter.add_sink(CallbackSink(lambda e: collected.append(e)))
    emitter.stage_start("x")
    assert len(collected) == 1
    emitter.clear_sinks()
    emitter.stage_start("y")
    assert len(collected) == 1  # no new events after clear


def test_sink_crash_does_not_propagate():
    def crasher(e):
        raise RuntimeError("boom")
    emitter = EventEmitter()
    emitter.add_sink(CallbackSink(crasher))
    emitter.stage_start("x")  # should not raise
