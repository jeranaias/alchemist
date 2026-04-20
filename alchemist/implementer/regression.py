"""Cross-function regression checks.

After writing function B, verify that function A still compiles and its
tests still pass. Without this, a later function can silently break an
earlier one by (for example) adding a conflicting type definition or
altering a shared constant.

Usage in TDD: after each successful function write, run
`check_workspace_regression(workspace_dir)`. If any previously-green
crate now fails, revert the latest write and report the regression.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RegressionResult:
    crate_name: str
    compile_ok: bool = True
    tests_ok: bool = True
    error_summary: str = ""


@dataclass
class RegressionReport:
    results: list[RegressionResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.compile_ok and r.tests_ok for r in self.results)

    @property
    def regressions(self) -> list[RegressionResult]:
        return [r for r in self.results if not r.compile_ok or not r.tests_ok]

    def summary(self) -> str:
        if self.ok:
            return f"regression check: {len(self.results)} crates OK"
        bad = self.regressions
        return (
            f"regression check: {len(bad)}/{len(self.results)} crate(s) regressed\n"
            + "\n".join(f"  - {r.crate_name}: {r.error_summary[:120]}" for r in bad)
        )


def check_workspace_regression(
    workspace_dir: Path,
    *,
    test_timeout: int = 300,
    check_timeout: int = 180,
) -> RegressionReport:
    """Run cargo check + cargo test on every crate in the workspace.

    Designed to be fast: check first (quick), test only if check passes.
    """
    report = RegressionReport()
    workspace_dir = Path(workspace_dir)

    # Find crate dirs (each has a Cargo.toml under workspace_dir)
    crate_dirs = sorted(
        d for d in workspace_dir.iterdir()
        if d.is_dir() and (d / "Cargo.toml").exists() and d.name != "target"
    )

    for crate_dir in crate_dirs:
        rr = RegressionResult(crate_name=crate_dir.name)
        try:
            chk = subprocess.run(
                ["cargo", "check"],
                cwd=str(crate_dir),
                capture_output=True, text=True, timeout=check_timeout,
                encoding="utf-8", errors="replace",
            )
            rr.compile_ok = chk.returncode == 0
            if not rr.compile_ok:
                rr.error_summary = _first_error(chk.stderr)
                report.results.append(rr)
                continue
        except Exception as e:
            rr.compile_ok = False
            rr.error_summary = str(e)[:200]
            report.results.append(rr)
            continue

        try:
            tst = subprocess.run(
                ["cargo", "test", "--no-fail-fast"],
                cwd=str(crate_dir),
                capture_output=True, text=True, timeout=test_timeout,
                encoding="utf-8", errors="replace",
            )
            rr.tests_ok = tst.returncode == 0
            if not rr.tests_ok:
                rr.error_summary = _first_error(tst.stderr) or _first_error(tst.stdout)
        except Exception as e:
            rr.tests_ok = False
            rr.error_summary = str(e)[:200]

        report.results.append(rr)

    return report


def _first_error(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("error"):
            return line[:200]
    return text[:200] if text else ""
