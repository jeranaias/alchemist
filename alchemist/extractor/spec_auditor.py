"""Pre-run spec auditor.

The LLM extractor produces specs with occasional systematic errors:
- state-type misclassification (InflateState claimed where source is in trees.c)
- return-type drift (Result<u64> where the C fn returns int)
- missing parameters
- wrong direction annotations

Historically we discovered these one-at-a-time via failed runs, each
costing 20–40 min. This auditor runs up front and catches the whole
class before Stage 4 wastes LLM calls.

Signal sources (cheapest first):
- Module name: `trees`, `deflate` → DeflateState; `inflate`, `inffast`
  → InflateState. Module name can't be wrong.
- Direct C source scan: for each spec's function, search C files for
  `<fnname>(` and extract the first-parameter type. Authoritative when
  the function is defined in exactly one file.
- Cross-check with normalizer rewrites: if normalizer already auto-
  corrects a spec, that's a fossil of a known failure mode.

The auditor is non-destructive: reports findings, optionally auto-
applies safe fixes. Pipeline can run with `--audit-only` to dry-run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from alchemist.extractor.schemas import AlgorithmSpec, ModuleSpec, Parameter


# ---------------------------------------------------------------------------
# Finding C function signatures in source
# ---------------------------------------------------------------------------

# Matches a C function definition: [modifiers] return_type name(args) { body
# Forgiving about whitespace/linewrap and K&R-style arg lists.
_C_FN_DEF = re.compile(
    r"(?:^|\n)\s*"
    r"(?:local\s+|static\s+|extern\s+|ZLIB_INTERNAL\s+|ZEXPORT\s+|ZEXPORTVA\s+)*"
    r"([a-zA-Z_][\w\s\*]*?)\s+"       # return type (group 1)
    r"([a-zA-Z_]\w*)\s*"              # fn name (group 2)
    r"\(([^)]*)\)\s*"                 # arg list (group 3)
    r"(?:\n\s*[a-zA-Z_][\w\s\*]*?;\s*)*"  # optional K&R arg decls
    r"\{",
    re.MULTILINE,
)


def _scan_c_signatures(c_root: Path) -> dict[str, tuple[str, str, str]]:
    """Return {fn_name: (return_type, first_param_type, source_file)}.

    First-defn-wins; second definitions (e.g., in headers) are ignored.
    """
    out: dict[str, tuple[str, str, str]] = {}
    for c_file in sorted(c_root.glob("*.c")):
        try:
            src = c_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in _C_FN_DEF.finditer(src):
            ret = m.group(1).strip()
            name = m.group(2).strip()
            args = m.group(3).strip()
            first_param = args.split(",")[0].strip() if args else ""
            if name not in out:
                out[name] = (ret, first_param, c_file.name)
    return out


# ---------------------------------------------------------------------------
# C type → Rust type heuristic
# ---------------------------------------------------------------------------

_C_TO_RUST_BASE: dict[str, str] = {
    # --- zlib ---
    "deflate_state": "DeflateState",
    "inflate_state": "InflateState",
    # internal_state is a typedef alias used inside z_stream; module-scope
    # decides which concrete state it points to.
    "internal_state": "_MODULE_STATE_",
    # z_stream is a single C type but we split it into DeflateStream /
    # InflateStream based on the module the function lives in (since the
    # fields each role uses are disjoint).
    "z_stream": "_MODULE_STREAM_",
    "z_streamp": "_MODULE_STREAM_",
    # --- miniz ---
    "tdefl_compressor": "DeflateState",
    "tinfl_decompressor": "InflateState",
}


def _resolve_module_token(base: str, module_role: str | None) -> str | None:
    """Replace module-scoped tokens with concrete types."""
    if base == "_MODULE_STATE_":
        return {"deflate": "DeflateState", "inflate": "InflateState"}.get(
            module_role or ""
        )
    if base == "_MODULE_STREAM_":
        return {"deflate": "DeflateStream", "inflate": "InflateStream"}.get(
            module_role or ""
        )
    return base


def _infer_module_role(module_name: str) -> str | None:
    m = module_name.lower()
    if any(tok in m for tok in ("inffast", "inftrees", "inflate", "inflat")):
        return "inflate"
    if any(tok in m for tok in ("trees", "deflate", "deflat")):
        return "deflate"
    return None


_C_RETURN_TO_RUST: dict[str, str] = {
    "void": "()",
    "int": "i32",
    "unsigned int": "u32",
    "unsigned": "u32",
    "short": "i16",
    "unsigned short": "u16",
    "long": "i64",
    "unsigned long": "u64",
    "long long": "i64",
    "unsigned long long": "u64",
    "char": "u8",
    "unsigned char": "u8",
    "signed char": "i8",
    "size_t": "usize",
    "uint8_t": "u8",
    "uint16_t": "u16",
    "uint32_t": "u32",
    "uint64_t": "u64",
    "int8_t": "i8",
    "int16_t": "i16",
    "int32_t": "i32",
    "int64_t": "i64",
    # zlib typedefs
    "uInt": "u32",
    "uLong": "u64",
    "ush": "u16",
    "ulg": "u64",
    "Byte": "u8",
    "Bytef": "u8",
    "z_word_t": "u64",
    "z_off_t": "i64",
    "z_size_t": "usize",
}


def _c_return_to_rust(c_ret: str) -> str | None:
    """Map a C return type (possibly with qualifiers) to its Rust equivalent.

    Strips common calling-convention / visibility qualifiers (ZEXPORT,
    ZLIB_INTERNAL, local, static, const) and looks up the remaining base
    type. Returns None when the type isn't understood.
    """
    import re as _re
    if not c_ret:
        return None
    s = c_ret.strip()
    # Strip qualifiers
    for qual in (
        "ZEXPORT", "ZEXPORTVA", "ZLIB_INTERNAL", "local", "static",
        "extern", "inline",
    ):
        s = _re.sub(rf"\b{qual}\b", "", s)
    s = _re.sub(r"\bconst\b", "", s)
    s = _re.sub(r"\s+", " ", s).strip()
    # Pointer return → drop pointer (Rust would need &/&mut wrapping, which
    # we don't emit for returns). Let the caller handle pointer returns.
    if "*" in s:
        return None
    if not s:
        return None
    return _C_RETURN_TO_RUST.get(s)


def _c_first_param_to_rust(
    c_param: str, module_role: str | None = None,
) -> str | None:
    """Map a C first-parameter decl to its expected Rust type.

    Example: `deflate_state *s` -> `&mut DeflateState`
             `z_streamp strm` (in deflate module) -> `&mut DeflateStream`
             `z_streamp strm` (in inflate module) -> `&mut InflateStream`
             `const ct_data *tree` -> can't infer, returns None
    """
    if not c_param or c_param == "void":
        return None
    # Drop leading `const`
    s = re.sub(r"^\s*const\s+", "", c_param).strip()
    # Detect pointer (handles `T *name`, `T* name`, `T **name`) and strip stars
    is_ptr = "*" in s
    s = s.replace("*", " ").strip()
    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s)
    # Strip trailing var name (now guaranteed to be whitespace-separated)
    s = re.sub(r"\s+\w+\s*$", "", s).strip()
    base = _C_TO_RUST_BASE.get(s)
    if not base:
        # Fall back to scalar return-type map (same C-to-Rust rules apply for scalar params).
        scalar = _C_RETURN_TO_RUST.get(s)
        if scalar and scalar != "()" and not is_ptr:
            return scalar
        return None
    # Resolve module-scoped aliases
    resolved = _resolve_module_token(base, module_role)
    if resolved is None:
        return None
    # z_streamp is already a pointer typedef; don't double-&mut.
    if s == "z_streamp":
        return f"&mut {resolved}"
    if is_ptr:
        return f"&mut {resolved}"
    return resolved


# ---------------------------------------------------------------------------
# Audit findings
# ---------------------------------------------------------------------------

@dataclass
class AuditFinding:
    module: str
    fn_name: str
    param_name: str = ""       # empty when the finding is about return type
    issue: str = ""
    current: str = ""
    expected: str = ""
    severity: str = "warn"     # "warn" | "error"
    auto_fix: bool = False


@dataclass
class AuditReport:
    findings: list[AuditFinding] = field(default_factory=list)
    c_signatures_scanned: int = 0
    specs_audited: int = 0

    def errors(self) -> list[AuditFinding]:
        return [f for f in self.findings if f.severity == "error"]

    def warnings(self) -> list[AuditFinding]:
        return [f for f in self.findings if f.severity == "warn"]

    def summary(self) -> str:
        return (
            f"audited {self.specs_audited} specs against "
            f"{self.c_signatures_scanned} C signatures — "
            f"{len(self.errors())} errors, {len(self.warnings())} warnings"
        )


# ---------------------------------------------------------------------------
# The audit itself
# ---------------------------------------------------------------------------

def audit_module(
    module: ModuleSpec,
    c_signatures: dict[str, tuple[str, str, str]],
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    module_role = _infer_module_role(module.name)
    for alg in module.algorithms or []:
        sig = c_signatures.get(alg.name)
        if sig is None:
            # No C signature found — extractor may have hallucinated the
            # function name, or it's a macro expansion. Flag it.
            findings.append(AuditFinding(
                module=module.name,
                fn_name=alg.name,
                issue="c_signature_missing",
                current="<spec only>",
                expected=f"a C function named `{alg.name}` in source",
                severity="warn",
            ))
            continue
        c_ret, c_first_param, c_file = sig
        # Check first-param type vs spec's first input
        if alg.inputs:
            first = alg.inputs[0]
            expected = _c_first_param_to_rust(c_first_param, module_role)
            if expected and first.rust_type and first.rust_type != expected:
                # If the only difference is mutability and the spec side
                # is the MORE RESTRICTIVE (&T vs &mut T), keep the spec's
                # choice. The C pointer type doesn't distinguish read-only
                # from read-write; specs authored by the hardport or a
                # human review may know better than the auditor's default.
                if not _only_mutability_diff(first.rust_type, expected):
                    findings.append(AuditFinding(
                        module=module.name,
                        fn_name=alg.name,
                        param_name=first.name,
                        issue="first_param_type_mismatch",
                        current=first.rust_type,
                        expected=expected,
                        severity="error",
                        auto_fix=True,
                    ))
        # Check return type vs spec's return_type. Auto-fix only the
        # UNAMBIGUOUS cases — notably detect_data_type's `int` return
        # that the LLM inferred as `u8`. Size-class flips (u32↔u64) are
        # ambiguous on zlib's `uLong` (which is 32-bit on LP64 Windows
        # but 64-bit on Unix) and the spec's value is often correct —
        # skip those to avoid breaking hardports that already use u32.
        spec_ret = (alg.return_type or "").strip()
        expected_ret = _c_return_to_rust(c_ret)
        if expected_ret and spec_ret and spec_ret != expected_ret:
            if _is_unambiguous_return_fix(spec_ret, expected_ret, c_ret=c_ret):
                findings.append(AuditFinding(
                    module=module.name,
                    fn_name=alg.name,
                    param_name="",
                    issue="return_type_mismatch",
                    current=spec_ret,
                    expected=expected_ret,
                    severity="error",
                    auto_fix=True,
                ))
    return findings


def _is_unambiguous_return_fix(
    spec_ret: str, expected_ret: str, c_ret: str = ""
) -> bool:
    """Decide whether auto-correcting spec_ret → expected_ret is safe.

    SAFE to fix:
      bool → i32    (status codes)
      u8/u16 → i32  (spec narrowed an `int` to a smaller unsigned)
      X → ()        (spec invented a return for a void fn)
      u32 ↔ u64 WHEN c_ret is z_word_t (platform type fixed to 64-bit for
                    x86_64/aarch64 builds; LLM routinely mis-guesses as u32)

    UNSAFE (skip):
      u32 ↔ u64     (ambiguous for uLong — varies by platform)
      Result/Option/& wrappers     (higher-level types the auditor can't judge)
    """
    s, e = spec_ret.strip(), expected_ret.strip()
    c = (c_ret or "").strip()
    if any(x in s for x in ("Result", "Option", "<", "&")):
        return False
    # z_word_t is always u64 on the 64-bit builds we target.
    if c == "z_word_t" and {s, e} == {"u32", "u64"}:
        return True
    # Cross-width unsigned-signed-unsigned flips are noisy; skip.
    if {s, e} == {"u32", "u64"} or {s, e} == {"u64", "u32"}:
        return False
    if {s, e} == {"i32", "i64"} or {s, e} == {"i64", "i32"}:
        return False
    # Cases we DO want to fix.
    if s in ("bool", "u8", "u16") and e == "i32":
        return True
    if s in ("u32", "u64", "i32", "i64", "bool") and e == "()":
        return True
    # Conservative default: skip everything else.
    return False


def _only_mutability_diff(current: str, expected: str) -> bool:
    """True when `current` and `expected` differ ONLY in `&mut T` vs `&T`.

    C pointer parameters don't encode whether the callee mutates, so an
    auditor that always outputs `&mut T` can't distinguish observers from
    mutators. Specs that already specify `&T` (e.g., detect_data_type)
    are more precise than the default — trust them.
    """
    def normalize(s: str) -> str:
        return s.replace("&mut ", "").replace("&", "").strip()
    return normalize(current) == normalize(expected) and current != expected


def audit_all(
    modules: list[ModuleSpec],
    subject_c_root: Path,
) -> AuditReport:
    sigs = _scan_c_signatures(subject_c_root)
    report = AuditReport(c_signatures_scanned=len(sigs))
    for m in modules:
        report.findings.extend(audit_module(m, sigs))
        report.specs_audited += len(m.algorithms or [])
    return report


def apply_auto_fixes(
    modules: list[ModuleSpec], report: AuditReport,
) -> list[ModuleSpec]:
    """Apply the auto-fixable findings in place (returning new modules)."""
    fixes_by_fn: dict[tuple[str, str], AuditFinding] = {
        (f.module, f.fn_name): f
        for f in report.findings
        if f.auto_fix
    }
    # Index ALL fixable findings per (module, fn), not just first-param.
    fixes_by_key: dict[tuple[str, str], list[AuditFinding]] = {}
    for f in report.findings:
        if f.auto_fix:
            fixes_by_key.setdefault((f.module, f.fn_name), []).append(f)
    out: list[ModuleSpec] = []
    for m in modules:
        new_algs: list[AlgorithmSpec] = []
        for alg in m.algorithms or []:
            fs = fixes_by_key.get((m.name, alg.name), [])
            alg = _apply_algorithm_fixes(alg, fs)
            new_algs.append(alg)
        out.append(m.model_copy(update={"algorithms": new_algs}))
    return out


def _apply_algorithm_fixes(
    alg: AlgorithmSpec, findings: list[AuditFinding],
) -> AlgorithmSpec:
    updates: dict = {}
    new_inputs = list(alg.inputs or [])
    for f in findings:
        if f.issue == "first_param_type_mismatch" and new_inputs:
            new_inputs[0] = new_inputs[0].model_copy(
                update={"rust_type": f.expected},
            )
        elif f.issue == "return_type_mismatch":
            updates["return_type"] = f.expected
    if new_inputs != list(alg.inputs or []):
        updates["inputs"] = new_inputs
    return alg.model_copy(update=updates) if updates else alg


def format_report(report: AuditReport) -> str:
    lines = [report.summary(), ""]
    for sev in ("error", "warn"):
        bucket = [f for f in report.findings if f.severity == sev]
        if not bucket:
            continue
        lines.append(f"== {sev.upper()}S ==")
        for f in bucket:
            loc = f"{f.module}::{f.fn_name}"
            if f.param_name:
                loc += f"::{f.param_name}"
            fix = " [auto-fix]" if f.auto_fix else ""
            lines.append(
                f"  {loc}: {f.issue}{fix}"
                f"  {f.current!r} -> {f.expected!r}"
            )
        lines.append("")
    return "\n".join(lines)
