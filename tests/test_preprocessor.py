"""Tests for alchemist.analyzer.preprocessor."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from alchemist.analyzer.preprocessor import (
    PreprocessResult,
    _strip_line_markers,
    preprocess,
    preprocess_to_dir,
)


def test_strip_line_markers():
    text = '# 1 "test.c"\nint x = 1;\n# 2 "test.c" 2\nint y = 2;\n'
    clean = _strip_line_markers(text)
    assert "int x = 1;" in clean
    assert "int y = 2;" in clean
    assert '# 1 "test.c"' not in clean


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not on PATH")
def test_preprocess_tiny_c_file(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.c").write_text(
        "#define BASE 65521\nint adler32(void) { return BASE; }\n",
        encoding="utf-8",
    )
    result = preprocess(src)
    assert result.ok
    expanded = list(result.expanded.values())[0]
    assert "65521" in expanded
    assert "#define" not in expanded


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not on PATH")
def test_preprocess_with_defines(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "b.c").write_text(
        "#ifdef MY_FLAG\nint flagged = 1;\n#else\nint flagged = 0;\n#endif\n",
        encoding="utf-8",
    )
    result = preprocess(src, defines={"MY_FLAG": "1"})
    assert result.ok
    expanded = list(result.expanded.values())[0]
    assert "flagged = 1" in expanded
    assert "flagged = 0" not in expanded


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not on PATH")
def test_preprocess_to_dir_writes_files(tmp_path):
    src = tmp_path / "src"
    out = tmp_path / "expanded"
    src.mkdir()
    (src / "x.c").write_text("int x = 42;\n", encoding="utf-8")
    result = preprocess_to_dir(src, out)
    assert result.ok
    i_files = list(out.rglob("*.i"))
    assert len(i_files) == 1
    assert "42" in i_files[0].read_text()


def test_preprocess_missing_compiler(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.c").write_text("int x;\n", encoding="utf-8")
    result = preprocess(src, compiler="nonexistent_compiler_xyz")
    assert not result.ok
    assert "(compiler)" in result.errors


def test_preprocess_result_summary():
    r = PreprocessResult(expanded={"a.c": "int x;"}, errors={"b.c": "fail"})
    assert "1 files expanded" in r.summary()
    assert "1 errors" in r.summary()
