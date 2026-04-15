"""Declarative unsafe fence.

`alchemist.toml` at the workspace root can declare which modules are
allowed to use unsafe:

    [unsafe]
    allow = ["hal/*", "ffi/*"]

Everything else is rejected outright — the anti-stub detector is
extended to flag `unsafe` blocks outside the allow list as errors.
This removes one LLM judgment call per function ("should I use unsafe
here?") and replaces it with a project-level policy decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class UnsafeFenceConfig:
    """Parsed unsafe fence configuration."""
    allow_patterns: list[str] = field(default_factory=list)
    # glob patterns relative to workspace root

    @classmethod
    def load(cls, workspace_dir: Path) -> "UnsafeFenceConfig":
        """Load from alchemist.toml if present."""
        toml_path = workspace_dir / "alchemist.toml"
        if not toml_path.exists():
            return cls()
        try:
            import tomllib
        except ImportError:
            try:
                import tomli as tomllib
            except ImportError:
                return cls()
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        unsafe_section = data.get("unsafe", {})
        allow = unsafe_section.get("allow", [])
        if isinstance(allow, str):
            allow = [allow]
        return cls(allow_patterns=list(allow))

    def is_allowed(self, rel_path: str) -> bool:
        """Check if a file (relative to workspace root) is in the allow list."""
        if not self.allow_patterns:
            return False
        from fnmatch import fnmatch
        for pattern in self.allow_patterns:
            if fnmatch(rel_path, pattern) or fnmatch(rel_path, f"**/{pattern}"):
                return True
            # Also try matching just the filename
            if fnmatch(Path(rel_path).name, pattern):
                return True
        return False


@dataclass
class UnsafeViolation:
    file: str
    line: int
    snippet: str


@dataclass
class UnsafeFenceReport:
    violations: list[UnsafeViolation] = field(default_factory=list)
    files_scanned: int = 0

    @property
    def ok(self) -> bool:
        return len(self.violations) == 0

    def summary(self) -> str:
        if self.ok:
            return f"unsafe fence: {self.files_scanned} files scanned, 0 violations"
        return (
            f"unsafe fence: {len(self.violations)} unauthorized unsafe block(s) "
            f"in {self.files_scanned} files"
        )


_UNSAFE_BLOCK_RE = re.compile(r"\bunsafe\s*\{")


def scan_workspace(
    workspace_dir: Path,
    config: UnsafeFenceConfig | None = None,
) -> UnsafeFenceReport:
    """Scan every .rs file for unauthorized `unsafe` blocks."""
    config = config or UnsafeFenceConfig.load(workspace_dir)
    report = UnsafeFenceReport()

    for rs in sorted(workspace_dir.rglob("*.rs")):
        if "target" in rs.parts:
            continue
        report.files_scanned += 1
        try:
            rel = str(rs.relative_to(workspace_dir)).replace("\\", "/")
        except ValueError:
            rel = str(rs)

        if config.is_allowed(rel):
            continue

        text = rs.read_text(encoding="utf-8", errors="replace")
        # Strip comments before scanning
        text_clean = re.sub(r"//[^\n]*", "", text)
        text_clean = re.sub(r"/\*.*?\*/", "", text_clean, flags=re.DOTALL)
        # Strip string literals
        text_clean = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text_clean)

        for m in _UNSAFE_BLOCK_RE.finditer(text_clean):
            line = text_clean[:m.start()].count("\n") + 1
            snippet_start = max(0, m.start() - 20)
            snippet = text_clean[snippet_start:m.end() + 30].strip()
            report.violations.append(UnsafeViolation(
                file=rel, line=line, snippet=snippet[:120],
            ))

    return report
