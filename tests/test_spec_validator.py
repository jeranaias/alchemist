"""Tests for alchemist.extractor.spec_validator."""

from __future__ import annotations

import pytest

from alchemist.extractor.schemas import (
    AlgorithmSpec,
    ModuleSpec,
    Parameter,
    TestVector,
)
from alchemist.extractor.spec_validator import (
    SpecValidationReport,
    validate_module,
    validate_spec,
    validate_specs,
)


def _adler_spec(math: str = "", return_type: str = "u32",
                test_vectors=None) -> AlgorithmSpec:
    return AlgorithmSpec(
        name="adler32",
        display_name="Adler-32",
        category="checksum",
        description="RFC 1950 Adler-32 checksum.",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="bytes")],
        return_type=return_type,
        source_functions=["adler32"],
        referenced_standards=["RFC 1950"],
        mathematical_description=math,
        test_vectors=test_vectors or [],
    )


# ---------- Constants check ----------

def test_adler32_with_correct_base_is_clean():
    spec = _adler_spec(math="BASE = 65521, NMAX = 5552. s1 += b; s2 += s1.")
    report = validate_spec(spec)
    # May have warnings, but no errors
    assert report.ok, f"expected clean, got: {report.issues}"


def test_adler32_with_wrong_base_is_flagged():
    """The canonical BASE=255 bug from the original zlib run MUST be caught."""
    spec = _adler_spec(math="BASE = 255, NMAX = 55.")
    report = validate_spec(spec)
    assert not report.ok
    err_msgs = " ".join(i.message for i in report.errors)
    assert "65521" in err_msgs
    assert "BASE" in err_msgs


def test_crc32_polynomial_mismatch_flagged():
    spec = AlgorithmSpec(
        name="crc32",
        display_name="CRC-32",
        category="checksum",
        description="CRC-32 (IEEE 802.3).",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="bytes")],
        return_type="u32",
        source_functions=["crc32"],
        referenced_standards=["IEEE 802.3"],
        mathematical_description="POLY = 0x04C11DB7",
    )
    report = validate_spec(spec)
    assert not report.ok
    assert any("0xedb88320" in i.message.lower() for i in report.errors)


# ---------- Test vector check ----------

def test_adler32_test_vector_mismatch_is_flagged():
    # Spec says Adler-32('Wikipedia') = 0x00f7009b — WRONG (the BASE=255 result)
    wrong_vec = TestVector(
        description="Wikipedia",
        inputs={"input": 'b"Wikipedia"'},
        expected_output="0x00f7009b",
        tolerance="exact",
        source="wrong",
    )
    spec = _adler_spec(test_vectors=[wrong_vec])
    report = validate_spec(spec)
    msgs = " ".join(i.message for i in report.errors)
    assert "11e60398" in msgs
    assert "Wikipedia" in spec.test_vectors[0].description


def test_adler32_correct_test_vector_is_accepted():
    right_vec = TestVector(
        description="Wikipedia",
        inputs={"input": 'b"Wikipedia"'},
        expected_output="0x11e60398",
        tolerance="exact",
    )
    spec = _adler_spec(test_vectors=[right_vec])
    report = validate_spec(spec)
    assert report.ok


# ---------- Category / return type ----------

def test_checksum_with_vec_return_warns():
    spec = _adler_spec(return_type="Vec<u8>")
    report = validate_spec(spec)
    # u32 return is what checksum expects
    assert any(i.rule == "return_type_category_mismatch" for i in report.warnings)


def test_cipher_with_u32_return_warns():
    spec = AlgorithmSpec(
        name="aes128_encrypt",
        display_name="AES-128",
        category="cipher",
        description="",
        inputs=[],
        return_type="u32",
    )
    report = validate_spec(spec)
    assert any(i.rule == "return_type_category_mismatch" for i in report.warnings)


# ---------- Sanity ----------

def test_no_inputs_warns_for_non_utility():
    spec = AlgorithmSpec(
        name="compute",
        display_name="",
        category="checksum",
        description="",
        inputs=[],
        return_type="u32",
        source_functions=["compute"],
    )
    report = validate_spec(spec)
    assert any(i.rule == "no_inputs" for i in report.warnings)


def test_no_source_functions_warns():
    spec = _adler_spec()
    spec.source_functions = []
    report = validate_spec(spec)
    assert any(i.rule == "no_source_functions" for i in report.warnings)


# ---------- Module / workspace level ----------

def test_validate_module_aggregates_issues():
    mod = ModuleSpec(
        name="checksum",
        display_name="",
        description="",
        algorithms=[
            _adler_spec(math="BASE = 255"),  # ERROR
            _adler_spec(return_type="Vec<u8>"),  # WARNING
        ],
    )
    report = validate_module(mod)
    assert not report.ok
    assert len(report.errors) >= 1
    assert len(report.warnings) >= 1


def test_validate_specs_chains_modules():
    mods = [
        ModuleSpec(
            name="checksum", display_name="", description="",
            algorithms=[_adler_spec()],
        ),
        ModuleSpec(
            name="bad", display_name="", description="",
            algorithms=[_adler_spec(math="BASE = 100")],
        ),
    ]
    report = validate_specs(mods)
    assert not report.ok
