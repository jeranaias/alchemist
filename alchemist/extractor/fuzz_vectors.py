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
    return an integer checksum value.
    """
    fn = binding.load(dll)
    rng = _rng(seed)
    inputs = _gen_byte_inputs(rng, count)
    vectors: list[SpecTestVector] = []
    for data in inputs:
        output = binding.adapter(fn, data)
        # Emit as a Rust byte-slice literal so the test generator can
        # splice it directly with no type conversion.
        rust_literal = _bytes_to_rust_literal(data)
        vectors.append(SpecTestVector(
            description=f"fuzz_input_len_{len(data)}",
            source=f"C reference: {binding.c_name}",
            inputs={_primary_input_name(alg): rust_literal},
            expected_output=f"0x{output:08x}",
            tolerance="exact",
        ))
    return vectors


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
    define the reference as a Python callable `fn(input_bytes) -> int`
    that faithfully mirrors the C semantics. This still grounds the
    test in a canonical source — just not the DLL.
    """
    rng = _rng(seed)
    inputs = _gen_byte_inputs(rng, count)
    vectors: list[SpecTestVector] = []
    primary = _primary_input_name(alg)
    for data in inputs:
        output = reference(data)
        if isinstance(output, (bytes, bytearray)):
            expected = _bytes_to_rust_literal(bytes(output))
        else:
            expected = f"0x{int(output):08x}"
        vectors.append(SpecTestVector(
            description=f"fuzz_input_len_{len(data)}",
            source=f"pure Python reference: {alg.name}",
            inputs={primary: _bytes_to_rust_literal(bytes(data))},
            expected_output=expected,
            tolerance="exact",
        ))
    return vectors


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
    """multmodp(a, b) in GF(2^32) with the zlib CRC polynomial.

    Mirrors the C implementation: multiply-and-reduce using the reflected
    CRC-32 polynomial x^32 = x^26 + x^23 + x^22 + x^16 + x^12 + x^11 +
    x^10 + x^8 + x^7 + x^5 + x^4 + x^2 + x + 1 (0xEDB88320 reflected).
    """
    padded = bytes(data[:8].ljust(8, b"\x00"))
    a = int.from_bytes(padded[0:4], "little")
    b = int.from_bytes(padded[4:8], "little")
    # multmodp per zlib's crc32.c: p = 0; for each bit of b, p ^= a shifted appropriately
    p = 0
    POLY = 0xEDB88320
    for i in range(32):
        if b & 0x80000000:
            p ^= a
        b = (b << 1) & 0xFFFFFFFF
        # Multiply a by x modulo the CRC polynomial
        if a & 1:
            a = (a >> 1) ^ POLY
        else:
            a >>= 1
    return p


ZLIB_PURE_REFERENCES: dict[str, callable] = {
    "byte_swap": _byte_swap_pure_ref,
    "bi_reverse": _bi_reverse_pure_ref,
    # multmodp disabled until we verify the reference matches C exactly —
    # the reflection direction is tricky to get right.
    # "multmodp": _multmodp_pure_ref,
}


def load_zlib_dll(dll_path: Path) -> ctypes.CDLL:
    """Load the zlib shared library from the given path."""
    return ctypes.CDLL(str(dll_path))
