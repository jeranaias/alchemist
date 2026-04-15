"""Tests for alchemist.architect.search."""

from __future__ import annotations

import pytest

from alchemist.architect.schemas import CrateArchitecture, CrateSpec
from alchemist.architect.search import ScoredArchitecture, SearchResult, search_architecture
from alchemist.architect.validator import ValidationReport, ValidationIssue, Severity
from alchemist.extractor.schemas import AlgorithmSpec, ModuleSpec, Parameter


def _make_report(n_errors=0, n_warnings=0):
    r = ValidationReport()
    for i in range(n_errors):
        r.add(ValidationIssue(rule="test", severity=Severity.error, message=f"err{i}"))
    for i in range(n_warnings):
        r.add(ValidationIssue(rule="test", severity=Severity.warning, message=f"warn{i}"))
    return r


def _make_arch(n_crates=1):
    return CrateArchitecture(
        workspace_name="test",
        description="",
        crates=[CrateSpec(name=f"c{i}", description="", modules=[]) for i in range(n_crates)],
    )


def test_scored_architecture_sorting():
    a = ScoredArchitecture(architecture=_make_arch(2), report=_make_report(0, 1), index=0)
    b = ScoredArchitecture(architecture=_make_arch(2), report=_make_report(1, 0), index=1)
    c = ScoredArchitecture(architecture=_make_arch(2), report=_make_report(0, 0), index=2)
    ranked = sorted([a, b, c], key=lambda s: s.score())
    assert ranked[0].index == 2  # 0 errors, 0 warnings
    assert ranked[1].index == 0  # 0 errors, 1 warning
    assert ranked[2].index == 1  # 1 error


def test_search_result_ok_requires_zero_errors():
    best = ScoredArchitecture(architecture=_make_arch(), report=_make_report(1, 0), index=0)
    r = SearchResult(candidates=[best], best=best)
    assert not r.ok

    clean = ScoredArchitecture(architecture=_make_arch(), report=_make_report(0, 2), index=0)
    r2 = SearchResult(candidates=[clean], best=clean)
    assert r2.ok


def test_search_result_empty_is_not_ok():
    assert not SearchResult().ok
