"""Tests for alchemist.implementer.multi_sample."""

from __future__ import annotations

from pathlib import Path

import pytest

from alchemist.implementer.multi_sample import (
    MultiSampleResult,
    SampleScore,
    _parse_test_counts,
    run_multi_sample,
)


def test_sample_score_prefers_compiling_candidates():
    compiled = SampleScore(candidate_idx=0, body="", compiled=True, tests_passed=0, length=100)
    uncompiled = SampleScore(candidate_idx=1, body="", compiled=False, tests_passed=10, length=50)
    assert compiled.score() > uncompiled.score()


def test_sample_score_tiebreaks_on_pass_rate():
    a = SampleScore(candidate_idx=0, body="", compiled=True, tests_passed=1, tests_failed=4, length=100)
    b = SampleScore(candidate_idx=1, body="", compiled=True, tests_passed=3, tests_failed=2, length=100)
    assert b.score() > a.score()


def test_sample_score_tiebreaks_on_length():
    a = SampleScore(candidate_idx=0, body="", compiled=True, tests_passed=3, tests_failed=0, length=200)
    b = SampleScore(candidate_idx=1, body="", compiled=True, tests_passed=3, tests_failed=0, length=100)
    assert b.score() > a.score()


# ---------- run_multi_sample end-to-end ----------

def test_run_multi_sample_picks_highest_pass_rate(tmp_path):
    f = tmp_path / "m.rs"
    f.write_text("orig", encoding="utf-8")

    generated = {}
    def sampler(idx: int) -> str:
        body = f"impl_{idx}"
        generated[idx] = body
        return body

    applied: list[str] = []
    def splicer(body: str) -> bool:
        applied.append(body)
        f.write_text(body, encoding="utf-8")
        return True

    # Simulate: candidates 0 and 1 fail compile, 2 passes 1/2 tests, 3 passes 2/2.
    eval_results = iter([
        (False, 0, 0, "fail 0"),
        (False, 0, 0, "fail 1"),
        (True,  1, 1, ""),
        (True,  2, 0, ""),
    ])
    def evaluator():
        return next(eval_results)

    result = run_multi_sample(
        sampler=sampler,
        splicer=splicer,
        evaluator=evaluator,
        original_source="orig",
        file_path=f,
        n_samples=4,
        max_workers=4,
    )
    assert result.ok
    assert result.best.tests_passed == 2
    assert result.best.tests_failed == 0
    # Final file content should be the winning candidate
    assert f.read_text() == result.best.body


def test_run_multi_sample_all_failed(tmp_path):
    f = tmp_path / "m.rs"
    f.write_text("orig", encoding="utf-8")

    def sampler(idx): return None  # every sample fails

    def splicer(body): return True
    def evaluator(): return (False, 0, 0, "")

    result = run_multi_sample(
        sampler=sampler, splicer=splicer, evaluator=evaluator,
        original_source="orig", file_path=f, n_samples=4,
    )
    assert result.all_failed
    assert not result.ok


def test_run_multi_sample_rejects_stubs_upfront(tmp_path):
    f = tmp_path / "m.rs"
    f.write_text("orig", encoding="utf-8")

    def sampler(idx):
        if idx == 0:
            return "fn x() { 42 }"
        return "fn x() { unimplemented!() }"   # other candidates are stubs

    def splicer(body):
        f.write_text(body, encoding="utf-8")
        return True

    def evaluator():
        return (True, 1, 0, "")

    def reject_stub(body):
        return "unimplemented!" in body

    result = run_multi_sample(
        sampler=sampler, splicer=splicer, evaluator=evaluator,
        original_source="orig", file_path=f, n_samples=4,
        reject_stub=reject_stub,
    )
    assert result.ok
    assert result.best.body == "fn x() { 42 }"


# ---------- test-count parser ----------

def test_parse_test_counts_basic():
    text = "test result: ok. 7 passed; 0 failed; 0 ignored"
    assert _parse_test_counts(text) == (7, 0)


def test_parse_test_counts_with_failures():
    text = """
    running 15 tests
    test result: FAILED. 10 passed; 5 failed; 0 ignored
    """
    assert _parse_test_counts(text) == (10, 5)


def test_parse_test_counts_multiple_crates():
    text = """
    test result: ok. 3 passed; 0 failed; ...
    test result: FAILED. 2 passed; 1 failed; ...
    """
    assert _parse_test_counts(text) == (5, 1)


def test_parse_test_counts_no_match():
    assert _parse_test_counts("nothing here") == (0, 0)
