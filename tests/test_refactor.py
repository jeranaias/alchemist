"""Tests for alchemist.architect.refactor."""

from __future__ import annotations

from pathlib import Path

import pytest

from alchemist.architect.refactor import (
    OrphanError,
    RefactorProposal,
    TypeRelocation,
    find_type_definition,
    parse_orphan_errors,
    propose_refactoring,
)


def test_parse_orphan_errors_from_real_stderr():
    stderr = """\
error[E0116]: cannot define inherent `impl` for a type outside of the crate where the type is defined
  --> zlib-deflate/src/deflate.rs:42:1
   |
42 | impl DeflateState {
   | ^^^^^^^^^^^^^^^^^
"""
    errors = parse_orphan_errors(stderr, Path("/workspace"))
    assert len(errors) == 1
    assert errors[0].type_name == "DeflateState"
    assert "zlib-deflate" in errors[0].impl_crate


def test_parse_orphan_errors_empty_on_clean_build():
    assert parse_orphan_errors("Compiling OK\n", Path(".")) == []


def test_find_type_definition_in_workspace(tmp_path):
    crate = tmp_path / "zlib-types" / "src"
    crate.mkdir(parents=True)
    (tmp_path / "zlib-types" / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    (crate / "lib.rs").write_text(
        "pub struct DeflateState {\n    pub mode: u32,\n}\n",
        encoding="utf-8",
    )
    result = find_type_definition("DeflateState", tmp_path)
    assert result is not None
    assert result[0] == "zlib-types"


def test_find_type_definition_returns_none_when_missing(tmp_path):
    crate = tmp_path / "empty" / "src"
    crate.mkdir(parents=True)
    (tmp_path / "empty" / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    (crate / "lib.rs").write_text("pub fn x() {}\n", encoding="utf-8")
    assert find_type_definition("NonExistent", tmp_path) is None


def test_propose_refactoring_identifies_relocation(tmp_path):
    # Set up: DeflateState defined in zlib-types, impl'd in zlib-deflate
    types_src = tmp_path / "zlib-types" / "src"
    types_src.mkdir(parents=True)
    (tmp_path / "zlib-types" / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    (types_src / "lib.rs").write_text(
        "pub struct DeflateState { pub mode: u32 }\n", encoding="utf-8")

    deflate_src = tmp_path / "zlib-deflate" / "src"
    deflate_src.mkdir(parents=True)
    (tmp_path / "zlib-deflate" / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    (deflate_src / "lib.rs").write_text("", encoding="utf-8")

    stderr = """\
error[E0116]: cannot define inherent `impl` for a type outside of the crate where the type is defined
  --> zlib-deflate/src/deflate.rs:42:1
   |
42 | impl DeflateState {
   | ^^^^^^^^^^^^^^^^^
"""
    proposal = propose_refactoring(stderr, tmp_path)
    assert not proposal.ok
    assert len(proposal.relocations) == 1
    r = proposal.relocations[0]
    assert r.type_name == "DeflateState"
    assert r.from_crate == "zlib-types"
    assert r.to_crate == "zlib-deflate"


def test_refactor_proposal_summary():
    p = RefactorProposal(relocations=[
        TypeRelocation("Foo", "a", "b", "E0116"),
    ])
    assert "1 type relocation" in p.summary()
    assert "Foo" in p.summary()


def test_empty_proposal_is_ok():
    assert RefactorProposal().ok
