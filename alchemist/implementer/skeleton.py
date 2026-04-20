"""Phase 4A: emit a compile-ready skeleton of types + signatures + `unimplemented!()` bodies.

Goal: produce a Rust workspace where every intended public function exists,
every type is declared, everything type-checks — and every function body
is `unimplemented!("<what this would do>")`. This is the foundation on top
of which Phase 4B (test generator) emits failing tests and Phase 4C fills
in real implementations.

Determinism matters here: a skeleton that "sometimes fails to compile"
undoes the whole value of TDD Stage 4. So skeleton emission is driven by
schemas, not by the LLM.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from alchemist.architect.schemas import (
    CrateArchitecture,
    CrateSpec,
    ErrorType,
    TraitSpec,
)
from alchemist.extractor.schemas import (
    AlgorithmSpec,
    ModuleSpec,
    Parameter,
    ParamDirection,
    SharedType,
)


# ---------------------------------------------------------------------------
# Results / diagnostics
# ---------------------------------------------------------------------------

@dataclass
class CrateSkeletonResult:
    crate_name: str
    crate_dir: Path
    files_written: list[Path] = field(default_factory=list)
    compiles: bool = False
    compile_stderr: str = ""
    error_summary: str = ""


@dataclass
class WorkspaceSkeletonResult:
    workspace_dir: Path
    crate_results: list[CrateSkeletonResult] = field(default_factory=list)
    workspace_compiles: bool = False
    workspace_stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.workspace_compiles and all(r.compiles for r in self.crate_results)


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

def _snake(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and name[i - 1].islower():
            out.append("_")
        out.append(ch.lower())
    s = "".join(out)
    # Replace non-alphanumerics
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unnamed"


def _type_is_owning(rust_type: str) -> bool:
    """Rough heuristic for when a return type is owned (no lifetime)."""
    if not rust_type or rust_type == "()":
        return True
    if rust_type.startswith("&"):
        return False
    return True


def _sanitize_rust_type(rust_type: str) -> str:
    """Fix common invalid Rust types from LLM extraction.

    The 122B model regularly hallucinates types that look plausible but
    aren't valid Rust. This sanitizer catches the most common patterns
    and replaces them with safe fallbacks. Each rule is additive —
    add new patterns here as new failure classes surface.
    """
    t = rust_type.strip()
    if not t or t == "()" or t == "\"\"":
        return "()"
    # Bare `mut` or `&` without a type → default to &[u8]
    if t in ("mut", "&mut", "&"):
        return "&[u8]"
    # Empty or whitespace-only within wrapper types
    if t in ("&mut ", "& ", "mut "):
        return "&mut [u8]"
    # `mut [u8]` or `mut T` without & → add &
    if t.startswith("mut ") and not t.startswith("mut &"):
        t = "&" + t  # mut [u8] → &mut [u8]
    # `[u8]` bare (not &[u8]) → &[u8]
    if re.match(r"^\[.+\]$", t) and not t.startswith("&"):
        t = "&" + t

    # [T; variable] → Vec<T> (runtime-sized arrays from C VLAs)
    for _ in range(5):
        new_t = re.sub(r"\[(.+?);\s*([a-z_]\w*)\s*\]", r"Vec<\1>", t)
        if new_t == t:
            break
        t = new_t

    # Box<> / Vec<> / Option<> with empty generic → Box<u8> etc
    t = re.sub(r"Box<\s*>", "Box<u8>", t)
    t = re.sub(r"Vec<\s*>", "Vec<u8>", t)
    t = re.sub(r"Option<\s*>", "Option<u8>", t)

    # File → std::fs::File
    t = re.sub(r"\bFile\b", "std::fs::File", t)

    # dyn std::any::Any → usize (opaque C pointer)
    if "dyn std::any::Any" in t or "dyn Any" in t:
        t = "usize"

    # ZlibStream, ZStream → () placeholder (these should be in shared types
    # but if they aren't, don't crash the skeleton)
    # Only replace if the type isn't already imported from a dep crate
    for phantom in ("ZlibStream", "ZStream", "z_stream"):
        if phantom in t:
            # Check if it's a &mut ref or Option wrapper
            if f"&mut {phantom}" in t:
                t = "&mut Vec<u8>"
            elif f"Option<&mut {phantom}>" in t:
                t = "Option<&mut Vec<u8>>"
            elif f"Option<{phantom}>" in t:
                t = "Option<Vec<u8>>"
            elif f"&{phantom}" in t:
                t = "&[u8]"
            else:
                t = "Vec<u8>"

    # Result<...> with incomplete generics
    t = re.sub(r"Result<\s*,", "Result<(),", t)

    # impl Fn/FnMut/FnOnce closures → simple function pointer placeholder
    # The skeleton body is unimplemented!() so the exact closure type doesn't matter.
    # Replace the ENTIRE impl Fn... expression (including return type) with a unit fn.
    if re.search(r"\bimpl\s+Fn", t):
        t = "fn()"

    # Box<[T]> (unsized in Box) → Vec<T>
    m = re.match(r"Box<\[(.+)\]>", t)
    if m:
        t = f"Vec<{m.group(1)}>"

    return t


def _sanitize_param_name(name: str, idx: int) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip()) if name else f"arg{idx}"
    if re.match(r"^\d", safe):
        safe = f"arg_{safe}"
    if safe in {"type", "match", "impl", "trait", "loop", "move", "ref", "mut", "self",
                 "in", "fn", "let", "if", "else", "for", "while", "return", "break",
                 "continue", "struct", "enum", "use", "mod", "pub", "where", "as",
                 "async", "await", "dyn", "extern", "super", "crate", "const", "static",
                 "unsafe", "abstract", "become", "box", "do", "final", "macro",
                 "override", "priv", "typeof", "unsized", "virtual", "yield", "try"}:
        safe = f"r#{safe}"
    return safe or f"arg{idx}"


def _fn_signature(alg: AlgorithmSpec) -> str:
    """Build a complete `pub fn name(...)  -> Return` signature from an AlgorithmSpec."""
    params: list[str] = []
    for i, p in enumerate(alg.inputs):
        name = _sanitize_param_name(p.name, i)
        rust_type = _sanitize_rust_type(p.rust_type)
        prefix = ""
        if p.direction == ParamDirection.output and not rust_type.startswith("&mut"):
            prefix = ""
        params.append(f"{name}: {rust_type}")
    # Output parameters that aren't folded into return type — also append as &mut args
    for i, p in enumerate(alg.outputs):
        rust_type = _sanitize_rust_type(p.rust_type)
        if not rust_type.startswith("&mut"):
            continue
        name = _sanitize_param_name(p.name, i + len(alg.inputs))
        params.append(f"{name}: {rust_type}")
    ret = _sanitize_rust_type(alg.return_type.strip() or "()")
    if ret == "()" or ret == "":
        return f"pub fn {_snake(alg.name)}({', '.join(params)})"
    return f"pub fn {_snake(alg.name)}({', '.join(params)}) -> {ret}"


def emit_function_stub(alg: AlgorithmSpec) -> str:
    """Emit a single function stub whose body is `unimplemented!(...)`."""
    doc = []
    if alg.display_name:
        doc.append(f"/// {alg.display_name}")
    if alg.description:
        for l in _wrap_for_doc(alg.description):
            doc.append(f"/// {l}")
    if alg.referenced_standards:
        doc.append("///")
        doc.append(f"/// Standards: {', '.join(alg.referenced_standards)}")
    sig = _fn_signature(alg)
    # Tag the unimplemented!() message so the anti-stub detector can find it
    # and so test output clearly identifies which function is the stub.
    body = f'    unimplemented!("skeleton: {_snake(alg.name)} not yet implemented")'
    # Suppress unused-param warnings in skeleton body
    param_idents = _extract_param_idents(sig)
    suppressions = "\n".join(f"    let _ = {p};" for p in param_idents)
    parts = []
    parts.extend(doc)
    parts.append("#[allow(clippy::unimplemented)]")
    parts.append(sig + " {")
    if suppressions:
        parts.append(suppressions)
    parts.append(body)
    parts.append("}")
    return "\n".join(parts)


def _extract_param_idents(sig: str) -> list[str]:
    m = re.search(r"\(([^)]*)\)", sig)
    if not m:
        return []
    params = m.group(1)
    out: list[str] = []
    for p in _split_params(params):
        p = p.strip()
        if not p:
            continue
        name = p.split(":", 1)[0].strip()
        if name in ("self", "&self", "&mut self"):
            continue
        out.append(name)
    return out


def _split_params(s: str) -> list[str]:
    depth = 0
    buf: list[str] = []
    out: list[str] = []
    for ch in s:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _wrap_for_doc(s: str, width: int = 80) -> list[str]:
    # Simple wrap at word boundaries
    words = s.split()
    lines: list[str] = []
    buf: list[str] = []
    length = 0
    for w in words:
        if length + len(w) + 1 > width:
            lines.append(" ".join(buf))
            buf = [w]
            length = len(w)
        else:
            buf.append(w)
            length += len(w) + 1
    if buf:
        lines.append(" ".join(buf))
    return lines


# ---------- Shared types ----------

def emit_shared_type(t: SharedType) -> str:
    """Return the SharedType's rust_definition, falling back to a placeholder struct."""
    body = (t.rust_definition or "").strip()
    if body:
        # Ensure `pub` visibility on the top-level item so other crates can use it
        if not re.match(r"^\s*pub\b", body):
            # Best-effort: prepend pub to the first `struct/enum/type/trait` token
            body = re.sub(r"^(\s*)(struct|enum|type|trait|union)\b",
                          r"\1pub \2", body, count=1, flags=re.MULTILINE)
        return body
    # No definition provided — emit a placeholder
    field_lines = "\n".join(
        f"    pub {f.name}: {f.rust_type},"
        for f in (t.fields or [])
    ) or "    _placeholder: (),"
    return f"pub struct {t.name} {{\n{field_lines}\n}}"


