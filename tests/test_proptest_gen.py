"""Tests for alchemist.verifier.proptest_gen."""

from __future__ import annotations

import pytest

from alchemist.verifier.proptest_gen import (
    AlgorithmHarness,
    emit_differential_test,
)


# ---------- checksum / hash ----------

def test_checksum_harness_includes_fixed_and_proptest():
    h = AlgorithmHarness(
        algorithm="adler32",
        category="checksum",
        rust_call="rust_adler32(&input)",
        c_call="c_adler32(&input)",
    )
    src = emit_differential_test([h], module_doc="auto-gen test")
    # Standards block — at least Wikipedia vector
    assert "11e60398" in src.lower()
    assert "fn adler32_matches_c_reference" in src
    assert "prop_assert_eq!(rust_out, c_out)" in src
    # Header
    assert "use proptest::prelude::*;" in src
    assert "//! auto-gen test" in src


def test_hash_harness_formats_digest_as_hex():
    h = AlgorithmHarness(
        algorithm="sha256",
        category="hash",
        rust_call="rust_sha256(&input)",
        c_call="c_sha256(&input)",
    )
    src = emit_differential_test([h])
    assert "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" in src
    assert "format!(\"{:02x}\"" in src
    assert "fn sha256_matches_c_reference" in src


def test_checksum_without_catalog_vectors_still_emits_proptest():
    h = AlgorithmHarness(
        algorithm="nonexistent_custom",
        category="checksum",
        rust_call="rust_x(&input)",
        c_call="c_x(&input)",
    )
    src = emit_differential_test([h])
    # Still emits proptest even though no catalog vectors
    assert "fn nonexistent_custom_matches_c_reference" in src


# ---------- cipher ----------

def test_cipher_harness_emits_roundtrip_and_interop():
    h = AlgorithmHarness(
        algorithm="aes128",
        category="cipher",
        rust_call="rust_aes128_encrypt(&key, &input)",
        c_call="c_aes128_encrypt(&key, &input)",
        rust_decrypt_call="rust_aes128_decrypt(&key, &ct)",
        c_decrypt_call="c_aes128_decrypt(&key, &ct)",
    )
    src = emit_differential_test([h])
    assert "fn aes128_roundtrip" in src
    assert "fn aes128_interop_rust_encrypt_c_decrypt" in src
    # Standards: FIPS 197 Appendix B key must appear as individual bytes
    assert "0x2b, 0x7e, 0x15, 0x16" in src
    assert "fn fixed_aes128_fips197_appendix_b" in src


def test_cipher_without_decrypt_still_checks_forward_path():
    h = AlgorithmHarness(
        algorithm="aes128",
        category="cipher",
        rust_call="rust_aes128_encrypt(&key, &input)",
        c_call="c_aes128_encrypt(&key, &input)",
    )
    src = emit_differential_test([h])
    assert "fn aes128_encrypt_matches_c" in src


# ---------- compression ----------

def test_compression_harness_emits_three_roundtrip_tests():
    h = AlgorithmHarness(
        algorithm="deflate",
        category="compression",
        rust_call="rust_compress(&input)",
        c_call="c_compress(&input)",
        rust_decompress_call="rust_decompress(&compressed, input.len() + 32)",
        c_decompress_call="c_uncompress(&compressed, input.len() + 32)",
    )
    src = emit_differential_test([h])
    assert "fn deflate_rust_roundtrip" in src
    assert "fn deflate_rust_compress_c_decompress" in src
    assert "fn deflate_c_compress_rust_decompress" in src
    # Standards vector names surface in fixed test names
    assert "fn fixed_deflate_hello_world" in src or "fn fixed_deflate_abc" in src


# ---------- filter (floating-point) ----------

def test_filter_harness_emits_ulp_compare():
    h = AlgorithmHarness(
        algorithm="kalman_update",
        category="filter",
        rust_call="rust_kalman(&input)",
        c_call="c_kalman(&input)",
        ulp_tolerance=4,
    )
    src = emit_differential_test([h])
    assert "within_ulps" in src
    assert "fn kalman_update_within_ulp_tolerance" in src
    assert ", 4)" in src  # ulp param


# ---------- smoke ----------

def test_smoke_harness_for_utility_category():
    h = AlgorithmHarness(
        algorithm="parse_header",
        category="utility",
        rust_call="rust_parse_header(&input)",
        c_call="",
    )
    src = emit_differential_test([h])
    assert "fn parse_header_smoke" in src


# ---------- Validation ----------

def test_unknown_category_raises():
    h = AlgorithmHarness(
        algorithm="x",
        category="nope",
        rust_call="x(&input)",
        c_call="c_x(&input)",
    )
    with pytest.raises(ValueError, match="Unknown category"):
        emit_differential_test([h])


# ---------- Multi-algo file ----------

def test_multiple_algos_concatenated():
    h1 = AlgorithmHarness(
        algorithm="adler32",
        category="checksum",
        rust_call="rust_adler32(&input)",
        c_call="c_adler32(&input)",
    )
    h2 = AlgorithmHarness(
        algorithm="crc32",
        category="checksum",
        rust_call="rust_crc32(&input)",
        c_call="c_crc32(&input)",
    )
    src = emit_differential_test([h1, h2])
    # Both fixed tests present (well-known canonical fixed vector names)
    assert "fn fixed_adler32_wikipedia" in src
    assert "fn fixed_crc32_check_123456789" in src
    # Both proptest blocks present
    assert "fn adler32_matches_c_reference" in src
    assert "fn crc32_matches_c_reference" in src


# ---------- Emitted code parses as valid Rust (smoke check) ----------

def test_emitted_code_smoke_parses():
    """Smoke: emitted harness should at minimum have balanced braces & lex-clean."""
    h = AlgorithmHarness(
        algorithm="adler32",
        category="checksum",
        rust_call="rust_adler32(&input)",
        c_call="c_adler32(&input)",
    )
    src = emit_differential_test([h])
    # Balanced braces / parens
    assert src.count("{") == src.count("}"), f"unbalanced braces in:\n{src}"
    assert src.count("(") == src.count(")"), f"unbalanced parens in:\n{src}"
