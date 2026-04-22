"""Regression tests for compressBound_z pure Python reference and
fuzz_pure_reference's handling of Option<T> return types.
"""

from __future__ import annotations

from alchemist.extractor.fuzz_vectors import (
    _compress_bound_z_pure_ref,
    fuzz_pure_reference,
)
from alchemist.extractor.schemas import AlgorithmSpec, Parameter


def _alg() -> AlgorithmSpec:
    return AlgorithmSpec(
        name="compressBound_z",
        display_name="Compress Bound Z",
        category="compression",
        description="upper bound",
        inputs=[
            Parameter(
                name="sourceLen",
                rust_type="usize",
                description="",
                direction="input",
                constraints="",
            )
        ],
        return_type="Option<usize>",
        source_functions=["compressBound_z"],
    )


def test_compress_bound_matches_zlib_formula() -> None:
    # sourceLen + sourceLen/1000 + 12 + 6
    assert _compress_bound_z_pure_ref((0).to_bytes(8, "little")) == 18
    assert _compress_bound_z_pure_ref((1).to_bytes(8, "little")) == 19
    assert _compress_bound_z_pure_ref((1000).to_bytes(8, "little")) == 1019
    assert _compress_bound_z_pure_ref((12345).to_bytes(8, "little")) == 12375


def test_compress_bound_overflow_returns_none() -> None:
    USIZE_MAX = (1 << 64) - 1
    # Any sourceLen close to USIZE_MAX will overflow after adding 18.
    assert _compress_bound_z_pure_ref(USIZE_MAX.to_bytes(8, "little")) is None


def test_fuzz_pure_reference_emits_some_wrapper_for_option() -> None:
    vecs = fuzz_pure_reference(_alg(), _compress_bound_z_pure_ref, count=8)
    assert len(vecs) >= 1
    # Every expected value must be wrapped in Some(...) since we declared
    # Option<usize>. None cases are allowed but rare at this fuzz size.
    for v in vecs:
        exp = v.expected_output
        assert exp.startswith("Some(") or exp == "None", (
            f"expected Some(..)/None, got {exp!r}"
        )


def test_fuzz_pure_reference_scalar_path_unchanged() -> None:
    # Non-Option return types should still emit bare typed literals.
    alg = AlgorithmSpec(
        name="byte_swap",
        display_name="Byte Swap",
        category="utility",
        description="",
        inputs=[
            Parameter(
                name="data",
                rust_type="u64",
                description="",
                direction="input",
                constraints="",
            )
        ],
        return_type="u64",
        source_functions=["byte_swap"],
    )
    from alchemist.extractor.fuzz_vectors import _byte_swap_pure_ref
    vecs = fuzz_pure_reference(alg, _byte_swap_pure_ref, count=3)
    for v in vecs:
        assert v.expected_output.endswith("u64"), v.expected_output
        assert "Some(" not in v.expected_output
