"""Tests for the deterministic scrubber.

Each rule has a documented before/after example. Tests prevent regression
when adding new rules.
"""

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))

import pytest
from alchemist.implementer.scrubber import (
    scrub_rust,
    scrub_toml,
    synthesize_missing_modules,
    _balance_braces,
    _strip_truncated_functions,
)


class TestKeywordTypos:
    """Model-induced doubled-letter typos: `ppub` → `pub`, `ffn` → `fn`, etc.

    These appear when the 122B model accidentally repeats the first letter
    of a keyword. Observed in zlib generation.
    """

    def test_ppub_to_pub(self):
        out, fixes = scrub_rust("ppub fn foo() {}")
        assert "pub fn foo()" in out
        assert "ppub" not in out

    def test_pppub_to_pub(self):
        out, _ = scrub_rust("pppub struct X {}")
        assert "pub struct X" in out

    def test_ffn_to_fn(self):
        out, _ = scrub_rust("pub ffn bar() {}")
        assert "pub fn bar()" in out

    def test_eenum_to_enum(self):
        out, _ = scrub_rust("pub eenum E { A }")
        assert "pub enum E" in out

    def test_ttrait_to_trait(self):
        out, _ = scrub_rust("pub ttrait T {}")
        assert "pub trait T" in out

    def test_normal_pub_unaffected(self):
        out, _ = scrub_rust("pub fn ok() {}")
        assert out.count("pub fn ok()") == 1


class TestStrayPrefixChars:
    """Stray `p ` before keywords: `p pub enum` → `pub enum`.

    Model occasionally outputs an extra letter before a keyword.
    """

    def test_stray_p_before_pub(self):
        out, _ = scrub_rust("p pub enum TreeError { A }")
        assert "pub enum TreeError" in out
        assert "p pub" not in out

    def test_stray_p_before_fn(self):
        out, _ = scrub_rust("p fn foo() {}")
        assert "fn foo()" in out

    def test_stray_p_before_struct(self):
        out, _ = scrub_rust("p struct X {}")
        assert "struct X" in out


class TestAttributeTypos:
    """Doubled hash in attributes: `##!` → `#!`, `##[` → `#[`."""

    def test_double_hash_inner_attribute(self):
        out, _ = scrub_rust("##![no_std]\nextern crate alloc;")
        assert out.startswith("#![no_std]")
        assert "##!" not in out

    def test_double_hash_outer_attribute(self):
        out, _ = scrub_rust("##[derive(Debug)]\nstruct X;")
        assert "#[derive(Debug)]" in out
        assert "##[" not in out

    def test_normal_attribute_unaffected(self):
        out, _ = scrub_rust("#![no_std]\n#[derive(Debug)]\nstruct X;")
        assert "#![no_std]" in out
        assert "#[derive(Debug)]" in out


class TestMarkdownFences:
    """Stray markdown code fences leaking into Rust files."""

    def test_strip_top_fence(self):
        out, _ = scrub_rust("```rust\npub fn foo() {}\n```")
        assert "```" not in out
        assert "pub fn foo()" in out

    def test_strip_mid_fence(self):
        code = "pub fn a() {}\n```\n}\npub fn b() {}"
        out, _ = scrub_rust(code)
        assert "```" not in out

    def test_strip_backticks_only(self):
        out, _ = scrub_rust("pub fn x() {}\n````\nmore code")
        # Lines of pure backticks should be stripped
        assert "````" not in out


class TestBraceBalance:
    """Auto-close unclosed delimiters at end of file (token truncation)."""

    def test_close_unclosed_brace(self):
        code = "pub fn foo() {\n    let x = 1;\n"  # missing }
        out, _ = _balance_braces(code)
        assert out.rstrip().endswith("}")

    def test_close_multiple_braces(self):
        code = "pub fn foo() {\n    if cond {\n        let x = 1;"
        out, _ = _balance_braces(code)
        assert out.count("}") == 2

    def test_strip_excess_braces(self):
        code = "pub fn foo() {\n    let x = 1;\n}\n}\n}"  # 2 extra }
        out, msg = _balance_braces(code)
        # Should remove the excess }
        assert out.count("}") == 1
        assert "stripped" in msg

    def test_balanced_unchanged(self):
        code = "pub fn foo() {\n    let x = 1;\n}"
        out, msg = _balance_braces(code)
        assert msg == ""

    def test_brace_in_string_ignored(self):
        code = 'pub fn foo() {\n    let s = "hello {";\n}'
        out, _ = _balance_braces(code)
        # Should NOT add an extra } for the { in string
        assert out.count("}") == 1


