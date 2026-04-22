"""Fuzz-generate test vectors by calling the C reference library.

When a function's spec has no extracted test_vectors and the standards
catalog has no matching published vectors, this module generates
(input, output) pairs by calling the C reference with random inputs.
The vectors are then used by Phase B of the TDD loop to emit real
correctness tests — closing the "compile-only pass" loophole.

Contract:
  - Takes a loaded C DLL (via ctypes), an AlgorithmSpec, and a function
    signature descriptor.
  - Returns a list of SpecTestVector objects suitable for adding to
    AlgorithmSpec.test_vectors.
  - Deterministic for a given seed — same inputs every time. Use PCG-64
    via Python's secrets-seeded random so vectors are stable for CI but
    not trivially predictable.
  - Supports categories: checksum, hash. Extension points for cipher,
    compression (see TODOs).
"""

from __future__ import annotations

import ctypes
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

from alchemist.extractor.schemas import AlgorithmSpec, TestVector as SpecTestVector


# Deterministic seed for reproducibility. Change when you want to
# regenerate fuzz vectors across CI runs.
_FUZZ_SEED = 0x41_4C_43_48  # "ALCH"


@dataclass
class CFunctionBinding:
    """Describes how to call a C function from Python ctypes.

    Example for adler32:
        CFunctionBinding(
            c_name="adler32",
            restype=ctypes.c_ulong,
            argtypes=(ctypes.c_ulong, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint),
            adapter=lambda fn, data: fn(1, (ctypes.c_ubyte * len(data))(*data), len(data)),
        )
    """
    c_name: str
    restype: type
    argtypes: tuple
    adapter: callable  # (resolved_fn, input_bytes) -> output

    def load(self, dll: ctypes.CDLL):
        fn = getattr(dll, self.c_name)
        fn.restype = self.restype
        fn.argtypes = list(self.argtypes)
        return fn


def _rng(seed: int = _FUZZ_SEED) -> random.Random:
    return random.Random(seed)


def _gen_byte_inputs(rng: random.Random, n: int) -> list[bytes]:
    """Generate a diverse set of byte-string inputs."""
    out: list[bytes] = []
    # Edge cases first
    out.append(b"")
    out.append(b"\x00")
    out.append(b"\xff")
    out.append(b"a")
    out.append(b"ab")
    out.append(b"abc")
    out.append(b"\x00" * 16)
    out.append(b"\xff" * 16)
    out.append(b"The quick brown fox jumps over the lazy dog")
    out.append(bytes(range(256)))
    # Random inputs of various lengths
    lengths = [1, 4, 7, 15, 31, 63, 127, 255, 511, 1023]
    for L in lengths:
        out.append(bytes(rng.randint(0, 255) for _ in range(L)))
    # More random inputs to pad to n
    while len(out) < n:
        L = rng.randint(0, 2048)
        out.append(bytes(rng.randint(0, 255) for _ in range(L)))
    return out[:n]


def fuzz_checksum_vectors(
    dll: ctypes.CDLL,
    alg: AlgorithmSpec,
    binding: CFunctionBinding,
    *,
    count: int = 20,
    seed: int = _FUZZ_SEED,
) -> list[SpecTestVector]:
    """Generate test vectors for a checksum-category function.

    The adapter is expected to accept (resolved_fn, data_bytes) and
    return an integer checksum value. Handles both byte-slice-input
    functions (adler32, crc32) and scalar-input functions (crc32_combine_gen64)
    by rendering each parameter according to its declared rust_type.
    """
    fn = binding.load(dll)
    rng = _rng(seed)
    inputs = _gen_byte_inputs(rng, count)
    vectors: list[SpecTestVector] = []
    for data in inputs:
        output = binding.adapter(fn, data)
        inputs_dict = _render_param_literals(alg, data)
        ret = (alg.return_type or "u32").strip()
        if ret in ("u8", "u16", "u32", "u64", "usize", "i8", "i16", "i32", "i64", "isize"):
            expected = f"{int(output)}{ret}"
        else:
            expected = f"0x{output:08x}"
        vectors.append(SpecTestVector(
            description=f"fuzz_input_len_{len(data)}",
            source=f"C reference: {binding.c_name}",
            inputs=inputs_dict,
            expected_output=expected,
            tolerance="exact",
        ))
    return vectors


