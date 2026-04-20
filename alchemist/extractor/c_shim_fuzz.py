"""C-shim driven fuzz-vector generation for state-mutating functions.

The Python state_mutator module needs a reference implementation for each
state-mutator fn. Manually porting dozens of C functions to Python is slow
and error-prone. This module uses a compiled C shim DLL as the reference
oracle: it calls the actual C function with fuzzed inputs and reads back
the post-state. The result is byte-exact with the C reference by
construction.

Generalizes to any codebase: write a shim per subject that exposes
shim_reset / shim_set_<field> / shim_get_<field> / shim_run_<fn>; this
module handles the Python side uniformly.
"""

from __future__ import annotations

import ctypes
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from alchemist.extractor.schemas import AlgorithmSpec, TestVector as SpecTestVector
from alchemist.extractor.state_mutator import (
    StateFieldSpec, StateMutatorBinding,
    _render_value, fuzz_u8, fuzz_u16, fuzz_u32, fuzz_i32, fuzz_bi_valid,
    fuzz_small_vec_u8,
)


# Fixed buffer size used by the shim for byte-slice fields.
SHIM_BUF_SIZE = 2048


@dataclass
class CShimField:
    """Describes how to set/get one field through the shim's C API."""
    name: str
    rust_type: str
    fuzzer: Callable[[random.Random], Any] | None = None
    # Shim entry point names, default to shim_set_<name>/shim_get_<name>
    setter: str = ""
    getter: str = ""
    # ctypes argument type for setter (must match shim signature)
    set_argtype: Any = ctypes.c_uint
    # ctypes return type for getter
    get_restype: Any = ctypes.c_uint
    # For byte-slice / Vec<u8> fields: True means setter takes (ptr, len)
    is_byte_buf: bool = False
    # For Vec<u16> fields: True means setter takes (ptr, len) of u16s
    is_u16_buf: bool = False
    # Variable-length output buffer size (default equals SHIM_BUF_SIZE)
    max_len: int = SHIM_BUF_SIZE

    def resolved_setter(self) -> str:
        return self.setter or f"shim_set_{self.name}"

    def resolved_getter(self) -> str:
        return self.getter or f"shim_get_{self.name}"


@dataclass
class CShimPureBinding:
    """Binds a pure (non-state-mutating) function to its C shim entry.

    Example: bi_reverse(code: u32, len: i32) -> u32 — no state involved.
    The shim runner returns the value; Python packages args and fetches
    the return value directly.
    """
    name: str
    # Scalar args (ordered) — each becomes a Rust test let-binding.
    args: list[StateFieldSpec]
    # ctypes argument types for the runner (must match shim signature)
    argtypes: list = field(default_factory=list)
    # ctypes return type
    restype: Any = ctypes.c_uint
    # Return type as Rust literal suffix (e.g., "u32")
    return_rust_type: str = "u32"
    # Shim runner entry point
    runner: str = ""

    def resolved_runner(self) -> str:
        return self.runner or f"shim_run_{self.name}"


@dataclass
class CShimMutatorBinding:
    """Binds a state-mutator fn to its C shim entry + field serializers."""
    name: str
    state_type: str
    fields: list[CShimField]
    extra_args: list[StateFieldSpec] = field(default_factory=list)
    # The shim's runner entry point, default shim_run_<name>
    runner: str = ""
    # Pre-setup: function called with (dll) to set up pinned/constant state
    # fields before the binding's fields are applied. Use for things like
    # w_size / hash_size that need to be set but aren't observed.
    pre_setup: Callable[[ctypes.CDLL], None] = None

    def resolved_runner(self) -> str:
        return self.runner or f"shim_run_{self.name}"


@dataclass
class CShimObserverBinding:
    """Binds a state-observer fn (reads state, returns scalar) to its shim.

    Distinct from CShimMutatorBinding: observers don't modify state, they
    inspect it. The vector records (pre_state_fields -> return_value) so
    the Rust test sets up a DeflateState, calls the fn, and asserts the
    scalar return equals the C oracle's value.
    """
    name: str
    state_type: str
    # State fields to fuzz and pre-set before calling the fn.
    fields: list[CShimField]
    # Runner signature: fn(state) -> scalar. Shim's runner is expected
    # to internally call the real C fn with g_state and return the value.
    return_restype: Any = ctypes.c_int
    return_rust_type: str = "i32"
    runner: str = ""

    def resolved_runner(self) -> str:
        return self.runner or f"shim_run_{self.name}"


