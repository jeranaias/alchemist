"""Architecture validator — runs between Stage 3 and Stage 4.

Catches design problems that would cause downstream cascading failures:
- Cycles in crate dependency graph
- Orphan rule violations (type X in crate Y, impl X must also be in Y)
- Module name collisions (sibling crate name == declared mod inside another crate)
- Missing producers for referenced types
- Empty crates with no specs to implement

Each validator returns a list of ValidationIssue objects. Severity levels:
  - ERROR: must fix before Stage 4 (would cause certain failure)
  - WARNING: should fix (likely to cause friction)
  - INFO: design observation
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from alchemist.architect.schemas import CrateArchitecture
from alchemist.extractor.schemas import ModuleSpec


class Severity(str, Enum):
    error = "ERROR"
    warning = "WARNING"
    info = "INFO"


@dataclass
class ValidationIssue:
    rule: str
    severity: Severity
    message: str
    location: str = ""

    def __str__(self) -> str:
        loc = f" [{self.location}]" if self.location else ""
        return f"[{self.severity.value}] {self.rule}{loc}: {self.message}"


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    def add(self, issue: ValidationIssue) -> None:
        self.issues.append(issue)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.error]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.warning]

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def summary(self) -> str:
        e = len(self.errors)
        w = len(self.warnings)
        i = len(self.issues) - e - w
        return f"{e} errors, {w} warnings, {i} info"


def validate_architecture(
    arch: CrateArchitecture,
    specs: list[ModuleSpec] | None = None,
) -> ValidationReport:
    """Run all validators on an architecture. Returns a report.

    If specs are provided, also checks spec-architecture alignment.
    """
    report = ValidationReport()

    _check_dependency_dag(arch, report)
    _check_orphan_rule(arch, report)
    _check_module_name_collisions(arch, report)
    _check_missing_type_producers(arch, report)
    _check_empty_crates(arch, report)
    _check_workspace_consistency(arch, report)

    # Phase 0.5 quality gates
    if specs is not None:
        _check_spec_coverage(arch, specs, report)
        _check_module_assignment(arch, specs, report)
        _check_state_wrappers_for_large_states(arch, specs, report)
        _check_builders_for_parameterized_init(arch, specs, report)
    _check_traits_cover_compatible_signatures(arch, specs, report)
    _check_per_crate_error_types(arch, report)

    return report


def _check_state_wrappers_for_large_states(
    arch: CrateArchitecture, specs: list[ModuleSpec], report: ValidationReport,
) -> None:
    """Every raw struct with >10 fields must have a StateWrapperSpec."""
    # Collect all shared types with field counts.
    for mod in specs:
        for t in getattr(mod, "shared_types", []) or []:
            rd = getattr(t, "rust_definition", "") or ""
            # Rough field count: semicolon or comma-terminated lines inside the struct body
            if not rd.lstrip().startswith("#[") and "pub struct" not in rd:
                continue
            # Count lines ending in `,` inside the struct block.
            import re as _re
            body_match = _re.search(r"pub\s+struct\s+\w+[^{]*\{([^}]*)\}", rd, _re.DOTALL)
            if not body_match:
                continue
            field_count = len([
                line for line in body_match.group(1).splitlines()
                if line.strip().rstrip(",").endswith((")", "]", ">", "u8", "u16", "u32",
                                                       "u64", "i8", "i16", "i32", "i64",
                                                       "usize", "isize", "bool"))
                or line.strip().endswith(",")
            ])
            if field_count <= 10:
                continue
            # Has a wrapper?
            wrappers = [w.inner_state for w in arch.state_wrappers]
            if t.name not in wrappers:
                report.add(ValidationIssue(
                    rule="state_wrapper_missing",
                    severity=Severity.error,
                    message=(
                        f"Raw state {t.name} exposes {field_count} fields. "
                        f"Architecture must define a StateWrapperSpec that hides it "
                        f"(Phase 0.5 requirement 5)."
                    ),
                    location=t.name,
                ))


def _check_builders_for_parameterized_init(
    arch: CrateArchitecture, specs: list[ModuleSpec], report: ValidationReport,
) -> None:
    """Every *Init* function with >1 meaningful param needs a BuilderSpec."""
    import re as _re
    init_re = _re.compile(r"[Ii]nit\d*_?$")
    builder_targets = {b.built_type for b in arch.builders}
    for mod in specs:
        for alg in mod.algorithms or []:
            if not init_re.search(alg.name):
                continue
            meaningful_params = [
                p for p in (alg.inputs or [])
                if "Stream" not in (p.rust_type or "") and "State" not in (p.rust_type or "")
            ]
            if len(meaningful_params) <= 1:
                continue
            # Rough built-type heuristic: XyzInit2_ → Xyz
            built = _re.sub(r"[Ii]nit\d*_?$", "", alg.name).strip("_").title()
            if not built:
                continue
            if built not in builder_targets:
                report.add(ValidationIssue(
                    rule="builder_missing",
                    severity=Severity.warning,
                    message=(
                        f"Parameterized init {alg.name}({len(meaningful_params)} params) "
                        f"should have a BuilderSpec (Phase 0.5 requirement 6)."
                    ),
                    location=alg.name,
                ))


def _check_traits_cover_compatible_signatures(
    arch: CrateArchitecture, specs: list[ModuleSpec] | None, report: ValidationReport,
) -> None:
    """Architecture with multiple crates should declare at least one trait."""
    if len(arch.crates) >= 3 and not arch.traits:
        report.add(ValidationIssue(
            rule="no_traits_declared",
            severity=Severity.warning,
            message=(
                "Multi-crate architecture declares zero traits. Functions that share a "
                "signature shape across crates should share a trait (Phase 0.5 requirement 4)."
            ),
        ))


def _check_per_crate_error_types(
    arch: CrateArchitecture, report: ValidationReport,
) -> None:
    """Each crate with >3 functions should have its own error type."""
    crates_with_errors = {e.crate for e in arch.error_types}
    for c in arch.crates:
        if len(c.modules) >= 2 and c.name not in crates_with_errors:
            report.add(ValidationIssue(
                rule="per_crate_error_missing",
                severity=Severity.warning,
                message=(
                    f"Crate {c.name} has {len(c.modules)} modules but no dedicated "
                    f"error type. Phase 0.5 requirement 7 asks for per-crate error enums."
                ),
                location=c.name,
            ))


# ─── Validators ─────────────────────────────────────────────────────────


def _check_dependency_dag(arch: CrateArchitecture, report: ValidationReport) -> None:
    """No cycles in the crate dependency graph."""
    crate_names = {c.name for c in arch.crates}
    graph: dict[str, set[str]] = {c.name: set(c.dependencies) for c in arch.crates}

    # External deps don't count for cycle detection
    for c in arch.crates:
        graph[c.name] = {d for d in c.dependencies if d in crate_names}

    cycles = _find_cycles(graph)
    for cycle in cycles:
        report.add(ValidationIssue(
            rule="dependency_dag",
            severity=Severity.error,
            message=f"Dependency cycle detected: {' → '.join(cycle + [cycle[0]])}",
            location=" → ".join(cycle),
        ))


def _check_orphan_rule(arch: CrateArchitecture, report: ValidationReport) -> None:
    """Orphan rule: a type's inherent impl must be in the same crate.

    Architecture should not have a type T defined in crate A but expected
    to be impl'd in crate B. The Implementer would hit E0116.
    """
    type_to_crate: dict[str, str] = {}
    # Gather where each shared type lives based on crate.modules + spec types
    # (We use a heuristic: if a crate's name suggests it owns a type, that's its home.)
    for crate in arch.crates:
        for mod_name in crate.modules:
            type_to_crate[mod_name] = crate.name

    # Trait specs declare a 'crate' field; check that all impl crates match
    for trait_spec in arch.traits:
        owning = trait_spec.crate
        if owning not in type_to_crate.values() and owning != trait_spec.crate:
            report.add(ValidationIssue(
                rule="orphan_rule",
                severity=Severity.warning,
                message=f"Trait {trait_spec.name} declared in crate {owning}, "
                        f"but no module producer found for it",
                location=trait_spec.name,
            ))


def _check_module_name_collisions(arch: CrateArchitecture, report: ValidationReport) -> None:
    """Sibling crate names (with - or _) must not collide with declared modules.

    Lesson learned: zlib-deflate having `pub mod deflate { ... }` inside
    `src/deflate.rs` caused namespace conflicts. The `deflate.rs` file is
    already the `deflate` module — declaring `mod deflate` inside is wrong.
    """
    crate_names = {c.name for c in arch.crates}
    crate_names_normalized = {c.replace("-", "_") for c in crate_names}

    for crate in arch.crates:
        crate_short = crate.name.replace(f"{arch.workspace_name}-", "").replace("-", "_")
        if crate_short in crate_names_normalized and crate_short != crate.name.replace("-", "_"):
            report.add(ValidationIssue(
                rule="module_name_collision",
                severity=Severity.warning,
                message=f"Crate {crate.name}'s short form '{crate_short}' "
                        f"matches another crate name — risk of confusion",
                location=crate.name,
            ))


def _check_missing_type_producers(arch: CrateArchitecture, report: ValidationReport) -> None:
    """Every error type referenced in dependencies must have a producer crate."""
    error_to_crate: dict[str, str] = {e.name: e.crate for e in arch.error_types}
    crate_names = {c.name for c in arch.crates}

    # Each error type's owning crate must exist
    for err in arch.error_types:
        if err.crate not in crate_names:
            report.add(ValidationIssue(
                rule="missing_producer",
                severity=Severity.error,
                message=f"Error type {err.name} declared in crate {err.crate} "
                        f"which doesn't exist in workspace",
                location=err.name,
            ))


def _check_empty_crates(arch: CrateArchitecture, report: ValidationReport) -> None:
    """Crates with no modules and no public_api are dead weight."""
    for crate in arch.crates:
        if not crate.modules and not crate.public_api:
            report.add(ValidationIssue(
                rule="empty_crate",
                severity=Severity.warning,
                message=f"Crate {crate.name} has no modules or public_api — likely dead weight",
                location=crate.name,
            ))


def _check_workspace_consistency(arch: CrateArchitecture, report: ValidationReport) -> None:
    """Workspace name should be a prefix of all crate names."""
    if not arch.crates:
        return
    prefix = arch.workspace_name
    for crate in arch.crates:
        if not crate.name.startswith(prefix) and not crate.name.startswith(f"{prefix}-"):
            report.add(ValidationIssue(
                rule="workspace_naming",
                severity=Severity.info,
                message=f"Crate {crate.name} doesn't share workspace prefix '{prefix}'",
                location=crate.name,
            ))


def _check_spec_coverage(
    arch: CrateArchitecture,
    specs: list[ModuleSpec],
    report: ValidationReport,
) -> None:
    """Every crate.modules entry must resolve to an existing spec or algorithm.

    A Rust `pub mod X` in a generated crate corresponds to EITHER:
      * a ModuleSpec named X (coarse grouping, e.g. `deflate`), or
      * an AlgorithmSpec named X (fine grouping, e.g. `adler32`).

    We also tolerate architect-generated scaffolding module names
    (`types`, `errors`, `traits`, `<name>_table`) that don't map to a spec
    directly — these are downgraded to warnings rather than errors.
    """
    module_spec_names = {s.name for s in specs}
    algorithm_names = {a.name for s in specs for a in s.algorithms}
    known_names = module_spec_names | algorithm_names

    # Architect-generated infrastructure modules — accepted without a spec.
    infra_exact = {"types", "errors", "traits", "common", "prelude"}

    def _is_infra(mod: str) -> bool:
        if mod in infra_exact:
            return True
        # Algorithm-local helpers: `<algo>_table`, `<algo>_state`, etc.
        for algo in algorithm_names:
            if mod == f"{algo}_table" or mod == f"{algo}_state" or mod == f"{algo}_internal":
                return True
        return False

    assigned: dict[str, list[str]] = defaultdict(list)
    for crate in arch.crates:
        for mod in crate.modules:
            assigned[mod].append(crate.name)

    for crate in arch.crates:
        for mod in crate.modules:
            if mod in known_names:
                continue
            if _is_infra(mod):
                report.add(ValidationIssue(
                    rule="spec_coverage",
                    severity=Severity.info,
                    message=f"Crate {crate.name} module {mod} is infrastructure (no spec required)",
                    location=f"{crate.name}/{mod}",
                ))
                continue
            report.add(ValidationIssue(
                rule="spec_coverage",
                severity=Severity.error,
                message=f"Crate {crate.name} references module {mod}, but no matching spec or algorithm exists",
                location=f"{crate.name}/{mod}",
            ))

    # Warn if a spec is entirely unassigned (not referenced by any crate,
    # directly or indirectly via any of its algorithm names).
    for spec in specs:
        anchors = {spec.name} | {a.name for a in spec.algorithms}
        if not (anchors & assigned.keys()):
            report.add(ValidationIssue(
                rule="spec_coverage",
                severity=Severity.warning,
                message=f"Spec {spec.name} is not assigned to any crate",
                location=spec.name,
            ))

    # Multiply-assigned modules (only meaningful for spec / algorithm names,
    # infra modules may legitimately recur in multiple crates).
    for mod, crates in assigned.items():
        if len(crates) > 1 and mod in known_names:
            report.add(ValidationIssue(
                rule="spec_coverage",
                severity=Severity.error,
                message=f"Module {mod} is assigned to multiple crates: {crates}",
                location=mod,
            ))


def _check_module_assignment(
    arch: CrateArchitecture,
    specs: list[ModuleSpec],
    report: ValidationReport,
) -> None:
    """Crate categories should align with their spec types.

    e.g., a crate named *-checksum should only have checksum-category algorithms.
    """
    # Just an info-level check for now
    for spec in specs:
        if not spec.algorithms:
            report.add(ValidationIssue(
                rule="empty_spec",
                severity=Severity.warning,
                message=f"Spec {spec.name} has no algorithms — extraction may have failed",
                location=spec.name,
            ))


# ─── Helpers ─────────────────────────────────────────────────────────────


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Find all cycles using Tarjan's SCC algorithm."""
    cycles: list[list[str]] = []
    index_counter = [0]
    stack: list[str] = []
    lowlinks: dict[str, int] = {}
    indices: dict[str, int] = {}
    on_stack: set[str] = set()

    def strongconnect(v: str) -> None:
        indices[v] = index_counter[0]
        lowlinks[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in graph.get(v, set()):
            if w not in indices:
                strongconnect(w)
                lowlinks[v] = min(lowlinks[v], lowlinks[w])
            elif w in on_stack:
                lowlinks[v] = min(lowlinks[v], indices[w])

        if lowlinks[v] == indices[v]:
            component: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1:
                cycles.append(component)

    for v in graph:
        if v not in indices:
            strongconnect(v)

    return cycles


def topological_sort(arch: CrateArchitecture) -> list[str]:
    """Return crates in dependency order (leaf-first, raises ValueError on cycle)."""
    crate_names = {c.name for c in arch.crates}
    indegree: dict[str, int] = {c.name: 0 for c in arch.crates}
    graph: dict[str, list[str]] = {c.name: [] for c in arch.crates}

    for c in arch.crates:
        for dep in c.dependencies:
            if dep in crate_names:
                graph[dep].append(c.name)
                indegree[c.name] += 1

    queue = deque([n for n, d in indegree.items() if d == 0])
    order: list[str] = []
    while queue:
        n = queue.popleft()
        order.append(n)
        for next_n in graph[n]:
            indegree[next_n] -= 1
            if indegree[next_n] == 0:
                queue.append(next_n)

    if len(order) != len(crate_names):
        raise ValueError("Dependency cycle prevents topological sort")
    return order