# ---------- Error enums ----------

def emit_error_enum(err: ErrorType) -> str:
    lines = [f"#[derive(Debug, Clone, PartialEq, Eq)]",
             f"pub enum {err.name} {{"]
    for v in err.variants:
        if v.fields:
            field_list = ", ".join(v.fields)
            lines.append(f"    /// {v.description}")
            lines.append(f"    {v.name}({field_list}),")
        else:
            lines.append(f"    /// {v.description}")
            lines.append(f"    {v.name},")
    lines.append("}")
    lines.append("")
    # impl Display
    lines.append(f"impl core::fmt::Display for {err.name} {{")
    lines.append("    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {")
    lines.append("        core::fmt::Debug::fmt(self, f)")
    lines.append("    }")
    lines.append("}")
    return "\n".join(lines)


# ---------- Trait definitions ----------

def emit_state_wrapper(w) -> str:
    """Emit a public wrapper struct that encapsulates a raw internal state.

    The wrapper has a single `inner: InnerState` field, kept private.
    Callers interact exclusively through the methods defined on the wrapper.
    """
    lines = []
    if getattr(w, "description", ""):
        for l in _wrap_for_doc(w.description):
            lines.append(f"/// {l}")
    lines.append(f"pub struct {w.public_name} {{")
    lines.append(f"    pub(crate) inner: {w.inner_state},")
    lines.append("}")
    lines.append("")
    if w.methods:
        lines.append(f"impl {w.public_name} {{")
        for m_sig in w.methods:
            sig = m_sig.rstrip(";").rstrip()
            lines.append(f"    {sig} {{")
            lines.append(f'        unimplemented!("{w.public_name} method skeleton")')
            lines.append("    }")
        lines.append("}")
    return "\n".join(lines)


