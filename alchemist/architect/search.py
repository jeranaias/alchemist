"""Architectural search — run the architect N times, validate each, pick best.

The single-shot architect design fails validation ~20% of the time on
non-trivial libraries. Running 3 parallel candidates and selecting the one
with the fewest validator errors (and fewest warnings as tiebreaker)
produces a cleaner workspace layout without burning much extra time —
LLM calls are the bottleneck, and parallel HTTP calls overlap nicely.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field

from alchemist.architect.crate_designer import CrateDesigner
from alchemist.architect.schemas import CrateArchitecture
from alchemist.architect.validator import ValidationReport, validate_architecture
from alchemist.config import AlchemistConfig
from alchemist.extractor.schemas import ModuleSpec


@dataclass
class ScoredArchitecture:
    architecture: CrateArchitecture
    report: ValidationReport
    index: int

    @property
    def error_count(self) -> int:
        return len(self.report.errors)

    @property
    def warning_count(self) -> int:
        return len(self.report.warnings)

    def score(self) -> tuple[int, int, int]:
        """Lower is better: (errors, warnings, -crate_count)."""
        return (self.error_count, self.warning_count, -len(self.architecture.crates))


@dataclass
class SearchResult:
    candidates: list[ScoredArchitecture] = field(default_factory=list)
    best: ScoredArchitecture | None = None

    @property
    def ok(self) -> bool:
        return self.best is not None and self.best.error_count == 0


def search_architecture(
    specs: list[ModuleSpec],
    *,
    project_name: str,
    source_description: str = "",
    n_candidates: int = 3,
    config: AlchemistConfig | None = None,
    max_workers: int = 3,
) -> SearchResult:
    """Generate N architectures in parallel, validate each, return the best.

    Each candidate uses a fresh CrateDesigner instance (which injects a
    unique cache-buster nonce per LLM call), so designs genuinely differ.
    """
    config = config or AlchemistConfig()
    result = SearchResult()

    def _design(idx: int) -> ScoredArchitecture:
        designer = CrateDesigner(config)
        arch = designer.design(
            specs,
            project_name=project_name,
            source_description=source_description,
        )
        report = validate_architecture(arch, specs)
        return ScoredArchitecture(architecture=arch, report=report, index=idx)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_design, i): i for i in range(n_candidates)}
        for fut in concurrent.futures.as_completed(futures):
            try:
                scored = fut.result()
                result.candidates.append(scored)
            except Exception:
                pass

    if not result.candidates:
        return result

    result.candidates.sort(key=lambda s: s.score())
    result.best = result.candidates[0]
    return result