class TestTomlFixes:
    """Cargo.toml syntax fixes — missing commas in arrays."""

    def test_missing_array_commas(self):
        toml = '[workspace]\nmembers = [\n    "a"\n    "b"\n    "c"\n]\n'
        out, fixes = scrub_toml(toml)
        # Each "a"\n"b" pattern should become "a",\n"b"
        assert '"a",' in out
        assert '"b",' in out

    def test_already_valid_unchanged(self):
        toml = '[package]\nname = "x"\nversion = "0.1.0"\n'
        out, _ = scrub_toml(toml)
        assert "x" in out


class TestModuleSynthesis:
    """If lib.rs declares `mod X;`, src/X.rs must exist."""

    def test_create_missing_module_file(self):
        files = {"src/lib.rs": "pub mod foo;\npub mod bar;\n"}
        out = synthesize_missing_modules(files)
        assert "src/foo.rs" in out
        assert "src/bar.rs" in out

    def test_dont_overwrite_existing(self):
        files = {
            "src/lib.rs": "pub mod foo;\n",
            "src/foo.rs": "pub fn original() {}",
        }
        out = synthesize_missing_modules(files)
        assert out["src/foo.rs"] == "pub fn original() {}"

    def test_no_mod_declarations(self):
        files = {"src/lib.rs": "pub fn standalone() {}"}
        out = synthesize_missing_modules(files)
        assert len(out) == 1


class TestTruncatedFunctions:
    """Detect and stub functions with truncated bodies."""

    def test_truncated_let_assignment(self):
        code = "pub fn foo() {\n    let x =\n}"
        out, msg = _strip_truncated_functions(code)
        assert "unimplemented!" in out

    def test_truncated_method_call(self):
        code = "pub fn foo() {\n    bar.do_thing(\n}"
        out, msg = _strip_truncated_functions(code)
        assert "unimplemented!" in out

    def test_complete_function_unchanged(self):
        code = "pub fn foo() {\n    let x = 1;\n    x + 1\n}"
        out, msg = _strip_truncated_functions(code)
        assert "unimplemented!" not in out


class TestUnicodeIssues:
    """Strip the U+FFFD replacement char (UTF-8 corruption marker)."""

    def test_strip_replacement_char(self):
        out, _ = scrub_rust("pub fn foo() {\n    // bad \ufffd char\n}")
        assert "\ufffd" not in out


class TestContaminationGate:
    """Reject non-code inputs (error bodies, tracebacks) outright."""

    def test_reject_503_error_body(self):
        poison = (
            "ERROR: Server error '503 Service Unavailable' "
            "for url 'http://100.109.172.64:8090/v1/chat/completions'"
        )
        out, fixes = scrub_rust(poison)
        assert out == ""
        assert any("REJECTED" in f for f in fixes)

    def test_reject_python_traceback(self):
        poison = "Traceback (most recent call last):\n  File ...\nValueError: x"
        out, fixes = scrub_rust(poison)
        assert out == ""
        assert any("REJECTED" in f for f in fixes)

    def test_reject_json_error_envelope(self):
        poison = '{"error": "bad request"}'
        out, fixes = scrub_rust(poison)
        assert out == ""

    def test_accepts_code_with_error_word_in_comment(self):
        """ERROR as a plain identifier in real code must NOT be rejected."""
        ok = "pub fn handle() -> Result<(), Error> {\n    Ok(())\n}"
        out, _ = scrub_rust(ok)
        assert "pub fn handle" in out


class TestComboFixes:
    """Multiple rules applied to a real broken file."""

    def test_real_zlib_failure_pattern(self):
        """The actual broken pattern observed in zlib-trees: ppub + extra brace."""
        broken = """\
##![no_std]

p pub enum TreeError {
    Invalid,
}

ppub fn build() {
    todo!()
}
}"""
        out, fixes = scrub_rust(broken)
        assert "#![no_std]" in out
        assert "pub enum TreeError" in out
        assert "pub fn build()" in out
        assert "ppub" not in out
        assert "p pub" not in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
