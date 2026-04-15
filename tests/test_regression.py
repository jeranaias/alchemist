"""Tests for alchemist.implementer.regression."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from alchemist.implementer.regression import (
    RegressionReport,
    RegressionResult,
    check_workspace_regression,
)


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not on PATH")
def test_clean_workspace_passes_regression(tmp_path):
    # Create a minimal passing workspace
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nresolver = "2"\nmembers = ["crateA"]\n', encoding="utf-8")
    ca = tmp_path / "crateA"
    (ca / "src").mkdir(parents=True)
    (ca / "Cargo.toml").write_text(
        '[package]\nname = "crateA"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
    (ca / "src" / "lib.rs").write_text(
        "pub fn hello() -> u32 { 42 }\n"
        "#[test] fn t() { assert_eq!(hello(), 42); }\n", encoding="utf-8")

    report = check_workspace_regression(tmp_path)
    assert report.ok
    assert len(report.results) == 1
    assert report.results[0].compile_ok
    assert report.results[0].tests_ok


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not on PATH")
def test_broken_workspace_reports_regression(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nresolver = "2"\nmembers = ["crateA"]\n', encoding="utf-8")
    ca = tmp_path / "crateA"
    (ca / "src").mkdir(parents=True)
    (ca / "Cargo.toml").write_text(
        '[package]\nname = "crateA"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
    (ca / "src" / "lib.rs").write_text("fn broken( -> {\n", encoding="utf-8")

    report = check_workspace_regression(tmp_path)
    assert not report.ok
    assert len(report.regressions) == 1
    assert not report.regressions[0].compile_ok


def test_regression_report_shape():
    r = RegressionReport(results=[
        RegressionResult(crate_name="a", compile_ok=True, tests_ok=True),
        RegressionResult(crate_name="b", compile_ok=True, tests_ok=False, error_summary="test fail"),
    ])
    assert not r.ok
    assert len(r.regressions) == 1
    assert "b" in r.summary()
