"""Tests for alchemist.implementer.skeleton (Phase 4A)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alchemist.architect.schemas import (
    CrateArchitecture,
    CrateSpec,
    ErrorType,
    ErrorVariant,
    TraitMethod,
    TraitSpec,
)
from alchemist.extractor.schemas import (
    AlgorithmSpec,
    ModuleSpec,
    Parameter,
    SharedType,
    TypeField,
)
from alchemist.implementer.skeleton import (
    _fn_signature,
    emit_error_enum,
    emit_function_stub,
    emit_shared_type,
    emit_trait,
    generate_crate_skeleton,
    generate_workspace_skeleton,
)


def _make_adler32_spec() -> AlgorithmSpec:
    return AlgorithmSpec(
        name="adler32",
        display_name="RFC 1950 Adler-32 checksum",
        category="checksum",
        description="Compute the Adler-32 checksum over a byte slice.",
        inputs=[
            Parameter(name="adler", rust_type="u32", description="previous checksum"),
            Parameter(name="buf", rust_type="&[u8]", description="input bytes"),
        ],
        return_type="u32",
        source_functions=["adler32"],
        referenced_standards=["RFC 1950"],
    )


def _make_simple_module() -> ModuleSpec:
    return ModuleSpec(
        name="checksum",
        display_name="Checksums",
        description="Adler-32 implementation.",
        algorithms=[_make_adler32_spec()],
        shared_types=[],
    )


def _make_simple_arch() -> CrateArchitecture:
    return CrateArchitecture(
        workspace_name="zlib_rs",
        description="test",
        crates=[
            CrateSpec(
                name="zlib-checksum",
                description="checksum algorithms",
                modules=["checksum"],
                is_no_std=True,
            ),
        ],
    )


# ---------- Unit tests: emission primitives ----------

def test_fn_signature_basic():
    spec = _make_adler32_spec()
    sig = _fn_signature(spec)
    assert sig == "pub fn adler32(adler: u32, buf: &[u8]) -> u32"


def test_fn_signature_unit_return():
    spec = AlgorithmSpec(
        name="reset",
        display_name="Reset",
        category="utility",
        description="Reset state.",
        inputs=[],
        return_type="()",
    )
    sig = _fn_signature(spec)
    assert sig == "pub fn reset()"


def test_emit_function_stub_has_unimplemented_body_and_doc():
    spec = _make_adler32_spec()
    code = emit_function_stub(spec)
    assert "unimplemented!" in code
    assert "pub fn adler32" in code
    assert "RFC 1950" in code


def test_emit_shared_type_preserves_definition():
    t = SharedType(
        name="Z_Stream",
        rust_definition="pub struct Z_Stream {\n    pub next_in: usize,\n}",
        description="stream handle",
        fields=[],
    )
    out = emit_shared_type(t)
    assert "pub struct Z_Stream" in out
    assert "next_in" in out


def test_emit_shared_type_prepends_pub_if_missing():
    t = SharedType(
        name="Foo",
        rust_definition="struct Foo { pub x: u32 }",
        description="",
    )
    assert emit_shared_type(t).startswith("pub ")


def test_emit_shared_type_placeholder_when_no_definition():
    t = SharedType(
        name="Bar",
        rust_definition="",
        description="",
        fields=[TypeField(name="count", rust_type="u32", description="")],
    )
    out = emit_shared_type(t)
    assert "pub struct Bar" in out
    assert "pub count: u32" in out


def test_emit_error_enum_emits_variants_and_display():
    err = ErrorType(
        name="CompressError",
        crate="zlib-compression",
        variants=[
            ErrorVariant(name="BufferTooSmall", description="output buffer too small"),
            ErrorVariant(name="InvalidInput", description="input was invalid"),
        ],
    )
    out = emit_error_enum(err)
    assert "pub enum CompressError" in out
    assert "BufferTooSmall" in out
    assert "InvalidInput" in out
    assert "impl core::fmt::Display for CompressError" in out


def test_emit_trait_without_default_emits_signature_only():
    tr = TraitSpec(
        name="Checksum",
        description="compute a checksum",
        crate="zlib-checksum",
        methods=[
            TraitMethod(name="update", signature="fn update(&mut self, data: &[u8])", description="feed data"),
            TraitMethod(name="digest", signature="fn digest(&self) -> u32", description="current checksum"),
        ],
    )
    code = emit_trait(tr)
    assert "pub trait Checksum {" in code
    assert "fn update(&mut self, data: &[u8]);" in code
    assert "fn digest(&self) -> u32;" in code


def test_emit_trait_with_default_emits_body():
    tr = TraitSpec(
        name="Closable",
        description="",
        crate="crateA",
        methods=[
            TraitMethod(
                name="close",
                signature="fn close(&mut self)",
                description="",
                has_default=True,
            ),
        ],
    )
    code = emit_trait(tr)
    assert "fn close(&mut self) {" in code
    assert "unimplemented!" in code


# ---------- Integration: generate_crate_skeleton emits files ----------

def test_generate_crate_skeleton_writes_cargo_lib_and_module(tmp_path):
    arch = _make_simple_arch()
    module = _make_simple_module()
    res = generate_crate_skeleton(
        arch.crates[0], [module], arch, tmp_path,
        include_workspace_tag=True,
    )
    assert (tmp_path / "zlib-checksum" / "Cargo.toml").exists()
    assert (tmp_path / "zlib-checksum" / "src" / "lib.rs").exists()
    assert (tmp_path / "zlib-checksum" / "src" / "checksum.rs").exists()
    # Module file contains the fn stub
    mod_text = (tmp_path / "zlib-checksum" / "src" / "checksum.rs").read_text()
    assert "pub fn adler32" in mod_text
    assert "unimplemented!" in mod_text
    # lib.rs declares the module
    lib_text = (tmp_path / "zlib-checksum" / "src" / "lib.rs").read_text()
    assert "pub mod checksum;" in lib_text
    assert "#![no_std]" in lib_text
    # Cargo.toml has workspace tag for standalone compile
    toml_text = (tmp_path / "zlib-checksum" / "Cargo.toml").read_text()
    assert "[workspace]" in toml_text


# ---------- Integration: generate_workspace_skeleton actually compiles ----------

def test_workspace_skeleton_compiles(tmp_path):
    """The produced skeleton must pass `cargo check` on the workspace."""
    # Require cargo on PATH
    try:
        subprocess.run(["cargo", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("cargo not on PATH")

    arch = _make_simple_arch()
    module = _make_simple_module()
    result = generate_workspace_skeleton(
        [module], arch, tmp_path, cargo_check=True,
    )
    if not result.workspace_compiles:
        pytest.fail(f"workspace skeleton did not compile:\n{result.workspace_stderr[:3000]}")
    assert all(cr.compiles for cr in result.crate_results)


def test_workspace_skeleton_compiles_with_errors_and_traits(tmp_path):
    """Skeleton with Error enum and Trait must also compile."""
    try:
        subprocess.run(["cargo", "--version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("cargo not on PATH")

    module = _make_simple_module()
    arch = CrateArchitecture(
        workspace_name="test_ws",
        description="",
        crates=[
            CrateSpec(
                name="zlib-checksum",
                description="",
                modules=["checksum"],
                is_no_std=True,
            ),
        ],
        error_types=[
            ErrorType(
                name="Error",
                crate="zlib-checksum",
                variants=[ErrorVariant(name="BadInput", description="bad input")],
            ),
        ],
        traits=[
            TraitSpec(
                name="Checksum",
                description="checksum trait",
                crate="zlib-checksum",
                methods=[
                    TraitMethod(
                        name="compute",
                        signature="fn compute(&self, input: &[u8]) -> u32",
                        description="",
                    ),
                ],
            ),
        ],
    )
    result = generate_workspace_skeleton([module], arch, tmp_path, cargo_check=True)
    assert result.ok, f"skeleton w/ errors+traits did not compile:\n{result.workspace_stderr[:3000]}"
