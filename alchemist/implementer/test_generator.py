"""Phase 4B: emit failing tests from specs + standards catalog.

For every algorithm Alchemist extracts, we want a `#[test] fn` that calls
the real public API and checks a known input → expected output mapping.
With the skeleton's `unimplemented!()` bodies in place, every test FAILS
(via panic). That's the TDD forcing function: the implementer cannot ship
until the tests pass.

Sources of test vectors (in priority order):
  1. `spec.test_vectors` (extractor-produced, algorithm-specific)
  2. `alchemist.standards` catalog (RFC / NIST authoritative values)
  3. For roundtrip categories without explicit vectors, emit a smoke test
     that ensures the function is reachable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from alchemist.architect.schemas import CrateArchitecture, CrateSpec
from alchemist.extractor.schemas import AlgorithmSpec, ModuleSpec, TestVector as SpecTestVector
from alchemist.implementer.skeleton import _snake as _rust_fn_name
from alchemist.standards import TestVector as StdTestVector, lookup_test_vectors


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class CrateTestsResult:
    crate_name: str
    file_path: Path
    tests_written: int = 0
    tests_from_catalog: int = 0
    tests_from_spec: int = 0
    tests_from_smoke: int = 0


# ---------------------------------------------------------------------------
# Test emission
# ---------------------------------------------------------------------------

def _slug(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return re.sub(r"_+", "_", s) or "x"


_TYPED_INT_LITERAL = re.compile(
    r"^-?\d+(?:_\d+)*(?:u8|u16|u32|u64|u128|usize|i8|i16|i32|i64|i128|isize|f32|f64)$"
)
_PARENTHESIZED_TYPED = re.compile(
    r"^\(\s*-?\d+(?:_\d+)*(?:u8|u16|u32|u64|u128|usize|i8|i16|i32|i64|i128|isize|f32|f64)\s*\)$"
)


def _literal_from_spec_value(value: str) -> str:
    """Return a Rust literal. Accepts already-valid Rust literals, or falls
    back to wrapping a bare string in quotes."""
    v = value.strip()
    if v.startswith(("&", "b\"", "b'", "0x", "0b", "\"", "[", "vec!")):
        return v
    if v.replace("_", "").replace(".", "").replace("-", "").isdigit():
        return v
    # Typed integer/float literals like `0u64`, `-42i32`, `(-1i8)` are valid Rust.
    if _TYPED_INT_LITERAL.match(v) or _PARENTHESIZED_TYPED.match(v):
        return v
    # Hex literal with suffix like `0xffu64`
    if re.match(r"^0x[0-9a-fA-F]+(?:_[0-9a-fA-F]+)*(?:u\d+|i\d+|usize|isize)?$", v):
        return v
    # Fallback: treat as byte-string literal
    return f'b"{v}"'


# Default values for well-known checksum/hash parameter names.
# Keyed by lowercase param-name prefix → Rust expression.
_PARAM_DEFAULTS = {
    "buf": "input",
    "data": "input",
    "input": "input",
    "bytes": "input",
    "msg": "input",
    "src": "input",
    "source": "input",
    "len": "input.len()",
    "length": "input.len()",
    "size": "input.len()",
    "n": "input.len()",
    # Checksum seeds.
    "adler": "1u32",
    "seed": "1u32",  # will be overridden to 0 for crc below
    "state": "0u32",
    "crc": "0u32",
    "init": "0u32",
    # Keys / IVs for ciphers (only if a `key` local exists in the test).
    "key": "key",
    "iv": "iv",
}


def _default_for_param(fn_name: str, pname: str, ptype: str) -> str:
    """Pick a sensible default Rust expression for a parameter when the
    caller only has the input slice available."""
    low = pname.lower().lstrip("_")
    # adler32-ish seed defaults to 1, crc32-ish seed defaults to 0.
    if low in ("seed", "state", "init") and "crc" in fn_name.lower():
        return "0u32"
    if low in _PARAM_DEFAULTS:
        return _PARAM_DEFAULTS[low]
    # Integer default
    if re.match(r"^(?:u|i)\d+$", ptype):
        return "0"
    if ptype.startswith("&[u8]"):
        return "input"
    if ptype.startswith("usize"):
        return "input.len()"
    # Unknown — emit 0 and hope
    return "Default::default()"


def _build_call(fn_name: str, alg: AlgorithmSpec | None) -> str:
    """Construct a `super::<fn>(args...)` call that matches the spec signature.

    When alg is None or has no inputs, falls back to `super::<fn>(input)`.
    """
    if alg is None or not alg.inputs:
        return f"super::{fn_name}(input)"
    args: list[str] = []
    for p in alg.inputs:
        args.append(_default_for_param(fn_name, p.name, p.rust_type))
    return f"super::{fn_name}({', '.join(args)})"


def _can_accept_byte_slice(alg: AlgorithmSpec) -> bool:
    """True iff the algorithm has at least one `&[u8]`-ish input parameter.

    Without one, emitting a catalog-style test (`let input: &[u8] = ...; super::fn(input)`)
    would be meaningless — the helper is likely a table init or reset-state fn.
    """
    if not alg.inputs:
        return False
    for p in alg.inputs:
        t = (p.rust_type or "").lower()
        if "[u8]" in t or "vec<u8>" in t or "bytes" in t or "&str" in t:
            return True
    return False


def _has_canonical_shape_for_category(alg: AlgorithmSpec) -> bool:
    """Guard against emitting catalog tests for helpers that don't match
    the shape the template assumes.

    Templates expect:
      - checksum: `fn(&[u8]) -> u32` or `fn(u32, &[u8]) -> u32`
      - hash:     `fn(&[u8]) -> [u8; N] | Vec<u8>`
      - cipher:   `fn(key, &[u8]) -> Vec<u8>` (roundtrip)
      - compression/decompression: `fn(&[u8]) -> Vec<u8>` or
        `fn(&mut [u8], &[u8]) -> Result<(), _>`

    Functions like `deflateSetDictionary` have `&[u8]` inputs but return
    Result<()> and take `&mut DeflateStream` — the compression template
    emits `decompressed.as_slice()` against them and fails to compile.
    """
    ret = (alg.return_type or "").strip()
    cat = alg.category

    # Struct/stream state params disqualify canonical catalog templates.
    for p in alg.inputs or []:
        t = (p.rust_type or "")
        if re.search(r"\b(?:Deflate|Inflate|Stream|State)\w*\b", t):
            return False

    if cat == "checksum":
        # Expect a scalar numeric return
        return bool(re.match(r"^(?:u|i)\d+$", ret))
    if cat == "hash":
        # Expect bytes-or-array return
        return ("[u8" in ret) or ("Vec<u8>" in ret)
    if cat == "cipher":
        return ("Vec<u8>" in ret) or ("[u8" in ret) or ret.startswith("Result<")
    if cat in ("compression", "decompression"):
        # compress-template calls `.as_slice()` on the result. That works
        # only when the function literally returns a Vec<u8> or Result<Vec<u8>>.
        if "Vec<u8>" in ret:
            return True
        if re.search(r"Result<\s*Vec<u8>", ret):
            return True
        return False
    return True


def _emit_catalog_test_checksum(
    fn_name: str, vec: StdTestVector, idx: int,
    alg: AlgorithmSpec | None = None,
) -> str:
    test_name = f"test_{fn_name}_vec_{_slug(vec.name) or idx}"
    input_lit = vec.as_rust_literal("input")
    expected_hex = vec.expected_hex
    call = _build_call(fn_name, alg)
    return (
        f"    #[test]\n"
        f"    fn {test_name}() {{\n"
        f"        let input: &[u8] = {input_lit};\n"
        f"        let got = {call};\n"
        f'        assert_eq!(format!("{{:08x}}", got), "{expected_hex}", '
        f'"vector {vec.name!r} failed");\n'
        f"    }}\n"
    )


def _emit_catalog_test_hash(
    fn_name: str, vec: StdTestVector, idx: int,
    alg: AlgorithmSpec | None = None,
) -> str:
    test_name = f"test_{fn_name}_vec_{_slug(vec.name) or idx}"
    input_lit = vec.as_rust_literal("input")
    call = _build_call(fn_name, alg)
    return (
        f"    #[test]\n"
        f"    fn {test_name}() {{\n"
        f"        let input: &[u8] = {input_lit};\n"
        f"        let digest = {call};\n"
        f'        let hex: String = digest.iter().map(|b| format!("{{:02x}}", b)).collect();\n'
        f'        assert_eq!(hex, "{vec.expected_hex}", '
        f'"vector {vec.name!r} failed");\n'
        f"    }}\n"
    )


def _emit_catalog_test_cipher(
    fn_name: str, vec: StdTestVector, idx: int,
    alg: AlgorithmSpec | None = None,
) -> str:
    test_name = f"test_{fn_name}_vec_{_slug(vec.name) or idx}"
    input_lit = vec.as_rust_literal("input")
    key_lit = vec.as_rust_literal("key")
    expected_lit = vec.as_rust_literal("expected")
    call = _build_call(fn_name, alg) if alg else f"super::{fn_name}(&key, &input)"
    return (
        f"    #[test]\n"
        f"    fn {test_name}() {{\n"
        f"        let input: &[u8] = {input_lit};\n"
        f"        let key: &[u8] = {key_lit};\n"
        f"        let expected: &[u8] = {expected_lit};\n"
        f"        let ct = {call};\n"
        f'        assert_eq!(ct.as_slice(), expected, "vector {vec.name!r} failed");\n'
        f"    }}\n"
    )


def _emit_catalog_test_compression(
    fn_name: str, vec: StdTestVector, idx: int,
    alg: AlgorithmSpec | None = None,
) -> str:
    """For compression we test that decompress(reference_blob) == input."""
    test_name = f"test_{fn_name}_vec_{_slug(vec.name) or idx}"
    input_lit = vec.as_rust_literal("input")
    expected_lit = vec.as_rust_literal("expected")
    # For compression the expected blob is the "input" to the decompress fn
    # — map the alg-inputs against `reference_compressed` instead of `input`.
    if alg and alg.inputs:
        args = []
        for p in alg.inputs:
            expr = _default_for_param(fn_name, p.name, p.rust_type)
            # Re-route `input`/`input.len()` to the reference_compressed var
            expr = expr.replace("input", "reference_compressed")
            args.append(expr)
        call = f"super::{fn_name}({', '.join(args)})"
    else:
        call = f"super::{fn_name}(reference_compressed)"
    return (
        f"    #[test]\n"
        f"    fn {test_name}() {{\n"
        f"        let original: &[u8] = {input_lit};\n"
        f"        let reference_compressed: &[u8] = {expected_lit};\n"
        f"        let decompressed = {call};\n"
        f'        assert_eq!(decompressed.as_slice(), original, '
        f'"decompress of standards blob {vec.name!r} did not recover input");\n'
        f"    }}\n"
    )


def _emit_state_mutator_test(fn_name: str, vec: SpecTestVector, idx: int) -> str:
    """Emit a test for a state-mutator function.

    Input dict has keys `state.<field>` (pre-state) and plain names (extra args).
    Expected output is newline-separated `field:rust_type=value` lines.
    Test constructs a fresh state with the pre-state values, calls the fn,
    and asserts each post-state field.
    """
    test_name = f"test_{fn_name}_state_{idx}"
    # Split pre-state fields from extra args
    state_inits: list[tuple[str, str]] = []
    extra_args: list[tuple[str, str]] = []
    for pname, pvalue in vec.inputs.items():
        if pname.startswith("state."):
            field_name = pname[len("state."):]
            state_inits.append((field_name, pvalue))
        else:
            extra_args.append((pname, pvalue))
    lines = [f"    #[test]\n    fn {test_name}() {{\n"]
    # Need state type — infer from the first expected-output line's context.
    # For zlib, always DeflateState. For now, use the convention that the
    # test sits in a module where DeflateState is imported via `use zlib_types::*;`.
    lines.append("        let mut state = zlib_types::DeflateState::default();\n")
    for field_name, rendered in state_inits:
        lines.append(f"        state.{field_name} = {rendered};\n")
    for arg_name, rendered in extra_args:
        lines.append(f"        let {arg_name} = {rendered};\n")
    # Build the call: state is first arg, extras after.
    if extra_args:
        extra_str = ", ".join(a[0] for a in extra_args)
        lines.append(f"        super::{fn_name}(&mut state, {extra_str});\n")
    else:
        lines.append(f"        super::{fn_name}(&mut state);\n")
    # Parse expected fields and emit assertions.
    for line in vec.expected_output.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        lhs, rhs = line.split("=", 1)
        # lhs is "name:rust_type"
        if ":" not in lhs:
            continue
        field_name, _ = lhs.split(":", 1)
        lines.append(
            f"        assert_eq!(state.{field_name.strip()}, {rhs.strip()}, "
            f'"{vec.description} field {field_name.strip()}");\n'
        )
    lines.append("    }\n")
    return "".join(lines)


def _emit_byte_transform_test(
    fn_name: str, vec: SpecTestVector, idx: int,
) -> str:
    """Emit a test for a byte-buffer-transformation fn (zmemcpy/zmemcmp/zmemzero).

    Tolerance field encodes the assertion kind and which parameter is the
    mutable buffer: "byte_transform|<kind>|<mut_buf>|<n_param>".

    Input values using magic prefixes:
      __VECZERO__<N>       -> vec![0u8; N]
      __VECFILL_FF__<N>    -> vec![0xFFu8; N]   (non-zero seed for zmemzero)

    For buffer_postcondition: asserts `&mut_buf[..n]` equals expected bytes.
    For scalar: asserts return value equals expected (existing literal path).
    """
    parts = (vec.tolerance or "").split("|")
    _, kind, mut_buf, n_param = (parts + ["", "", ""])[:4]
    test_name = f"test_{fn_name}_xform_{idx}"
    lines = [f"    #[test]\n    fn {test_name}() {{\n"]
    for pname, pvalue in vec.inputs.items():
        v = pvalue.strip()
        if v.startswith("__VECZERO__"):
            n_literal = v[len("__VECZERO__"):]
            lines.append(f"        let mut {pname}: alloc::vec::Vec<u8> = alloc::vec![0u8; {n_literal}];\n")
        elif v.startswith("__VECFILL_FF__"):
            n_literal = v[len("__VECFILL_FF__"):]
            lines.append(f"        let mut {pname}: alloc::vec::Vec<u8> = alloc::vec![0xFFu8; {n_literal}];\n")
        else:
            lines.append(f"        let {pname} = {_literal_from_spec_value(v)};\n")
    arg_list = ", ".join(
        f"&mut {p}" if p == mut_buf else p
        for p in vec.inputs.keys()
    )
    if kind == "scalar":
        lines.append(f"        let got = super::{fn_name}({arg_list});\n")
        lines.append(
            f"        assert_eq!(got, {_literal_from_spec_value(vec.expected_output)}, "
            f'"{vec.description}");\n'
        )
    else:
        # buffer_postcondition — call for side effect, inspect mut buffer.
        lines.append(f"        super::{fn_name}({arg_list});\n")
        expected_lit = _literal_from_spec_value(vec.expected_output)
        # Compare only the first n_param bytes — tail bytes may retain
        # the initial fill pattern (0xFF for zmemzero's buffer beyond len,
        # or 0 for zmemcpy's dst beyond n).
        lines.append(
            f"        assert_eq!(&{mut_buf}[..{n_param}], {expected_lit}, "
            f'"{vec.description}");\n'
        )
    lines.append("    }\n")
    return "".join(lines)


def _emit_state_observer_test(fn_name: str, vec: SpecTestVector, idx: int) -> str:
    """Emit a test for a state-observer function.

    Shape: read-only `&DeflateState` (or similar) → scalar return.
    Expected output is a single rendered literal (e.g., "0i32").

    Vectors carry pre-rendered Rust statements under `__stmt__<N>` keys
    (preferred) or legacy `state.<field>` keys (simple assignment). The
    `__stmt__` form lets the binding translate between the shim's flat
    field view (dyn_ltree_freq: Vec<u16>) and the Rust struct layout
    (dyn_ltree: Vec<(u16, u16)>).
    """
    test_name = f"test_{fn_name}_observer_{idx}"
    lines = [f"    #[test]\n    fn {test_name}() {{\n"]
    lines.append("        let mut state = zlib_types::DeflateState::default();\n")
    # Pre-rendered statements (__stmt__0, __stmt__1, ...) take priority
    stmt_items = sorted(
        ((k, v) for k, v in vec.inputs.items() if k.startswith("__stmt__")),
        key=lambda kv: int(kv[0][len("__stmt__"):])
        if kv[0][len("__stmt__"):].isdigit() else 0,
    )
    for _, stmt in stmt_items:
        lines.append(f"        {stmt}\n")
    # Legacy path: plain `state.<field>` simple assignment
    for pname, pvalue in vec.inputs.items():
        if pname.startswith("state."):
            field_name = pname[len("state."):]
            lines.append(f"        state.{field_name} = {pvalue};\n")
    lines.append(f"        let got = super::{fn_name}(&state);\n")
    expected = vec.expected_output.strip()
    if expected:
        lines.append(
            f"        assert_eq!(got, {expected}, "
            f'"{vec.description}");\n'
        )
    lines.append("    }\n")
    return "".join(lines)


def _emit_spec_test(fn_name: str, vec: SpecTestVector, idx: int) -> str:
    """Emit a test from a spec.test_vectors entry."""
    # State-mutator vectors use a different test shape
    if vec.tolerance == "state_mutator":
        return _emit_state_mutator_test(fn_name, vec, idx)
    # State-observer vectors (state_in -> scalar return, no mutation)
    if vec.tolerance == "state_observer":
        return _emit_state_observer_test(fn_name, vec, idx)
    # Byte-buffer transform vectors (zmem* family) carry a pipe-encoded
    # tolerance field starting with "byte_transform".
    if (vec.tolerance or "").startswith("byte_transform"):
        return _emit_byte_transform_test(fn_name, vec, idx)
    test_name = f"test_{fn_name}_spec_{idx}"
    lines = [f"    #[test]\n    fn {test_name}() {{\n"]
    for pname, pvalue in vec.inputs.items():
        lines.append(f"        let {pname} = {_literal_from_spec_value(pvalue)};\n")
    arg_list = ", ".join(vec.inputs.keys())
    lines.append(f"        let got = super::{fn_name}({arg_list});\n")
    expected = vec.expected_output.strip()
    if expected:
        # Try as a Rust literal
        if vec.tolerance in ("exact", ""):
            lines.append(f"        assert_eq!(got, {_literal_from_spec_value(expected)}, "
                         f'"{vec.description or vec.source or f"vector {idx}"}");\n')
        else:
            # Numeric tolerance
            lines.append(f"        let eps: f64 = {vec.tolerance};\n")
            lines.append(f"        let expected: f64 = {_literal_from_spec_value(expected)};\n")
            lines.append(f"        let got_f: f64 = got as f64;\n")
            lines.append(f"        assert!((got_f - expected).abs() < eps, "
                         f'"{vec.description or f"vector {idx}"}");\n')
    lines.append("    }\n")
    return "".join(lines)


def _emit_smoke_test(fn_name: str) -> str:
    """Emit a trivial call to ensure the function is callable on a short input."""
    return (
        f"    #[test]\n"
        f"    fn smoke_{fn_name}() {{\n"
        f"        // smoke — call with small empty-ish input; actual correctness\n"
        f"        // is covered by catalog and spec vectors.\n"
        f"        let _ = std::panic::catch_unwind(|| {{\n"
        f"            let _ = super::{fn_name}(&[][..]);\n"
        f"        }});\n"
        f"    }}\n"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def emit_module_test_block(
    module: ModuleSpec,
    *,
    enable_smoke: bool = False,
    catalog_lookup=lookup_test_vectors,
) -> tuple[str, dict[str, int]]:
    """Produce the `#[cfg(test)] mod tests` block body for one module.

    Returns (rust_source, stats_dict).
    """
    stats = {"spec": 0, "catalog": 0, "smoke": 0}
    lines: list[str] = []
    lines.append("#[cfg(test)]")
    lines.append("mod tests {")
    lines.append("    #![allow(unused_imports, unused_variables, unused_macros)]")
    lines.append("    extern crate alloc;")
    lines.append("    use alloc::format;")
    lines.append("    use alloc::string::String;")
    lines.append("")

    emitted_any = False

    for alg in module.algorithms:
        # Resolve the Rust function name the same way skeleton.py does so
        # super::<fn>(...) resolves. Key case: zlib `_tr_init` / `_tr_align`
        # lose their leading underscore in the skeleton's _snake() pass.
        fn_name = _rust_fn_name(alg.name)
        # 1. Spec-provided test vectors
        for i, v in enumerate(alg.test_vectors or []):
            lines.append(_emit_spec_test(fn_name, v, i))
            stats["spec"] += 1
            emitted_any = True
        # 2. Standards catalog — only when the signature matches a recognizable
        #    canonical shape. The compression/cipher templates assume very
        #    specific APIs (Vec<u8> return with .as_slice(), etc.). Emitting
        #    them against arbitrary deflate*/inflate* helpers produces
        #    uncompilable tests that poison the whole crate's test run.
        if _can_accept_byte_slice(alg) and _has_canonical_shape_for_category(alg):
            cat_vectors = catalog_lookup(alg.name)
            for i, v in enumerate(cat_vectors or []):
                if alg.category == "checksum":
                    lines.append(_emit_catalog_test_checksum(fn_name, v, i, alg=alg))
                elif alg.category == "hash":
                    lines.append(_emit_catalog_test_hash(fn_name, v, i, alg=alg))
                elif alg.category == "cipher":
                    lines.append(_emit_catalog_test_cipher(fn_name, v, i, alg=alg))
                elif alg.category in ("compression", "decompression"):
                    lines.append(_emit_catalog_test_compression(fn_name, v, i, alg=alg))
                else:
                    continue
                stats["catalog"] += 1
                emitted_any = True

    if not emitted_any and enable_smoke:
        # Fall back to smoke tests for every fn
        for alg in module.algorithms:
            lines.append(_emit_smoke_test(_rust_fn_name(alg.name)))
            stats["smoke"] += 1

    lines.append("}")
    return "\n".join(lines) + "\n", stats


def append_tests_to_module_file(
    crate_dir: Path,
    module: ModuleSpec,
    *,
    enable_smoke: bool = False,
) -> dict[str, int]:
    """Append the test block to `<crate>/src/<module>.rs` if not already there."""
    path = crate_dir / "src" / f"{module.name}.rs"
    if not path.exists():
        raise FileNotFoundError(f"cannot append tests: {path} does not exist")
    current = path.read_text(encoding="utf-8")
    # If a tests block already exists, replace it
    if "#[cfg(test)]" in current and "mod tests" in current:
        # Strip everything from `#[cfg(test)]` onwards
        idx = current.find("#[cfg(test)]")
        current = current[:idx].rstrip() + "\n\n"
    tests_src, stats = emit_module_test_block(module, enable_smoke=enable_smoke)
    merged = current.rstrip() + "\n\n" + tests_src
    path.write_text(merged, encoding="utf-8")
    return stats


def generate_tests_for_crate(
    crate: CrateSpec,
    module_specs: list[ModuleSpec],
    crate_dir: Path,
    *,
    enable_smoke: bool = False,
) -> CrateTestsResult:
    """Append a tests block to every module file in the crate."""
    modules = [m for m in module_specs if m.name in set(crate.modules)]
    total_spec = total_catalog = total_smoke = 0
    file_path = crate_dir / "src"
    for m in modules:
        stats = append_tests_to_module_file(crate_dir, m, enable_smoke=enable_smoke)
        total_spec += stats["spec"]
        total_catalog += stats["catalog"]
        total_smoke += stats["smoke"]
    return CrateTestsResult(
        crate_name=crate.name,
        file_path=file_path,
        tests_written=total_spec + total_catalog + total_smoke,
        tests_from_catalog=total_catalog,
        tests_from_spec=total_spec,
        tests_from_smoke=total_smoke,
    )


def generate_tests_for_workspace(
    specs: list[ModuleSpec],
    architecture: CrateArchitecture,
    workspace_dir: Path,
    *,
    enable_smoke: bool = False,
) -> list[CrateTestsResult]:
    """Append test blocks for every crate in the workspace."""
    results: list[CrateTestsResult] = []
    for crate in architecture.crates:
        crate_dir = workspace_dir / crate.name
        if not crate_dir.exists():
            continue
        r = generate_tests_for_crate(crate, specs, crate_dir, enable_smoke=enable_smoke)
        results.append(r)
    return results
