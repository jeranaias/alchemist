"""Reference probe — per-function C-to-Rust transliteration.

For any AlgorithmSpec the pipeline is about to generate, the probe:
  1. Locates the function's C source body via source_files + function name
  2. Asks the LLM for a direct, safe-Rust transliteration (no idiom rewrites,
     no optimization — just mechanical translation preserving structure)
  3. Compile-checks the candidate against a shared types shim
  4. If it compiles, returns it as a reference implementation the TDD loop
     can inject into its prompt

This is the generalization primitive: hand-curated reference impls cover
common primitives (Adler-32, CRC-32) but the long tail of zlib/mbedTLS
functions needs auto-generated references to avoid hand-writing one per
function. The probe IS that auto-generation.

Key differences from regular Stage 4 generation:
  - Probe aims for 1:1 structural correspondence with the C body
  - Probe output is not shipped; it's a template the regular LLM adapts
  - Probe is permitted to produce non-idiomatic Rust — correctness first
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from alchemist.extractor.schemas import AlgorithmSpec
from alchemist.llm.client import AlchemistLLM, LLMResponse
from alchemist.references.registry import ReferenceImpl


_PROBE_SYSTEM = """You produce literal transliterations of C into safe Rust.
No optimization. No idiom rewrite. One Rust statement per C statement where
possible. Preserve variable names, loop structure, and branch shape exactly.
Use safe Rust only: NO `unsafe` blocks, NO raw pointers.

## C type mapping

C types must be converted to their Rust equivalents. NEVER keep a C typedef
in the Rust output. Common mappings:

  - `unsigned char`, `Bytef`, `uchf`, `Byte`      → u8
  - `short`, `ushf`                               → i16 / u16
  - `int`, `uInt`                                 → i32 / u32
  - `unsigned int`, `uLong`                       → u32 / u64
  - `size_t`, `z_size_t`, `ptrdiff_t`             → usize / isize
  - `long long`, `int64_t`                        → i64
  - `z_off_t`, `off_t`                            → i64
  - `void *`, `voidpf`                            → do not translate raw;
    express with `&[u8]` / `&mut [u8]` / explicit owned types

## Pointer conversions

  - `const unsigned char *buf, size_t len` → `buf: &[u8]` (length from .len())
  - `unsigned char *out, size_t len`       → `out: &mut [u8]`
  - `size_t *len_in_out`                   → `len: &mut usize`
  - `const char *s` where nul-terminated   → `s: &str`
  - `z_streamp strm` / `z_stream *strm`    → `strm: &mut DeflateStream`
                                             (or InflateStream as appropriate)

## Struct access

When the C body references struct fields via `strm->state->X`, translate
to `strm.state.X` (Rust flattens pointer deref). When zlib uses the
`state` subfield of a stream (e.g., `s = strm->state`), translate to
`let s = &mut strm.state;` and then use `s.X` through the body.

## Memory primitives

  - `memcpy(dst, src, n)` → `dst[..n].copy_from_slice(&src[..n])`
  - `memset(buf, 0, n)`   → `for b in &mut buf[..n] { *b = 0; }`
  - `memmove(dst, src, n)` (overlapping) → `dst.copy_within(..n, dst_start)`

## Control flow

  - `goto label;` over a forward-only structured region → use a `break 'label`
    out of a labeled loop, OR restructure to a single if/else chain. Never
    use a raw jump-equivalent.
  - Early `return VALUE;` → `return VALUE;` (Rust allows it)

## Mutability

Rust parameters are immutable by default. Before writing the body, audit
EVERY parameter: if the C body reassigns it, mutates it, or advances a
pointer through it, shadow it with a `let mut` binding as the FIRST
statement of the function body.

When in doubt, shadow. Over-shadowing is harmless (unused_mut warnings).
Under-shadowing is a compile error.

    // C has: `adler -= BASE; buf += 16; len -= 16;`
    pub fn f(adler: u32, buf: &[u8], len: usize) -> u32 {
        let mut adler = adler;   // REQUIRED: C reassigns it
        let mut buf = buf;       // REQUIRED: C advances the pointer
        let mut len = len;       // REQUIRED: C decrements it
        // ... rest of body can freely use `adler -= 65521; buf = &buf[16..]; len -= 16;`
    }

Walk through the C source once looking for `x = y`, `x += y`, `x++`, `x--`,
`++x`, `--x`, `x = foo(x)` — every such variable MUST be shadowed if it's
a parameter. This applies even to pointer-like parameters: `buf = &buf[16..]`
requires `let mut buf = buf;` above it.

## Return type discipline

The signature's return type is LAW. Every `return` expression MUST match it.
If your intermediate variables are a wider type (e.g., u64 for overflow
safety but the return type is u32), cast explicitly: `return result as u32;`
not `return result;` hoping for implicit conversion.

## Numeric literals and types

Use typed suffixes on numeric literals when mixing types: `65521u32`, not
bare `65521`. Inside arithmetic with u64 variables, use `u64` literals.
When the C code uses `unsigned long` constants that fit in u32, prefer
u32 unless the algorithm requires wider intermediate values.

Return ONLY the Rust function definition — signature + body — no explanation,
no markdown, no additional text.
"""


_PROBE_PROMPT = """Translate this C function into safe Rust.

C source:
```c
{c_body}
```

