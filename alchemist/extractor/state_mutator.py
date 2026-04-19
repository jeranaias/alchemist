"""State-mutator fuzz infrastructure — makes `&mut SomeState` functions verifiable.

Most zlib/mbedTLS/lwIP internal functions mutate a state struct rather
than return a scalar. They can't be verified by the scalar fuzz pipeline,
so the strict P2 policy skips them — which caps zlib at ~10% verified.

This module closes that gap. It treats every state-mutator as a function
of (pre_state_fields) → (post_state_fields), with both sides described by
a StateMutatorSpec. A pure-Python reference (faithful port of the C body)
produces ground-truth post-state dicts; the test generator emits Rust
tests that initialize a struct from the pre-state, call the function,
and assert every post-state field.

This generalizes to any codebase: every state-mutating fn gets a
StateMutatorSpec + Python reference, and the pipeline does the rest.

Contract:
  - StateMutatorSpec declares the struct type, which fields are inputs,
    which are outputs, and the Python reference fn
  - fuzz_state_mutator generates N random pre-state dicts, runs the
    reference on each, pairs them as (pre, post) TestVector inputs
  - The test generator renders these as Rust test bodies:

        #[test]
        fn test_bi_windup_state_0() {
            let mut s = DeflateState { bi_buf: 0x1234, bi_valid: 10, pending: vec![], ..Default::default() };
            super::bi_windup(&mut s);
            assert_eq!(s.bi_buf, 0u16);
            assert_eq!(s.bi_valid, 0i32);
            assert_eq!(s.pending, vec![0x34u8, 0x12u8]);
        }
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

from alchemist.extractor.schemas import AlgorithmSpec, TestVector as SpecTestVector


_FUZZ_SEED = 0x41_4C_43_48  # "ALCH"


@dataclass
class StateFieldSpec:
    """Describes one field of a state struct for test-vector generation."""
    name: str
    rust_type: str         # e.g., "u16", "i32", "Vec<u8>", "u32"
    # Random-value generator. Called with a seeded random.Random; returns
    # a Python value that will be rendered as a Rust literal.
    fuzzer: Callable[[random.Random], Any] | None = None


@dataclass
class StateMutatorBinding:
    """Binds a state-mutating C/Rust function to a Python reference.

    Example for bi_windup:
        StateMutatorBinding(
            name="bi_windup",
            state_type="DeflateState",
            fields_in=[
                StateFieldSpec("bi_buf", "u16", fuzz_u16),
                StateFieldSpec("bi_valid", "i32", fuzz_bi_valid),
                StateFieldSpec("pending", "Vec<u8>", lambda _: vec_u8()),
            ],
            reference=_bi_windup_ref,
        )
    """
    name: str
    state_type: str
    fields_in: list[StateFieldSpec]
    # Python reference: takes a dict of {field_name: value} representing
    # the pre-state, returns a dict with the same keys but post-state
    # values. The test generator compares every post-state field.
    reference: Callable[[dict[str, Any]], dict[str, Any]]
    # Optional extra args the function takes beyond the state (e.g.,
    # `send_bits(state, value: u16, length: u8)` has value+length as
    # regular args, not state fields). Each entry is (name, rust_type, fuzzer).
    extra_args: list[StateFieldSpec] = field(default_factory=list)


def _render_scalar_literal(val: int, rust_type: str) -> str:
    """Render a Python int as a typed Rust literal matching rust_type."""
    if rust_type.startswith("i") and val < 0:
        return f"({val}{rust_type})"
    return f"{int(val)}{rust_type}"


def _render_value(value: Any, rust_type: str) -> str:
    """Render a Python value as a Rust literal of the given type."""
    t = rust_type.strip()
    if t.startswith("Vec<") and t.endswith(">"):
        inner = t[4:-1].strip()
        if inner == "u8":
            items = ", ".join(f"{int(b) & 0xff}u8" for b in value)
            return f"vec![{items}]"
        if inner in ("u16", "u32", "u64", "i32", "i64", "usize"):
            items = ", ".join(_render_scalar_literal(v, inner) for v in value)
            return f"vec![{items}]"
    if t.startswith("[") and t.endswith("]"):
        # Fixed-size array — render as Rust array literal
        return "[" + ", ".join(str(v) for v in value) + "]"
    if t == "bool":
        return "true" if value else "false"
    if t.startswith("(") or t.startswith("Option<"):
        # Skip complex types we don't know how to render
        return "Default::default()"
    # Scalar
    if isinstance(value, int):
        return _render_scalar_literal(value, t)
    # Fallback
    return repr(value)


def fuzz_state_mutator(
    alg: AlgorithmSpec,
    binding: StateMutatorBinding,
    *,
    count: int = 8,
    seed: int = _FUZZ_SEED,
) -> list[SpecTestVector]:
    """Generate (pre_state → post_state) test vectors for a state-mutator.

    Each vector encodes the full pre-state + extra-args in `inputs` and
    the expected post-state as a pipe-separated `expected_output` string
    that the test generator parses to emit struct-field assertions.
    """
    rng = random.Random(seed)
    vectors: list[SpecTestVector] = []
    for i in range(count):
        pre_state: dict[str, Any] = {}
        for fs in binding.fields_in:
            if fs.fuzzer is None:
                pre_state[fs.name] = 0
            else:
                pre_state[fs.name] = fs.fuzzer(rng)
        extras: dict[str, Any] = {}
        for ea in binding.extra_args:
            if ea.fuzzer is None:
                extras[ea.name] = 0
            else:
                extras[ea.name] = ea.fuzzer(rng)
        # Run reference with combined pre-state + extras
        ref_input = dict(pre_state)
        ref_input.update(extras)
        post_state = binding.reference(ref_input)
        # Encode the test vector: inputs dict carries pre-state + extras
        # as rendered Rust literals keyed by field name. The expected_output
        # is a multi-line string the test-generator parses field-by-field.
        rendered_inputs: dict[str, str] = {}
        for fs in binding.fields_in:
            rendered_inputs[f"state.{fs.name}"] = _render_value(pre_state[fs.name], fs.rust_type)
        for ea in binding.extra_args:
            rendered_inputs[ea.name] = _render_value(extras[ea.name], ea.rust_type)
        # Encode post-state as "field:rust_type=rendered_value" lines.
        lines: list[str] = []
        for fs in binding.fields_in:
            if fs.name not in post_state:
                continue
            rendered = _render_value(post_state[fs.name], fs.rust_type)
            lines.append(f"{fs.name}:{fs.rust_type}={rendered}")
        expected = "\n".join(lines)
        vectors.append(SpecTestVector(
            description=f"state_mutator_fuzz_{i}",
            source=f"Python reference port: {binding.name}",
            inputs=rendered_inputs,
            expected_output=expected,
            tolerance="state_mutator",  # signal to test generator
        ))
    return vectors


# ---------------------------------------------------------------------------
# Field fuzzers (reusable)
# ---------------------------------------------------------------------------

def fuzz_u8(rng: random.Random) -> int:
    return rng.randint(0, 0xFF)


def fuzz_u16(rng: random.Random) -> int:
    return rng.randint(0, 0xFFFF)


def fuzz_u32(rng: random.Random) -> int:
    return rng.randint(0, 0xFFFFFFFF)


def fuzz_i32(rng: random.Random) -> int:
    return rng.randint(-0x80000000, 0x7FFFFFFF)


def fuzz_bi_valid(rng: random.Random) -> int:
    """bi_valid is bit count in [0, 16]. Zlib's invariant."""
    return rng.randint(0, 16)


