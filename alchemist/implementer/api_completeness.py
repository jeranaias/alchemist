"""API completeness checker.

For every `AlgorithmSpec.source_functions` entry, verify a matching
`pub fn <name>` exists somewhere under the expected crate. When functions
are missing, report them in a form that the implementer / re-prompt loop
can act on.

This plugs into the Phase B TDD flow as a guard that runs after code
generation but before declaring the crate complete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from alchemist.architect.schemas import CrateArchitecture, CrateSpec
from alchemist.extractor.schemas import AlgorithmSpec, ModuleSpec


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MissingFunction:
    crate: str
    module: str
    algorithm: str
    c_function: str
    spec_hint: str = ""  # brief description for re-prompt context

    def __str__(self) -> str:
        return f"{self.crate}/{self.module}::{self.c_function}  (algorithm {self.algorithm!r})"


@dataclass
class ApiCompletenessReport:
    expected: int = 0
    found: int = 0
    missing: list[MissingFunction] = field(default_factory=list)
    # For diagnostics: map crate → list of public fns found there
    public_fns_per_crate: dict[str, set[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return len(self.missing) == 0

    def summary(self) -> str:
        if self.ok:
            return f"API complete: {self.found}/{self.expected} expected public functions present"
        lines = [f"API INCOMPLETE: {len(self.missing)} missing functions (of {self.expected} expected)"]
        for m in self.missing[:50]:
            lines.append(f"  - {m}")
        if len(self.missing) > 50:
            lines.append(f"  ... and {len(self.missing) - 50} more")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

_PUB_FN_RE = re.compile(
    r"""
    (?:^|\n)                      # line start
    [\t ]*                        # leading whitespace
    pub                           # pub keyword
    (?:\s*\(\s*crate\s*\)|        # optional pub(crate) (no whitespace required)
       \s*\(\s*super\s*\)|
       \s*\(\s*in\s+[^\)]+\))?
    \s+
    (?:async\s+|const\s+|unsafe\s+|extern\s+(?:"[^"]*"\s+)?)*
    fn\s+(?P<name>\w+)\s*
    """,
    re.VERBOSE,
)


def collect_public_fns(crate_dir: Path) -> set[str]:
    """Return the set of `pub fn <name>` names defined anywhere in the crate."""
    out: set[str] = set()
    src = crate_dir / "src"
    if not src.exists():
        return out
    for rs in src.rglob("*.rs"):
        text = rs.read_text(encoding="utf-8", errors="replace")
        # Strip line/block comments
        text = re.sub(r"//[^\n]*", "", text)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        for m in _PUB_FN_RE.finditer(text):
            out.add(m.group("name"))
    return out


def _snake(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and (name[i - 1].islower() or name[i - 1].isdigit()):
            out.append("_")
        out.append(ch.lower())
    return re.sub(r"_+", "_", "".join(out)).strip("_")


def _variants(name: str) -> set[str]:
    """Candidate public function names the model might use for a C function.

    Handles common renamings:
      * C `crc32_combine` → Rust `crc32_combine` or `combine` or `Crc32::combine`.
      * We cannot detect method-on-type here (that's another layer); we just
        match free functions.
    """
    base = name.strip()
    cands = {base, _snake(base)}
    # Strip common C prefixes
    if base.startswith("z_"):
        cands.add(base[2:])
    if base.startswith("Z_"):
        cands.add(_snake(base[2:]))
    # Strip `_cb` suffix etc — less common, omit for now
    return {c for c in cands if c}


def check_crate(
    crate_spec: CrateSpec,
    module_specs: list[ModuleSpec],
    workspace_dir: Path,
) -> ApiCompletenessReport:
    """Check API completeness for one crate."""
    crate_dir = workspace_dir / crate_spec.name
    report = ApiCompletenessReport()
    pub_fns = collect_public_fns(crate_dir)
    report.public_fns_per_crate[crate_spec.name] = pub_fns
    crate_modules = {m.name for m in module_specs if m.name in set(crate_spec.modules)}
    for module in module_specs:
        if module.name not in crate_modules:
            continue
        for alg in module.algorithms:
            expected_sources = alg.source_functions or [alg.name]
            for src_fn in expected_sources:
                report.expected += 1
                if _variants(src_fn) & pub_fns:
                    report.found += 1
                else:
                    report.missing.append(MissingFunction(
                        crate=crate_spec.name,
                        module=module.name,
                        algorithm=alg.name,
                        c_function=src_fn,
                        spec_hint=alg.description[:160] if alg.description else "",
                    ))
    return report


def check_workspace(
    specs: list[ModuleSpec],
    architecture: CrateArchitecture,
    workspace_dir: Path,
) -> ApiCompletenessReport:
    """Check API completeness across the whole workspace."""
    combined = ApiCompletenessReport()
    for crate in architecture.crates:
        sub = check_crate(crate, specs, workspace_dir)
        combined.expected += sub.expected
        combined.found += sub.found
        combined.missing.extend(sub.missing)
        combined.public_fns_per_crate.update(sub.public_fns_per_crate)
    return combined


def missing_to_reprompt_context(missing: list[MissingFunction]) -> str:
    """Render a missing-function list into a paragraph suitable for re-prompting."""
    if not missing:
        return "No missing functions."
    lines = ["The following functions from the spec are missing `pub fn` definitions:"]
    for m in missing:
        line = f"- {m.c_function} (algorithm: {m.algorithm}, crate: {m.crate}, module: {m.module})"
        if m.spec_hint:
            line += f" — {m.spec_hint}"
        lines.append(line)
    lines.append(
        "Implement EACH as a `pub fn` in the matching module file. "
        "Do not stub or simulate — implement the actual algorithm."
    )
    return "\n".join(lines)
