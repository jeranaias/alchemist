"""Tests for alchemist.verifier.amplifier."""

from __future__ import annotations

import zlib

import pytest

from alchemist.extractor.schemas import TestVector
from alchemist.verifier.amplifier import (
    CHECKSUM_STRATEGY,
    HASH_STRATEGY,
    AmplifyReport,
    InputStrategy,
    Mismatch,
    _bytes_to_rust_literal,
    amplify,
    fold_mismatches_into_spec,
)


# ---------- Strategy generation ----------

def test_strategy_yields_preset_then_random():
    strat = InputStrategy(
        name="test", min_len=0, max_len=4,
        preset=[b"first", b"second"],
    )
    samples = list(strat.generate(10, seed=42))
    assert samples[0] == b"first"
    assert samples[1] == b"second"
    assert len(samples) == 10
    # remaining 8 are random, length 0..4
    for s in samples[2:]:
        assert 0 <= len(s) <= 4


def test_strategy_deterministic_with_seed():
    strat = InputStrategy(name="test", min_len=0, max_len=32)
    a = list(strat.generate(20, seed=123))
    b = list(strat.generate(20, seed=123))
    assert a == b


def test_strategy_preset_only_when_n_smaller():
    strat = InputStrategy(
        name="test", min_len=0, max_len=4,
        preset=[b"a", b"b", b"c"],
    )
    samples = list(strat.generate(2, seed=1))
    assert samples == [b"a", b"b"]


# ---------- amplify() ----------

class _MatchingRunner:
    def __init__(self, fn): self.fn = fn
    def run_c(self, data): return self.fn(data)
    def run_rust(self, data): return self.fn(data)


class _DivergingRunner:
    """Agrees on ASCII input, diverges on any byte > 0x7F."""
    def run_c(self, data):
        return zlib.crc32(data).to_bytes(4, "big")
    def run_rust(self, data):
        # Pretend to be CRC-32 but actually return just adler32 — they agree
        # only on very specific inputs.
        return zlib.adler32(data).to_bytes(4, "big")


def test_amplify_finds_no_mismatch_when_impls_agree():
    runner = _MatchingRunner(lambda d: zlib.crc32(d).to_bytes(4, "big"))
    report = amplify(runner, CHECKSUM_STRATEGY, iterations=50, seed=1)
    assert report.ok
    assert report.iterations_run >= 10


def test_amplify_stops_on_first_mismatch():
    runner = _DivergingRunner()
    report = amplify(runner, CHECKSUM_STRATEGY, iterations=100, seed=1, stop_after_mismatches=1)
    assert not report.ok
    assert len(report.mismatches) == 1
    assert report.stopped_early
    # The first preset is b"" which happens to agree (both=0 / 1 packed to bytes);
    # but b"a" will diverge.
    first = report.mismatches[0]
    assert first.c_output != first.rust_output


def test_amplify_collects_multiple_mismatches():
    runner = _DivergingRunner()
    report = amplify(runner, CHECKSUM_STRATEGY, iterations=50, seed=1, stop_after_mismatches=3)
    assert len(report.mismatches) <= 3


def test_amplify_handles_runner_crash():
    class _Crasher:
        def run_c(self, data): raise RuntimeError("boom")
        def run_rust(self, data): return b""
    report = amplify(_Crasher(), CHECKSUM_STRATEGY, iterations=5, seed=1, stop_after_mismatches=1)
    # crash counts as mismatch
    assert not report.ok


# ---------- Rust literal encoding ----------

def test_bytes_to_rust_literal_empty():
    assert _bytes_to_rust_literal(b"") == "&[]"


def test_bytes_to_rust_literal_bytes():
    assert _bytes_to_rust_literal(b"abc") == "&[0x61, 0x62, 0x63]"


def test_bytes_to_rust_literal_high_bits():
    assert _bytes_to_rust_literal(b"\xff\x00") == "&[0xff, 0x00]"


# ---------- Mismatch → TestVector ----------

def test_mismatch_becomes_test_vector():
    mm = Mismatch(
        input_bytes=b"hello",
        c_output=b"\x00\x01\x02\x03",
        rust_output=b"\xde\xad\xbe\xef",
        index=42,
    )
    tv = mm.as_test_vector()
    assert tv.inputs["input"] == "&[0x68, 0x65, 0x6c, 0x6c, 0x6f]"
    assert tv.expected_output == "&[0x00, 0x01, 0x02, 0x03]"
    assert "Amplifier mismatch" in tv.description
    assert tv.tolerance == "exact"


def test_fold_mismatches_deduplicates():
    existing = [
        TestVector(
            description="orig", inputs={"input": '&[0x61]'},
            expected_output="&[0x00, 0x01]", tolerance="exact",
        ),
    ]
    mm1 = Mismatch(input_bytes=b"a", c_output=b"\x00\x01", rust_output=b"\x00\x02", index=1)
    mm2 = Mismatch(input_bytes=b"b", c_output=b"\x00\x03", rust_output=b"\x00\x04", index=2)
    # mm1 has the same input as the existing test vector (skipped).
    out = fold_mismatches_into_spec(existing, [mm1, mm2])
    assert len(out) == 2
    assert out[0].description == "orig"
    assert out[1].inputs["input"] == "&[0x62]"


def test_fold_mismatches_respects_max_to_add():
    mismatches = [
        Mismatch(input_bytes=bytes([i]), c_output=b"\x00", rust_output=b"\x01", index=i)
        for i in range(10)
    ]
    out = fold_mismatches_into_spec([], mismatches, max_to_add=3)
    assert len(out) == 3


# ---------- Report summary ----------

def test_amplify_report_ok_summary():
    r = AmplifyReport(iterations_run=1000, mismatches=[], elapsed_seconds=2.5)
    assert "0 mismatches" in r.summary()


def test_amplify_report_fail_summary():
    r = AmplifyReport(
        iterations_run=500,
        mismatches=[Mismatch(
            input_bytes=b"x", c_output=b"\x01\x02", rust_output=b"\x03\x04", index=7,
        )],
    )
    assert "FAIL" in r.summary()
    assert "7/500" in r.summary()
