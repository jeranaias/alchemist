"""Tests for alchemist.extractor.variant_resolver."""

from __future__ import annotations

import pytest

from alchemist.extractor.schemas import (
    AlgorithmSpec,
    ModuleSpec,
    Parameter,
    TestVector,
)
from alchemist.extractor.variant_resolver import (
    AES_FAMILY,
    CRC32_FAMILY,
    SHA_FAMILY,
    ResolutionResult,
    Variant,
    VariantFamily,
    apply_resolution,
    resolve_specs,
    resolve_variant,
)


def _alg(name="crc32", math="", standards=None, tvecs=None):
    return AlgorithmSpec(
        name=name,
        display_name=name,
        category="checksum",
        description=f"{name} algorithm",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="")],
        return_type="u32",
        mathematical_description=math,
        referenced_standards=standards or [],
        test_vectors=tvecs or [],
    )


# ---------- Family matching ----------

def test_crc32_family_matches_by_name():
    assert CRC32_FAMILY.matches_algorithm(_alg("crc32"))
    assert CRC32_FAMILY.matches_algorithm(_alg("crc_32"))
    assert CRC32_FAMILY.matches_algorithm(_alg("CRC-32"))


def test_crc32_family_matches_by_standards():
    assert CRC32_FAMILY.matches_algorithm(
        _alg("compute_checksum", standards=["RFC 1952 CRC-32"])
    )


def test_crc32_family_does_not_match_unrelated():
    assert not CRC32_FAMILY.matches_algorithm(_alg("adler32"))


def test_sha_family_matches_variants():
    assert SHA_FAMILY.matches_algorithm(_alg("sha256"))
    assert SHA_FAMILY.matches_algorithm(_alg("sha-512"))


# ---------- Fingerprint disambiguation ----------

def test_crc32_reflected_detected_from_polynomial():
    spec = _alg(
        "crc32",
        math="Uses the polynomial 0xEDB88320 with shift-right traversal. zlib-compatible.",
    )
    result = resolve_variant(spec)
    assert result.resolved
    assert result.chosen.name == "ieee_reflected"


def test_crc32_non_reflected_detected_from_polynomial():
    spec = _alg(
        "crc32",
        math="Uses polynomial 0x04C11DB7 with shift-left (MSB-first) traversal and checks 0x80000000.",
    )
    result = resolve_variant(spec)
    assert result.resolved
    assert result.chosen.name == "ieee_non_reflected"


def test_crc32c_detected_from_castagnoli_keyword():
    spec = _alg(
        "crc32_compute",
        math="CRC-32C Castagnoli variant used by iSCSI. Polynomial 0x82F63B78.",
        standards=["RFC 3720"],
    )
    result = resolve_variant(spec)
    assert result.resolved
    assert result.chosen.name == "castagnoli"


def test_crc32_ambiguous_when_both_polynomials_mentioned():
    """The canonical bug: extracted spec describes both variants. Resolver
    must report ambiguous rather than picking arbitrarily."""
    spec = _alg(
        "crc32",
        math=(
            "The algorithm uses polynomial 0xEDB88320 (reflected) with shift right. "
            "Alternative: non-reflected 0x04C11DB7 with shift left."
        ),
    )
    result = resolve_variant(spec)
    assert not result.resolved
    assert result.ambiguous
    assert len(result.candidates) >= 2


def test_crc32_ambiguity_resolved_by_spec_test_vectors():
    """When spec.test_vectors agree with one variant's catalog outputs,
    we can disambiguate even with both polynomials mentioned."""
    # CRC-32 of 'a' should be 0xe8b7be43 (IEEE reflected)
    spec = _alg(
        "crc32",
        math="Polynomial 0xEDB88320 or 0x04C11DB7 — both are CRC-32 variants.",
        tvecs=[
            TestVector(
                description="single byte 'a'",
                inputs={"input": '&[0x61]'},
                expected_output="0xe8b7be43",
                tolerance="exact",
            ),
        ],
    )
    result = resolve_variant(spec)
    # Catalog check filters out the non-reflected variant
    assert result.resolved
    assert result.chosen.name == "ieee_reflected"


# ---------- Single-variant families default ----------

def test_single_variant_family_defaults():
    family = VariantFamily(
        family="mystery",
        name_patterns=[r"\bmystery\b"],
        variants=[Variant(name="only_one", description="...", fingerprints=[])],
    )
    spec = _alg("mystery", math="nothing matches here")
    result = resolve_variant(spec, families=[family])
    assert result.resolved
    assert result.chosen.name == "only_one"


# ---------- AES / SHA ----------

def test_aes128_ecb_detected():
    spec = _alg("aes_encrypt", math="AES-128 ECB single block, Nk=4, Nr=10")
    result = resolve_variant(spec)
    assert result.resolved
    assert result.chosen.name == "aes128_ecb"


def test_sha256_detected_by_name():
    spec = _alg("sha256", math="", standards=["FIPS 180-4"])
    result = resolve_variant(spec)
    assert result.resolved
    assert result.chosen.name == "sha256"


# ---------- LLM tiebreaker ----------

def test_llm_tiebreaker_breaks_tie():
    spec = _alg(
        "crc32",
        math="Polynomial 0xEDB88320 or 0x04C11DB7.",
    )

    def pick_reflected(alg, candidates):
        for c in candidates:
            if c.name == "ieee_reflected":
                return c
        return None

    result = resolve_variant(spec, llm_tiebreaker=pick_reflected)
    assert result.resolved
    assert result.chosen.name == "ieee_reflected"
    assert "LLM tiebreaker" in result.rationale


def test_llm_tiebreaker_returning_none_leaves_ambiguous():
    spec = _alg("crc32", math="0xEDB88320 and 0x04C11DB7")
    result = resolve_variant(spec, llm_tiebreaker=lambda a, c: None)
    assert not result.resolved


# ---------- apply_resolution ----------

def test_apply_resolution_writes_variant_into_spec():
    spec = _alg("crc32", math="Uses 0xEDB88320 reflected.")
    result = resolve_variant(spec)
    assert result.resolved
    apply_resolution(spec, result)
    # Variant tag visible in standards
    assert any(s.startswith("variant:") for s in spec.referenced_standards)
    # Variant notes prepended to math
    assert "ieee_reflected" in spec.mathematical_description


def test_apply_resolution_noop_when_unresolved():
    spec = _alg("crc32", math="")
    result = ResolutionResult(
        algorithm="crc32", family="crc32", chosen=None, candidates=[],
    )
    before = spec.mathematical_description
    apply_resolution(spec, result)
    assert spec.mathematical_description == before


# ---------- resolve_specs ----------

def test_resolve_specs_touches_every_algorithm():
    mod = ModuleSpec(
        name="checksum", display_name="", description="",
        algorithms=[
            _alg("adler32", math="BASE=65521"),     # not multi-variant
            _alg("crc32",   math="0xEDB88320 reflected"),
            _alg("sha256",  math=""),
        ],
    )
    results = resolve_specs([mod])
    assert len(results) == 3
    # crc32 and sha256 should be resolved; adler32 doesn't match any family
    algs_resolved = {r.algorithm for r in results if r.resolved}
    assert "crc32" in algs_resolved
    assert "sha256" in algs_resolved
