"""Tests for alchemist.implementer.semantic_lints."""

from __future__ import annotations

import pytest

from alchemist.extractor.schemas import (
    AlgorithmSpec,
    Parameter,
)
from alchemist.implementer.semantic_lints import (
    SemanticFinding,
    format_findings,
    has_errors,
    lint_adler32,
    lint_aes,
    lint_crc32,
    lint_function,
    lint_md5,
    lint_sha256,
    lint_unused_input,
    summarize_for_reprompt,
)


def _alg(name, standards=None, inputs=None, return_type="u32"):
    return AlgorithmSpec(
        name=name,
        display_name=name,
        category="checksum",
        description="",
        inputs=inputs or [Parameter(name="buf", rust_type="&[u8]", description="")],
        return_type=return_type,
        referenced_standards=standards or [],
    )


# ---------- CRC-32 ----------

def test_crc32_reflected_missing_polynomial_flagged():
    src = "pub fn crc32(seed: u32, buf: &[u8]) -> u32 { seed }"
    alg = _alg("crc32", standards=["variant:ieee_reflected"])
    findings = lint_crc32(src, alg)
    assert any(f.rule == "crc32_wrong_polynomial" for f in findings)


def test_crc32_reflected_with_shift_left_flagged():
    src = """
    const POLY: u32 = 0xEDB88320;
    pub fn crc32(seed: u32, buf: &[u8]) -> u32 {
        let mut c = seed;
        for &b in buf { c = (c << 1) ^ POLY; }
        c
    }"""
    alg = _alg("crc32", standards=["variant:ieee_reflected"])
    findings = lint_crc32(src, alg)
    assert any(f.rule == "crc32_traversal_direction_mismatch" for f in findings)


def test_crc32_reflected_with_shift_right_is_clean():
    src = """
    const POLY: u32 = 0xEDB88320;
    pub fn crc32(seed: u32, buf: &[u8]) -> u32 {
        let mut c = seed ^ 0xFFFFFFFF;
        for &b in buf { c = (c >> 1) ^ POLY; }
        c ^ 0xFFFFFFFF
    }"""
    alg = _alg("crc32", standards=["variant:ieee_reflected"])
    findings = lint_crc32(src, alg)
    assert not has_errors(findings)


def test_crc32_mixed_polynomials_flagged():
    src = "let a = 0xEDB88320; let b = 0x04C11DB7;"
    alg = _alg("crc32")
    findings = lint_crc32(src, alg)
    assert any(f.rule == "crc32_polynomial_mixed" for f in findings)


def test_crc32c_castagnoli_polynomial_required():
    src = "pub fn crc32c() -> u32 { 0 }"
    alg = _alg("crc32c", standards=["variant:castagnoli"])
    findings = lint_crc32(src, alg)
    assert any(f.rule == "crc32_wrong_polynomial" for f in findings)


# ---------- Adler-32 ----------

def test_adler32_with_base_65521_is_clean():
    src = """
    const BASE: u32 = 65521;
    pub fn adler32(seed: u32, buf: &[u8]) -> u32 {
        let mut s1 = seed & 0xFFFF;
        let mut s2 = (seed >> 16) & 0xFFFF;
        for &b in buf { s1 += b as u32; s2 += s1; }
        (s2 << 16) | s1
    }"""
    alg = _alg("adler32")
    findings = lint_adler32(src, alg)
    assert not has_errors(findings)


def test_adler32_with_base_255_flagged():
    """The canonical BASE=255 bug must be caught at semantic-lint time."""
    src = "const BASE: u32 = 255; pub fn adler32() {}"
    alg = _alg("adler32")
    findings = lint_adler32(src, alg)
    assert any(f.rule == "adler32_wrong_base" for f in findings)


def test_adler32_s1_zero_init_flagged():
    src = """
    const BASE: u32 = 65521;
    pub fn adler32(buf: &[u8]) -> u32 {
        let mut s1 = 0;
        let mut s2 = 0;
        for &b in buf { s1 += b as u32 % BASE; }
        s1
    }"""
    alg = _alg("adler32")
    findings = lint_adler32(src, alg)
    assert any(f.rule == "adler32_s1_zero_init" for f in findings)


# ---------- SHA-256 ----------

def test_sha256_with_to_le_length_flagged():
    src = """
    pub fn sha256(input: &[u8]) -> [u8; 32] {
        let bit_len = input.len() * 8;
        let mut padded = input.to_vec();
        padded.extend_from_slice(&bit_len.to_le_bytes());
        [0; 32]
    }"""
    alg = _alg("sha256", standards=["FIPS 180-4"])
    findings = lint_sha256(src, alg)
    assert any(f.rule == "sha256_le_length_padding" for f in findings)