def _render_param_literals(alg: AlgorithmSpec, data: bytes) -> dict[str, str]:
    """Render each parameter's value as a Rust literal consuming bytes."""
    result: dict[str, str] = {}
    offset = 0
    for p in alg.inputs or []:
        t = (p.rust_type or "").strip()
        if "[u8]" in t or "Vec<u8>" in t:
            result[p.name] = _bytes_to_rust_literal(bytes(data))
            continue
        if _re_scalar.fullmatch(t):
            size = _scalar_size(t)
            chunk = bytes(data[offset:offset + size].ljust(size, b"\x00"))
            val = int.from_bytes(chunk, "little")
            if t.startswith("i") and val & (1 << (size * 8 - 1)):
                val -= 1 << (size * 8)
            if val < 0:
                result[p.name] = f"({val}{t})"
            else:
                result[p.name] = f"{val}{t}"
            offset += size
            continue
        # Fallback
        result[p.name] = _bytes_to_rust_literal(bytes(data))
    return result


def _bytes_to_rust_literal(data: bytes) -> str:
    """Convert raw bytes into a Rust &[u8] literal."""
    if not data:
        return "&[]"
    return "&[" + ", ".join(f"0x{b:02x}" for b in data) + "]"


def _primary_input_name(alg: AlgorithmSpec) -> str:
    """Pick the primary byte-slice input parameter name."""
    for p in alg.inputs or []:
        t = (p.rust_type or "").lower()
        if "[u8]" in t or "bytes" in t or "vec<u8>" in t:
            return p.name
    # Fallback: first param
    return (alg.inputs[0].name if alg.inputs else "input")


# ---------------------------------------------------------------------------
# Registry of category → fuzz strategy
# ---------------------------------------------------------------------------

def fuzz_hash_vectors(
    dll: ctypes.CDLL,
    alg: AlgorithmSpec,
    binding: CFunctionBinding,
    *,
    count: int = 20,
    seed: int = _FUZZ_SEED,
) -> list[SpecTestVector]:
    """Generate test vectors for a hash-category function.

    The adapter returns bytes (the hash digest). Output is encoded as a
    Rust byte-literal so the test generator can splice it verbatim.
    """
    fn = binding.load(dll)
    rng = _rng(seed)
    inputs = _gen_byte_inputs(rng, count)
    vectors: list[SpecTestVector] = []
    primary = _primary_input_name(alg)
    for data in inputs:
        digest = binding.adapter(fn, data)
        if not isinstance(digest, (bytes, bytearray)):
            # Unsupported return type for this adapter shape
            continue
        vectors.append(SpecTestVector(
            description=f"fuzz_input_len_{len(data)}",
            source=f"C reference: {binding.c_name}",
            inputs={primary: _bytes_to_rust_literal(bytes(data))},
            expected_output=_bytes_to_rust_literal(bytes(digest)),
            tolerance="exact",
        ))
    return vectors


