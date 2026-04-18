"""Tests for the Stage 5 mandatory differential gate."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from alchemist.implementer.anti_stub import ScanReport
from alchemist.verifier.differential_tester import (
    DifferentialConfig,
    DifferentialTester,
    GateResult,
    VerificationReport,
    verify_workspace,
)


def _fake_gate(name: str, ok: bool, summary: str = "") -> GateResult:
    return GateResult(name=name, passed=ok, summary=summary)


def test_report_passed_requires_all_gates():
    r = VerificationReport(
        compile=_fake_gate("compile", True),
        anti_stub=_fake_gate("anti-stub", True),
        no_unsafe=_fake_gate("no-unsafe", True),
        test=_fake_gate("test", True),
        differential=_fake_gate("differential", True),
    )
    assert r.passed


def test_report_fails_if_any_gate_fails():
    for fail in ("compile", "anti_stub", "no_unsafe", "test", "differential"):
        gates = {
            "compile": _fake_gate("compile", True),
            "anti_stub": _fake_gate("anti-stub", True),
            "no_unsafe": _fake_gate("no-unsafe", True),
            "test": _fake_gate("test", True),
            "differential": _fake_gate("differential", True),
        }
        gates[fail] = _fake_gate(fail, False, "simulated failure")
        r = VerificationReport(**gates)
        assert not r.passed
        assert r.first_failure is not None


def test_missing_diff_config_refuses_success(tmp_path):
    """If no diff_config is supplied, the gate must refuse to declare success."""
    # Make a throwaway Cargo workspace so compile/test commands run cleanly
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "dummy"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("pub fn ok() -> u32 { 42 }\n", encoding="utf-8")

    tester = DifferentialTester(tmp_path, diff_config=None)
    result = tester.gate_differential()
    assert not result.passed
    assert "REFUSING" in result.summary


def test_run_all_short_circuits_on_compile_failure(tmp_path):
    # Compile-failing workspace
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "broken"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("fn broken( -> {\n", encoding="utf-8")

    tester = DifferentialTester(tmp_path)
    with patch.object(tester, "gate_test") as mock_test, \
         patch.object(tester, "gate_differential") as mock_diff:
        report = tester.run_all()
        # Tests and diff must NOT have been invoked when compile fails
        mock_test.assert_not_called()
        mock_diff.assert_not_called()
    assert not report.compile.passed
    assert not report.passed


def test_anti_stub_gate_is_informational_even_on_compile_failure(tmp_path):
    """We still want anti-stub results when compile fails, for diagnostics."""
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "broken"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        "pub fn x() { unimplemented!() }\n", encoding="utf-8")

    tester = DifferentialTester(tmp_path)
    report = tester.run_all()
    assert report.anti_stub.anti_stub_report is not None
    assert not report.anti_stub.passed


def test_verify_workspace_is_public_api(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "broken"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        "pub fn x() { unimplemented!() }\n", encoding="utf-8")

    report = verify_workspace(tmp_path)
    # Must return a VerificationReport; compile may or may not pass
    assert isinstance(report, VerificationReport)


def test_verification_summary_string_contains_all_gates():
    r = VerificationReport(
        compile=_fake_gate("compile", True, "clean"),
        anti_stub=_fake_gate("anti-stub", False, "3 violations"),
        no_unsafe=_fake_gate("no-unsafe", True, "zero unsafe"),
        test=_fake_gate("test", True, "10 passed"),
        differential=_fake_gate("differential", False, "no config"),
    )
    s = r.summary()
    assert "compile" in s
    assert "anti-stub" in s
    assert "test" in s
    assert "diff" in s
    assert "FAIL" in s


def test_anti_stub_gate_flags_real_zlib_stubs():
    """Integration: run the anti-stub gate against the current zlib output."""
    zlib_root = Path(__file__).parent.parent / "subjects" / "zlib" / ".alchemist" / "output"
    if not zlib_root.exists():
        pytest.skip("zlib output not generated")
    tester = DifferentialTester(zlib_root)
    result = tester.gate_anti_stub()
    assert not result.passed, "Expected stubs in current zlib output"
    assert result.anti_stub_report is not None
    assert len(result.anti_stub_report.violations) >= 18
