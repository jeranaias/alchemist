"""Stage 6: Collect and display metrics for the generated Rust project.

Metrics: unsafe count, clippy score, test results, LOC ratio, comparison dashboard.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console(force_terminal=True, legacy_windows=False)


class MetricsCollector:
    """Collect quality metrics from a generated Rust project."""

    def __init__(self, rust_project: Path, c_source: Path | None = None):
        self.rust_project = rust_project
        self.c_source = c_source

    def collect_all(self) -> dict:
        """Collect all metrics. Returns a dict for JSON serialization."""
        metrics = {}

        metrics["unsafe"] = self._count_unsafe()
        metrics["loc"] = self._count_lines()
        metrics["clippy"] = self._run_clippy()
        metrics["tests"] = self._run_tests()

        if self.c_source:
            metrics["c_loc"] = self._count_c_lines()
            metrics["loc_ratio"] = (
                metrics["loc"]["rust_lines"] / max(metrics["c_loc"]["c_lines"], 1)
            )

        return metrics

    def print_dashboard(self, metrics: dict):
        """Pretty-print a metrics dashboard."""
        table = Table(title="Alchemist Translation Metrics")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        table.add_column("Status", style="bold")

        # Unsafe blocks
        unsafe_count = metrics.get("unsafe", {}).get("total_unsafe_blocks", 0)
        unsafe_status = "[green]SAFE" if unsafe_count == 0 else f"[red]{unsafe_count} UNSAFE"
        table.add_row("Unsafe blocks", str(unsafe_count), unsafe_status)

        # LOC
        rust_lines = metrics.get("loc", {}).get("rust_lines", 0)
        table.add_row("Rust lines of code", f"{rust_lines:,}", "")

        if "c_loc" in metrics:
            c_lines = metrics["c_loc"]["c_lines"]
            ratio = metrics.get("loc_ratio", 0)
            table.add_row("C lines of code", f"{c_lines:,}", "")
            table.add_row("LOC ratio (Rust/C)", f"{ratio:.2f}", "")

        # Crate count
        crate_count = metrics.get("loc", {}).get("crate_count", 0)
        table.add_row("Crates", str(crate_count), "")

        # Tests
        tests = metrics.get("tests", {})
        passed = tests.get("passed", 0)
        failed = tests.get("failed", 0)
        test_status = "[green]PASS" if failed == 0 and passed > 0 else "[red]FAIL"
        table.add_row("Tests", f"{passed} passed, {failed} failed", test_status)

        # Clippy
        clippy = metrics.get("clippy", {})
        warnings = clippy.get("warnings", 0)
        clippy_status = "[green]CLEAN" if warnings == 0 else f"[yellow]{warnings} warnings"
        table.add_row("Clippy", str(warnings) + " warnings", clippy_status)

        console.print(table)

        # Overall grade
        if unsafe_count == 0 and failed == 0 and warnings == 0:
            console.print(Panel("[bold green]GRADE: A — Safe, tested, lint-free", border_style="green"))
        elif unsafe_count == 0 and failed == 0:
            console.print(Panel("[bold yellow]GRADE: B — Safe and tested, has lint warnings", border_style="yellow"))
        elif unsafe_count == 0:
            console.print(Panel("[bold yellow]GRADE: C — Safe but has test failures", border_style="yellow"))
        else:
            console.print(Panel(f"[bold red]GRADE: D — Contains {unsafe_count} unsafe blocks", border_style="red"))

    def _count_unsafe(self) -> dict:
        """Count unsafe blocks in the Rust project."""
        total_blocks = 0
        total_lines = 0
        files_with_unsafe = []

        for rs_file in self.rust_project.rglob("*.rs"):
            if "target" in rs_file.parts:
                continue
            content = rs_file.read_text(errors="replace")
            blocks = len(re.findall(r"\bunsafe\s*\{", content))
            blocks += len(re.findall(r"\bunsafe\s+fn\b", content))
            blocks += len(re.findall(r"\bunsafe\s+impl\b", content))
            if blocks > 0:
                total_blocks += blocks
                files_with_unsafe.append({
                    "file": str(rs_file.relative_to(self.rust_project)),
                    "count": blocks,
                })

        return {
            "total_unsafe_blocks": total_blocks,
            "files_with_unsafe": files_with_unsafe,
        }

    def _count_lines(self) -> dict:
        """Count lines of Rust code (excluding target/, comments, blanks)."""
        total = 0
        code_lines = 0
        file_count = 0
        crates = set()

        for rs_file in self.rust_project.rglob("*.rs"):
            if "target" in rs_file.parts:
                continue
            file_count += 1
            lines = rs_file.read_text(errors="replace").split("\n")
            total += len(lines)
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("//"):
                    code_lines += 1

        for toml in self.rust_project.rglob("Cargo.toml"):
            if "target" not in toml.parts and toml.parent != self.rust_project:
                crates.add(toml.parent.name)

        return {
            "rust_files": file_count,
            "rust_lines": total,
            "rust_code_lines": code_lines,
            "crate_count": len(crates),
        }

    def _count_c_lines(self) -> dict:
        """Count lines in the original C source."""
        if not self.c_source:
            return {"c_lines": 0}

        total = 0
        for f in self.c_source.glob("*.c"):
            total += len(f.read_text(errors="replace").split("\n"))
        for f in self.c_source.glob("*.h"):
            total += len(f.read_text(errors="replace").split("\n"))

        return {"c_lines": total}

    def _run_clippy(self) -> dict:
        """Run clippy and count warnings."""
        try:
            result = subprocess.run(
                ["cargo", "clippy", "--all-targets", "2>&1"],
                cwd=str(self.rust_project),
                capture_output=True, text=True, timeout=120,
                shell=True,
            )
            warnings = result.stderr.count("warning:")
            return {"success": result.returncode == 0, "warnings": warnings}
        except Exception:
            return {"success": False, "warnings": -1}

    def _run_tests(self) -> dict:
        """Run cargo test and count results."""
        try:
            result = subprocess.run(
                ["cargo", "test"],
                cwd=str(self.rust_project),
                capture_output=True, text=True, timeout=300,
            )
            lines = result.stdout.split("\n")
            passed = sum(1 for l in lines if "test " in l and "... ok" in l)
            failed = sum(1 for l in lines if "test " in l and "... FAILED" in l)

            return {
                "success": result.returncode == 0,
                "passed": passed,
                "failed": failed,
            }
        except Exception:
            return {"success": False, "passed": 0, "failed": -1}