def fuzz_pure_reference(
    alg: AlgorithmSpec,
    reference,
    *,
    count: int = 20,
    seed: int = _FUZZ_SEED,
) -> list[SpecTestVector]:
    """Generate vectors via a pure-Python reference implementation.

    For functions the C DLL doesn't export (static/inline helpers),
    define the reference as a Python callable `fn(input_bytes) -> output`
    that faithfully mirrors the C semantics. This still grounds the
    test in a canonical source — just not the DLL.

    Input representation in the emitted test is chosen per-parameter:
    byte-slice params (`&[u8]`, `Vec<u8>`) get `&[...]` literals; scalar
    params (u8/u16/u32/u64/i32/usize/...) get typed integer literals.
    Multi-param scalar functions get inputs packed from the fuzz bytes.
    """
    import struct
    rng = _rng(seed)
    inputs_raw = _gen_byte_inputs(rng, count)
    vectors: list[SpecTestVector] = []
    params = alg.inputs or []

    def as_input_dict(data: bytes) -> dict[str, str]:
        """Render each parameter's value as a Rust literal, consuming bytes in order."""
        result: dict[str, str] = {}
        offset = 0
        for p in params:
            t = (p.rust_type or "").strip()
            if "[u8]" in t or "Vec<u8>" in t:
                result[p.name] = _bytes_to_rust_literal(bytes(data))
                continue
            if _re_scalar.fullmatch(t):
                size = _scalar_size(t)
                chunk = bytes(data[offset:offset + size].ljust(size, b"\x00"))
                val = int.from_bytes(chunk, "little")
                if t.startswith("i") and val & (1 << (size * 8 - 1)):
                    val -= 1 << (size * 8)
                result[p.name] = f"{val}{t}" if not t.startswith("i") or val >= 0 else f"({val}{t})"
                offset += size
                continue
            # Fallback — pass as byte slice
            result[p.name] = _bytes_to_rust_literal(bytes(data))
        return result

    for data in inputs_raw:
        # Adapter returns either an int (scalar output) or bytes.
        output = reference(data)
        if isinstance(output, (bytes, bytearray)):
            expected = _bytes_to_rust_literal(bytes(output))
        else:
            # Prefer the declared return type for correct suffix.
            ret = (alg.return_type or "u32").strip()
            if ret in ("u8", "u16", "u32", "u64", "usize"):
                expected = f"{int(output)}{ret}"
            elif ret in ("i8", "i16", "i32", "i64", "isize"):
                expected = f"{int(output)}{ret}"
            else:
                expected = f"0x{int(output):08x}"
        vectors.append(SpecTestVector(
            description=f"fuzz_input_len_{len(data)}",
            source=f"pure Python reference: {alg.name}",
            inputs=as_input_dict(data),
            expected_output=expected,
            tolerance="exact",
        ))
    return vectors


_re_scalar = re.compile(r"^(?:u|i)(?:8|16|32|64|size)$")


def _scalar_size(t: str) -> int:
    if t.endswith("8"):
        return 1
    if t.endswith("16"):
        return 2
    if t.endswith("32"):
        return 4
    if t.endswith("64") or t.endswith("size"):
        return 8
    return 4


def fuzz_for_spec(
    dll: ctypes.CDLL,
    alg: AlgorithmSpec,
    bindings: dict[str, CFunctionBinding],
    *,
    count: int = 20,
    seed: int = _FUZZ_SEED,
    pure_references: dict[str, callable] | None = None,
) -> list[SpecTestVector]:
    """Dispatch based on algorithm category. Returns [] if unsupported.

    Prefers C-DLL bindings when available; falls back to pure-Python
    references for functions not exported by the DLL.
    """
    if pure_references and alg.name in pure_references:
        return fuzz_pure_reference(
            alg, pure_references[alg.name], count=count, seed=seed,
        )
    binding = bindings.get(alg.name)
    if binding is None:
        return []
    cat = alg.category or ""
    if cat == "checksum":
        return fuzz_checksum_vectors(dll, alg, binding, count=count, seed=seed)
    if cat == "hash":
        return fuzz_hash_vectors(dll, alg, binding, count=count, seed=seed)
    # TODO (Phase 3): cipher (encrypt-then-decrypt roundtrip),
    #                 compression (compress-then-decompress)
    return []


# ---------------------------------------------------------------------------
# Zlib-specific binding library — pre-built for the zlib subject.
# Other subjects will provide their own binding files.
# ---------------------------------------------------------------------------

def _adler32_adapter(fn, data: bytes) -> int:
    buf = (ctypes.c_ubyte * len(data))(*data) if data else ctypes.POINTER(ctypes.c_ubyte)()
    return int(fn(1, buf, len(data)))


def _crc32_adapter(fn, data: bytes) -> int:
    buf = (ctypes.c_ubyte * len(data))(*data) if data else ctypes.POINTER(ctypes.c_ubyte)()
    return int(fn(0, buf, len(data)))


def _crc32_combine_gen64_adapter(fn, data: bytes) -> int:
    """crc32_combine_gen64(len2): len2 derived from input bytes as u64."""
    padded = bytes(data[:8].ljust(8, b"\x00"))
    len2 = int.from_bytes(padded, "little")
    return int(fn(len2))


def _crc32_combine_op_adapter(fn, data: bytes) -> int:
    """crc32_combine_op(crc1, crc2, op): three u32 values packed from input."""
    padded = bytes(data[:12].ljust(12, b"\x00"))
    crc1 = int.from_bytes(padded[0:4], "little")
    crc2 = int.from_bytes(padded[4:8], "little")
    op = int.from_bytes(padded[8:12], "little")
    return int(fn(crc1, crc2, op))


