"""Regression tests for test-filter isolation and cache restore integrity.

These tests lock in fixes from the v8.6→v8.9 sequence:
1. _test_filters_for_fn produces distinct filters that don't prefix-match
   sibling function tests (crc32 vs crc32_combine_*).
2. _extract_full_fn saves signature+body, not body-only, so the splice
   guard at restore time accepts the cache entry.
3. _only_mutability_diff preserves `&T` when only mutability differs
   from the auditor's always-`&mut T` output.
"""

from __future__ import annotations

from pathlib import Path

from alchemist.implementer.tdd_generator import (
    TDDGenerator,
    _test_filters_for_fn,
)


def test_filters_target_this_fn_and_no_sibling() -> None:
    """Filters for `crc32` must match its own tests but none of
    `crc32_combine_gen64` or `crc32_combine_op`'s tests.
    """
    filters = _test_filters_for_fn("crc32")
    assert "test_crc32_vec_" in filters
    assert "test_crc32_spec_" in filters
    assert "test_crc32_state_" in filters
    assert "test_crc32_observer_" in filters
    assert "smoke_crc32" in filters

    # No filter substring matches 'test_crc32_combine_gen64_spec_0', which
    # is the sibling test that was falsely failing the crc32 function
    # under the old `test_crc32_` prefix.
    sibling = "test_crc32_combine_gen64_spec_0"
    for f in filters:
        assert f not in sibling, (
            f"filter {f!r} matches sibling test name {sibling!r} — "
            "would cause prefix-collision failures"
        )

    # Also check crc32_combine_op — different sibling prefix.
    sibling2 = "test_crc32_combine_op_spec_5"
    for f in filters:
        assert f not in sibling2, (
            f"filter {f!r} matches sibling test name {sibling2!r}"
        )


def test_filters_are_distinct_per_family() -> None:
    filters = _test_filters_for_fn("adler32_z")
    # Each suffix category should be its own filter — never concatenated
    assert any("_vec_" in f for f in filters)
    assert any("_spec_" in f for f in filters)
    assert any("_state_" in f for f in filters)
    assert any("_observer_" in f for f in filters)


def test_splice_guard_accepts_full_fn_cache_entry(tmp_path: Path) -> None:
    """A cached win stored as a full `pub fn ... { ... }` item must splice
    in without being rejected by the body-only guard.
    """
    gen = TDDGenerator()
    source = (
        "#![allow(unused_imports)]\n"
        "use crate::*;\n"
        "\n"
        "pub fn adler32(input: &[u8]) -> u32 {\n"
        "    unimplemented!(\"stub\")\n"
        "}\n"
    )
    # Full-fn format (correct for the modern cache).
    cache = "pub fn adler32(input: &[u8]) -> u32 { 0 }"
    replaced = gen._replace_fn_in_source(source, "adler32", cache)
    assert replaced is not None
    assert "pub fn adler32" in replaced
    assert "unimplemented" not in replaced


def test_splice_guard_rejects_body_only_legacy_cache(tmp_path: Path) -> None:
    """A legacy cache entry stored as body-only (no `pub fn` signature)
    must be rejected. Splicing a bare body in place of the entire fn
    item produces orphan code.
    """
    gen = TDDGenerator()
    source = (
        "#![allow(unused_imports)]\n"
        "use crate::*;\n"
        "\n"
        "pub fn adler32_z(adler: u32, buf: &[u8], len: usize) -> u32 {\n"
        "    unimplemented!(\"stub\")\n"
        "}\n"
    )
    # Body-only cache (the broken legacy format).
    cache = "let s1 = adler & 0xFFFF; let s2 = adler >> 16; (s2 << 16) | s1"
    replaced = gen._replace_fn_in_source(source, "adler32_z", cache)
    assert replaced is None, (
        "body-only cache entry must be rejected — splicing it replaces "
        "the whole fn item with orphan statements"
    )


def test_auditor_preserves_immutable_ref_when_spec_knows() -> None:
    """Spec auditor must respect `&DeflateState` spec even when its own
    heuristic would output `&mut DeflateState` (C pointer syntax carries
    no mutability info, so the spec is more precise).
    """
    from alchemist.extractor.spec_auditor import _only_mutability_diff
    assert _only_mutability_diff("&DeflateState", "&mut DeflateState")
    assert _only_mutability_diff("&mut X", "&X")  # the reverse
    # Same type, nothing to fix
    assert not _only_mutability_diff("&DeflateState", "&DeflateState")
    # Different base type, a real mismatch
    assert not _only_mutability_diff("&InflateState", "&mut DeflateState")
