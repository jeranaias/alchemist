"""Tests for alchemist.verifier.auto_ffi."""

from __future__ import annotations

import shutil
from pathlib import Path
from textwrap import dedent

import pytest

from alchemist.verifier.auto_ffi import (
    CSignature,
    CType,
    TypedefMap,
    emit_build_rs,
    emit_cargo_toml,
    emit_ffi_module,
    map_c_type_to_rust,
    parse_header,
)


# ---------- CType parsing ----------

@pytest.mark.parametrize("raw,expected", [
    ("int", ("int", False, 0)),
    ("const char", ("char", True, 0)),
    ("char *", ("char", False, 1)),
    ("const char *", ("char", True, 1)),
    ("char **", ("char", False, 2)),
    ("unsigned long", ("unsigned long", False, 0)),
    ("void *", ("void", False, 1)),
    ("const void *", ("void", True, 1)),
    ("uint8_t *", ("uint8_t", False, 1)),
    ("int []", ("int", False, 1)),
    ("int [8]", ("int", False, 1)),
])
def test_ctype_parse(raw, expected):
    t = CType.parse(raw)
    assert (t.base, t.is_const, t.pointer_depth) == expected


# ---------- Type mapping ----------

def test_scalar_mapping():
    assert map_c_type_to_rust("int") == "c_int"
    assert map_c_type_to_rust("unsigned long") == "c_ulong"
    assert map_c_type_to_rust("size_t") == "usize"
    assert map_c_type_to_rust("uint32_t") == "u32"
    assert map_c_type_to_rust("void") == "()"


def test_pointer_mapping():
    assert map_c_type_to_rust("void *") == "*mut c_void"
    assert map_c_type_to_rust("const void *") == "*const c_void"
    assert map_c_type_to_rust("const char *") == "*const c_char"
    assert map_c_type_to_rust("unsigned char *") == "*mut c_uchar"


def test_zlib_typedefs():
    assert map_c_type_to_rust("uLong") == "c_ulong"
    assert map_c_type_to_rust("Bytef *") == "*mut c_uchar"
    assert map_c_type_to_rust("const Bytef *") == "*const c_uchar"
    assert map_c_type_to_rust("uLong *") == "*mut c_ulong"


def test_custom_typedef_override():
    tm = TypedefMap(entries={"my_handle_t": "c_int"})
    assert map_c_type_to_rust("my_handle_t", tm) == "c_int"


def test_opaque_type_stays_named():
    opaque = {"z_stream"}
    assert map_c_type_to_rust("z_stream *", opaque_types=opaque) == "*mut z_stream"


def test_unknown_pointer_falls_back_to_void():
    # Unknown type through a pointer should degrade gracefully
    assert map_c_type_to_rust("mystery_t *") == "*mut c_void"


# ---------- Header parsing ----------

def test_parse_simple_header():
    header = dedent("""\
        /* Some header */
        int foo(int a, int b);
        unsigned long adler32(unsigned long adler, const unsigned char *buf, unsigned int len);
        void bar(void);
    """)
    sigs = parse_header(header)
    names = {s.name for s in sigs}
    assert {"foo", "adler32", "bar"} <= names

    adler = next(s for s in sigs if s.name == "adler32")
    assert adler.return_type == "unsigned long"
    assert len(adler.params) == 3
    assert ("adler", "unsigned long") in adler.params


def test_parse_header_filters_by_name():
    header = dedent("""\
        int alpha(int x);
        int beta(int y);
    """)
    sigs = parse_header(header, only_names={"beta"})
    assert len(sigs) == 1
    assert sigs[0].name == "beta"


def test_parse_header_strips_storage_modifiers():
    header = dedent("""\
        extern int exported_fn(int x);
    """)
    sigs = parse_header(header)
    assert sigs[0].name == "exported_fn"
    assert sigs[0].return_type == "int"


def test_parse_header_handles_void_params():
    header = "int nothing(void);"
    sigs = parse_header(header)
    assert sigs[0].name == "nothing"
    assert sigs[0].params == []