def emit_builder(b) -> str:
    """Emit a builder struct with fluent setter methods plus build()."""
    lines = []
    lines.append(f"/// Builder for {b.built_type}.")
    lines.append(f"#[derive(Default)]")
    lines.append(f"pub struct {b.builder_name} {{")
    lines.append(f"    // fields populated by fluent setters")
    lines.append("}")
    lines.append("")
    lines.append(f"impl {b.builder_name} {{")
    lines.append("    pub fn new() -> Self {")
    lines.append("        Self::default()")
    lines.append("    }")
    for p_sig in b.parameters:
        sig = p_sig.rstrip(";").rstrip()
        lines.append(f"    {sig} {{")
        lines.append("        unimplemented!(\"builder setter skeleton\")")
        lines.append("    }")
    build_sig = b.build_signature.rstrip(";").rstrip()
    lines.append(f"    {build_sig} {{")
    lines.append("        unimplemented!(\"builder build skeleton\")")
    lines.append("    }")
    lines.append("}")
    return "\n".join(lines)


def emit_trait(tr: TraitSpec) -> str:
    lines = []
    if tr.description:
        for l in _wrap_for_doc(tr.description):
            lines.append(f"/// {l}")
    super_clause = f": {' + '.join(tr.supertraits)}" if tr.supertraits else ""
    lines.append(f"pub trait {tr.name}{super_clause} {{")
    for m in tr.methods:
        if m.description:
            for l in _wrap_for_doc(m.description):
                lines.append(f"    /// {l}")
        if m.has_default:
            lines.append(f"    {m.signature} {{")
            lines.append(f'        unimplemented!("trait-default skeleton: {tr.name}::{m.name}")')
            lines.append("    }")
        else:
            sig = m.signature.rstrip(";").rstrip()
            lines.append(f"    {sig};")
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Crate layout
# ---------------------------------------------------------------------------