def fuzz_small_vec_u8(max_len: int = 32):
    def gen(rng: random.Random) -> list[int]:
        return [rng.randint(0, 255) for _ in range(rng.randint(0, max_len))]
    return gen


# ---------------------------------------------------------------------------
# Zlib-specific state-mutator bindings
# ---------------------------------------------------------------------------

def _bi_flush_ref(s: dict[str, Any]) -> dict[str, Any]:
    """Faithful port of zlib trees.c bi_flush.

    Updates bi_buf / bi_valid / pending per the C source.
    """
    bi_buf = s["bi_buf"]
    bi_valid = s["bi_valid"]
    pending = list(s.get("pending", []))
    if bi_valid == 16:
        pending.append(bi_buf & 0xFF)
        pending.append((bi_buf >> 8) & 0xFF)
        bi_buf = 0
        bi_valid = 0
    elif bi_valid >= 8:
        pending.append(bi_buf & 0xFF)
        bi_buf = (bi_buf >> 8) & 0xFFFF
        bi_valid -= 8
    return {"bi_buf": bi_buf, "bi_valid": bi_valid, "pending": pending}


def _bi_windup_ref(s: dict[str, Any]) -> dict[str, Any]:
    """Faithful port of zlib trees.c bi_windup.

    Flushes remaining bits and zeros bi_buf / bi_valid.
    """
    bi_buf = s["bi_buf"]
    bi_valid = s["bi_valid"]
    pending = list(s.get("pending", []))
    if bi_valid > 8:
        pending.append(bi_buf & 0xFF)
        pending.append((bi_buf >> 8) & 0xFF)
    elif bi_valid > 0:
        pending.append(bi_buf & 0xFF)
    return {"bi_buf": 0, "bi_valid": 0, "pending": pending}


def _slide_hash_ref(s: dict[str, Any]) -> dict[str, Any]:
    """Faithful port of zlib deflate.c slide_hash.

    Subtracts wsize from every head[] and prev[] entry, clamping to 0
    (NIL) on underflow.
    """
    wsize = int(s["wsize"])
    head = list(s["head"])
    prev = list(s["prev"])
    new_head = [
        (h - wsize) if h >= wsize else 0 for h in head
    ]
    new_prev = [
        (p - wsize) if p >= wsize else 0 for p in prev
    ]
    return {"wsize": wsize, "head": new_head, "prev": new_prev}


