"""Regression tests for crc_word and crc_word_big pure Python references.

These functions are `local` in zlib/crc32.c so they cannot be verified
via shim FFI. The Python ports are therefore the source of truth for
the pipeline — these tests lock them in against known values and
invariants derived from zlib's CRC-32 table.
"""

from __future__ import annotations

import struct

from alchemist.extractor.fuzz_vectors import (
    _CRC_TABLE,
    _CRC_BIG_TABLE,
    _crc_word_pure_ref,
    _crc_word_big_pure_ref,
)


def test_crc_table_matches_standard_polynomial() -> None:
    # Standard CRC-32 (IEEE 802.3) polynomial 0xEDB88320.
    # Known anchor values from the published table.
    assert _CRC_TABLE[0] == 0x00000000
    assert _CRC_TABLE[1] == 0x77073096
    assert _CRC_TABLE[255] == 0x2D02EF8D


def test_crc_big_table_is_byte_reversed() -> None:
    for i in range(256):
        swapped = int.from_bytes(
            _CRC_TABLE[i].to_bytes(4, "little"), "big"
        )
        assert _CRC_BIG_TABLE[i] == swapped


def test_crc_word_zero_fixed_point() -> None:
    # Zero in, zero out (no pre/post conditioning).
    assert _crc_word_pure_ref(b"\x00" * 8) == 0


def test_crc_word_varies_by_input() -> None:
    outputs = set()
    for n in range(64):
        outputs.add(_crc_word_pure_ref(n.to_bytes(8, "little")))
    # Widely different outputs — a constant-returning impl would yield 1.
    assert len(outputs) >= 50


def test_crc_word_big_zero_fixed_point() -> None:
    assert _crc_word_big_pure_ref(b"\x00" * 8) == 0


def test_crc_word_big_varies_by_input() -> None:
    outputs = set()
    for n in range(64):
        outputs.add(_crc_word_big_pure_ref(n.to_bytes(8, "little")))
    assert len(outputs) >= 50


def test_crc_word_byte_cycle_invariant() -> None:
    """crc_word is an 8-iteration byte-consumption loop.

    After 8 iterations, the original data is gone — only table lookups
    accumulated. So crc_word(data) XOR crc_word(data ^ 1) must equal
    crc_word(0) XOR crc_word(1) (linearity holds for the first byte),
    because the CRC is a GF(2) linear transform.
    """
    d0 = _crc_word_pure_ref((0).to_bytes(8, "little"))
    d1 = _crc_word_pure_ref((1).to_bytes(8, "little"))
    for v in (0x42, 0x100, 0xDEADBEEF):
        left = _crc_word_pure_ref(v.to_bytes(8, "little"))
        right = _crc_word_pure_ref((v ^ 1).to_bytes(8, "little"))
        assert (left ^ right) & 0xFFFFFFFF == (d0 ^ d1) & 0xFFFFFFFF
