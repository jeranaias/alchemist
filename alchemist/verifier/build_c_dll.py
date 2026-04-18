"""Build the C subject as a shared library for differential testing.

When the pipeline translates a fresh C codebase, Stage 5's differential
gate needs a pre-built C shared library to call via ctypes/FFI. This
module discovers toolchains (MinGW on Windows, gcc/clang on Linux/macOS),
compiles all .c files in the subject, and produces a DLL/so/dylib.

The build is cached per (source-hash, flags) tuple so re-runs skip
recompilation unless C sources changed.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BuildResult:
    """Outcome of a C shared-library build."""
    ok: bool
    dll_path: Path | None = None
    compile_log: str = ""
    error: str = ""
    # Cache-key fingerprint (hash of all source file contents + flags)
    fingerprint: str = ""


def _source_fingerprint(sources: list[Path], flags: tuple[str, ...]) -> str:
    h = hashlib.sha256()
    for p in sorted(sources):
        try:
            h.update(p.read_bytes())
            h.update(str(p).encode("utf-8"))
        except OSError:
            pass
    for f in flags:
        h.update(f.encode("utf-8"))
    return h.hexdigest()[:16]


def _detect_compiler() -> tuple[str, str] | None:
    """Find a C compiler. Returns (command, style) where style is 'gcc' or 'msvc'.

    Prefers MinGW on Windows (for consistency with pre-built zlib1.dll),
    falls back to MSVC cl.exe. On Linux/macOS uses gcc/clang.
    """
    if platform.system() == "Windows":
        for candidate in ("x86_64-w64-mingw32-gcc", "gcc", "cc"):
            if shutil.which(candidate):
                return (candidate, "gcc")
        if shutil.which("cl"):
            return ("cl", "msvc")
    else:
        for candidate in ("gcc", "clang", "cc"):
            if shutil.which(candidate):
                return (candidate, "gcc")
    return None


def _dll_extension() -> str:
    sys = platform.system()
    if sys == "Windows":
        return ".dll"
    if sys == "Darwin":
        return ".dylib"
    return ".so"


def build_shared_library(
    name: str,
    sources: list[Path],
    include_dirs: list[Path],
    output_dir: Path,
    *,
    extra_flags: tuple[str, ...] = (),
    defines: tuple[str, ...] = (),
) -> BuildResult:
    """Compile a C shared library from the given sources.

    Args:
      name: library base name (no extension). Result is output_dir / <name>.<ext>.
      sources: list of .c files to compile.
      include_dirs: include paths (will be -I'd).
      output_dir: where the shared library is written.
      extra_flags: additional compiler flags.
      defines: preprocessor defines (without leading -D).

    Returns BuildResult describing outcome.
    """
    sources = [Path(s) for s in sources if Path(s).exists()]
    if not sources:
        return BuildResult(ok=False, error="no source files provided")
    fingerprint = _source_fingerprint(sources, extra_flags + defines)
    output_dir.mkdir(parents=True, exist_ok=True)
    dll_path = output_dir / f"{name}{_dll_extension()}"
    # Cache hit: skip build if the DLL already matches the fingerprint.
    fp_path = dll_path.with_suffix(".fingerprint")
    if dll_path.exists() and fp_path.exists():
        try:
            if fp_path.read_text().strip() == fingerprint:
                return BuildResult(
                    ok=True, dll_path=dll_path,
                    compile_log="cache hit", fingerprint=fingerprint,
                )
        except OSError:
            pass
    compiler = _detect_compiler()
    if compiler is None:
        return BuildResult(
            ok=False,
            error="no C compiler found on PATH (tried gcc/clang/cl/mingw)",
            fingerprint=fingerprint,
        )
    cc, style = compiler
    # Build the command line.
    if style == "gcc":
        cmd = [cc, "-shared", "-fPIC", "-O2", "-o", str(dll_path)]
        for d in include_dirs:
            cmd.extend(["-I", str(d)])
        for define in defines:
            cmd.append(f"-D{define}")
        cmd.extend(extra_flags)
        cmd.extend(str(s) for s in sources)
    else:  # msvc
        cmd = [cc, "/LD", "/O2", f"/Fe:{dll_path}"]
        for d in include_dirs:
            cmd.append(f"/I{d}")
        for define in defines:
            cmd.append(f"/D{define}")
        cmd.extend(extra_flags)
        cmd.extend(str(s) for s in sources)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return BuildResult(
            ok=False, error="compile timed out after 600s",
            fingerprint=fingerprint,
        )
    log = result.stdout + "\n" + result.stderr
    if result.returncode != 0 or not dll_path.exists():
        return BuildResult(
            ok=False, compile_log=log,
            error=f"compile failed (exit {result.returncode})",
            fingerprint=fingerprint,
        )
    # Record fingerprint for future cache-hit checks.
    try:
        fp_path.write_text(fingerprint, encoding="utf-8")
    except OSError:
        pass
    return BuildResult(
        ok=True, dll_path=dll_path, compile_log=log, fingerprint=fingerprint,
    )


def build_subject_dll(
    c_source_dir: Path,
    output_dir: Path,
    *,
    lib_name: str | None = None,
    exclude_globs: tuple[str, ...] = ("*test*.c", "*example*.c", "*bench*.c"),
) -> BuildResult:
    """Convenience wrapper: compile all .c files in a subject directory.

    Excludes common test/example/benchmark files by default.
    """
    c_source_dir = Path(c_source_dir)
    all_sources: list[Path] = []
    for p in c_source_dir.rglob("*.c"):
        if any(p.match(g) for g in exclude_globs):
            continue
        all_sources.append(p)
    if not all_sources:
        return BuildResult(
            ok=False,
            error=f"no .c files found under {c_source_dir}",
        )
    return build_shared_library(
        name=lib_name or c_source_dir.name,
        sources=sorted(all_sources),
        include_dirs=[c_source_dir],
        output_dir=output_dir,
    )
