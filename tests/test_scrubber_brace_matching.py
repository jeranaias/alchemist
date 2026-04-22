"""find_matching_brace: string- and comment-aware brace matching.

Naive brace counting in the scrubber previously miscounted when test
bodies or const expressions contained braces inside strings or comments.
This suite locks in correct behavior across the edge cases.
"""

from __future__ import annotations

import pytest

from alchemist.implementer.scrubber import find_matching_brace


def _match(text: str) -> int:
    """Helper: find the `{` at first position, return matching `}` index."""
    idx = text.find("{")
    assert idx >= 0, "test source must contain at least one `{`"
    return find_matching_brace(text, idx)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_simple_block() -> None:
    assert _match("{ x }") == 4


def test_nested_blocks() -> None:
    # { { } }  -> matches index 6
    assert _match("{ { } }") == 6


def test_fn_body() -> None:
    src = "fn f() { let x = 1; }"
    idx = src.find("{")
    assert find_matching_brace(src, idx) == src.rfind("}")


# ---------------------------------------------------------------------------
# Strings with braces
# ---------------------------------------------------------------------------

def test_brace_inside_string_literal() -> None:
    # `{ let s = "}"; }`  — the `}` inside the string must NOT close
    src = 'fn f() { let s = "}"; }'
    idx = src.find("{")
    assert find_matching_brace(src, idx) == len(src) - 1


def test_escaped_quote_in_string() -> None:
    # `{ let s = "\""; }`
    src = 'fn f() { let s = "\\""; }'
    idx = src.find("{")
    assert find_matching_brace(src, idx) == len(src) - 1


def test_nested_braces_in_string() -> None:
    src = 'fn f() { let s = "{{{{{}}}"; }'
    idx = src.find("{")
    assert find_matching_brace(src, idx) == len(src) - 1


def test_raw_string_with_braces() -> None:
    src = 'fn f() { let s = r"{{raw}}"; }'
    idx = src.find("{")
    assert find_matching_brace(src, idx) == len(src) - 1


def test_raw_string_with_hashes() -> None:
    src = 'fn f() { let s = r#"{"a": 1}"#; }'
    idx = src.find("{")
    assert find_matching_brace(src, idx) == len(src) - 1


def test_byte_string_with_braces() -> None:
    src = 'fn f() { let s = b"{}"; }'
    idx = src.find("{")
    assert find_matching_brace(src, idx) == len(src) - 1


# ---------------------------------------------------------------------------
# Char literals
# ---------------------------------------------------------------------------

def test_char_literal_opening_brace() -> None:
    src = "fn f() { let c = '{'; }"
    idx = src.find("{")
    assert find_matching_brace(src, idx) == len(src) - 1


def test_char_literal_closing_brace() -> None:
    src = "fn f() { let c = '}'; }"
    idx = src.find("{")
    assert find_matching_brace(src, idx) == len(src) - 1


def test_lifetime_not_confused_for_char() -> None:
    src = "fn f<'a>() { let s: &'a str = \"x\"; }"
    idx = src.find("{")
    assert find_matching_brace(src, idx) == len(src) - 1


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

def test_line_comment_with_brace() -> None:
    src = "fn f() { // not a real }\n let x = 1; }"
    idx = src.find("{")
    assert find_matching_brace(src, idx) == len(src) - 1


def test_block_comment_with_brace() -> None:
    src = "fn f() { /* not a } brace */ let x = 1; }"
    idx = src.find("{")
    assert find_matching_brace(src, idx) == len(src) - 1


def test_nested_block_comment() -> None:
    src = "fn f() { /* outer /* inner } */ still in */ let x = 1; }"
    idx = src.find("{")
    assert find_matching_brace(src, idx) == len(src) - 1


# ---------------------------------------------------------------------------
# Unbalanced cases
# ---------------------------------------------------------------------------

def test_unmatched_returns_negative_one() -> None:
    src = "fn f() { let x = 1;"
    idx = src.find("{")
    assert find_matching_brace(src, idx) == -1


def test_real_test_module_with_tricky_strings() -> None:
    src = '''#[cfg(test)]
mod tests {
    #[test]
    fn has_braces_in_assertion() {
        let s = "{hello}";
        assert_eq!(s, "{hello}", "should match: {s}");
    }
}
'''
    idx = src.find("{")
    close = find_matching_brace(src, idx)
    # Match should be the final `}` closing the mod block
    assert close == src.rstrip().rfind("}")