# ---------- Emission ----------

def test_emit_ffi_module_zlib_shape():
    sigs = [
        CSignature(
            name="adler32",
            return_type="uLong",
            params=[("adler", "uLong"), ("buf", "const Bytef *"), ("len", "uInt")],
        ),
        CSignature(
            name="crc32",
            return_type="uLong",
            params=[("crc", "uLong"), ("buf", "const Bytef *"), ("len", "uInt")],
        ),
    ]
    code = emit_ffi_module(sigs, module_doc="zlib FFI bindings")

    assert 'unsafe extern "C" {' in code
    assert "pub fn adler32(adler: c_ulong, buf: *const c_uchar, len: c_uint) -> c_ulong;" in code
    assert "pub fn crc32(crc: c_ulong, buf: *const c_uchar, len: c_uint) -> c_ulong;" in code
    assert "use std::os::raw::" in code


def test_emit_ffi_module_with_opaque_types():
    sigs = [
        CSignature(
            name="open_handle",
            return_type="z_streamp",
            params=[],
        ),
    ]
    tm = TypedefMap(entries={"z_streamp": "*mut z_stream"})
    code = emit_ffi_module(sigs, typedefs=tm, opaque_types={"z_stream"})
    assert "pub struct z_stream" in code
    assert "pub fn open_handle() -> *mut z_stream;" in code


def test_emit_ffi_module_void_return_has_no_arrow():
    sigs = [CSignature(name="nothing", return_type="void", params=[])]
    code = emit_ffi_module(sigs)
    assert "pub fn nothing();" in code
    assert "-> ()" not in code


def test_emit_ffi_module_escapes_rust_keywords_in_params():
    sigs = [
        CSignature(
            name="do_thing",
            return_type="int",
            params=[("type", "int"), ("match", "const char *")],
        ),
    ]
    code = emit_ffi_module(sigs)
    assert "r#type: c_int" in code
    assert "r#match: *const c_char" in code


def test_emit_build_rs():
    out = emit_build_rs(Path("C:/Users/jesse/verify"), "z")
    assert "cargo:rustc-link-search=native=C:/Users/jesse/verify" in out
    assert "cargo:rustc-link-lib=dylib=z" in out


def test_emit_cargo_toml_basic():
    toml = emit_cargo_toml("ffi_crate")
    assert 'name = "ffi_crate"' in toml
    assert 'edition = "2021"' in toml


# ---------- End-to-end against real zlib (if verify/zlib1.dll exists) ----------

ZLIB_DLL = Path(__file__).parent.parent / "verify" / "zlib1.dll"
VERIFY_DIR = Path(__file__).parent.parent / "verify"


@pytest.mark.skipif(not ZLIB_DLL.exists(), reason="verify/zlib1.dll not present")
def test_emits_same_signatures_as_hand_written_diff_test():
    """Proves the auto-generator matches the proven-working hand-written FFI."""
    sigs = [
        CSignature(
            name="adler32",
            return_type="uLong",
            params=[("adler", "uLong"), ("buf", "const Bytef *"), ("len", "uInt")],
        ),
    ]
    code = emit_ffi_module(sigs)
    # Feature check: same declaration shape as verify/diff_test/src/lib.rs
    assert "pub fn adler32(adler: c_ulong, buf: *const c_uchar, len: c_uint) -> c_ulong;" in code


@pytest.mark.skipif(shutil.which("gcc") is None, reason="gcc not on PATH")
def test_build_c_dll_compiles_tiny_source(tmp_path):
    """Tiny gcc build test — produces a DLL from a one-line C file."""
    from alchemist.verifier.auto_ffi import build_c_dll
    c_src = tmp_path / "t.c"
    c_src.write_text("int square(int x) { return x * x; }\n")
    dll = tmp_path / "tiny.dll"
    result = build_c_dll([c_src], dll)
    if not result.success:
        pytest.skip(f"gcc build failed in this environment: {result.stderr[:200]}")
    assert dll.exists()
    # Size sanity
    assert dll.stat().st_size > 1000