_BUILTIN_CRATES = {"std", "core", "alloc", "proc_macro", "test"}

def _cargo_toml_for_crate(crate: CrateSpec, include_workspace_tag: bool) -> str:
    dep_lines = [f'{d} = {{ path = "../{d}" }}' for d in crate.dependencies]
    for ext in getattr(crate, "external_deps", []) or []:
        # Skip built-in crates the LLM sometimes hallucinates as external deps
        if ext.name in _BUILTIN_CRATES:
            continue
        if ext.features:
            feat = ", ".join(f'"{f}"' for f in ext.features)
            dep_lines.append(f'{ext.name} = {{ version = "{ext.version}", features = [{feat}] }}')
        else:
            dep_lines.append(f'{ext.name} = "{ext.version}"')
    deps = "\n".join(dep_lines)
    content = (
        "[package]\n"
        f'name = "{crate.name}"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n'
        "\n"
        "[dependencies]\n"
        f"{deps}\n"
    )
    if include_workspace_tag:
        content += "\n[workspace]\n"
    return content


def _module_rs_for(
    module: ModuleSpec,
    import_alias: dict[str, str],
    dep_crate_names: list[str] | None = None,
) -> str:
    """Produce src/<module>.rs content: shared types + fn stubs."""
    lines: list[str] = []
    lines.append(f"//! {module.display_name or module.name}")
    lines.append("//!")
    if module.description:
        for l in _wrap_for_doc(module.description):
            lines.append(f"//! {l}")
    lines.append("")
    lines.append("#![allow(unused_variables, unused_imports, dead_code)]")
    lines.append("")
    # Import everything from dependency crates (shared types like DeflateState)
    for dep in (dep_crate_names or []):
        rust_crate = dep.replace("-", "_")
        lines.append(f"use {rust_crate}::*;")
    if dep_crate_names:
        lines.append("")
    # Crate-internal imports — allow module files to see items defined in lib.rs
    # (error enums, re-exported types from other modules)
    lines.append("use crate::*;")
    lines.append("")
    # Bring sibling module types into scope where configured
    for import_path, alias in import_alias.items():
        lines.append(f"use {import_path};")
    if import_alias:
        lines.append("")
    # Shared types
    for t in module.shared_types or []:
        lines.append(emit_shared_type(t))
        lines.append("")
    # Function stubs
    for alg in module.algorithms:
        lines.append(emit_function_stub(alg))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _lib_rs_for(
    crate: CrateSpec,
    module_names: list[str],
    errors: list[ErrorType],
    traits: list[TraitSpec],
    no_std: bool,
    dep_crate_names: list[str] | None = None,
    all_error_types: list[ErrorType] | None = None,
    state_wrappers: list | None = None,
    builders: list | None = None,
) -> str:
    lines: list[str] = []
    # Structural no-unsafe proof: every Alchemist-generated crate forbids
    # unsafe code at the crate level. If any function needs unsafe, the
    # pipeline fails rather than ship it. This is a P5 invariant.
    lines.append("#![forbid(unsafe_code)]")
    lines.append("#![allow(unused_imports)]")
    if no_std:
        lines.append("#![no_std]")
        lines.append("extern crate alloc;")
        lines.append("use alloc::vec::Vec;")
        lines.append("use alloc::string::String;")
    lines.append("")
    for m in module_names:
        lines.append(f"pub mod {m};")
    if module_names:
        lines.append("")
    # Re-export everything from modules
    for m in module_names:
        lines.append(f"pub use self::{m}::*;")
    if module_names:
        lines.append("")
    # Auto-import types from dependency crates that traits/error types
    # in THIS crate reference. Scans trait method signatures and error
    # variant fields for type names defined in other crates' error_types.
    if dep_crate_names and all_error_types:
        imported: set[str] = set()
        # Collect type names defined in dependency crates
        dep_type_names: dict[str, str] = {}  # type_name -> crate_name
        for et in all_error_types:
            if et.crate != crate.name and et.crate in set(dep_crate_names):
                dep_type_names[et.name] = et.crate
        # Scan this crate's traits for references to those types
        for t in traits:
            for m in t.methods:
                sig = m.signature
                for type_name, dep_crate in dep_type_names.items():
                    if type_name in sig and type_name not in imported:
                        rust_crate = dep_crate.replace("-", "_")
                        lines.append(f"pub use {rust_crate}::{type_name};")
                        imported.add(type_name)
        if imported:
            lines.append("")
    # Trait definitions
    for t in traits:
        lines.append(emit_trait(t))
        lines.append("")
    # State wrappers: encapsulate raw internal state types
    for w in (state_wrappers or []):
        if w.crate == crate.name:
            lines.append(emit_state_wrapper(w))
            lines.append("")
    # Builders: fluent wrappers for parameterized init
    for b in (builders or []):
        if b.crate == crate.name:
            lines.append(emit_builder(b))
            lines.append("")
    # Error enums
    for e in errors:
        lines.append(emit_error_enum(e))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_crate_skeleton(
    crate_spec: CrateSpec,
    module_specs: list[ModuleSpec],
    architecture: CrateArchitecture,
    output_dir: Path,
    *,
    include_workspace_tag: bool = True,
) -> CrateSkeletonResult:
    """Emit a compile-ready skeleton crate.

    Keeps `include_workspace_tag=True` until the workspace Cargo.toml is written
    at the end (matches the per-crate standalone-compile pattern in the old
    code_generator).
    """
    crate_dir = output_dir / crate_spec.name
    (crate_dir / "src").mkdir(parents=True, exist_ok=True)

    result = CrateSkeletonResult(crate_name=crate_spec.name, crate_dir=crate_dir)

    # Cargo.toml
    cargo_path = crate_dir / "Cargo.toml"
    cargo_path.write_text(
        _cargo_toml_for_crate(crate_spec, include_workspace_tag=include_workspace_tag),
        encoding="utf-8",
    )
    result.files_written.append(cargo_path)

    # Collect errors and traits that belong to this crate
    errors = [e for e in architecture.error_types if e.crate == crate_spec.name]
    traits = [t for t in architecture.traits if t.crate == crate_spec.name]
    modules = [m for m in module_specs if m.name in set(crate_spec.modules)]
    module_names = [m.name for m in modules]

    # src/lib.rs
    lib_path = crate_dir / "src" / "lib.rs"
    lib_path.write_text(
        _lib_rs_for(
            crate_spec, module_names, errors, traits, crate_spec.is_no_std,
            dep_crate_names=list(crate_spec.dependencies),
            all_error_types=list(architecture.error_types),
            state_wrappers=list(getattr(architecture, "state_wrappers", []) or []),
            builders=list(getattr(architecture, "builders", []) or []),
        ),
        encoding="utf-8",
    )
    result.files_written.append(lib_path)

    # src/<module>.rs for each module
    for m in modules:
        mod_path = crate_dir / "src" / f"{m.name}.rs"
        mod_path.write_text(
            _module_rs_for(m, {}, dep_crate_names=list(crate_spec.dependencies)),
            encoding="utf-8",
        )
        result.files_written.append(mod_path)

    return result


