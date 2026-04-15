"""Tests for the TDD code generator (Phase 4C).

These focus on unit-level behaviors (source splicing, skeleton/test
integration) and avoid invoking the real LLM. LLM-driven behavior is
covered by higher-level integration runs outside pytest.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from alchemist.architect.schemas import CrateArchitecture, CrateSpec
from alchemist.extractor.schemas import (
    AlgorithmSpec,
    ModuleSpec,
    Parameter,
)
from alchemist.implementer.tdd_generator import (
    TDDGenerator,
    TDDResult,
)


def _cargo_available() -> bool:
    try:
        subprocess.run(["cargo", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _make_adler_spec() -> AlgorithmSpec:
    return AlgorithmSpec(
        name="adler32",
        display_name="Adler-32",
        category="checksum",
        description="Compute the Adler-32 checksum.",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="bytes")],
        return_type="u32",
        source_functions=["adler32"],
        referenced_standards=["RFC 1950"],
    )


# ---------- Source splicing ----------

def test_fn_splicing_replaces_body():
    gen = TDDGenerator()
    source = (
        "//! module doc\n"
        "\n"
        "pub fn adler32(input: &[u8]) -> u32 {\n"
        "    unimplemented!(\"stub\")\n"
        "}\n"
        "\n"
        "pub fn other(x: u32) -> u32 { x + 1 }\n"
    )
    new_fn = (
        "pub fn adler32(input: &[u8]) -> u32 {\n"
        "    let mut s1: u32 = 1;\n"
        "    let mut s2: u32 = 0;\n"
        "    for b in input { s1 = (s1 + *b as u32) % 65521; s2 = (s2 + s1) % 65521; }\n"
        "    (s2 << 16) | s1\n"
        "}"
    )
    replaced = gen._replace_fn_in_source(source, "adler32", new_fn)
    assert replaced is not None
    assert "65521" in replaced
    # other fn must survive
    assert "pub fn other" in replaced


def test_fn_splicing_survives_nested_braces():
    gen = TDDGenerator()
    source = (
        "pub fn adler32(input: &[u8]) -> u32 {\n"
        "    if true { let _x = [1, 2, 3]; }\n"
        "    unimplemented!(\"nested\")\n"
        "}\n"
    )
    new_fn = "pub fn adler32(input: &[u8]) -> u32 { 0 }"
    replaced = gen._replace_fn_in_source(source, "adler32", new_fn)
    assert replaced is not None
    assert "unimplemented" not in replaced


def test_fn_not_found_returns_none():
    gen = TDDGenerator()
    source = "pub fn foo() {}\n"
    assert gen._replace_fn_in_source(source, "bar", "pub fn bar() {}") is None


# ---------- Module-item stripping ----------

def test_strip_module_items_removes_use_lines():
    gen = TDDGenerator()
    code = (
        "use crate::tables::CRC32_TABLE;\n"
        "use core::num::Wrapping;\n"
        "\n"
        "pub fn crc32(input: &[u8]) -> u32 {\n"
        "    0\n"
        "}\n"
    )
    result = gen._strip_module_items(code)
    assert "use crate" not in result
    assert "use core" not in result
    assert "pub fn crc32" in result


def test_strip_module_items_removes_static_and_const():
    gen = TDDGenerator()
    code = (
        "static BASE: u32 = 65521;\n"
        "const NMAX: usize = 5552;\n"
        "pub fn adler32(input: &[u8]) -> u32 {\n"
        "    let base = 65521u32;\n"
        "    0\n"
        "}\n"
    )
    result = gen._strip_module_items(code)
    assert "static BASE" not in result
    assert "const NMAX" not in result
    assert "pub fn adler32" in result
    # Body content must survive
    assert "let base = 65521u32;" in result


def test_strip_module_items_keeps_const_fn():
    gen = TDDGenerator()
    code = (
        "const fn helper() -> u32 { 42 }\n"
        "pub fn main_algo(x: u32) -> u32 {\n"
        "    helper() + x\n"
        "}\n"
    )
    result = gen._strip_module_items(code)
    # const fn is a function, not a module-level const — it should be kept
    assert "const fn helper" in result
    assert "pub fn main_algo" in result


def test_strip_module_items_preserves_items_after_fn():
    """Items (use/static/const) INSIDE or after the function must survive."""
    gen = TDDGenerator()
    code = (
        "use crate::leaked_import;\n"
        "pub fn foo() -> u32 {\n"
        "    static INNER: u32 = 1;\n"
        "    INNER\n"
        "}\n"
        "const AFTER: u32 = 99;\n"
    )
    result = gen._strip_module_items(code)
    assert "use crate::leaked_import" not in result
    # Items inside or after the fn body are untouched
    assert "static INNER" in result
    assert "const AFTER" in result


def test_strip_module_items_no_fn_returns_unchanged():
    gen = TDDGenerator()
    code = "use crate::foo;\nconst X: u32 = 1;\n"
    result = gen._strip_module_items(code)
    assert result == code


# ---------- Full pipeline (mocked LLM) ----------

class _FakeLLM:
    """Stand-in for AlchemistLLM that returns a real Adler-32 implementation."""

    def __init__(self):
        self.total_cost = 0.0
        self._cached_context = None

    def create_cached_context(self, system_text, project_context=""):
        from alchemist.llm.client import CachedContext
        return CachedContext(system_prompt=system_text, project_context=project_context)

    def call_structured(self, messages, tool_name, tool_schema, cached_context=None,
                         max_tokens=6000, temperature=0.15, **kwargs):
        from alchemist.llm.client import LLMResponse
        # Return a working Adler-32 implementation
        impl = (
            "pub fn adler32(input: &[u8]) -> u32 {\n"
            "    let mut s1: u32 = 1;\n"
            "    let mut s2: u32 = 0;\n"
            "    for b in input {\n"
            "        s1 = (s1 + *b as u32) % 65521;\n"
            "        s2 = (s2 + s1) % 65521;\n"
            "    }\n"
            "    (s2 << 16) | s1\n"
            "}"
        )
        return LLMResponse(content="", structured={"content": impl})


@pytest.mark.skipif(not _cargo_available(), reason="cargo not on PATH")
def test_end_to_end_with_fake_llm(tmp_path):
    """Prove the TDD loop: skeleton → tests → per-fn impl → pass.

    Uses a fake LLM that returns a correct Adler-32 impl. The TDD loop
    must splice it in, have it compile, and have the catalog tests pass.
    """
    alg = _make_adler_spec()
    module = ModuleSpec(
        name="checksum",
        display_name="Checksums",
        description="",
        algorithms=[alg],
    )
    arch = CrateArchitecture(
        workspace_name="zlib_rs",
        description="",
        crates=[
            CrateSpec(
                name="zlib-checksum",
                description="",
                modules=["checksum"],
                is_no_std=False,
            ),
        ],
    )

    gen = TDDGenerator(llm=_FakeLLM(), max_iter_per_fn=2, holistic_after=5)
    result = gen.generate_workspace([module], arch, tmp_path)

    assert result.skeleton is not None
    assert result.skeleton.ok, "skeleton must compile"
    assert result.workspace_compiles, f"final workspace must compile:\n{result.final_stderr[:2000]}"
    assert result.workspace_tests_passed, (
        f"final tests must pass:\n{result.final_stdout[-2000:]}\n{result.final_stderr[-1000:]}"
    )
    # The adler32 attempt should have succeeded in 1 iteration
    adler_attempts = [a for a in result.attempts if a.algorithm == "adler32"]
    assert len(adler_attempts) == 1
    assert adler_attempts[0].tests_passed


@pytest.mark.skipif(not _cargo_available(), reason="cargo not on PATH")
def test_tdd_rejects_stub_code_from_llm(tmp_path):
    """Anti-stub gate must reject code containing unimplemented!() or stubby comments."""

    class _StubbyLLM:
        def __init__(self): self.total_cost = 0.0
        def create_cached_context(self, system_text, project_context=""):
            from alchemist.llm.client import CachedContext
            return CachedContext(system_prompt=system_text, project_context=project_context)
        def call_structured(self, messages, tool_name, tool_schema, **kwargs):
            from alchemist.llm.client import LLMResponse
            stubby = (
                "pub fn adler32(input: &[u8]) -> u32 {\n"
                "    // Since we don't have the actual algorithm, we simulate.\n"
                "    0\n"
                "}"
            )
            return LLMResponse(content="", structured={"content": stubby})

    alg = _make_adler_spec()
    module = ModuleSpec(name="checksum", display_name="", description="", algorithms=[alg])
    arch = CrateArchitecture(
        workspace_name="x", description="",
        crates=[CrateSpec(name="zlib-checksum", description="",
                          modules=["checksum"], is_no_std=False)],
    )
    gen = TDDGenerator(llm=_StubbyLLM(), max_iter_per_fn=2, holistic_after=5)
    result = gen.generate_workspace([module], arch, tmp_path)

    adler = next(a for a in result.attempts if a.algorithm == "adler32")
    # The stub should have been rejected every iteration → never compiled a real impl
    assert not adler.tests_passed
    # Module file should still contain unimplemented!() from the skeleton
    mod_file = (tmp_path / "zlib-checksum" / "src" / "checksum.rs").read_text()
    assert "unimplemented" in mod_file
