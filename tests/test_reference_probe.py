"""Tests for the reference probe module."""

from __future__ import annotations

from pathlib import Path

import pytest

from alchemist.extractor.schemas import AlgorithmSpec, Parameter
from alchemist.implementer.reference_probe import (
    ProbeResult,
    _find_body_in_sources,
    extract_c_function_body,
    probe_result_as_reference,
)


ZLIB_SRC = Path(__file__).parent.parent / "subjects" / "zlib"


def _zlib_available() -> bool:
    return (ZLIB_SRC / "adler32.c").exists()


@pytest.mark.skipif(not _zlib_available(), reason="zlib subject not present")
def test_extract_body_adler32_z():
    body = extract_c_function_body(ZLIB_SRC / "adler32.c", "adler32_z")
    assert body is not None
    assert "adler32_z" in body
    assert "BASE" in body  # references the zlib Adler modulus


@pytest.mark.skipif(not _zlib_available(), reason="zlib subject not present")
def test_extract_body_missing_returns_none():
    body = extract_c_function_body(ZLIB_SRC / "adler32.c", "not_a_real_function")
    assert body is None


def test_extract_body_nonexistent_file_returns_none():
    body = extract_c_function_body(Path("no/such/file.c"), "anything")
    assert body is None


@pytest.mark.skipif(not _zlib_available(), reason="zlib subject not present")
def test_find_body_in_sources_uses_source_files():
    alg = AlgorithmSpec(
        name="adler32_z",
        display_name="",
        category="checksum",
        description="",
        inputs=[Parameter(name="buf", rust_type="&[u8]", description="")],
        return_type="u32",
        source_functions=["adler32_z"],
        source_files=["adler32.c"],
    )
    body = _find_body_in_sources(alg, ZLIB_SRC)
    assert body is not None
    assert "adler32_z" in body


@pytest.mark.skipif(not _zlib_available(), reason="zlib subject not present")
def test_find_body_falls_back_to_rglob():
    alg = AlgorithmSpec(
        name="adler32_z",
        display_name="",
        category="checksum",
        description="",
        inputs=[Parameter(name="buf", rust_type="&[u8]", description="")],
        return_type="u32",
        source_functions=["adler32_z"],
        # source_files empty — probe must search
    )
    body = _find_body_in_sources(alg, ZLIB_SRC)
    assert body is not None


def test_probe_result_as_reference_on_success():
    probe = ProbeResult(
        algorithm="myfn",
        success=True,
        rust_source="pub fn myfn() {}",
    )
    ref = probe_result_as_reference(probe, "pub fn myfn()")
    assert ref is not None
    assert ref.algorithm == "myfn"
    assert ref.rust_source == "pub fn myfn() {}"
    assert ref.variant == "probe"


def test_probe_result_as_reference_on_failure():
    probe = ProbeResult(
        algorithm="myfn",
        success=False,
        error="compile failed",
    )
    ref = probe_result_as_reference(probe, "pub fn myfn()")
    assert ref is None


def test_probe_result_as_reference_empty_source():
    probe = ProbeResult(
        algorithm="myfn",
        success=True,
        rust_source="",  # success but empty — treat as failure
    )
    ref = probe_result_as_reference(probe, "pub fn myfn()")
    assert ref is None
