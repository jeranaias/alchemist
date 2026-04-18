"""Tests for the fuzz-vector generation pipeline."""

from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

from alchemist.extractor.fuzz_vectors import (
    CFunctionBinding,
    ZLIB_BINDINGS,
    _bytes_to_rust_literal,
    _gen_byte_inputs,
    _primary_input_name,
    fuzz_checksum_vectors,
    fuzz_for_spec,
    load_zlib_dll,
)
from alchemist.extractor.schemas import AlgorithmSpec, Parameter


DLL_PATH = Path(__file__).parent.parent / "verify" / "zlib1.dll"


def _adler_spec() -> AlgorithmSpec:
    return AlgorithmSpec(
        name="adler32",
        display_name="Adler-32",
        category="checksum",
        description="",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="")],
        return_type="u32",
    )


def _crc_spec() -> AlgorithmSpec:
    return AlgorithmSpec(
        name="crc32",
        display_name="CRC-32",
        category="checksum",
        description="",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="")],
        return_type="u32",
    )


# ---------- Literal helpers ----------

def test_bytes_literal_empty():
    assert _bytes_to_rust_literal(b"") == "&[]"


def test_bytes_literal_ascii():
    assert _bytes_to_rust_literal(b"abc") == "&[0x61, 0x62, 0x63]"


def test_bytes_literal_bin():
    assert _bytes_to_rust_literal(bytes([0x00, 0xff, 0x42])) == "&[0x00, 0xff, 0x42]"


def test_primary_input_prefers_byte_slice():
    alg = AlgorithmSpec(
        name="f", display_name="", category="utility", description="",
        inputs=[
            Parameter(name="seed", rust_type="u32", description=""),
            Parameter(name="buf", rust_type="&[u8]", description=""),
        ],
        return_type="u32",
    )
    assert _primary_input_name(alg) == "buf"


def test_primary_input_falls_back_to_first():
    alg = AlgorithmSpec(
        name="f", display_name="", category="utility", description="",
        inputs=[Parameter(name="x", rust_type="u32", description="")],
        return_type="u32",
    )
    assert _primary_input_name(alg) == "x"


# ---------- Input-generation diversity ----------

def test_gen_inputs_includes_edge_cases():
    out = _gen_byte_inputs(__import__("random").Random(0), 30)
    assert b"" in out
    assert b"\x00" in out
    assert b"\xff" in out
    assert b"abc" in out
    # Diverse lengths
    lengths = {len(x) for x in out}
    assert len(lengths) >= 10


def test_gen_inputs_deterministic():
    a = _gen_byte_inputs(__import__("random").Random(42), 20)
    b = _gen_byte_inputs(__import__("random").Random(42), 20)
    assert a == b


# ---------- C-DLL integration (skip if DLL missing) ----------

@pytest.mark.skipif(not DLL_PATH.exists(), reason="zlib1.dll not built")
def test_adler32_fuzz_matches_canonical_empty():
    dll = load_zlib_dll(DLL_PATH)
    vecs = fuzz_checksum_vectors(dll, _adler_spec(), ZLIB_BINDINGS["adler32"], count=5)
    empty = [v for v in vecs if v.inputs["input"] == "&[]"]
    assert empty
    # RFC 1950: Adler-32 of empty input is 0x00000001
    assert empty[0].expected_output == "0x00000001"


@pytest.mark.skipif(not DLL_PATH.exists(), reason="zlib1.dll not built")
def test_adler32_fuzz_matches_wikipedia():
    """Wikipedia's canonical test: Adler-32("Wikipedia") == 0x11e60398"""
    dll = load_zlib_dll(DLL_PATH)
    fn = ZLIB_BINDINGS["adler32"].load(dll)
    adapter = ZLIB_BINDINGS["adler32"].adapter
    out = adapter(fn, b"Wikipedia")
    assert out == 0x11e60398


@pytest.mark.skipif(not DLL_PATH.exists(), reason="zlib1.dll not built")
def test_crc32_fuzz_matches_empty():
    dll = load_zlib_dll(DLL_PATH)
    vecs = fuzz_checksum_vectors(dll, _crc_spec(), ZLIB_BINDINGS["crc32"], count=5)
    empty = [v for v in vecs if v.inputs["input"] == "&[]"]
    assert empty
    # CRC-32 of empty is 0
    assert empty[0].expected_output == "0x00000000"


@pytest.mark.skipif(not DLL_PATH.exists(), reason="zlib1.dll not built")
def test_fuzz_for_spec_dispatches_on_category():
    dll = load_zlib_dll(DLL_PATH)
    vecs = fuzz_for_spec(dll, _adler_spec(), ZLIB_BINDINGS, count=3)
    assert len(vecs) == 3


@pytest.mark.skipif(not DLL_PATH.exists(), reason="zlib1.dll not built")
def test_fuzz_for_spec_skips_unsupported_category():
    dll = load_zlib_dll(DLL_PATH)
    alg = AlgorithmSpec(
        name="unknown_fn", display_name="", category="protocol",
        description="",
        inputs=[Parameter(name="x", rust_type="&[u8]", description="")],
        return_type="u32",
    )
    vecs = fuzz_for_spec(dll, alg, ZLIB_BINDINGS)
    assert vecs == []


@pytest.mark.skipif(not DLL_PATH.exists(), reason="zlib1.dll not built")
def test_fuzz_for_spec_returns_empty_when_no_binding():
    dll = load_zlib_dll(DLL_PATH)
    alg = _adler_spec()
    alg.name = "not_in_bindings"
    vecs = fuzz_for_spec(dll, alg, ZLIB_BINDINGS)
    assert vecs == []
