"""Parallel Stage 4 — generate independent algorithms concurrently.

Algorithms in the same module share a source file and must be serialized
(the splicing and cargo check operate on the same directory). But algorithms
in DIFFERENT crates can run fully in parallel — they have independent
source trees, independent cargo check targets, and independent test suites.

This module partitions the work into parallelizable batches based on the
crate dependency graph: crates with no unfinished dependencies can run in
the same batch. Each batch runs in a ThreadPoolExecutor (LLM calls are
I/O-bound, cargo is subprocess-bound — both benefit from parallelism).

Wall-time reduction: 3-5x on libraries with ≥3 independent crate chains.
"""

from __future__ import annotations

import concurrent.futures
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from alchemist.architect.schemas import CrateArchitecture


@dataclass
class ParallelBatch:
    """A set of crates that can be generated in parallel."""
    batch_index: int
    crate_names: list[str] = field(default_factory=list)


def compute_batches(arch: CrateArchitecture) -> list[ParallelBatch]:
    """Partition crates into parallelizable batches using topological levels.

    Level 0 = crates with no dependencies (can all run in parallel).
    Level 1 = crates whose dependencies are all in level 0 (run after level 0).
    etc.
    """
    crate_names = {c.name for c in arch.crates}
    deps: dict[str, set[str]] = {
        c.name: {d for d in c.dependencies if d in crate_names}
        for c in arch.crates
    }

    levels: dict[str, int] = {}
    remaining = set(crate_names)

    level = 0
    while remaining:
        # Find crates whose deps are all assigned to earlier levels
        ready = {
            n for n in remaining
            if all(d in levels for d in deps[n])
        }
        if not ready:
            # Cycle — assign everything remaining to the next level
            ready = remaining.copy()
        for n in ready:
            levels[n] = level
        remaining -= ready
        level += 1

    # Group by level
    by_level: dict[int, list[str]] = defaultdict(list)
    for name, lvl in sorted(levels.items(), key=lambda x: x[1]):
        by_level[lvl].append(name)

    return [
        ParallelBatch(batch_index=i, crate_names=sorted(names))
        for i, names in sorted(by_level.items())
    ]


@dataclass
class ParallelResult:
    batches: list[ParallelBatch] = field(default_factory=list)
    results: dict[str, object] = field(default_factory=dict)
    # crate_name -> whatever the per-crate generator returns
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"parallel stage 4: {len(self.batches)} batch(es), "
            f"{len(self.results)} crates generated, "
            f"{len(self.errors)} errors"
        )


# Type for the per-crate generation function.
# Takes (crate_name) and returns an arbitrary result object.
CrateGenerator = Callable[[str], object]


def run_parallel_stage4(
    arch: CrateArchitecture,
    generate_crate: CrateGenerator,
    *,
    max_workers: int = 4,
) -> ParallelResult:
    """Execute Stage 4 with crate-level parallelism.

    `generate_crate(crate_name)` is called once per crate. Crates in the
    same batch run concurrently; batches run sequentially (each batch waits
    for all its crates before the next batch starts).
    """
    batches = compute_batches(arch)
    result = ParallelResult(batches=batches)

    for batch in batches:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(generate_crate, name): name
                for name in batch.crate_names
            }
            for fut in concurrent.futures.as_completed(futures):
                name = futures[fut]
                try:
                    crate_result = fut.result()
                    result.results[name] = crate_result
                except Exception as e:
                    result.errors[name] = str(e)[:500]

    return result