def _load_shim(dll_path: Path) -> ctypes.CDLL:
    return ctypes.CDLL(str(dll_path))


def _fuzzer_for_field(fs: CShimField, rng: random.Random) -> Any:
    if fs.fuzzer is not None:
        return fs.fuzzer(rng)
    if fs.is_byte_buf:
        n = rng.randint(0, 16)
        return bytes(rng.randint(0, 255) for _ in range(n))
    if fs.is_u16_buf:
        return [rng.randint(0, 0xFFFF) for _ in range(16)]
    return 0


def _set_field(dll: ctypes.CDLL, fs: CShimField, value: Any) -> None:
    setter = getattr(dll, fs.resolved_setter())
    if fs.is_byte_buf:
        setter.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint]
        buf = (ctypes.c_ubyte * len(value))(*value) if value else ctypes.POINTER(ctypes.c_ubyte)()
        setter(buf, len(value))
    elif fs.is_u16_buf:
        setter.argtypes = [ctypes.POINTER(ctypes.c_ushort), ctypes.c_uint]
        buf = (ctypes.c_ushort * len(value))(*value) if value else ctypes.POINTER(ctypes.c_ushort)()
        setter(buf, len(value))
    else:
        setter.argtypes = [fs.set_argtype]
        setter(fs.set_argtype(value))


def _get_field(dll: ctypes.CDLL, fs: CShimField) -> Any:
    getter = getattr(dll, fs.resolved_getter())
    if fs.is_byte_buf:
        getter.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint]
        out = (ctypes.c_ubyte * fs.max_len)()
        len_fn = getattr(dll, f"shim_get_{fs.name}_len")
        len_fn.restype = ctypes.c_uint
        n = int(len_fn())
        getter(out, fs.max_len)
        return list(out[:n])
    if fs.is_u16_buf:
        getter.argtypes = [ctypes.POINTER(ctypes.c_ushort), ctypes.c_uint]
        out = (ctypes.c_ushort * fs.max_len)()
        getter(out, fs.max_len)
        # Caller knows expected length from the pre-state value
        return list(out[:fs.max_len])
    getter.restype = fs.get_restype
    val = getter()
    if isinstance(val, int):
        return val
    return val.value if hasattr(val, "value") else val


def fuzz_pure_shim(
    dll: ctypes.CDLL,
    alg: AlgorithmSpec,
    binding: CShimPureBinding,
    *,
    count: int = 16,
    seed: int = 0x41_4C_43_48,
) -> list[SpecTestVector]:
    """Generate (args → return value) vectors via pure C shim entry."""
    rng = random.Random(seed)
    runner = getattr(dll, binding.resolved_runner())
    runner.argtypes = list(binding.argtypes)
    runner.restype = binding.restype
    vectors: list[SpecTestVector] = []
    for i in range(count):
        values: dict[str, Any] = {}
        call_args: list[Any] = []
        for idx, arg in enumerate(binding.args):
            v = arg.fuzzer(rng) if arg.fuzzer else 0
            values[arg.name] = v
            # Use the binding's declared ctypes argtype — this is the
            # shim's actual signature. Rust-side rendering (via
            # arg.rust_type) stays independent so the Rust test can use
            # the idiomatic Rust parameter type (e.g., u8) while the
            # shim's C ABI takes a promoted int.
            if idx < len(binding.argtypes):
                call_args.append(binding.argtypes[idx](v))
            else:
                call_args.append(_rust_to_ctypes(v, arg.rust_type))
        ret = runner(*call_args)
        if hasattr(ret, "value"):
            ret = ret.value
        rendered_inputs = {
            arg.name: _render_value(values[arg.name], arg.rust_type)
            for arg in binding.args
        }
        expected = _render_value(int(ret), binding.return_rust_type)
        vectors.append(SpecTestVector(
            description=f"c_shim_pure_fuzz_{i}",
            source=f"C reference via shim: {binding.resolved_runner()}",
            inputs=rendered_inputs,
            expected_output=expected,
            tolerance="exact",
        ))
    return vectors