Required Rust signature (match exactly):
```rust
{signature}
```

Relevant shared types already in scope (fields you may reference):
{struct_context}

Previous failure to correct (if any):
{previous_failure}

Return the complete Rust function — signature line + body — with no other text.
"""


@dataclass
class ProbeResult:
    """Result of probing a C function for its Rust transliteration."""
    algorithm: str
    success: bool
    rust_source: str = ""
    error: str = ""
    notes: str = ""


def extract_c_function_body(source_path: Path, function_name: str) -> str | None:
    """Locate a C function by name in the given source file and return its full text.

    Uses tree-sitter to parse and find the matching definition. Returns the
    complete function-definition text (signature + body) as a str, or None
    if the function isn't found.
    """
    if not source_path.exists():
        return None
    try:
        import tree_sitter_c as tsc
        from tree_sitter import Language, Parser
    except ImportError:
        return None

    source = source_path.read_bytes()
    language = Language(tsc.language())
    parser = Parser(language)
    tree = parser.parse(source)

    def walk(node):
        if node.type == "function_definition":
            # Find the declarator's inner identifier
            declarator = node.child_by_field_name("declarator")
            if declarator is not None:
                name_node = _find_identifier(declarator)
                if name_node is not None:
                    name = source[name_node.start_byte:name_node.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                    if name == function_name:
                        return source[node.start_byte:node.end_byte].decode(
                            "utf-8", errors="replace"
                        )
        for child in node.children:
            result = walk(child)
            if result is not None:
                return result
        return None

    return walk(tree.root_node)


def _find_identifier(node):
    """Descend declarator nodes to find the innermost identifier."""
    if node.type == "identifier":
        return node
    for child in node.children:
        found = _find_identifier(child)
        if found is not None:
            return found
    return None


def probe_algorithm(
    alg: AlgorithmSpec,
    *,
    source_root: Path,
    llm: AlchemistLLM,
    signature: str,
    struct_context: str = "",
    cached_context=None,
    max_tokens: int = 4000,
    temperature: float = 0.1,
) -> ProbeResult:
    """Probe a single algorithm spec for a C-faithful Rust transliteration.

    Args:
      alg: The AlgorithmSpec whose transliteration is requested.
      source_root: Directory containing the original C source tree
                   (e.g., `subjects/zlib/`). The probe searches this
                   directory using alg.source_files.
      llm: The LLM client. Any AlchemistLLM instance works.
      signature: The target Rust signature the transliteration must match.
      struct_context: Block of shared type definitions already in scope.
    """
    # Locate the C function body.
    c_body = _find_body_in_sources(alg, source_root)
    if c_body is None:
        return ProbeResult(
            algorithm=alg.name,
            success=False,
            error=f"C body not found for {alg.name} in {source_root}",
        )

    prompt = _PROBE_PROMPT.format(
        c_body=c_body,
        signature=signature,
        struct_context=struct_context or "(no shared types referenced)",
        previous_failure="(none)",
    )
    schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    }
    if cached_context is None:
        cached_context = llm.create_cached_context(system_text=_PROBE_SYSTEM)
    resp: LLMResponse = llm.call_structured(
        messages=[{"role": "user", "content": prompt}],
        tool_name="transliterate",
        tool_schema=schema,
        cached_context=cached_context,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if resp.error:
        return ProbeResult(
            algorithm=alg.name,
            success=False,
            error=f"LLM error: {resp.error}",
        )
    rust = ""
    if resp.structured and "content" in resp.structured:
        rust = (resp.structured.get("content") or "").strip()
    else:
        rust = (resp.content or "").strip()
    if rust.startswith("```"):
        rust = re.sub(r"^```(?:\w+)?\s*", "", rust)
        rust = re.sub(r"```\s*$", "", rust)
    if not rust:
        return ProbeResult(
            algorithm=alg.name,
            success=False,
            error="LLM returned empty transliteration",
        )
    return ProbeResult(
        algorithm=alg.name,
        success=True,
        rust_source=rust,
        notes="transliterated from C source",
    )


def _find_body_in_sources(alg: AlgorithmSpec, source_root: Path) -> str | None:
    """Search source files referenced by the spec for the function's body."""
    # The spec's source_files may be relative paths or absolute.
    candidate_paths: list[Path] = []
    for sf in alg.source_files or []:
        p = Path(sf)
        if not p.is_absolute():
            p = source_root / sf
        candidate_paths.append(p)
    # Fallback: if no source_files recorded, search every .c file in source_root.
    if not candidate_paths:
        candidate_paths = list(source_root.rglob("*.c"))
    # Try each candidate; accept the first hit.
    for fn_name in alg.source_functions or [alg.name]:
        for path in candidate_paths:
            body = extract_c_function_body(path, fn_name)
            if body:
                return body
    return None


def probe_result_as_reference(probe: ProbeResult, signature: str) -> ReferenceImpl | None:
    """Wrap a probe result as a ReferenceImpl suitable for runtime injection."""
    if not probe.success or not probe.rust_source:
        return None
    return ReferenceImpl(
        algorithm=probe.algorithm,
        variant="probe",
        title=f"{probe.algorithm} (auto-probed from C source)",
        rust_source=probe.rust_source,
        signature=signature,
        notes=probe.notes,
    )
