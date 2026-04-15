"""Tests for alchemist.implementer.parallel."""

from __future__ import annotations

from alchemist.architect.schemas import CrateArchitecture, CrateSpec
from alchemist.implementer.parallel import (
    ParallelBatch,
    compute_batches,
    run_parallel_stage4,
)


def _arch(crate_deps: dict[str, list[str]]) -> CrateArchitecture:
    return CrateArchitecture(
        workspace_name="test",
        description="",
        crates=[
            CrateSpec(name=n, description="", modules=[], dependencies=d)
            for n, d in crate_deps.items()
        ],
    )


def test_independent_crates_single_batch():
    arch = _arch({"a": [], "b": [], "c": []})
    batches = compute_batches(arch)
    assert len(batches) == 1
    assert set(batches[0].crate_names) == {"a", "b", "c"}


def test_linear_chain_three_batches():
    arch = _arch({"a": [], "b": ["a"], "c": ["b"]})
    batches = compute_batches(arch)
    assert len(batches) == 3
    assert batches[0].crate_names == ["a"]
    assert batches[1].crate_names == ["b"]
    assert batches[2].crate_names == ["c"]


def test_diamond_dependency():
    arch = _arch({"types": [], "left": ["types"], "right": ["types"], "top": ["left", "right"]})
    batches = compute_batches(arch)
    assert len(batches) == 3
    assert batches[0].crate_names == ["types"]
    assert set(batches[1].crate_names) == {"left", "right"}
    assert batches[2].crate_names == ["top"]


def test_run_parallel_collects_results():
    arch = _arch({"a": [], "b": [], "c": ["a"]})
    results_collected = []

    def gen(name):
        results_collected.append(name)
        return f"done_{name}"

    result = run_parallel_stage4(arch, gen, max_workers=4)
    assert result.ok
    assert set(result.results.keys()) == {"a", "b", "c"}
    assert all(v.startswith("done_") for v in result.results.values())


def test_run_parallel_catches_errors():
    arch = _arch({"good": [], "bad": []})

    def gen(name):
        if name == "bad":
            raise RuntimeError("simulated crash")
        return "ok"

    result = run_parallel_stage4(arch, gen)
    assert not result.ok
    assert "bad" in result.errors
    assert "good" in result.results
