"""Tests for alchemist.implementer.decomposed."""

from __future__ import annotations

import pytest

from alchemist.implementer.decomposed import (
    DecomposedGenerator,
    DecomposedResult,
    StepResult,
    should_decompose,
)


# ---------- should_decompose heuristic ----------

@pytest.mark.parametrize("name,desc,expected", [
    ("crc32", "CRC-32 checksum", True),
    ("deflate", "DEFLATE compression", True),
    ("sha256", "SHA-256 hash", True),
    ("aes_encrypt", "AES-128 block cipher", True),
    ("adler32", "Adler-32 checksum using lookup table", True),
    ("add", "simple integer addition", False),
    ("to_upper", "ASCII upper-casing", False),
])
def test_should_decompose_heuristic(name, desc, expected):
    assert should_decompose(name, desc) == expected


# ---------- DecomposedGenerator end-to-end (mocked LLM + cargo) ----------

class _MockHarness:
    """Simulate the LLM / cargo tool surface without real subprocess calls."""
    def __init__(self, responses: dict, check_results: list, test_results: list):
        self.responses = responses
        self.check_results = iter(check_results)
        self.test_results = iter(test_results)
        self.sources: list[str] = []
        self.constants_source = ""
        self.restored = 0

    def call_llm(self, prompt, step):
        return self.responses.get(step)

    def check_crate(self):
        return next(self.check_results)

    def run_tests(self, test_filter):
        return next(self.test_results)

    def splice_whole_fn(self, body):
        self.sources.append(body)
        return True

    def splice_constants(self, src):
        self.constants_source = src
        return True

    def restore(self):
        self.restored += 1


def test_decomposed_happy_path():
    harness = _MockHarness(
        responses={
            "constants": "const X: u32 = 42;",
            "shape":     "pub fn f() -> u32 { X }",
            "body":      "pub fn f() -> u32 { X }",
        },
        check_results=[(True, ""), (True, ""), (True, "")],
        test_results=[(1, 0, "1 passed")],
    )
    gen = DecomposedGenerator(
        call_llm=harness.call_llm,
        check_crate=harness.check_crate,
        run_tests=harness.run_tests,
        splice_whole_fn=harness.splice_whole_fn,
        splice_constants=harness.splice_constants,
        restore=harness.restore,
    )
    result = gen.generate(
        algorithm_name="f", description="test", math="", standards=[],
        signature="pub fn f() -> u32",
        test_filter="test_f_",
    )
    assert result.ok
    assert [s.step for s in result.steps] == ["constants", "shape", "body"]
    assert harness.restored == 0


def test_decomposed_bails_on_constants_compile_fail():
    harness = _MockHarness(
        responses={"constants": "const BAD: = ;"},
        check_results=[(False, "syntax error")],
        test_results=[],
    )
    gen = DecomposedGenerator(
        call_llm=harness.call_llm,
        check_crate=harness.check_crate,
        run_tests=harness.run_tests,
        splice_whole_fn=harness.splice_whole_fn,
        splice_constants=harness.splice_constants,
        restore=harness.restore,
    )
    result = gen.generate(
        algorithm_name="f", description="", math="", standards=[],
        signature="pub fn f()", test_filter="",
    )
    assert not result.ok
    assert result.steps[0].step == "constants"
    assert not result.steps[0].success
    assert harness.restored == 1


def test_decomposed_edge_case_fix_succeeds():
    """Body compiles and passes 0/1 tests. Edge-case fix flips to 1/0 pass."""
    harness = _MockHarness(
        responses={
            "constants": "const X: u32 = 0;",
            "shape":     "pub fn f() -> u32 { 0 }",
            "body":      "pub fn f() -> u32 { 0 }",
            "edge_cases": "pub fn f() -> u32 { 42 }",
        },
        check_results=[
            (True, ""),        # constants OK
            (True, ""),        # shape OK
            (True, ""),        # body compiles
            (True, ""),        # edge-case iter 1 compiles
        ],
        test_results=[
            (0, 1, "1 failed"),  # body fails test
            (1, 0, "1 passed"),  # edge-case iter 1 passes
        ],
    )
    gen = DecomposedGenerator(
        call_llm=harness.call_llm,
        check_crate=harness.check_crate,
        run_tests=harness.run_tests,
        splice_whole_fn=harness.splice_whole_fn,
        splice_constants=harness.splice_constants,
        restore=harness.restore,
    )
    result = gen.generate(
        algorithm_name="f", description="", math="", standards=[],
        signature="pub fn f() -> u32", test_filter="test_f_",
    )
    assert result.ok
    assert result.steps[-1].step == "edge_cases"
    assert result.steps[-1].success


def test_step_result_ok_requires_all_success():
    dr = DecomposedResult(algorithm="f", steps=[
        StepResult(step="constants", success=True),
        StepResult(step="shape", success=False, error="x"),
    ])
    assert not dr.ok
    assert dr.last_error() == "x"


def test_empty_result_is_not_ok():
    dr = DecomposedResult(algorithm="f", steps=[])
    assert not dr.ok
