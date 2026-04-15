"""Tests for alchemist.references."""

from __future__ import annotations

import pytest

from alchemist.references import (
    ReferenceImpl,
    ReferenceMatch,
    find_references,
    list_references,
    register_reference,
)
from alchemist.references.registry import (
    _canonical,
    clear_runtime_registry,
    references_for_standards,
)


@pytest.fixture(autouse=True)
def _isolate_runtime():
    clear_runtime_registry()
    yield
    clear_runtime_registry()


# ---------- Alias normalization ----------

@pytest.mark.parametrize("name,canonical", [
    ("adler32", "adler32"),
    ("Adler-32", "adler32"),
    ("ADLER_32", "adler32"),
    ("crc32", "crc32_ieee"),
    ("CRC-32", "crc32_ieee"),
    ("crc32_ieee", "crc32_ieee"),
    ("crc32c", "crc32c"),
    ("Castagnoli", "crc32c"),
    ("sha-256", "sha256"),
    ("SHA256", "sha256"),
    ("md5", "md5"),
    ("Fletcher-16", "fletcher16"),
    ("aes-128", "aes128"),
])
def test_canonical_routing(name, canonical):
    assert _canonical(name) == canonical


def test_unknown_algorithm_returns_none():
    assert _canonical("not_a_real_alg") is None
    assert _canonical("") is None


# ---------- Disk-backed reference lookup ----------

def test_adler32_reference_loads_from_disk():
    match = find_references("adler32")
    assert match.ok
    assert any("BASE" in impl.rust_source for impl in match.impls)
    best = match.best()
    assert best is not None
    assert best.algorithm == "adler32"
    assert "65521" in best.rust_source


def test_crc32_reference_has_both_variants():
    match = find_references("crc32")
    assert match.ok
    variants = {impl.variant for impl in match.impls}
    assert "reflected" in variants
    assert "non_reflected" in variants
    # Reflected should be first (and therefore "best" without a hint)
    assert match.best().variant == "reflected"


def test_crc32_variant_hint_selects_explicit_impl():
    match = find_references("crc32")
    non_reflected = match.best(variant_hint="non_reflected")
    assert non_reflected is not None
    assert non_reflected.variant == "non_reflected"
    assert "0x04C1_1DB7" in non_reflected.rust_source


def test_sha256_reference_has_k_constants():
    match = find_references("sha256")
    assert match.ok
    src = match.best().rust_source
    assert "0x428a_2f98" in src  # first K constant
    assert "0x5be0_cd19" in src  # H0[7]


def test_md5_reference_uses_little_endian_length():
    match = find_references("md5")
    assert match.ok
    assert "to_le_bytes" in match.best().rust_source


# ---------- Unknown algorithm ----------

def test_unknown_returns_empty_match():
    match = find_references("flibbertigibbet42")
    assert not match.ok
    assert match.best() is None


# ---------- list_references ----------

def test_list_references_includes_core_set():
    algos = set(list_references())
    for required in ["adler32", "crc32_ieee", "md5", "sha256"]:
        assert required in algos


# ---------- Runtime registration ----------

def test_runtime_registration_overrides_disk():
    ref = ReferenceImpl(
        algorithm="adler32",
        variant="test_override",
        title="Test adler32 override",
        rust_source="pub fn adler32(_: u32, _: &[u8]) -> u32 { 42 }",
        signature="pub fn adler32(seed: u32, buf: &[u8]) -> u32",
        standards=["TEST"],
    )
    register_reference(ref)
    match = find_references("adler32")
    # Runtime override is first in results
    assert match.impls[0].variant == "test_override"
    # Disk version still present behind it
    assert any(i.variant == "rfc1950" for i in match.impls)


def test_prompt_snippet_includes_key_fields():
    ref = ReferenceImpl(
        algorithm="adler32",
        variant="rfc1950",
        title="Adler-32",
        rust_source="pub fn adler32() {}",
        signature="pub fn adler32()",
        standards=["RFC 1950"],
        notes="BASE = 65521",
    )
    snippet = ref.as_prompt_snippet()
    assert "Adler-32" in snippet
    assert "rfc1950" in snippet
    assert "RFC 1950" in snippet
    assert "BASE = 65521" in snippet
    assert "```rust" in snippet
    assert "pub fn adler32()" in snippet


# ---------- standards-based lookup ----------

def test_references_for_standards_finds_rfc1950():
    refs = references_for_standards(["RFC 1950"])
    assert any(r.algorithm == "adler32" for r in refs)


def test_references_for_standards_finds_fips180():
    refs = references_for_standards(["FIPS 180-4"])
    algs = {r.algorithm for r in refs}
    assert "sha1" in algs or "sha256" in algs
