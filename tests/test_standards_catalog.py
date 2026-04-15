"""Tests for alchemist.standards.catalog."""

from __future__ import annotations

import hashlib
import zlib

import pytest

from alchemist.standards import (
    TestVector,
    list_algorithms,
    lookup_test_vectors,
    match_algorithm,
)


# ---------- alias resolution ----------

@pytest.mark.parametrize("alias,canonical", [
    ("adler32", "adler32"),
    ("Adler-32", "adler32"),
    ("adler_32", "adler32"),
    ("ADLER32", "adler32"),
    ("crc32", "crc32"),
    ("CRC-32", "crc32"),
    ("crc32_ieee", "crc32"),
    ("aes-128", "aes128"),
    ("AES128", "aes128"),
    ("sha-256", "sha256"),
    ("SHA256", "sha256"),
    ("deflate", "deflate"),
    ("inflate", "deflate"),
    ("MD5", "md5"),
])
def test_match_algorithm_aliases(alias, canonical):
    assert match_algorithm(alias) == canonical


def test_match_algorithm_unknown_returns_none():
    assert match_algorithm("not_a_real_algorithm") is None


def test_match_algorithm_empty_returns_none():
    assert match_algorithm("") is None
    assert match_algorithm(None) is None  # type: ignore[arg-type]


# ---------- Adler-32 ----------

def test_adler32_vectors_load():
    vectors = lookup_test_vectors("adler32")
    assert len(vectors) > 0
    assert all(isinstance(v, TestVector) for v in vectors)


def test_adler32_wikipedia_is_canonical():
    """THE canonical test: Adler-32('Wikipedia') == 0x11e60398.

    This is the exact value that caught the BASE=255 bug.
    """
    vectors = lookup_test_vectors("adler32")
    wiki = next((v for v in vectors if v.name == "Wikipedia"), None)
    assert wiki is not None, "Wikipedia test vector must be present"
    assert wiki.input_bytes == b"Wikipedia"
    assert wiki.expected_hex == "11e60398"


def test_all_adler32_vectors_match_python_zlib():
    """Every Adler-32 vector must match Python's zlib.adler32 — no typos allowed."""
    for v in lookup_test_vectors("adler32"):
        actual = zlib.adler32(v.input_bytes)
        expected = int(v.expected_hex, 16)
        assert actual == expected, (
            f"Adler-32 catalog mismatch for {v.name!r}: "
            f"got 0x{actual:08x}, catalog says 0x{expected:08x}"
        )


# ---------- CRC-32 ----------

def test_crc32_check_value():
    """CRC-32('123456789') == 0xcbf43926 is the industry standard check value."""
    vectors = lookup_test_vectors("crc32")
    check = next((v for v in vectors if v.name == "check_123456789"), None)
    assert check is not None
    assert check.expected_hex == "cbf43926"


def test_all_crc32_vectors_match_python_zlib():
    for v in lookup_test_vectors("crc32"):
        actual = zlib.crc32(v.input_bytes)
        expected = int(v.expected_hex, 16)
        assert actual == expected, (
            f"CRC-32 catalog mismatch for {v.name!r}: "
            f"got 0x{actual:08x}, catalog says 0x{expected:08x}"
        )


# ---------- DEFLATE ----------

def test_deflate_vectors_are_valid_zlib():
    """Each DEFLATE vector's expected output must decompress to its input."""
    for v in lookup_test_vectors("deflate"):
        decompressed = zlib.decompress(v.expected_bytes)
        assert decompressed == v.input_bytes, (
            f"DEFLATE vector {v.name!r} does not round-trip: "
            f"decompress(expected) != input"
        )


# ---------- SHA family ----------

@pytest.mark.parametrize("algorithm", ["sha1", "sha224", "sha256", "sha384", "sha512"])
def test_sha_vectors_match_python_hashlib(algorithm):
    vectors = lookup_test_vectors(algorithm)
    assert len(vectors) >= 3, f"{algorithm} needs at least empty, abc, alphabet"
    for v in vectors:
        actual = hashlib.new(algorithm, v.input_bytes).hexdigest()
        assert actual == v.expected_hex.lower(), (
            f"{algorithm} catalog mismatch for {v.name!r}"
        )


def test_sha_variants_are_filtered():
    """Looking up sha256 must not return sha1 vectors."""
    sha256 = lookup_test_vectors("sha256")
    assert all(len(v.expected_hex) == 64 for v in sha256), "sha256 digest is 32 bytes"
    sha1 = lookup_test_vectors("sha1")
    assert all(len(v.expected_hex) == 40 for v in sha1), "sha1 digest is 20 bytes"


# ---------- AES ----------

def test_aes128_fips197_vector():
    """FIPS 197 Appendix B worked example must be present and structurally correct."""
    vectors = lookup_test_vectors("aes128")
    appendix_b = next(
        (v for v in vectors if v.name == "fips197_appendix_b"), None)
    assert appendix_b is not None
    assert appendix_b.key_hex == "2b7e151628aed2a6abf7158809cf4f3c"
    assert appendix_b.input_hex == "6bc1bee22e409f96e93d7e117393172a"
    assert appendix_b.expected_hex == "3ad77bb40d7a3660a89ecaf32466ef97"
    assert appendix_b.mode == "ECB"


def test_aes256_vectors_have_256bit_keys():
    for v in lookup_test_vectors("aes256"):
        assert len(v.key_hex or "") == 64, f"AES-256 key must be 32 bytes: {v.name}"


# ---------- MD5 ----------

def test_md5_vectors_match_hashlib():
    for v in lookup_test_vectors("md5"):
        actual = hashlib.md5(v.input_bytes).hexdigest()
        assert actual == v.expected_hex


# ---------- Listing ----------

def test_list_algorithms_covers_core_set():
    algos = set(list_algorithms())
    # At minimum these must be present
    for required in ["adler32", "crc32", "sha256", "aes128"]:
        assert required in algos or any(a.startswith(required[:3]) for a in algos)


# ---------- TestVector API ----------

def test_test_vector_as_rust_literal_produces_valid_syntax():
    v = TestVector(
        algorithm="adler32",
        name="abc",
        input_hex="616263",
        expected_hex="024d0127",
    )
    lit = v.as_rust_literal("input")
    assert lit == "&[0x61, 0x62, 0x63]"
    empty = v.as_rust_literal("key")  # no key set → empty literal
    assert empty == "&[]"


def test_unknown_algorithm_returns_empty_list():
    assert lookup_test_vectors("nonexistent") == []
    assert lookup_test_vectors("") == []
