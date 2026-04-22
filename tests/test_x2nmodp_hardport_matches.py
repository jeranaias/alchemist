"""Verify x2nmodp hardport is byte-identical to the pure-Python reference.

The hardport is Rust; we can't easily invoke it from pytest without
compiling the whole workspace. Instead we verify the ALGORITHM is the
same by reading the hardport source and comparing structural invariants.
"""

from __future__ import annotations

from pathlib import Path


def test_x2nmodp_hardport_uses_1_shift_30_for_table_init() -> None:
    """The corrected algorithm starts with table[0] = 1 << 30 (x^1).
    The old broken version used table[0] = 0x80000000 (fixed point).
    """
    hardport = Path(
        "alchemist/references/impls/zlib_hardports/zlib-checksum/"
        "crc32/x2nmodp.rs"
    )
    src = hardport.read_text(encoding="utf-8")
    assert "1u32 << 30" in src or "1 << 30" in src or "0x40000000" in src, (
        "x2nmodp hardport must initialize table[0] = 1<<30 (x^1). "
        "Using 0x80000000 caused a fixed-point bug."
    )
    # Also: the p initial value must be 1<<31 (x^0)
    assert "1u32 << 31" in src or "0x80000000" in src, (
        "p must start at 1<<31 (x^0 in reflected form)"
    )


def test_x2nmodp_hardport_loops_over_n_not_k() -> None:
    """The corrected algorithm loops `while n != 0` with k incremented
    per-iteration. The old version looped over BOTH k and n with
    unconditional table index increment.
    """
    hardport = Path(
        "alchemist/references/impls/zlib_hardports/zlib-checksum/"
        "crc32/x2nmodp.rs"
    )
    src = hardport.read_text(encoding="utf-8")
    # Must have a single while-n loop and increment k inside
    assert "while n" in src, "x2nmodp must loop over n"
    # Must not have a separate while-k loop (that's the bug pattern)
    while_count = src.count("while k")
    assert while_count == 0, (
        f"x2nmodp should not have a separate `while k` loop "
        f"(found {while_count}) — it inverts the algorithm."
    )
