"""Regression tests for the x2nmodp pure-Python reference.

x2nmodp must vary by input — a constant-returning implementation would
let LLM cheats like `fn crc32_combine_gen64(_) -> u32 { 0x80000000 }`
fraudulently pass the verification gate.
"""

from __future__ import annotations

import struct

from alchemist.extractor.fuzz_vectors import (
    _x2nmodp_pure_ref,
    _crc32_combine_gen64_pure_ref,
    _crc32_combine_op_pure_ref,
)


def test_x2nmodp_varies_by_input() -> None:
    """x2nmodp must NOT return a fixed value regardless of input."""
    outputs = set()
    for n in (0, 1, 10, 100, 1000, 1 << 20, 1 << 40):
        for k in (0, 3, 8, 15, 32):
            data = struct.pack("<Q", n) + struct.pack("<I", k)
            outputs.add(_x2nmodp_pure_ref(data))
    # At least 5 distinct outputs across 35 combinations — if we ever
    # regress to a fixed-point (the v8.9 bug) we get 1 output.
    assert len(outputs) >= 5, (
        f"x2nmodp appears to be constant-returning: {len(outputs)} distinct "
        "outputs across 35 inputs — did the fixed-point bug regress?"
    )


def test_x2nmodp_matches_zlib_table_entries() -> None:
    """x2nmodp(1, k) must equal zlib's x2n_table[k] (x^(2^k) mod p(x)).

    Values from zlib's crc32.h.
    """
    # x2n_table entries computed by zlib's make_crc_table, indexed [0..31].
    expected_table = [
        0x40000000, 0x20000000, 0x08000000, 0x00800000,
        0x00008000, 0xEDB88320, 0xB1E6B092, 0xA06A2517,
        0xED627DAE, 0x88D14467, 0xD7BBFE6A, 0xEC447F11,
        0x8E7EA170, 0x6427800E, 0x4D47BAE0, 0x09FE548F,
        0x83852D0F, 0x30362F1A, 0x7B5A9CC3, 0x31FEC169,
        0x9FEC022A, 0x6C8DEDC4, 0x15D6874D, 0x5FDE7A4E,
        0xBAD90E37, 0x2E4E5EEF, 0x4EABA214, 0xA8A472C0,
        0x429A969E, 0x148D302A, 0xC40BA6D0, 0xC4E22C3C,
    ]
    # x2nmodp(1, k) = multmodp(table[k], 1<<31) = table[k] (since 1<<31 is
    # x^0, the identity in reflected form).
    for k in range(32):
        data = struct.pack("<Q", 1) + struct.pack("<I", k)
        got = _x2nmodp_pure_ref(data)
        assert got == expected_table[k], (
            f"x2nmodp(1, {k}) = 0x{got:08x}, expected 0x{expected_table[k]:08x}"
        )


def test_x2nmodp_identity_at_n0() -> None:
    """x2nmodp(0, any) should return x^0 = 1 (reflected = 1<<31)."""
    for k in (0, 1, 5, 31):
        data = struct.pack("<Q", 0) + struct.pack("<I", k)
        assert _x2nmodp_pure_ref(data) == (1 << 31), (
            f"x2nmodp(0, {k}) should be x^0 = 0x80000000, "
            f"got 0x{_x2nmodp_pure_ref(data):08x}"
        )


def test_crc32_combine_gen64_varies() -> None:
    """crc32_combine_gen64 shells into x2nmodp; must vary by len2."""
    outputs = set()
    for n in (0, 1, 100, 10_000, 1_000_000, 1 << 40):
        data = struct.pack("<Q", n)
        outputs.add(_crc32_combine_gen64_pure_ref(data))
    assert len(outputs) >= 4, (
        "crc32_combine_gen64 appears constant — x2nmodp may have regressed"
    )


def test_crc32_combine_op_zero_op_returns_zero() -> None:
    """crc32_combine_op(_, _, 0) == 0 per zlib C source."""
    data = struct.pack("<III", 0x12345678, 0xABCDEF01, 0)
    assert _crc32_combine_op_pure_ref(data) == 0
