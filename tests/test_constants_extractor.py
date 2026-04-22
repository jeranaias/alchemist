"""Constants extractor: C #define / enum / static const → Rust pub const."""

from __future__ import annotations

import pytest

from alchemist.extractor.constants_extractor import (
    extract_constants,
    render_constants_block,
    _c_literal_to_rust,
    _infer_type_from_literal,
    _rust_type_for,
)


# ---------------------------------------------------------------------------
# Literal translation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "c_literal, rust_expr",
    [
        ("42", "42"),
        ("0x42", "0x42"),
        ("0xEDB88320", "0xedb88320"),
        ("0xFFFFFFFFu32", "0xffffffff"),
        ("65521U", "65521"),
        ("0b1010", "0b1010"),
        ("0644", "0o644"),
        ("-2", "-2"),
        ("(-1)", "-1"),
        ("(1 << 15)", "1 << 15"),
        ("'A'", "65"),
        ("'\\n'", "b'\\n' as u32"),
    ],
)
def test_c_literal_to_rust(c_literal: str, rust_expr: str) -> None:
    assert _c_literal_to_rust(c_literal, "u32") == rust_expr


def test_string_literal_becomes_byte_string() -> None:
    assert _c_literal_to_rust('"hello"', "&'static [u8]") == 'b"hello"'


def test_complex_expression_returns_none() -> None:
    # Function calls can't be const-evaluated by this extractor
    assert _c_literal_to_rust("foo(42)", "u32") is None


# ---------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------

def test_hex_large_inferred_u64() -> None:
    assert _infer_type_from_literal("0xEDB8832012345678") == "u64"


def test_decimal_negative_inferred_i32() -> None:
    assert _infer_type_from_literal("-2") == "i32"


def test_u_suffix_forces_u32() -> None:
    assert _infer_type_from_literal("65521U") == "u32"


def test_c_type_name_mapping() -> None:
    assert _rust_type_for("unsigned int") == "u32"
    assert _rust_type_for("int") == "i32"
    assert _rust_type_for("size_t") == "usize"
    assert _rust_type_for("uInt") == "u32"
    assert _rust_type_for("uLong") == "u64"
    assert _rust_type_for("unsigned char") == "u8"
    assert _rust_type_for("const int") == "i32"


# ---------------------------------------------------------------------------
# End-to-end extraction
# ---------------------------------------------------------------------------

def test_zlib_adler32_constants() -> None:
    src = """
#define BASE 65521U  /* largest prime smaller than 65536 */
#define NMAX 5552    /* largest n such that ... */
#define Z_OK 0
"""
    report = extract_constants(src)
    names = {c.name for c in report.extracted}
    assert names == {"BASE", "NMAX", "Z_OK"}


def test_function_like_macros_ignored() -> None:
    src = """
#define FOO(x) ((x) + 1)
#define BAR 42
"""
    report = extract_constants(src)
    names = {c.name for c in report.extracted}
    assert names == {"BAR"}


def test_enum_auto_increment_and_explicit() -> None:
    src = """
enum { A = 1, B, C = 0x10, D };
"""
    report = extract_constants(src)
    by_name = {c.name: c.rust_expr for c in report.extracted}
    assert by_name["A"] == "1"
    assert by_name["B"] == "2"
    assert by_name["C"] == "0x10"
    assert by_name["D"] == "17"


def test_static_const_table() -> None:
    src = """
static const unsigned int table[4] = { 0, 1, 2, 3 };
"""
    report = extract_constants(src)
    assert len(report.extracted) == 1
    c = report.extracted[0]
    assert c.name == "table"
    assert c.rust_type == "[u32; 4]"
    assert c.rust_expr == "[0, 1, 2, 3]"


def test_render_block_contains_pub_const() -> None:
    src = "#define BASE 42"
    report = extract_constants(src)
    rust = render_constants_block(report.extracted)
    assert "pub const BASE" in rust
    assert "42" in rust


def test_duplicate_defines_later_wins() -> None:
    # C semantics: the later #define shadows the earlier. Our extractor
    # emits both in source order — later code using BASE sees the last.
    src = """
#define BASE 1
#define BASE 2
"""
    report = extract_constants(src)
    assert len(report.extracted) == 2
    assert [c.rust_expr for c in report.extracted] == ["1", "2"]


def test_complex_expression_is_skipped() -> None:
    src = """
#define FOO function_call(42)
#define BAR 99
"""
    report = extract_constants(src)
    assert {c.name for c in report.extracted} == {"BAR"}
    assert any("FOO" in name for name, _ in report.skipped)


def test_line_numbers_preserved() -> None:
    src = "#define FOO 1\n#define BAR 2\n#define BAZ 3\n"
    report = extract_constants(src)
    by_name = {c.name: c.c_line for c in report.extracted}
    assert by_name["FOO"] == 1
    assert by_name["BAR"] == 2
    assert by_name["BAZ"] == 3