ZLIB_BINDINGS: dict[str, CFunctionBinding] = {
    "adler32": CFunctionBinding(
        c_name="adler32",
        restype=ctypes.c_ulong,
        argtypes=(ctypes.c_ulong, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint),
        adapter=_adler32_adapter,
    ),
    "adler32_z": CFunctionBinding(
        c_name="adler32_z",
        restype=ctypes.c_ulong,
        argtypes=(ctypes.c_ulong, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t),
        adapter=_adler32_adapter,
    ),
    "crc32": CFunctionBinding(
        c_name="crc32",
        restype=ctypes.c_ulong,
        argtypes=(ctypes.c_ulong, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_uint),
        adapter=_crc32_adapter,
    ),
    "crc32_z": CFunctionBinding(
        c_name="crc32_z",
        restype=ctypes.c_ulong,
        argtypes=(ctypes.c_ulong, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t),
        adapter=_crc32_adapter,
    ),
    "crc32_combine_gen64": CFunctionBinding(
        c_name="crc32_combine_gen64",
        restype=ctypes.c_ulong,
        argtypes=(ctypes.c_longlong,),
        adapter=_crc32_combine_gen64_adapter,
    ),
    "crc32_combine_op": CFunctionBinding(
        c_name="crc32_combine_op",
        restype=ctypes.c_ulong,
        argtypes=(ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong),
        adapter=_crc32_combine_op_adapter,
    ),
}


# ---------------------------------------------------------------------------
# Pure-Python references for functions zlib keeps static/local in its C
# source (not exported from zlib1.dll). These reference impls must
# faithfully mirror the C semantics — they ARE the correctness oracle
# for these functions.
# ---------------------------------------------------------------------------

def _byte_swap_pure_ref(data: bytes) -> int:
    """byte_swap reverses byte order of a 64-bit integer.

    The C body: s[7] | s[6]<<8 | s[5]<<16 | ... | s[0]<<56 for little-endian
    input interpreted as big-endian, or equivalently u64::swap_bytes.
    """
    padded = bytes(data[:8].ljust(8, b"\x00"))
    word = int.from_bytes(padded, "little")
    return int.from_bytes(word.to_bytes(8, "little"), "big")


def _bi_reverse_pure_ref(data: bytes) -> int:
    """bi_reverse reverses the low `len` bits of `code`.

    Test input: first 4 bytes are code (u32), 5th byte is len.
    """
    padded = bytes(data[:5].ljust(5, b"\x00"))
    code = int.from_bytes(padded[0:4], "little") & 0xFFFFFFFF
    length = padded[4] & 0x1F  # clamp to [0, 31]
    res = 0
    for _ in range(length):
        res = ((res << 1) | (code & 1)) & 0xFFFFFFFF
        code >>= 1
    return res


def _multmodp_pure_ref(data: bytes) -> int:
    """multmodp(a, b) — faithful port of zlib's crc32.c implementation.

    m = 0x80000000
    p = 0
    while true:
        if a & m:
            p ^= b
            if (a & (m-1)) == 0: break
        m >>= 1
        b = (b >> 1) ^ POLY if b & 1 else b >> 1

    POLY = 0xEDB88320 (reflected IEEE). Requires a != 0 (C spec).
    """
    padded = bytes(data[:8].ljust(8, b"\x00"))
    a = int.from_bytes(padded[0:4], "little")
    b = int.from_bytes(padded[4:8], "little")
    if a == 0:
        return 0  # zlib says undefined, be defensive
    POLY = 0xEDB88320
    m = 1 << 31
    p = 0
    while True:
        if a & m:
            p ^= b
            if (a & (m - 1)) == 0:
                break
        m >>= 1
        if b & 1:
            b = (b >> 1) ^ POLY
        else:
            b >>= 1
    return p


