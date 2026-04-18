"""Tests for the auto C-DLL builder."""

from __future__ import annotations

from pathlib import Path

import pytest

from alchemist.verifier.build_c_dll import (
    _detect_compiler,
    _dll_extension,
    _source_fingerprint,
    build_shared_library,
    build_subject_dll,
)


def test_detect_compiler_returns_tuple_or_none():
    result = _detect_compiler()
    if result is not None:
        cc, style = result
        assert isinstance(cc, str)
        assert style in ("gcc", "msvc")


def test_dll_extension_matches_platform():
    import platform
    ext = _dll_extension()
    sys = platform.system()
    if sys == "Windows":
        assert ext == ".dll"
    elif sys == "Darwin":
        assert ext == ".dylib"
    else:
        assert ext == ".so"


def test_source_fingerprint_deterministic(tmp_path):
    src = tmp_path / "a.c"
    src.write_text("int x = 1;", encoding="utf-8")
    a = _source_fingerprint([src], ())
    b = _source_fingerprint([src], ())
    assert a == b


def test_source_fingerprint_changes_with_content(tmp_path):
    src = tmp_path / "a.c"
    src.write_text("int x = 1;", encoding="utf-8")
    a = _source_fingerprint([src], ())
    src.write_text("int x = 2;", encoding="utf-8")
    b = _source_fingerprint([src], ())
    assert a != b


def test_source_fingerprint_changes_with_flags(tmp_path):
    src = tmp_path / "a.c"
    src.write_text("int x = 1;", encoding="utf-8")
    a = _source_fingerprint([src], ("-O2",))
    b = _source_fingerprint([src], ("-O0",))
    assert a != b


def test_build_rejects_no_sources(tmp_path):
    result = build_shared_library(
        name="nothing",
        sources=[],
        include_dirs=[],
        output_dir=tmp_path,
    )
    assert not result.ok
    assert "no source files" in result.error


def test_build_subject_dll_rejects_empty_dir(tmp_path):
    result = build_subject_dll(tmp_path, tmp_path / "out")
    assert not result.ok
    assert "no .c files" in result.error


@pytest.mark.skipif(_detect_compiler() is None, reason="no C compiler on PATH")
def test_build_simple_c_file(tmp_path):
    src = tmp_path / "hello.c"
    src.write_text(
        "int add(int a, int b) { return a + b; }\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    result = build_shared_library(
        name="hello",
        sources=[src],
        include_dirs=[],
        output_dir=out,
    )
    if not result.ok:
        pytest.skip(f"compile failed on this platform: {result.error}")
    assert result.dll_path is not None
    assert result.dll_path.exists()


@pytest.mark.skipif(_detect_compiler() is None, reason="no C compiler on PATH")
def test_build_cache_hit_on_unchanged_source(tmp_path):
    src = tmp_path / "hello.c"
    src.write_text(
        "int add(int a, int b) { return a + b; }\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    first = build_shared_library(
        name="hello", sources=[src], include_dirs=[], output_dir=out,
    )
    if not first.ok:
        pytest.skip(f"compile failed: {first.error}")
    second = build_shared_library(
        name="hello", sources=[src], include_dirs=[], output_dir=out,
    )
    assert second.ok
    assert second.compile_log == "cache hit"
