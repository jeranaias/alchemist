"""Tests for the post-extraction spec normalizer."""

from __future__ import annotations

from alchemist.extractor.normalizer import (
    normalize_all,
    normalize_module,
    normalize_spec,
)
from alchemist.extractor.schemas import (
    AlgorithmSpec,
    ModuleSpec,
    ParamDirection,
    Parameter,
)


def _alg(name: str, inputs: list[Parameter], ret: str = "()") -> AlgorithmSpec:
    return AlgorithmSpec(
        name=name,
        display_name=name,
        category="utility",
        description="",
        inputs=inputs,
        return_type=ret,
    )


def test_vec_u8_inout_becomes_mut_slice():
    a = _alg("compress", [
        Parameter(name="dest", rust_type="Vec<u8>", description="",
                  direction=ParamDirection.inout),
    ])
    new, notes = normalize_spec(a)
    assert new.inputs[0].rust_type == "&mut [u8]"
    assert any("Vec<u8>" in n for n in notes)


def test_vec_u8_output_becomes_mut_slice():
    a = _alg("compress_z", [
        Parameter(name="dest", rust_type="Vec<u8>", description="",
                  direction=ParamDirection.output),
    ])
    new, _ = normalize_spec(a)
    assert new.inputs[0].rust_type == "&mut [u8]"


def test_vec_u8_input_becomes_borrowed_slice():
    a = _alg("uncompress_z", [
        Parameter(name="source", rust_type="Vec<u8>", description="",
                  direction=ParamDirection.input),
    ])
    new, _ = normalize_spec(a)
    assert new.inputs[0].rust_type == "&[u8]"


def test_destlen_u64_inout_becomes_mut_usize():
    a = _alg("compress2", [
        Parameter(name="destLen", rust_type="u64", description="",
                  direction=ParamDirection.inout),
    ])
    new, _ = normalize_spec(a)
    assert new.inputs[0].rust_type == "&mut usize"


def test_destlen_option_usize_inout_becomes_mut_usize():
    a = _alg("uncompress_z", [
        Parameter(name="destLen", rust_type="Option<usize>", description="",
                  direction=ParamDirection.inout),
    ])
    new, _ = normalize_spec(a)
    assert new.inputs[0].rust_type == "&mut usize"


def test_dest_len_snake_case_matches_pattern():
    a = _alg("something", [
        Parameter(name="dest_len", rust_type="u32", description="",
                  direction=ParamDirection.inout),
    ])
    new, _ = normalize_spec(a)
    assert new.inputs[0].rust_type == "&mut usize"


def test_plain_u64_input_left_alone():
    """Non-length, plain-input u64 shouldn't be rewritten."""
    a = _alg("byte_swap", [
        Parameter(name="word", rust_type="u64", description="",
                  direction=ParamDirection.input),
    ])
    new, notes = normalize_spec(a)
    assert new.inputs[0].rust_type == "u64"
    assert not notes


def test_return_type_rewritten_when_length_was_fixed():
    a = _alg(
        "compress2",
        [Parameter(name="destLen", rust_type="u64", description="",
                   direction=ParamDirection.inout)],
        ret="Result<u64, Error>",
    )
    new, _ = normalize_spec(a)
    assert new.return_type == "Result<usize, Error>"


def test_return_type_left_alone_when_no_length_fixed():
    """Result<u64, _> should only be rewritten alongside the length param."""
    a = _alg(
        "adler32_z",
        [Parameter(name="buf", rust_type="&[u8]", description="",
                   direction=ParamDirection.input)],
        ret="Result<u64, Error>",
    )
    new, _ = normalize_spec(a)
    assert new.return_type == "Result<u64, Error>"


def test_normalize_module_rewrites_each_algorithm():
    m = ModuleSpec(
        name="compress",
        display_name="",
        description="",
        algorithms=[
            _alg("a", [Parameter(name="dest", rust_type="Vec<u8>", description="",
                                 direction=ParamDirection.inout)]),
            _alg("b", [Parameter(name="destLen", rust_type="u64", description="",
                                 direction=ParamDirection.inout)]),
        ],
    )
    new, notes = normalize_module(m)
    assert new.algorithms[0].inputs[0].rust_type == "&mut [u8]"
    assert new.algorithms[1].inputs[0].rust_type == "&mut usize"
    assert len(notes) >= 2


def test_normalize_all_no_changes_when_clean():
    m = ModuleSpec(
        name="clean", display_name="", description="",
        algorithms=[_alg("good", [
            Parameter(name="buf", rust_type="&[u8]", description="",
                      direction=ParamDirection.input),
        ])],
    )
    new_mods, notes = normalize_all([m])
    assert not notes
    assert new_mods[0] is m  # unchanged reference
