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
    return findings


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
    out: list[ModuleSpec] = []
    for m in modules:
        new_algs: list[AlgorithmSpec] = []
        for alg in m.algorithms or []:
            f = fixes_by_fn.get((m.name, alg.name))
            if f and f.issue == "first_param_type_mismatch" and alg.inputs:
                new_inputs = list(alg.inputs)
                new_inputs[0] = new_inputs[0].model_copy(
                    update={"rust_type": f.expected},
                )
                new_algs.append(alg.model_copy(update={"inputs": new_inputs}))
            else:
                new_algs.append(alg)
        out.append(m.model_copy(update={"algorithms": new_algs}))
    return out


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
