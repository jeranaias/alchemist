"""Generate proptest harnesses that differentially test Rust vs a C reference.

For each algorithm Alchemist extracts, this emits:

  - Standards-based fixed test vectors (from alchemist.standards).
  - A category-specific proptest block:
      * checksum / hash  → byte-exact output comparison
      * cipher           → encrypt(C) → decrypt(Rust) == plaintext, CAVP vectors
      * compression      → roundtrip equivalence, C↔Rust interop
      * filter / control → ULP-bounded numeric equality
      * data_structure / utility → smoke calls only

The result is written to a test crate's `tests/differential.rs`. The FFI
crate (see auto_ffi.py) is expected to already provide the C wrappers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent, indent

from alchemist.standards import TestVector, lookup_test_vectors


# ---------------------------------------------------------------------------
# Harness config
# ---------------------------------------------------------------------------

VALID_CATEGORIES = {
    "checksum", "hash", "cipher", "compression", "decompression",
    "filter", "controller", "transform", "data_structure",
    "protocol", "scheduler", "utility", "other",
}


@dataclass
class AlgorithmHarness:
    """One algorithm to differentially test.

    All expressions are snippets that appear inside a test function body.
    `rust_call` is a Rust expression that takes `&input` and returns the output.
    `c_call` is the corresponding C-wrapper invocation.
    For ciphers and roundtrips, extra call points (decrypt/decompress) are
    configured via the optional fields.
    """
    algorithm: str
    category: str
    rust_call: str       # e.g. "zlib_checksum::Adler32::compute(1, &input, input.len())"
    c_call: str          # e.g. "c_ref::c_adler32(&input)"
    # For ciphers / round-tripping pairs:
    rust_decrypt_call: str | None = None    # takes `&ct`, `&key` → plaintext
    c_decrypt_call: str | None = None
    rust_decompress_call: str | None = None  # takes `&compressed`, `orig_len` → Vec<u8>
    c_decompress_call: str | None = None
    # For floating-point tolerance
    ulp_tolerance: int = 0
    # Override input strategy — defaults per category
    input_strategy: str | None = None
    # Additional imports required in the emitted harness
    extra_imports: list[str] = field(default_factory=list)
    # Upper proptest case count
    cases: int = 1024


DEFAULT_IMPORTS = [
    "use proptest::prelude::*;",
]


# ---------------------------------------------------------------------------
# Per-category block templates
# ---------------------------------------------------------------------------

def _fixed_vectors_block(h: AlgorithmHarness) -> str:
    """Emit #[test] fns for every standards vector (exact match).

    Only emits if standards catalog has entries for this algorithm.
    """
    vectors = lookup_test_vectors(h.algorithm)
    if not vectors:
        return ""
    body = []
    body.append(f"// Fixed test vectors for {h.algorithm} from alchemist.standards catalog.")
    for i, v in enumerate(vectors):
        if h.category in ("checksum", "hash"):
            body.append(_fixed_digest_test(h, v, i))
        elif h.category == "cipher":
            body.append(_fixed_cipher_test(h, v, i))
        elif h.category in ("compression", "decompression"):
            body.append(_fixed_roundtrip_test(h, v, i))
        else:
            body.append(_fixed_generic_test(h, v, i))
    return "\n\n".join(b for b in body if b)


def _fixed_digest_test(h: AlgorithmHarness, v: TestVector, idx: int) -> str:
    fn_name = f"fixed_{h.algorithm}_{_slug(v.name) or idx}"
    input_lit = v.as_rust_literal("input")
    expected = v.expected_hex
    # For checksum: expected is 32-bit. For hash: expected is arbitrary digest bytes.
    if h.category == "checksum":
        return dedent(f"""\
            #[test]
            fn {fn_name}() {{
                let input: &[u8] = {input_lit};
                let rust_out = {h.rust_call};
                assert_eq!(format!("{{:08x}}", rust_out), "{expected}",
                    "standards vector {v.name!r} failed");
            }}
        """).rstrip()
    # hash: hex match
    return dedent(f"""\
        #[test]
        fn {fn_name}() {{
            let input: &[u8] = {input_lit};
            let digest = {h.rust_call};
            let hex: String = digest.iter().map(|b| format!("{{:02x}}", b)).collect();
            assert_eq!(hex, "{expected}", "standards vector {v.name!r} failed");
        }}
    """).rstrip()


def _fixed_cipher_test(h: AlgorithmHarness, v: TestVector, idx: int) -> str:
    fn_name = f"fixed_{h.algorithm}_{_slug(v.name) or idx}"
    input_lit = v.as_rust_literal("input")
    key_lit = v.as_rust_literal("key")
    expected_lit = v.as_rust_literal("expected")
    return dedent(f"""\
        #[test]
        fn {fn_name}() {{
            let input: &[u8] = {input_lit};
            let key: &[u8] = {key_lit};
            let expected: &[u8] = {expected_lit};
            let ct = {h.rust_call};
            assert_eq!(ct.as_slice(), expected, "cipher vector {v.name!r} encrypt failed");
        }}
    """).rstrip()


def _fixed_roundtrip_test(h: AlgorithmHarness, v: TestVector, idx: int) -> str:
    fn_name = f"fixed_{h.algorithm}_{_slug(v.name) or idx}"
    input_lit = v.as_rust_literal("input")
    # For compression: we check decompress-compatibility with known-good blob
    expected_lit = v.as_rust_literal("expected")
    decomp = h.rust_decompress_call or "/* no rust_decompress_call configured */"
    return dedent(f"""\
        #[test]
        fn {fn_name}() {{
            let original: &[u8] = {input_lit};
            let reference_compressed: &[u8] = {expected_lit};
            let decompressed = {decomp};
            assert_eq!(decompressed.as_slice(), original,
                "decompress of standards blob {v.name!r} did not recover input");
        }}
    """).rstrip()


def _fixed_generic_test(h: AlgorithmHarness, v: TestVector, idx: int) -> str:
    fn_name = f"fixed_{h.algorithm}_{_slug(v.name) or idx}"
    input_lit = v.as_rust_literal("input")
    return dedent(f"""\
        #[test]
        fn {fn_name}() {{
            let input: &[u8] = {input_lit};
            let _ = {h.rust_call};
        }}
    """).rstrip()


# ----- Proptest blocks -----

def _proptest_block(h: AlgorithmHarness) -> str:
    if h.category in ("checksum", "hash"):
        return _proptest_digest_block(h)
    if h.category == "cipher":
        return _proptest_cipher_block(h)
    if h.category in ("compression", "decompression"):
        return _proptest_compression_block(h)
    if h.category in ("filter", "controller"):
        return _proptest_float_block(h)
    # data_structure / utility → smoke
    return _proptest_smoke_block(h)


def _proptest_digest_block(h: AlgorithmHarness) -> str:
    strategy = h.input_strategy or "prop::collection::vec(any::<u8>(), 0..8192)"
    return dedent(f"""\
        proptest! {{
            #![proptest_config(ProptestConfig::with_cases({h.cases}))]

            #[test]
            fn {h.algorithm}_matches_c_reference(input in {strategy}) {{
                let rust_out = {h.rust_call};
                let c_out = {h.c_call};
                prop_assert_eq!(rust_out, c_out);
            }}
        }}
    """).rstrip()


def _proptest_cipher_block(h: AlgorithmHarness) -> str:
    if not h.rust_decrypt_call or not h.c_decrypt_call:
        # Without decrypt, at least ensure forward pass matches C ciphertext
        return dedent(f"""\
            proptest! {{
                #![proptest_config(ProptestConfig::with_cases({h.cases}))]

                #[test]
                fn {h.algorithm}_encrypt_matches_c(
                    input in prop::collection::vec(any::<u8>(), 16..16),
                    key in prop::collection::vec(any::<u8>(), 16..16),
                ) {{
                    let rust_ct = {h.rust_call};
                    let c_ct = {h.c_call};
                    prop_assert_eq!(rust_ct.as_slice(), c_ct.as_slice());
                }}
            }}
        """).rstrip()
    return dedent(f"""\
        proptest! {{
            #![proptest_config(ProptestConfig::with_cases({h.cases}))]

            #[test]
            fn {h.algorithm}_roundtrip(
                input in prop::collection::vec(any::<u8>(), 16..16),
                key in prop::collection::vec(any::<u8>(), 16..16),
            ) {{
                // Rust encrypt → Rust decrypt
                let ct = {h.rust_call};
                let pt = {h.rust_decrypt_call};
                prop_assert_eq!(pt.as_slice(), input.as_slice());
            }}

            #[test]
            fn {h.algorithm}_interop_rust_encrypt_c_decrypt(
                input in prop::collection::vec(any::<u8>(), 16..16),
                key in prop::collection::vec(any::<u8>(), 16..16),
            ) {{
                let ct = {h.rust_call};
                let pt = {h.c_decrypt_call};
                prop_assert_eq!(pt.as_slice(), input.as_slice());
            }}
        }}
    """).rstrip()


def _proptest_compression_block(h: AlgorithmHarness) -> str:
    strategy = h.input_strategy or "prop::collection::vec(any::<u8>(), 0..8192)"
    # Needs at least one decompress side to verify roundtrip
    rust_decomp = h.rust_decompress_call or ""
    c_decomp = h.c_decompress_call or ""
    blocks = []
    # 1. Rust roundtrip
    if rust_decomp:
        blocks.append(dedent(f"""\
            #[test]
            fn {h.algorithm}_rust_roundtrip(input in {strategy}) {{
                let compressed = {h.rust_call};
                let decompressed = {rust_decomp};
                prop_assert_eq!(decompressed.as_slice(), input.as_slice());
            }}
        """).rstrip())
    # 2. Rust compress → C decompress
    if c_decomp:
        blocks.append(dedent(f"""\
            #[test]
            fn {h.algorithm}_rust_compress_c_decompress(input in {strategy}) {{
                let compressed = {h.rust_call};
                let decompressed = {c_decomp};
                prop_assert_eq!(decompressed.as_slice(), input.as_slice());
            }}
        """).rstrip())
    # 3. C compress → Rust decompress (requires both c_call and rust_decompress_call)
    if rust_decomp and h.c_call:
        blocks.append(dedent(f"""\
            #[test]
            fn {h.algorithm}_c_compress_rust_decompress(input in {strategy}) {{
                let compressed = {h.c_call};
                let decompressed = {rust_decomp};
                prop_assert_eq!(decompressed.as_slice(), input.as_slice());
            }}
        """).rstrip())
    if not blocks:
        return ""
    joined = "\n\n".join(indent(b, "    ") for b in blocks)
    return dedent(f"""\
        proptest! {{
            #![proptest_config(ProptestConfig::with_cases({h.cases}))]

        """).rstrip() + "\n" + joined + "\n}"


def _proptest_float_block(h: AlgorithmHarness) -> str:
    strategy = h.input_strategy or "prop::collection::vec(any::<f64>().prop_filter(\"finite\", |f| f.is_finite()), 1..256)"
    ulp = h.ulp_tolerance
    return dedent(f"""\
        fn within_ulps(a: f64, b: f64, ulps: i64) -> bool {{
            if a == b {{ return true; }}
            if a.is_nan() || b.is_nan() {{ return false; }}
            let ua = a.to_bits() as i64;
            let ub = b.to_bits() as i64;
            (ua - ub).abs() <= ulps
        }}

        proptest! {{
            #![proptest_config(ProptestConfig::with_cases({h.cases}))]

            #[test]
            fn {h.algorithm}_within_ulp_tolerance(input in {strategy}) {{
                let rust_out = {h.rust_call};
                let c_out = {h.c_call};
                prop_assert!(within_ulps(rust_out, c_out, {ulp}),
                    "rust={{}}, c={{}}", rust_out, c_out);
            }}
        }}
    """).rstrip()


def _proptest_smoke_block(h: AlgorithmHarness) -> str:
    strategy = h.input_strategy or "prop::collection::vec(any::<u8>(), 0..256)"
    return dedent(f"""\
        proptest! {{
            #![proptest_config(ProptestConfig::with_cases({h.cases}))]

            #[test]
            fn {h.algorithm}_smoke(input in {strategy}) {{
                let _ = {h.rust_call};
            }}
        }}
    """).rstrip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug(s: str) -> str:
    out = []
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_/":
            out.append("_")
    slug = "".join(out).strip("_")
    # Collapse multiple underscores
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug


def emit_differential_test(
    harnesses: list[AlgorithmHarness],
    *,
    extra_imports: list[str] | None = None,
    module_doc: str | None = None,
) -> str:
    """Emit a full `tests/differential.rs` file for a list of harnesses."""
    imports: list[str] = list(DEFAULT_IMPORTS)
    for h in harnesses:
        imports.extend(h.extra_imports)
    if extra_imports:
        imports.extend(extra_imports)
    # Dedup while preserving order
    seen: set[str] = set()
    unique_imports: list[str] = []
    for imp in imports:
        if imp not in seen:
            seen.add(imp)
            unique_imports.append(imp)

    lines: list[str] = []
    if module_doc:
        for l in module_doc.splitlines():
            lines.append(f"//! {l}")
        lines.append("")
    lines.extend(unique_imports)
    lines.append("")

    for h in harnesses:
        if h.category not in VALID_CATEGORIES:
            raise ValueError(f"Unknown category for {h.algorithm}: {h.category}")
        lines.append(f"// === {h.algorithm} ({h.category}) ===")
        block = _fixed_vectors_block(h)
        if block:
            lines.append(block)
            lines.append("")
        lines.append(_proptest_block(h))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_differential_test(
    harnesses: list[AlgorithmHarness],
    output_path: Path,
    *,
    module_doc: str | None = None,
) -> Path:
    """Emit and write a differential test file; returns the path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    src = emit_differential_test(harnesses, module_doc=module_doc)
    output_path.write_text(src, encoding="utf-8")
    return output_path
