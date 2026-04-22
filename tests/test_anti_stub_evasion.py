"""Obfuscation-evasion suite for anti_stub detection.

The LLM reliably produces rewordings, whitespace variants, and compound
stub bodies that an exact-substring match misses. Phase 0 Bug #1 fix
replaced that with `has_stub_for_fn` (regex + canonical-body check).

This suite locks in the guarantee with ~30 obfuscation patterns the
previous code DID NOT catch. Every one must be detected post-fix.
"""

from __future__ import annotations

import pytest

from alchemist.implementer.anti_stub import (
    has_stub_for_fn,
    scan_text,
    _canonicalize_body,
    _is_canonical_stub,
)


# ---------------------------------------------------------------------------
# has_stub_for_fn — the Phase D revert check
# ---------------------------------------------------------------------------

BASE_FN = "my_fn"

# Each case: (rust_source, expected_detected, reason)
EVASION_CASES: list[tuple[str, bool, str]] = [
    # --- Pre-fix exact-string behaviour (all of these used to slip through) ---
    (
        'pub fn my_fn() { unimplemented!("missing fn: my_fn") }',
        True,
        "canonical stub with exact message (pre-fix also caught)",
    ),
    (
        'pub fn my_fn() { unimplemented!("fixme: my_fn") }',
        True,
        "pre-fix missed: reworded message mentioning fn name",
    ),
    (
        'pub fn my_fn() { todo!("missing fn: my_fn") }',
        True,
        "pre-fix missed: todo! instead of unimplemented!",
    ),
    (
        'pub fn my_fn() { panic!("my_fn not implemented") }',
        True,
        "pre-fix missed: panic! with 'not implemented' phrase",
    ),
    (
        'pub fn my_fn() { unimplemented  ! ( "missing fn: my_fn" ) }',
        True,
        "pre-fix missed: extra whitespace around macro !",
    ),
    (
        'pub fn my_fn() {\n    unimplemented!("my_fn")\n}',
        True,
        "pre-fix missed: newlines in body",
    ),
    (
        'pub fn my_fn() {\n    // left over from a prior iter\n    unimplemented!("my_fn")\n}',
        True,
        "pre-fix missed: comment embedded in body",
    ),
    # --- Canonical-body matches (bare stub without fn-name mention) ---
    (
        "pub fn my_fn() { unimplemented!() }",
        True,
        "bare unimplemented!()",
    ),
    (
        "pub fn my_fn() { todo!() }",
        True,
        "bare todo!()",
    ),
    (
        'pub fn my_fn() { panic!("stub") }',
        True,
        "bare panic!(stub)",
    ),
    (
        "pub fn my_fn(a: u32, b: u32) { let _ = a; let _ = b; unimplemented!() }",
        True,
        "skeleton-style body: unused + stub",
    ),
    (
        "pub fn my_fn(x: &[u8]) {\n    let _ = x;\n    todo!()\n}",
        True,
        "single param unused + stub on newlines",
    ),
    # --- TRUE POSITIVES that must still be caught ---
    (
        'fn my_fn() { todo  !  ( "x" ) }',
        True,
        "whitespace in todo! call",
    ),
    (
        'pub fn my_fn() -> u32 {\n    panic!("unimplemented: my_fn")\n}',
        True,
        "panic with 'unimplemented' phrase",
    ),
    # --- TRUE NEGATIVES: real implementations must NOT be flagged ---
    (
        "pub fn my_fn(x: u32) -> u32 { x.wrapping_add(1) }",
        False,
        "real one-liner",
    ),
    (
        'pub fn my_fn() -> &\'static str { "hello" }',
        False,
        "real function returning a literal",
    ),
    (
        "pub fn my_fn(a: u32, b: u32) -> u32 { a ^ b }",
        False,
        "real two-arg function",
    ),
    (
        'pub fn my_fn() { println!("my_fn running"); }',
        False,
        "println (not a stub, mentions fn name only in string)",
    ),
    (
        'pub fn my_fn() { let s = "unimplemented!"; eprintln!("{}", s); }',
        False,
        "string literal containing stub syntax — not a real stub",
    ),
    # --- Sibling-fn coexistence: bar is a stub but my_fn is real ---
    (
        'pub fn my_fn() -> u32 { 42 }\n'
        'pub fn bar() { unimplemented!("x") }',
        False,
        "sibling is a stub, my_fn is real — must not flag",
    ),
    # --- Compound bodies that reference fn-name in a non-stub context ---
    (
        'pub fn my_fn(input: &[u8]) -> u32 {\n'
        '    // "my_fn" is the caller-facing name\n'
        '    input.iter().copied().map(u32::from).sum()\n'
        '}',
        False,
        "comment mentions fn name, body is real",
    ),
]


@pytest.mark.parametrize("source, expected, reason", EVASION_CASES)
def test_has_stub_for_fn(source: str, expected: bool, reason: str) -> None:
    assert has_stub_for_fn(source, BASE_FN) is expected, (
        f"FAILED: {reason}\n---\n{source}\n---"
    )


# ---------------------------------------------------------------------------
# scan_text — the module-level gate used everywhere
# ---------------------------------------------------------------------------

def test_scan_catches_obfuscated_unimplemented() -> None:
    source = 'pub fn my_fn() { unimplemented  ! ( "x" ) }'
    violations = scan_text("test.rs", source, skip_tests=False)
    assert any(v.pattern == "unimplemented_macro" for v in violations)


def test_scan_catches_obfuscated_todo() -> None:
    source = 'pub fn my_fn() { todo  !( ) }'
    violations = scan_text("test.rs", source, skip_tests=False)
    assert any(v.pattern == "todo_macro" for v in violations)


def test_scan_catches_missing_phrase_in_panic() -> None:
    source = 'pub fn my_fn() { panic!("missing my_fn impl") }'
    violations = scan_text("test.rs", source, skip_tests=False)
    assert any(v.pattern == "panic_not_impl" for v in violations)


def test_scan_ignores_real_implementations() -> None:
    source = "pub fn my_fn(x: u32) -> u32 { x + 1 }"
    violations = scan_text("test.rs", source, skip_tests=False)
    # No violations expected for a real fn.
    assert len([v for v in violations if v.pattern.startswith("unimplemented")]) == 0


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------

def test_canonicalize_strips_comments_and_whitespace() -> None:
    body = """
        // a comment
        /* block comment */
        unimplemented!("x")
    """
    canonical = _canonicalize_body(body)
    assert canonical == 'unimplemented!("x")'


def test_canonicalize_detects_let_underscore_pattern() -> None:
    body = "let _ = a; let _ = b; unimplemented!()"
    canonical = _canonicalize_body(body)
    assert _is_canonical_stub(canonical)


def test_canonicalize_rejects_real_bodies() -> None:
    body = "x.wrapping_add(1)"
    canonical = _canonicalize_body(body)
    assert not _is_canonical_stub(canonical)