def _rust_to_ctypes(value: int, rust_type: str) -> Any:
    """Convert a Python int to the matching ctypes primitive."""
    t = rust_type.strip()
    if t == "u8": return ctypes.c_ubyte(value)
    if t == "u16": return ctypes.c_ushort(value)
    if t == "u32": return ctypes.c_uint(value)
    if t == "u64": return ctypes.c_ulonglong(value)
    if t == "usize": return ctypes.c_size_t(value)
    if t == "i8": return ctypes.c_byte(value)
    if t == "i16": return ctypes.c_short(value)
    if t == "i32": return ctypes.c_int(value)
    if t == "i64": return ctypes.c_longlong(value)
    if t == "isize": return ctypes.c_ssize_t(value)
    return ctypes.c_uint(value)


def fuzz_with_shim(
    dll: ctypes.CDLL,
    alg: AlgorithmSpec,
    binding: CShimMutatorBinding,
    *,
    count: int = 8,
    seed: int = 0x41_4C_43_48,
) -> list[SpecTestVector]:
    """Generate (pre_state → post_state) vectors using the C shim.

    Each vector: pre-state fields packed into the shim, fn executed, post-
    state fields read out. Used by the test generator to emit Rust state-
    mutator tests that assert field-by-field equality with the C ref.
    """
    rng = random.Random(seed)
    vectors: list[SpecTestVector] = []
    runner = getattr(dll, binding.resolved_runner())
    for i in range(count):
        dll.shim_reset()
        if binding.pre_setup is not None:
            binding.pre_setup(dll)
        pre_values: dict[str, Any] = {}
        for fs in binding.fields:
            v = _fuzzer_for_field(fs, rng)
            pre_values[fs.name] = v
            _set_field(dll, fs, v)
        # Collect extra args
        extra_values: dict[str, Any] = {}
        runner_args: list = []
        for ea in binding.extra_args:
            v = ea.fuzzer(rng) if ea.fuzzer else 0
            extra_values[ea.name] = v
            # Infer ctypes type from rust_type
            if ea.rust_type in ("u8", "u16", "u32", "usize"):
                runner_args.append(ctypes.c_uint(v))
            elif ea.rust_type in ("i8", "i16", "i32", "isize"):
                runner_args.append(ctypes.c_int(v))
            else:
                runner_args.append(ctypes.c_uint(v))
        runner(*runner_args)
        post_values: dict[str, Any] = {}
        for fs in binding.fields:
            post_values[fs.name] = _get_field(dll, fs)
        # Render vector
        rendered_inputs: dict[str, str] = {}
        for fs in binding.fields:
            rendered_inputs[f"state.{fs.name}"] = _render_value(
                pre_values[fs.name], fs.rust_type,
            )
        for ea in binding.extra_args:
            rendered_inputs[ea.name] = _render_value(
                extra_values[ea.name], ea.rust_type,
            )
        lines: list[str] = []
        for fs in binding.fields:
            lines.append(
                f"{fs.name}:{fs.rust_type}="
                f"{_render_value(post_values[fs.name], fs.rust_type)}"
            )
        expected = "\n".join(lines)
        vectors.append(SpecTestVector(
            description=f"c_shim_fuzz_{i}",
            source=f"C reference via shim: {binding.resolved_runner()}",
            inputs=rendered_inputs,
            expected_output=expected,
            tolerance="state_mutator",
        ))
    return vectors


def fuzz_observer_shim(
    dll: ctypes.CDLL,
    alg: AlgorithmSpec,
    binding: CShimObserverBinding,
    *,
    count: int = 8,
    seed: int = 0x41_4C_43_48,
) -> list[SpecTestVector]:
    """Generate (pre_state -> return value) vectors for state-observer fns.

    Observer functions read the state without mutating it and return a
    scalar. Example: `detect_data_type(deflate_state*) -> int`.
    """
    rng = random.Random(seed)
    vectors: list[SpecTestVector] = []
    runner = getattr(dll, binding.resolved_runner())
    runner.restype = binding.return_restype
    for i in range(count):
        dll.shim_reset()
        pre_values: dict[str, Any] = {}
        for fs in binding.fields:
            v = _fuzzer_for_field(fs, rng)
            pre_values[fs.name] = v
            _set_field(dll, fs, v)
        ret = runner()
        if hasattr(ret, "value"):
            ret = ret.value
        rendered_inputs: dict[str, str] = {}
        for fs in binding.fields:
            rendered_inputs[f"state.{fs.name}"] = _render_value(
                pre_values[fs.name], fs.rust_type,
            )
        expected = _render_value(int(ret), binding.return_rust_type)
        vectors.append(SpecTestVector(
            description=f"c_shim_observer_{i}",
            source=f"C reference via shim: {binding.resolved_runner()}",
            inputs=rendered_inputs,
            expected_output=expected,
            tolerance="state_observer",
        ))
    return vectors