def generate_workspace_skeleton(
    specs: list[ModuleSpec],
    architecture: CrateArchitecture,
    output_dir: Path,
    *,
    cargo_check: bool = True,
) -> WorkspaceSkeletonResult:
    """Emit skeletons for the entire workspace and (optionally) verify compile."""
    output_dir.mkdir(parents=True, exist_ok=True)
    workspace_result = WorkspaceSkeletonResult(workspace_dir=output_dir)

    # Per-crate skeleton (keep [workspace] tag so each compiles standalone
    # until we write the real workspace toml).
    order = _topo_sort(architecture)
    for crate_name in order:
        crate_spec = next((c for c in architecture.crates if c.name == crate_name), None)
        if crate_spec is None:
            continue
        res = generate_crate_skeleton(
            crate_spec, specs, architecture, output_dir,
            include_workspace_tag=True,
        )
        workspace_result.crate_results.append(res)

    # Strip [workspace] tags and write real workspace Cargo.toml
    for crate in architecture.crates:
        ct = output_dir / crate.name / "Cargo.toml"
        if ct.exists():
            content = ct.read_text(encoding="utf-8")
            content = re.sub(r"\n*\[workspace\]\s*\n?", "\n", content).rstrip() + "\n"
            ct.write_text(content, encoding="utf-8")
    _write_workspace_toml(architecture, output_dir)

    if cargo_check:
        workspace_ok, stderr = _run_cargo_check(output_dir)
        workspace_result.workspace_compiles = workspace_ok
        workspace_result.workspace_stderr = stderr
        # Update per-crate compile flags
        for cr in workspace_result.crate_results:
            crate_ok, crate_stderr = _run_cargo_check(cr.crate_dir)
            cr.compiles = crate_ok
            cr.compile_stderr = crate_stderr
            if not crate_ok:
                cr.error_summary = _top_errors(crate_stderr, n=3)

    return workspace_result