def test_sha256_missing_h0_flagged():
    src = "pub fn sha256(_: &[u8]) -> [u8; 32] { [0; 32] }"
    alg = _alg("sha256")
    findings = lint_sha256(src, alg)
    assert any(f.rule == "sha256_missing_h0" for f in findings)


def test_sha256_with_h0_and_be_padding_clean():
    src = """
    const H0: [u32; 8] = [0x6a09_e667, 0, 0, 0, 0, 0, 0, 0];
    pub fn sha256(input: &[u8]) -> [u8; 32] {
        let bit_len = input.len() as u64;
        let mut padded = input.to_vec();
        padded.extend_from_slice(&bit_len.to_be_bytes());
        [0; 32]
    }"""
    alg = _alg("sha256")
    findings = lint_sha256(src, alg)
    assert not has_errors(findings)


# ---------- MD5 ----------

def test_md5_with_be_length_flagged():
    src = """
    pub fn md5(input: &[u8]) -> [u8; 16] {
        let bit_len = input.len() as u64;
        let mut padded = input.to_vec();
        padded.extend_from_slice(&bit_len.to_be_bytes());
        [0; 16]
    }"""
    alg = _alg("md5")
    findings = lint_md5(src, alg)
    assert any(f.rule == "md5_be_length_padding" for f in findings)


# ---------- AES ----------

def test_aes128_wrong_round_count_flagged():
    src = "const Nr: u32 = 14; pub fn aes128() {}"
    alg = _alg("aes_encrypt", standards=["variant:aes128_ecb"])
    findings = lint_aes(src, alg)
    assert any(f.rule == "aes_wrong_round_count" for f in findings)


def test_aes128_correct_round_count_clean():
    src = "const Nr: u32 = 10; pub fn aes128() {}"
    alg = _alg("aes_encrypt", standards=["variant:aes128_ecb"])
    findings = lint_aes(src, alg)
    assert not has_errors(findings)


# ---------- Unused input ----------

def test_unused_buf_flagged():
    src = "pub fn compute(buf: &[u8]) -> u32 { 42 }"
    alg = _alg("compute", inputs=[Parameter(name="buf", rust_type="&[u8]", description="")])
    findings = lint_unused_input(src, alg)
    assert any(f.rule == "unused_input" for f in findings)


def test_used_buf_clean():
    src = "pub fn compute(buf: &[u8]) -> u32 { buf.len() as u32 }"
    alg = _alg("compute", inputs=[Parameter(name="buf", rust_type="&[u8]", description="")])
    findings = lint_unused_input(src, alg)
    assert not findings


# ---------- lint_function routing ----------

def test_lint_function_routes_crc32():
    src = "let a = 0xEDB88320;"
    alg = _alg("compute_crc32", standards=["variant:ieee_non_reflected"])
    findings = lint_function(src, alg)
    assert any("crc32" in f.rule for f in findings)


def test_lint_function_routes_adler32():
    src = "const BASE: u32 = 100;"
    alg = _alg("adler32")
    findings = lint_function(src, alg)
    assert any("adler32" in f.rule for f in findings)


def test_lint_function_handles_unknown_family():
    src = "pub fn foo(buf: &[u8]) -> u32 { 0 }"
    alg = _alg("foo", inputs=[Parameter(name="buf", rust_type="&[u8]", description="")])
    findings = lint_function(src, alg)
    # Should at least run unused_input
    assert any(f.rule == "unused_input" for f in findings)


# ---------- Summary helpers ----------

def test_summarize_for_reprompt_mentions_rules():
    findings = [
        SemanticFinding(rule="crc32_wrong_polynomial", severity="error", message="use 0xEDB88320"),
        SemanticFinding(rule="whatever", severity="warning", message="not critical"),
    ]
    summary = summarize_for_reprompt(findings)
    assert "crc32_wrong_polynomial" in summary
    # Warnings are not included
    assert "whatever" not in summary


def test_summarize_for_reprompt_empty_when_no_errors():
    assert summarize_for_reprompt([]) == ""
    assert summarize_for_reprompt([
        SemanticFinding(rule="x", severity="warning", message="w"),
    ]) == ""


def test_format_findings_line_per_finding():
    findings = [
        SemanticFinding(rule="r1", severity="error", message="m1"),
        SemanticFinding(rule="r2", severity="warning", message="m2"),
    ]
    out = format_findings(findings)
    assert "r1" in out and "r2" in out