# ---------------------------------------------------------------------------
# Zlib shim bindings
# ---------------------------------------------------------------------------

def _fuzz_byte_buf(rng: random.Random) -> bytes:
    n = rng.randint(0, 16)
    return bytes(rng.randint(0, 255) for _ in range(n))


def _fuzz_u16_vec_fixed16(rng: random.Random) -> list[int]:
    return [rng.randint(0, 0xFFFF) for _ in range(16)]


ZLIB_SHIM_PURE_BINDINGS: dict[str, CShimPureBinding] = {
    "bi_reverse": CShimPureBinding(
        name="bi_reverse",
        args=[
            StateFieldSpec("code", "u32", fuzz_u32),
            StateFieldSpec("len", "u8", lambda rng: rng.randint(1, 15)),
        ],
        argtypes=[ctypes.c_uint, ctypes.c_int],
        restype=ctypes.c_uint,
        return_rust_type="u32",
    ),
}


def _fuzz_dyn_ltree_freq(rng: random.Random) -> list[int]:
    # Fill a representative slice of the dynamic literal tree's Freq field.
    # detect_data_type scans [0..31] as binary-heavy and [33..LITERALS-1] as
    # text-heavy. 128 entries is enough to exercise both branches.
    return [rng.randint(0, 255) for _ in range(128)]


ZLIB_SHIM_OBSERVER_BINDINGS: dict[str, CShimObserverBinding] = {
    "detect_data_type": CShimObserverBinding(
        name="detect_data_type",
        state_type="DeflateState",
        runner="shim_run_detect_data_type_ret",
        fields=[
            # dyn_ltree[i].Freq is the only state this function inspects.
            # The shim exposes shim_set_dyn_ltree_freq which takes (ptr, n).
            CShimField(
                "dyn_ltree_freq",
                "Vec<u16>",
                _fuzz_dyn_ltree_freq,
                setter="shim_set_dyn_ltree_freq",
                # No getter — observer doesn't read post-state.
                getter="shim_set_dyn_ltree_freq",
                is_u16_buf=True,
                max_len=128,
            ),
        ],
        return_restype=ctypes.c_int,
        return_rust_type="i32",
    ),
}