def _write_workspace_toml(arch: CrateArchitecture, output_dir: Path) -> None:
    members = ",\n".join(f'    "{c.name}"' for c in arch.crates)
    (output_dir / "Cargo.toml").write_text(
        "[workspace]\n"
        'resolver = "2"\n'
        "members = [\n"
        f"{members}\n"
        "]\n",
        encoding="utf-8",
    )


def _topo_sort(arch: CrateArchitecture) -> list[str]:
    graph = {c.name: set(c.dependencies) for c in arch.crates}
    out: list[str] = []
    visited: set[str] = set()

    def visit(n: str):
        if n in visited:
            return
        visited.add(n)
        for d in graph.get(n, set()):
            if d in graph:
                visit(d)
        out.append(n)

    for n in graph:
        visit(n)
    return out


def _run_cargo_check(path: Path, timeout: int = 180) -> tuple[bool, str]:
    import time
    last_stderr = ""
    for attempt in range(3):
        try:
            r = subprocess.run(
                ["cargo", "check"],
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except Exception as e:
            return False, f"cargo check failed to run: {e}"
        if r.returncode == 0:
            return True, r.stderr or ""
        stderr = r.stderr or ""
        last_stderr = stderr
        # Retry on Windows linker file-lock (LNK1104). Not a real failure.
        if "LNK1104" not in stderr and "cannot open file" not in stderr:
            return False, stderr
        time.sleep(0.5 * (3 ** attempt))
    return False, last_stderr


def _top_errors(stderr: str, n: int = 3) -> str:
    lines = stderr.splitlines()
    errs = [l for l in lines if l.startswith("error")]
    return "\n".join(errs[:n])
