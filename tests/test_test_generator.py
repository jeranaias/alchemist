"""Tests for alchemist.implementer.test_generator (Phase 4B)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alchemist.architect.schemas import CrateArchitecture, CrateSpec
from alchemist.extractor.schemas import (
    AlgorithmSpec,
    ModuleSpec,
    Parameter,
    TestVector,
)
from alchemist.implementer.skeleton import generate_workspace_skeleton
from alchemist.implementer.test_generator import (
    _build_call,
    emit_module_test_block,
    generate_tests_for_workspace,
)


def _cargo_available() -> bool:
    try:
        subprocess.run(["cargo", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


# ---------- Emission ----------

def test_emit_test_block_uses_catalog_for_adler32():
    alg = AlgorithmSpec(
        name="adler32",
        display_name="Adler-32",
        category="checksum",
        description="RFC 1950 Adler-32.",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="input bytes")],
        return_type="u32",
    )
    module = ModuleSpec(
        name="checksum",
        display_name="Checksums",
        description="",
        algorithms=[alg],
    )
    src, stats = emit_module_test_block(module)
    assert stats["catalog"] >= 1
    # Canonical RFC 1950 Wikipedia test
    assert "11e60398" in src
    # Tests call through `super::adler32`
    assert "super::adler32" in src


def test_emit_test_block_uses_spec_test_vectors():
    alg = AlgorithmSpec(
        name="custom_add",
        display_name="Custom add",
        category="utility",
        description="",
        inputs=[
            Parameter(name="a", rust_type="u32", description="first"),
            Parameter(name="b", rust_type="u32", description="second"),
        ],
        return_type="u32",
        test_vectors=[
            TestVector(
                description="1 + 2 == 3",
                inputs={"a": "1", "b": "2"},
                expected_output="3",
                tolerance="exact",
            ),
        ],
    )
    module = ModuleSpec(name="math", display_name="Math", description="", algorithms=[alg])
    src, stats = emit_module_test_block(module)
    assert stats["spec"] == 1
    assert "test_custom_add_spec_0" in src
    assert "super::custom_add(a, b)" in src


def test_build_call_single_slice_fn():
    """Adler-32-style one-slice signature gets a one-arg call."""
    alg = AlgorithmSpec(
        name="adler32", display_name="", category="checksum",
        description="",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="")],
        return_type="u32",
    )
    assert _build_call("adler32", alg) == "super::adler32(input)"


def test_build_call_three_arg_signature_canonical_seed():
    """C-style `adler32(seed, buf, len)` gets `(1u32, input, input.len())`."""
    alg = AlgorithmSpec(
        name="adler32", display_name="", category="checksum",
        description="",
        inputs=[
            Parameter(name="seed", rust_type="u32", description=""),
            Parameter(name="buf",  rust_type="&[u8]", description=""),
            Parameter(name="len",  rust_type="usize", description=""),
        ],
        return_type="u32",
    )
    call = _build_call("adler32", alg)
    assert call == "super::adler32(1u32, input, input.len())"


def test_build_call_crc32_seed_defaults_to_zero():
    alg = AlgorithmSpec(
        name="crc32", display_name="", category="checksum",
        description="",
        inputs=[
            Parameter(name="seed", rust_type="u32", description=""),
            Parameter(name="buf",  rust_type="&[u8]", description=""),
            Parameter(name="len",  rust_type="usize", description=""),
        ],
        return_type="u32",
    )
    call = _build_call("crc32", alg)
    assert call == "super::crc32(0u32, input, input.len())"


def test_build_call_with_no_inputs_falls_back():
    assert _build_call("foo", None) == "super::foo(input)"


def test_generated_catalog_test_uses_full_signature_for_adler32():
    alg = AlgorithmSpec(
        name="adler32", display_name="", category="checksum",
        description="",
        inputs=[
            Parameter(name="seed", rust_type="u32", description=""),
            Parameter(name="buf",  rust_type="&[u8]", description=""),
            Parameter(name="len",  rust_type="usize", description=""),
        ],
        return_type="u32",
    )
    module = ModuleSpec(
        name="checksum", display_name="", description="",
        algorithms=[alg],
    )
    src, _stats = emit_module_test_block(module)
    # The Wikipedia catalog test must call with full signature
    assert "super::adler32(1u32, input, input.len())" in src


def test_emit_test_block_smoke_when_empty_and_enabled():
    alg = AlgorithmSpec(
        name="mystery",
        display_name="Mystery",
        category="utility",
        description="",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="")],
        return_type="()",
    )
    module = ModuleSpec(name="misc", display_name="Misc", description="", algorithms=[alg])
    src, stats = emit_module_test_block(module, enable_smoke=True)
    assert stats["smoke"] == 1
    assert "smoke_mystery" in src


# ---------- Integration with skeleton ----------

@pytest.mark.skipif(not _cargo_available(), reason="cargo not on PATH")
def test_tests_fail_on_skeleton_by_design(tmp_path):
    """The end-to-end Phase A+B smoke test.

    1. Generate skeleton (unimplemented!() bodies).
    2. Append spec + catalog tests to each module file.
    3. Run cargo test.
    4. Expect: compiles OK (still meets TDD skeleton bar), tests FAIL
       because the stubs panic with unimplemented!().
    """
    alg = AlgorithmSpec(
        name="adler32",
        display_name="Adler-32",
        category="checksum",
        description="RFC 1950 Adler-32.",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="bytes")],
        return_type="u32",
    )
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
                is_no_std=False,  # std needed for format!/String in tests
            ),
        ],
    )
    skel = generate_workspace_skeleton([module], arch, tmp_path, cargo_check=True)
    assert skel.ok, f"skeleton must compile: {skel.workspace_stderr[:1500]}"

    results = generate_tests_for_workspace([module], arch, tmp_path)
    assert len(results) == 1
    assert results[0].tests_written >= 1

    # Re-run cargo check (tests still need to compile)
    check = subprocess.run(
        ["cargo", "check", "--all-targets"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert check.returncode == 0, (
        f"cargo check --all-targets failed after test gen:\n"
        f"{check.stderr[:3000]}"
    )

    # Tests should FAIL (panic on unimplemented!()) — this is the TDD design
    test_run = subprocess.run(
        ["cargo", "test", "--no-fail-fast"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert test_run.returncode != 0, (
        "tests against skeleton stubs must FAIL (TDD forcing function)"
    )
    combined = test_run.stdout + "\n" + test_run.stderr
    assert "unimplemented" in combined.lower() or "panic" in combined.lower(), (
        f"expected panic-on-unimplemented, got:\n{combined[:2000]}"
    )