def _x2nmodp_pure_ref(data: bytes) -> int:
    """x2nmodp(n, k) — GF(2)[x]/p(x) exponent combinator from zlib's crc32.c.

    Computes x^(n * 2^(k+3)) mod p(x) in reflected IEEE-802.3 form, used
    to combine CRC-32 checksums of two byte streams without rescanning.

    Input layout: first 8 bytes = n (u64 LE), next 4 bytes = k (u32 LE).
    """
    padded = bytes(data[:12].ljust(12, b"\x00"))
    n = int.from_bytes(padded[0:8], "little")
    k = int.from_bytes(padded[8:12], "little")
    POLY = 0xEDB88320

    def _mm(a: int, b: int) -> int:
        if a == 0:
            return 0
        m, p = 1 << 31, 0
        while True:
            if a & m:
                p ^= b
                if (a & (m - 1)) == 0:
                    break
            m >>= 1
            if b & 1:
                b = (b >> 1) ^ POLY
            else:
                b >>= 1
        return p & 0xFFFFFFFF

    t = [0] * 32
    t[0] = 0x80000000
    for i in range(1, 32):
        t[i] = _mm(t[i - 1], t[i - 1])

    p = 0x80000000
    idx = 3
    while k:
        if k & 1:
            p = _mm(t[idx & 31], p)
        idx = (idx + 1) & 31
        k >>= 1
    while n:
        if n & 1:
            p = _mm(t[idx & 31], p)
        idx = (idx + 1) & 31
        n >>= 1
    return p


ZLIB_PURE_REFERENCES: dict[str, callable] = {
    "byte_swap": _byte_swap_pure_ref,
    "bi_reverse": _bi_reverse_pure_ref,
    "multmodp": _multmodp_pure_ref,
    "x2nmodp": _x2nmodp_pure_ref,
}


# ---------------------------------------------------------------------------
# Byte-buffer transformation fuzzing.
#
# Functions like zmemcpy / zmemcmp / zmemzero don't fit the scalar-input,
# scalar-output contract of fuzz_pure_reference. They take mutable slice
# args and either mutate them in place (void return) or return an int
# derived from comparing two slices.
#
# The reference callable returns a dict describing how the Rust test should
# be rendered:
#   {
#       "inputs": {<param_name>: <rust_literal_str>, ...},
#       "expected_output": <rust_literal_str>,   # what `got`/buffer equals
#       "assert_kind": "scalar" | "buffer_postcondition",
#       "mut_buffer": <param_name>,              # only for buffer_postcondition
#       "n_param": <param_name>,                 # only for buffer_postcondition
#   }
#
# The test generator reads the `tolerance="byte_transform"` marker and
# dispatches to _emit_byte_transform_test, which knows how to turn that
# description into a real #[test].
# ---------------------------------------------------------------------------


def _zmemcpy_pure_ref(src: bytes, n: int) -> bytes:
    """zmemcpy(dst, src, n): copies n bytes from src into dst. Post-state
    of dst[..n] equals src[..n]. Bytes beyond n must remain at their prior
    value; the test uses a zero-init dst so the tail stays 0.
    """
    return bytes(src[:n])


def _zmemcmp_pure_ref(s1: bytes, s2: bytes, n: int) -> int:
    """Signed difference at first differing byte, 0 if all match."""
    for i in range(n):
        if s1[i] != s2[i]:
            return int(s1[i]) - int(s2[i])
    return 0


def _byte_transform_inputs(rng: random.Random, count: int) -> list[dict]:
    """Build the raw fuzz-parameter tuples for each zmem* call.

    We produce a shared schedule (src, s2, n, pad) so all three functions
    exercise matching edge cases (n=0, aligned, misaligned, tail-bytes).
    """
    out: list[dict] = []
    # Edge n values
    edge_ns = [0, 1, 7, 16, 31]
    for n in edge_ns:
        src = bytes(rng.randint(0, 255) for _ in range(max(n, 1)))
        s2 = bytes(rng.randint(0, 255) for _ in range(max(n, 1)))
        out.append({"src": src, "s2": s2, "n": n})
    # Equal-buffer cases for zmemcmp (ensure the 0-return branch fires)
    for _ in range(3):
        n = rng.randint(0, 24)
        buf = bytes(rng.randint(0, 255) for _ in range(max(n, 1)))
        out.append({"src": buf, "s2": buf, "n": n})
    # Random cases
    while len(out) < count:
        n = rng.randint(0, 32)
        src = bytes(rng.randint(0, 255) for _ in range(max(n + 4, 1)))
        s2 = bytes(rng.randint(0, 255) for _ in range(max(n + 4, 1)))
        out.append({"src": src[:max(n, 1)], "s2": s2[:max(n, 1)], "n": n})
    return out[:count]


