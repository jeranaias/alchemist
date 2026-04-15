"""Preprocessor expansion via gcc -E.

Libraries like FreeRTOS have 40% of their logic in macros. tree-sitter
sees `#define TASK_CREATE(...)` as a preprocessor directive and skips the
body entirely — so Stage 1 misses functions that live inside macros.

This module runs `gcc -E` with target-appropriate defines, producing
expanded `.i` files that tree-sitter can parse normally. The pipeline
uses the expanded output for extraction while keeping the original
source for display and diff purposes.

Usage:
    from alchemist.analyzer.preprocessor import preprocess

    expanded = preprocess(
        source_dir=Path("subjects/freertos"),
        defines={"configUSE_PREEMPTION": "1"},
        include_dirs=[Path("subjects/freertos/include")],
    )
    # expanded is a dict: {original_path: expanded_text}
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PreprocessResult:
    """Result of preprocessing a C codebase."""
    expanded: dict[str, str] = field(default_factory=dict)
    # original_path -> expanded text
    errors: dict[str, str] = field(default_factory=dict)
    # original_path -> gcc error text (for files that failed)
    compiler: str = "gcc"

    @property
    def ok(self) -> bool:
        return bool(self.expanded) and not self.errors

    def summary(self) -> str:
        return (
            f"preprocessor: {len(self.expanded)} files expanded, "
            f"{len(self.errors)} errors"
        )


def preprocess(
    source_dir: Path,
    *,
    defines: dict[str, str] | None = None,
    undefines: list[str] | None = None,
    include_dirs: list[Path] | None = None,
    compiler: str = "gcc",
    extensions: set[str] | None = None,
    timeout: int = 60,
    extra_flags: list[str] | None = None,
) -> PreprocessResult:
    """Run gcc -E on every C file under source_dir.

    Returns a PreprocessResult with expanded text keyed by original path.
    Files that fail to preprocess are recorded in .errors but don't block
    the rest — partial expansion is still useful for Stage 1.
    """
    source_dir = Path(source_dir).resolve()
    defines = defines or {}
    undefines = undefines or []
    include_dirs = include_dirs or [source_dir]
    extensions = extensions or {".c", ".h"}
    extra_flags = extra_flags or []
    result = PreprocessResult(compiler=compiler)

    if not shutil.which(compiler):
        result.errors["(compiler)"] = f"{compiler} not found on PATH"
        return result

    c_files = sorted(
        f for f in source_dir.rglob("*")
        if f.suffix in extensions
        and ".git" not in f.parts
        and "test" not in f.name.lower()
    )

    for c_file in c_files:
        cmd = _build_cmd(
            compiler, c_file, defines, undefines, include_dirs, extra_flags,
        )
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            if proc.returncode == 0:
                # Strip line markers (# <line> "<file>") to keep tree-sitter happy
                clean = _strip_line_markers(proc.stdout)
                result.expanded[str(c_file)] = clean
            else:
                result.errors[str(c_file)] = proc.stderr[:1000]
        except subprocess.TimeoutExpired:
            result.errors[str(c_file)] = f"gcc -E timed out after {timeout}s"
        except Exception as e:
            result.errors[str(c_file)] = str(e)[:500]

    return result


def _build_cmd(
    compiler: str,
    c_file: Path,
    defines: dict[str, str],
    undefines: list[str],
    include_dirs: list[Path],
    extra_flags: list[str],
) -> list[str]:
    cmd = [compiler, "-E"]
    for k, v in defines.items():
        cmd.append(f"-D{k}={v}" if v else f"-D{k}")
    for u in undefines:
        cmd.append(f"-U{u}")
    for inc in include_dirs:
        cmd.extend(["-I", str(inc)])
    cmd.extend(extra_flags)
    cmd.append(str(c_file))
    return cmd


def _strip_line_markers(text: str) -> str:
    """Remove `# <line> "<file>"` lines from gcc -E output.

    These confuse tree-sitter (it tries to parse them as preprocessor
    directives that reference non-existent files).
    """
    lines = text.splitlines()
    return "\n".join(
        line for line in lines
        if not (line.startswith("# ") and '"' in line)
    )


def preprocess_to_dir(
    source_dir: Path,
    output_dir: Path,
    **kwargs,
) -> PreprocessResult:
    """Preprocess and write expanded files to output_dir (mirroring structure)."""
    result = preprocess(source_dir, **kwargs)
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = Path(source_dir).resolve()
    for orig_path, expanded in result.expanded.items():
        orig = Path(orig_path)
        try:
            rel = orig.relative_to(source_dir)
        except ValueError:
            rel = Path(orig.name)
        out_file = output_dir / rel.with_suffix(".i")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(expanded, encoding="utf-8")
    return result
