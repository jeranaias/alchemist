"""Tests for alchemist.implementer.anti_stub."""

from __future__ import annotations

from pathlib import Path

import pytest

from alchemist.implementer.anti_stub import (
    ScanReport,
    StubViolation,
    scan_crate,
    scan_text,
    scan_workspace,
)


# ---------- Built-in stub markers ----------

def test_detects_unimplemented_macro():
    code = """
pub fn compress(input: &[u8], output: &mut [u8]) -> Result<usize, ()> {
    unimplemented!();
}
"""
    violations = scan_text("a.rs", code)
    assert any(v.pattern == "unimplemented_macro" for v in violations)


def test_detects_todo_macro():
    code = """
fn foo() -> u32 { todo!("implement later") }
"""
    violations = scan_text("a.rs", code)
    assert any(v.pattern == "todo_macro" for v in violations)


def test_detects_panic_not_implemented():
    code = '''
fn foo() { panic!("not implemented yet"); }
'''
    violations = scan_text("a.rs", code)
    assert any(v.pattern == "panic_not_impl" for v in violations)


def test_unimplemented_inside_test_module_ignored():
    code = """
#[cfg(test)]
mod tests {
    #[test]
    fn smoke() {
        unimplemented!()
    }
}
"""
    violations = scan_text("a.rs", code, skip_tests=True)
    assert all(v.pattern != "unimplemented_macro" for v in violations)


def test_unimplemented_inside_test_module_caught_when_skip_tests_false():
    code = """
#[cfg(test)]
mod tests {
    #[test]
    fn smoke() { unimplemented!() }
}
"""
    violations = scan_text("a.rs", code, skip_tests=False)
    assert any(v.pattern == "unimplemented_macro" for v in violations)


# ---------- Comment-phrase markers ----------

def test_detects_we_dont_have_the_actual_algorithm():
    code = """
pub fn compress(data: &[u8]) -> Vec<u8> {
    // Since we don't have the actual algorithm, we'll use a simple heuristic.
    data.to_vec()
}
"""
    violations = scan_text("a.rs", code)
    patterns = {v.pattern for v in violations}
    assert "comment_dont_have_algorithm" in patterns
    assert "comment_simple_heuristic" in patterns


def test_detects_for_this_spec_we_simulate():
    code = """
fn run() {
    // For this spec, we simulate the process.
    let _ = 0;
}
"""
    violations = scan_text("a.rs", code)
    assert any(v.pattern == "comment_for_this_spec" for v in violations)


def test_detects_conceptually():
    code = """
fn foo() {
    // Conceptually, this calls the underlying engine.
}
"""
    violations = scan_text("a.rs", code)
    assert any(v.pattern == "comment_conceptually" for v in violations)


def test_detects_todo_implement_comment():
    code = """
fn deflate() {
    // TODO: implement the actual deflate algorithm
}
"""
    violations = scan_text("a.rs", code)
    assert any(v.pattern == "comment_todo_implement" for v in violations)


def test_detects_not_accurate_fix_needed():
    code = """
fn compress() {
    // But this is not accurate. We need to simulate the actual process.
}
"""
    violations = scan_text("a.rs", code)
    assert any(v.pattern == "comment_not_accurate" for v in violations)


def test_block_comment_with_stub_phrase():
    code = """
/*
 * In reality, this would call the underlying engine.
 * For this spec, we simulate the call.
 */
fn foo() {}
"""
    violations = scan_text("a.rs", code)
    patterns = {v.pattern for v in violations}
    assert "comment_for_this_spec" in patterns or "comment_in_reality" in patterns


# ---------- Semantic: fn ignores inputs ----------

def test_detects_fn_ignores_input_bytes():
    code = """
pub fn compress(input: &[u8], output: &mut Vec<u8>) -> Result<(), ()> {
    Ok(())
}
"""
    violations = scan_text("a.rs", code)
    assert any(v.pattern == "fn_ignores_inputs" for v in violations)


def test_does_not_flag_fn_that_uses_input():
    code = """
pub fn compress(input: &[u8], output: &mut Vec<u8>) -> Result<(), ()> {
    for b in input { output.push(*b); }
    Ok(())
}
"""
    violations = scan_text("a.rs", code)
    assert not any(v.pattern == "fn_ignores_inputs" for v in violations)


def test_does_not_flag_underscore_prefixed_params():
    code = """
pub fn reserved(_data: &[u8]) -> Result<(), ()> {
    Ok(())
}
"""
    violations = scan_text("a.rs", code)
    assert not any(v.pattern == "fn_ignores_inputs" for v in violations)


# ---------- Utilities ----------

def test_stub_violation_str_includes_file_and_pattern():
    v = StubViolation(file="crate/src/a.rs", line=42, pattern="todo_macro", snippet="todo!()")
    s = str(v)
    assert "crate/src/a.rs:42" in s
    assert "todo_macro" in s


def test_report_summary_ok():
    r = ScanReport(files_scanned=5)
    assert r.ok
    assert "clean" in r.summary()


def test_report_summary_with_violations():
    r = ScanReport(
        violations=[
            StubViolation(file="a.rs", line=1, pattern="unimplemented_macro", snippet="unimplemented!()"),
            StubViolation(file="a.rs", line=2, pattern="unimplemented_macro", snippet="unimplemented!()"),
            StubViolation(file="b.rs", line=3, pattern="comment_conceptually", snippet="// conceptually"),
        ],
        files_scanned=2,
    )
    assert not r.ok
    summary = r.summary()
    assert "3 violations" in summary
    assert "unimplemented_macro=2" in summary


# ---------- Against real zlib output ----------

ZLIB_OUTPUT = Path(__file__).parent.parent / "subjects" / "zlib" / ".alchemist" / "output"


@pytest.mark.skipif(not ZLIB_OUTPUT.exists(), reason="zlib output not present")
def test_flags_18_plus_stubs_in_zlib_output():
    """Phase A acceptance: must flag 18+ stubs in current zlib output."""
    report = scan_workspace(ZLIB_OUTPUT)
    assert len(report.violations) >= 18, (
        f"Expected 18+ stub violations in zlib output, got {len(report.violations)}\n"
        f"{report.summary()}"
    )


@pytest.mark.skipif(not ZLIB_OUTPUT.exists(), reason="zlib output not present")
def test_compress_rs_has_violations():
    """compress.rs is the canonical stub example — must catch it."""
    compress_rs = ZLIB_OUTPUT / "zlib-compression" / "src" / "compress.rs"
    if not compress_rs.exists():
        pytest.skip("compress.rs missing")
    text = compress_rs.read_text(encoding="utf-8", errors="replace")
    violations = scan_text("zlib-compression/src/compress.rs", text)
    assert len(violations) > 0, "compress.rs should have stub violations"