def _fuzz_zmemcpy(params: dict) -> dict:
    src = params["src"]
    n = params["n"]
    # Pad src so src.len() >= n always.
    src_padded = src.ljust(n, b"\x00")
    expected = _zmemcpy_pure_ref(src_padded, n)
    return {
        "inputs": {
            # `dst` is rendered as a `vec![0u8; N]` expression. The emit
            # helper turns this into `let mut dst = vec![0u8; N];`.
            "dst": f"__VECZERO__{n}",
            "src": _bytes_to_rust_literal(bytes(src_padded)),
            "n": f"{n}usize",
        },
        "expected_output": _bytes_to_rust_literal(bytes(expected)),
        "assert_kind": "buffer_postcondition",
        "mut_buffer": "dst",
        "n_param": "n",
    }


def _fuzz_zmemcmp(params: dict) -> dict:
    s1 = params["src"]
    s2 = params["s2"]
    n = params["n"]
    s1p = s1.ljust(n, b"\x00")
    s2p = s2.ljust(n, b"\x00")
    got = _zmemcmp_pure_ref(s1p, s2p, n)
    # Normalize to signed i32; zlib's C returns first-byte signed diff, not
    # clamped to {-1,0,1}. Mirror that exactly.
    return {
        "inputs": {
            "s1": _bytes_to_rust_literal(bytes(s1p)),
            "s2": _bytes_to_rust_literal(bytes(s2p)),
            "n": f"{n}usize",
        },
        "expected_output": f"({got}i32)" if got < 0 else f"{got}i32",
        "assert_kind": "scalar",
    }


def _fuzz_zmemzero(params: dict) -> dict:
    n = params["n"]
    expected = bytes(n)  # len = n, all 0
    return {
        "inputs": {
            "buffer": f"__VECFILL_FF__{n}",
            "len": f"{n}usize",
        },
        "expected_output": _bytes_to_rust_literal(expected),
        "assert_kind": "buffer_postcondition",
        "mut_buffer": "buffer",
        "n_param": "len",
    }


# Registry of byte-buffer-transformation fn -> ref callable.
ZLIB_BYTE_TRANSFORMS: dict[str, callable] = {
    "zmemcpy": _fuzz_zmemcpy,
    "zmemcmp": _fuzz_zmemcmp,
    "zmemzero": _fuzz_zmemzero,
}


def fuzz_byte_transform(
    alg: AlgorithmSpec,
    reference,
    *,
    count: int = 12,
    seed: int = _FUZZ_SEED,
) -> list[SpecTestVector]:
    """Generate vectors for byte-buffer-transformation functions.

    The `reference` callable receives a dict of raw fuzz params and returns
    a descriptor (see module docstring) with `inputs` already rendered as
    Rust literals plus `expected_output`, `assert_kind`, and optional
    `mut_buffer`/`n_param` keys. The assertion kind is threaded through the
    SpecTestVector via a magic prefix so the test generator can route to
    the right emit function without schema changes.
    """
    rng = _rng(seed)
    raw_params = _byte_transform_inputs(rng, count)
    vectors: list[SpecTestVector] = []
    for i, params in enumerate(raw_params):
        desc = reference(params)
        # Encode assert_kind + mut_buffer + n_param into the tolerance
        # field. Format: "byte_transform|<kind>|<mut_buffer>|<n_param>".
        # Absent fields use the empty string.
        kind = desc.get("assert_kind", "scalar")
        mut_buf = desc.get("mut_buffer", "")
        n_param = desc.get("n_param", "")
        tolerance = f"byte_transform|{kind}|{mut_buf}|{n_param}"
        vectors.append(SpecTestVector(
            description=f"fuzz_byte_xform_{i}_n{params.get('n')}",
            source=f"pure Python reference: {alg.name}",
            inputs=desc["inputs"],
            expected_output=desc["expected_output"],
            tolerance=tolerance,
        ))
    return vectors


def load_zlib_dll(dll_path: Path) -> ctypes.CDLL:
    """Load the zlib shared library from the given path."""
    return ctypes.CDLL(str(dll_path))
