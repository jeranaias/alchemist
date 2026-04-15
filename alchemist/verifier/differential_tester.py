"""Stage 5 — mandatory differential verification gate.

Contract: the pipeline CANNOT declare success unless every generated crate
passes four layers of checks:

  1. COMPILE   — `cargo check --workspace` exits clean.
  2. ANTI-STUB — no stub markers, no fake code (anti_stub.scan_workspace).
  3. TEST      — `cargo test --workspace` passes.
  4. DIFFERENTIAL — every configured harness passes against C reference.

If any layer fails, `run_all()` returns a VerificationReport with `passed=False`
and a reason populated. Stage 5 callers MUST refuse to declare success when
this returns False.

This module leverages the sibling auto_ffi.py and proptest_gen.py: those
build the C DLL and emit the Rust harness, this module sequences the gate.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from alchemist.implementer.anti_stub import ScanReport, scan_workspace
from alchemist.verifier.auto_ffi import (
    AutoFfiRequest,
    AutoFfiResult,
    CSignature,
    TypedefMap,
    generate_ffi_crate,
)
from alchemist.verifier.proptest_gen import (
    AlgorithmHarness,
    write_differential_test,
)

console = Console(force_terminal=True, legacy_windows=False)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    """Result of a single gate."""
    name: str
    passed: bool
    summary: str = ""
    stdout: str = ""
    stderr: str = ""
    # For anti-stub gate: carries the violation list
    anti_stub_report: ScanReport | None = None


@dataclass
class VerificationReport:
    compile: GateResult
    anti_stub: GateResult
    test: GateResult
    differential: GateResult

    @property
    def passed(self) -> bool:
        return (
            self.compile.passed
            and self.anti_stub.passed
            and self.test.passed
            and self.differential.passed
        )

    @property
    def first_failure(self) -> GateResult | None:
        for g in (self.compile, self.anti_stub, self.test, self.differential):
            if not g.passed:
                return g
        return None

    def summary(self) -> str:
        def mark(g: GateResult) -> str:
            return "PASS" if g.passed else "FAIL"
        return (
            f"[compile    {mark(self.compile)}] {self.compile.summary}\n"
            f"[anti-stub  {mark(self.anti_stub)}] {self.anti_stub.summary}\n"
            f"[test       {mark(self.test)}] {self.test.summary}\n"
            f"[diff       {mark(self.differential)}] {self.differential.summary}\n"
            f"OVERALL: {'PASS' if self.passed else 'FAIL'}"
        )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@dataclass
class DifferentialConfig:
    """Everything needed to build and run the differential harness."""
    c_sources: list[Path] = field(default_factory=list)
    c_include_dirs: list[Path] = field(default_factory=list)
    c_public_signatures: list[CSignature] = field(default_factory=list)
    c_typedefs: TypedefMap = field(default_factory=TypedefMap)
    c_opaque_types: set[str] = field(default_factory=set)
    harnesses: list[AlgorithmHarness] = field(default_factory=list)
    # Used as the name of the generated FFI crate + DLL.
    ffi_crate_name: str = "c_reference"
    # Optional path under which to emit the FFI crate + differential test crate.
    # If None, uses `<rust_workspace>/verify/`.
    verify_dir: Path | None = None


class DifferentialTester:
    """Stage 5 gate: compile → anti-stub → test → differential."""

    def __init__(
        self,
        rust_workspace: Path,
        *,
        diff_config: DifferentialConfig | None = None,
        timeout_compile: int = 300,
        timeout_test: int = 600,
        timeout_diff: int = 900,
    ):
        self.rust_workspace = Path(rust_workspace)
        self.diff_config = diff_config
        self.timeout_compile = timeout_compile
        self.timeout_test = timeout_test
        self.timeout_diff = timeout_diff

    # --- Individual gates ---

    def gate_compile(self) -> GateResult:
        console.print("[cyan]gate 1/4: cargo check --workspace[/cyan]")
        try:
            r = subprocess.run(
                ["cargo", "check", "--workspace", "--all-targets"],
                cwd=str(self.rust_workspace),
                capture_output=True,
                text=True,
                timeout=self.timeout_compile,
            )
        except subprocess.TimeoutExpired as e:
            return GateResult(
                name="compile",
                passed=False,
                summary=f"cargo check timed out after {self.timeout_compile}s",
                stderr=str(e),
            )
        errors = r.stderr.count("error[") + r.stderr.count("error:")
        return GateResult(
            name="compile",
            passed=r.returncode == 0,
            summary=(
                f"cargo check clean" if r.returncode == 0
                else f"{errors} compile errors"
            ),
            stdout=r.stdout,
            stderr=r.stderr,
        )

    def gate_anti_stub(self) -> GateResult:
        console.print("[cyan]gate 2/4: anti-stub scan[/cyan]")
        report = scan_workspace(self.rust_workspace)
        return GateResult(
            name="anti-stub",
            passed=report.ok,
            summary=report.summary().splitlines()[0],
            anti_stub_report=report,
        )

    def gate_test(self) -> GateResult:
        console.print("[cyan]gate 3/4: cargo test --workspace[/cyan]")
        try:
            r = subprocess.run(
                ["cargo", "test", "--workspace", "--", "--nocapture"],
                cwd=str(self.rust_workspace),
                capture_output=True,
                text=True,
                timeout=self.timeout_test,
            )
        except subprocess.TimeoutExpired as e:
            return GateResult(
                name="test",
                passed=False,
                summary=f"cargo test timed out after {self.timeout_test}s",
                stderr=str(e),
            )
        passed = r.returncode == 0
        # Roughly count test counts
        lines = (r.stdout + "\n" + r.stderr).splitlines()
        result_lines = [l for l in lines if "test result:" in l]
        summary = "; ".join(result_lines[-4:]) or (
            "cargo test passed" if passed else "cargo test failed"
        )
        return GateResult(
            name="test",
            passed=passed,
            summary=summary,
            stdout=r.stdout,
            stderr=r.stderr,
        )

    def gate_differential(self) -> GateResult:
        console.print("[cyan]gate 4/4: differential tests[/cyan]")
        if not self.diff_config or not self.diff_config.harnesses:
            return GateResult(
                name="differential",
                passed=False,
                summary="no differential config provided — REFUSING to claim success",
            )
        try:
            result = self._build_and_run_differential(self.diff_config)
            return result
        except Exception as e:
            return GateResult(
                name="differential",
                passed=False,
                summary=f"differential harness failed to build: {e}",
                stderr=str(e),
            )

    # --- Orchestration ---

    def run_all(self) -> VerificationReport:
        compile_r = self.gate_compile()
        # Even if compile fails, run anti-stub so the report is informative
        anti_r = self.gate_anti_stub()
        if not compile_r.passed:
            # Can't run tests if it doesn't compile
            return VerificationReport(
                compile=compile_r,
                anti_stub=anti_r,
                test=GateResult(
                    name="test", passed=False,
                    summary="skipped — compile failed",
                ),
                differential=GateResult(
                    name="differential", passed=False,
                    summary="skipped — compile failed",
                ),
            )
        test_r = self.gate_test()
        diff_r = self.gate_differential() if test_r.passed else GateResult(
            name="differential", passed=False,
            summary="skipped — test gate failed",
        )
        return VerificationReport(
            compile=compile_r,
            anti_stub=anti_r,
            test=test_r,
            differential=diff_r,
        )

    # --- Differential harness build / run ---

    def _build_and_run_differential(self, cfg: DifferentialConfig) -> GateResult:
        verify_dir = cfg.verify_dir or (self.rust_workspace.parent / "verify_gen")
        ffi_dir = verify_dir / cfg.ffi_crate_name
        diff_dir = verify_dir / "diff_test"

        ffi_result = generate_ffi_crate(AutoFfiRequest(
            c_sources=cfg.c_sources,
            include_dirs=cfg.c_include_dirs,
            public_signatures=cfg.c_public_signatures,
            output_dir=ffi_dir,
            crate_name=cfg.ffi_crate_name,
            lib_name=cfg.ffi_crate_name,
            typedefs=cfg.c_typedefs,
            opaque_types=cfg.c_opaque_types,
        ))
        if not ffi_result.build.success:
            return GateResult(
                name="differential",
                passed=False,
                summary="FFI C DLL build failed",
                stderr=ffi_result.build.stderr,
            )

        # Emit test crate
        self._write_test_crate(diff_dir, cfg, ffi_result)

        # Run cargo test --test differential inside diff_dir
        try:
            r = subprocess.run(
                ["cargo", "test", "--test", "differential", "--release", "--",
                 "--nocapture"],
                cwd=str(diff_dir),
                capture_output=True,
                text=True,
                timeout=self.timeout_diff,
            )
        except subprocess.TimeoutExpired as e:
            return GateResult(
                name="differential",
                passed=False,
                summary=f"differential tests timed out after {self.timeout_diff}s",
                stderr=str(e),
            )
        passed = r.returncode == 0
        lines = (r.stdout + "\n" + r.stderr).splitlines()
        summary_line = next(
            (l for l in lines[::-1] if "test result:" in l),
            "differential pass" if passed else "differential fail",
        )
        return GateResult(
            name="differential",
            passed=passed,
            summary=summary_line.strip(),
            stdout=r.stdout,
            stderr=r.stderr,
        )

    def _write_test_crate(
        self,
        diff_dir: Path,
        cfg: DifferentialConfig,
        ffi: AutoFfiResult,
    ) -> None:
        """Emit a tiny test crate with the differential harness."""
        diff_dir.mkdir(parents=True, exist_ok=True)
        (diff_dir / "tests").mkdir(parents=True, exist_ok=True)
        (diff_dir / "src").mkdir(parents=True, exist_ok=True)

        # Cargo.toml — depends on ffi crate (by path)
        cargo = (
            "[package]\n"
            f'name = "diff_test"\n'
            'version = "0.1.0"\n'
            'edition = "2021"\n\n'
            "[dependencies]\n"
            f'{cfg.ffi_crate_name} = {{ path = "{ffi.cargo_toml_path.parent.as_posix()}" }}\n'
            "\n"
            "[dev-dependencies]\n"
            'proptest = "1.4"\n'
            "\n"
            "[workspace]\n"
        )
        (diff_dir / "Cargo.toml").write_text(cargo, encoding="utf-8")
        # Empty lib.rs
        (diff_dir / "src" / "lib.rs").write_text(
            "//! differential test crate — harness lives in tests/differential.rs\n",
            encoding="utf-8",
        )

        # Write the harness
        write_differential_test(
            cfg.harnesses,
            diff_dir / "tests" / "differential.rs",
            module_doc="Auto-generated differential tests (Stage 5 gate).",
        )


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------

def verify_workspace(
    rust_workspace: Path,
    diff_config: DifferentialConfig | None = None,
    *,
    refuse_without_diff: bool = True,
) -> VerificationReport:
    """Public API — run all gates and return report.

    If `refuse_without_diff=True` (the production default) and no diff_config
    is supplied, the differential gate will FAIL with the reason "no
    differential config". This enforces the rule that Alchemist refuses to
    claim success without verification.
    """
    tester = DifferentialTester(rust_workspace, diff_config=diff_config)
    report = tester.run_all()
    return report