def _send_bits_ref(s: dict[str, Any]) -> dict[str, Any]:
    """Faithful port of zlib trees.c send_bits.

    Writes `length` bits of `value` into the bit buffer.
    Requires extra args `value` and `length` in the input dict.
    """
    bi_buf = s["bi_buf"] & 0xFFFF
    bi_valid = s["bi_valid"]
    pending = list(s.get("pending", []))
    value = s["value"] & 0xFFFF
    length = s["length"]
    BUF_SIZE = 16
    if bi_valid > BUF_SIZE - length:
        bi_buf |= (value << bi_valid) & 0xFFFF
        pending.append(bi_buf & 0xFF)
        pending.append((bi_buf >> 8) & 0xFF)
        bi_buf = (value >> (BUF_SIZE - bi_valid)) & 0xFFFF
        bi_valid = bi_valid + length - BUF_SIZE
    else:
        bi_buf |= (value << bi_valid) & 0xFFFF
        bi_valid += length
    return {"bi_buf": bi_buf, "bi_valid": bi_valid, "pending": pending}


def _init_block_ref(s: dict[str, Any]) -> dict[str, Any]:
    """Faithful port of zlib trees.c init_block.

    Zeroes opt_len/static_len/last_lit/matches and a few counters.
    For the test, we track these observable fields only.
    """
    return {
        "opt_len": 0,
        "static_len": 0,
        "last_lit": 0,
        "matches": 0,
    }


def _fuzz_small_vec_u16(max_len: int = 8, min_val: int = 0, max_val: int = 0xFFFF):
    def gen(rng: random.Random) -> list[int]:
        return [rng.randint(min_val, max_val) for _ in range(max_len)]
    return gen


ZLIB_STATE_MUTATORS: dict[str, StateMutatorBinding] = {
    "bi_flush": StateMutatorBinding(
        name="bi_flush",
        state_type="DeflateState",
        fields_in=[
            StateFieldSpec("bi_buf", "u16", fuzz_u16),
            StateFieldSpec("bi_valid", "i32", fuzz_bi_valid),
            StateFieldSpec("pending", "Vec<u8>", fuzz_small_vec_u8(16)),
        ],
        reference=_bi_flush_ref,
    ),
    "bi_windup": StateMutatorBinding(
        name="bi_windup",
        state_type="DeflateState",
        fields_in=[
            StateFieldSpec("bi_buf", "u16", fuzz_u16),
            StateFieldSpec("bi_valid", "i32", fuzz_bi_valid),
            StateFieldSpec("pending", "Vec<u8>", fuzz_small_vec_u8(16)),
        ],
        reference=_bi_windup_ref,
    ),
    "slide_hash": StateMutatorBinding(
        name="slide_hash",
        state_type="DeflateState",
        fields_in=[
            StateFieldSpec("wsize", "u32", lambda rng: 16),
            StateFieldSpec("w_size", "usize", lambda rng: 16),
            StateFieldSpec("hash_size", "u32", lambda rng: 16),
            StateFieldSpec("head", "Vec<u16>", _fuzz_small_vec_u16(16)),
            StateFieldSpec("prev", "Vec<u16>", _fuzz_small_vec_u16(16)),
        ],
        reference=_slide_hash_ref,
    ),
    "init_block": StateMutatorBinding(
        name="init_block",
        state_type="DeflateState",
        fields_in=[
            StateFieldSpec("opt_len", "u64", fuzz_u32),
            StateFieldSpec("static_len", "u64", fuzz_u32),
            StateFieldSpec("last_lit", "u32", fuzz_u32),
            StateFieldSpec("matches", "u32", fuzz_u32),
        ],
        reference=_init_block_ref,
    ),
    "_tr_init": StateMutatorBinding(
        name="_tr_init",
        state_type="DeflateState",
        fields_in=[
            StateFieldSpec("bi_buf", "u16", fuzz_u16),
            StateFieldSpec("bi_valid", "i32", fuzz_i32),
            StateFieldSpec("opt_len", "u64", fuzz_u32),
            StateFieldSpec("static_len", "u64", fuzz_u32),
            StateFieldSpec("last_lit", "u32", fuzz_u32),
            StateFieldSpec("matches", "u32", fuzz_u32),
        ],
        reference=lambda s: {
            "bi_buf": 0, "bi_valid": 0,
            "opt_len": 0, "static_len": 0,
            "last_lit": 0, "matches": 0,
        },
    ),
    "send_bits": StateMutatorBinding(
        name="send_bits",
        state_type="DeflateState",
        fields_in=[
            StateFieldSpec("bi_buf", "u16", fuzz_u16),
            StateFieldSpec("bi_valid", "i32", lambda rng: rng.randint(0, 15)),
            StateFieldSpec("pending", "Vec<u8>", fuzz_small_vec_u8(16)),
        ],
        extra_args=[
            StateFieldSpec("value", "u16", fuzz_u16),
            StateFieldSpec("length", "u8", lambda rng: rng.randint(1, 15)),
        ],
        reference=_send_bits_ref,
    ),
}