ZLIB_SHIM_BINDINGS: dict[str, CShimMutatorBinding] = {
    "bi_flush": CShimMutatorBinding(
        name="bi_flush",
        state_type="DeflateState",
        fields=[
            CShimField("bi_buf", "u16", fuzz_u16,
                       set_argtype=ctypes.c_ushort, get_restype=ctypes.c_ushort),
            CShimField("bi_valid", "i32", fuzz_bi_valid,
                       set_argtype=ctypes.c_int, get_restype=ctypes.c_int),
            CShimField("pending", "Vec<u8>", _fuzz_byte_buf,
                       is_byte_buf=True),
        ],
    ),
    "bi_windup": CShimMutatorBinding(
        name="bi_windup",
        state_type="DeflateState",
        fields=[
            CShimField("bi_buf", "u16", fuzz_u16,
                       set_argtype=ctypes.c_ushort, get_restype=ctypes.c_ushort),
            CShimField("bi_valid", "i32", fuzz_bi_valid,
                       set_argtype=ctypes.c_int, get_restype=ctypes.c_int),
            CShimField("pending", "Vec<u8>", _fuzz_byte_buf,
                       is_byte_buf=True),
        ],
    ),
    "init_block": CShimMutatorBinding(
        name="init_block",
        state_type="DeflateState",
        fields=[
            CShimField("opt_len", "u64", fuzz_u32,
                       set_argtype=ctypes.c_ulong, get_restype=ctypes.c_ulong),
            CShimField("static_len", "u64", fuzz_u32,
                       set_argtype=ctypes.c_ulong, get_restype=ctypes.c_ulong),
            # NOTE: newer zlib renamed `last_lit` to `sym_next`. Our specs
            # were extracted when the name was `last_lit`, so the Rust type
            # uses `last_lit`. The shim exposes setter/getter under
            # shim_set_sym_next name but writes to the state field — we
            # map the Rust field name via setter/getter overrides.
            CShimField("last_lit", "u32", fuzz_u32,
                       setter="shim_set_sym_next",
                       getter="shim_get_sym_next",
                       set_argtype=ctypes.c_uint, get_restype=ctypes.c_uint),
            CShimField("matches", "u32", fuzz_u32,
                       set_argtype=ctypes.c_uint, get_restype=ctypes.c_uint),
        ],
    ),
    "_tr_init": CShimMutatorBinding(
        name="_tr_init",
        state_type="DeflateState",
        runner="shim_run_tr_init",
        fields=[
            CShimField("bi_buf", "u16", fuzz_u16,
                       set_argtype=ctypes.c_ushort, get_restype=ctypes.c_ushort),
            CShimField("bi_valid", "i32", fuzz_i32,
                       set_argtype=ctypes.c_int, get_restype=ctypes.c_int),
            CShimField("opt_len", "u64", fuzz_u32,
                       set_argtype=ctypes.c_ulong, get_restype=ctypes.c_ulong),
            CShimField("static_len", "u64", fuzz_u32,
                       set_argtype=ctypes.c_ulong, get_restype=ctypes.c_ulong),
            # Rust field is `last_lit` (see DeflateState in zlib-types);
            # shim exposes the field under shim_*_sym_next name.
            CShimField("last_lit", "u32", fuzz_u32,
                       setter="shim_set_sym_next",
                       getter="shim_get_sym_next",
                       set_argtype=ctypes.c_uint, get_restype=ctypes.c_uint),
            CShimField("matches", "u32", fuzz_u32,
                       set_argtype=ctypes.c_uint, get_restype=ctypes.c_uint),
        ],
    ),
    "send_bits": CShimMutatorBinding(
        name="send_bits",
        state_type="DeflateState",
        fields=[
            CShimField("bi_buf", "u16", fuzz_u16,
                       set_argtype=ctypes.c_ushort, get_restype=ctypes.c_ushort),
            CShimField("bi_valid", "i32", lambda rng: rng.randint(0, 15),
                       set_argtype=ctypes.c_int, get_restype=ctypes.c_int),
            CShimField("pending", "Vec<u8>", _fuzz_byte_buf, is_byte_buf=True),
        ],
        extra_args=[
            StateFieldSpec("value", "u16", fuzz_u16),
            StateFieldSpec("length", "u8", lambda rng: rng.randint(1, 15)),
        ],
    ),
    "slide_hash": CShimMutatorBinding(
        name="slide_hash",
        state_type="DeflateState",
        pre_setup=lambda dll: (
            dll.shim_set_w_size(ctypes.c_ulong(16)),
            dll.shim_set_hash_size(ctypes.c_ulong(16)),
        ),
        fields=[
            CShimField("head", "Vec<u16>", _fuzz_u16_vec_fixed16, is_u16_buf=True, max_len=16),
            CShimField("prev", "Vec<u16>", _fuzz_u16_vec_fixed16, is_u16_buf=True, max_len=16),
        ],
    ),
    "_tr_align": CShimMutatorBinding(
        name="_tr_align",
        state_type="DeflateState",
        runner="shim_run_tr_align",
        fields=[
            CShimField("bi_buf", "u16", lambda rng: 0,
                       set_argtype=ctypes.c_ushort, get_restype=ctypes.c_ushort),
            CShimField("bi_valid", "i32", lambda rng: 0,
                       set_argtype=ctypes.c_int, get_restype=ctypes.c_int),
            CShimField("pending", "Vec<u8>", lambda rng: b"", is_byte_buf=True),
        ],
    ),
}


def locate_zlib_shim() -> Path | None:
    """Find the zlib C shim DLL. Convention: subjects/zlib/shim/..."""
    candidates = [
        Path("subjects/zlib/shim/zlib_state_shim.dll"),
        Path("subjects/zlib/shim/libzlib_state_shim.so"),
    ]
    for c in candidates:
        if c.exists():
            return c.resolve()
    return None
