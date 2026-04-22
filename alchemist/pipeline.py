"""Pipeline orchestrator — sequences the 6 stages, manages checkpoints."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from alchemist.analyzer.parser import CParser
from alchemist.analyzer.call_graph import CallGraphBuilder
from alchemist.analyzer.module_detector import ModuleDetector
from alchemist.config import AlchemistConfig

console = Console(force_terminal=True, legacy_windows=False)


def run_analyze(
    source: Path,
    preprocessed: bool = False,
    config: AlchemistConfig | None = None,
) -> dict:
    """Run Stage 1: Analyze a C/C++ codebase.

    Returns a dict with keys: files, call_graph, modules, summary.
    """
    config = config or AlchemistConfig()
    source = Path(source).resolve()

    if not source.is_dir():
        console.print(f"[red]Source path {source} is not a directory.[/red]")
        raise SystemExit(1)

    # Discover C/C++ files
    c_extensions = {".c", ".h", ".cpp", ".hpp", ".cc", ".cxx"}
    all_files = sorted(
        f for f in source.rglob("*")
        if f.suffix in c_extensions
        and ".git" not in f.parts
        and "test" not in f.name.lower()  # skip test files for now
    )

    # Filter to only .c and .h in the root (not contrib/test dirs for zlib)
    # Heuristic: if source has subdirs like contrib/, test/, only parse root-level files
    root_files = [f for f in all_files if f.parent == source]
    if root_files:
        parse_files = root_files
    else:
        parse_files = all_files

    console.print(f"[cyan]Analyzing {len(parse_files)} files in {source}[/cyan]")

    # Parse all files
    parser = CParser()
    parsed_files = {}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Parsing C files...", total=len(parse_files))
        for f in parse_files:
            parsed = parser.parse_file(f)
            parsed_files[str(f)] = parsed
            progress.update(task, advance=1, description=f"Parsing {f.name}")

    # Build call graph
    console.print("[cyan]Building call graph...[/cyan]")
    cg_builder = CallGraphBuilder()
    call_graph = cg_builder.build(parsed_files)

    # Detect modules
    console.print("[cyan]Detecting algorithmic modules...[/cyan]")
    detector = ModuleDetector()
    modules = detector.detect(parsed_files, call_graph)

    # Build summary
    total_functions = sum(len(pf["functions"]) for pf in parsed_files.values())
    total_structs = sum(len(pf["structs"]) for pf in parsed_files.values())
    total_globals = sum(len(pf["globals"]) for pf in parsed_files.values())
    total_macros = sum(len(pf["macros"]) for pf in parsed_files.values())
    total_typedefs = sum(len(pf["typedefs"]) for pf in parsed_files.values())
    total_lines = sum(pf["line_count"] for pf in parsed_files.values())

    summary = {
        "total_files": len(parsed_files),
        "total_lines": total_lines,
        "total_functions": total_functions,
        "total_structs": total_structs,
        "total_globals": total_globals,
        "total_macros": total_macros,
        "total_typedefs": total_typedefs,
    }

    # Print summary
    console.print(f"\n  Files: {summary['total_files']}")
    console.print(f"  Lines: {summary['total_lines']:,}")
    console.print(f"  Functions: {summary['total_functions']}")
    console.print(f"  Structs: {summary['total_structs']}")
    console.print(f"  Globals: {summary['total_globals']}")
    console.print(f"  Macros: {summary['total_macros']}")
    console.print(f"  Modules detected: {len(modules)}")

    for mod in modules:
        console.print(
            f"    [yellow]{mod['name']}[/yellow] ({mod['category']}) — "
            f"{len(mod['functions'])} functions, {mod['total_lines']} lines"
        )

    return {
        "source": str(source),
        "files": {k: _serialize_parsed(v) for k, v in parsed_files.items()},
        "call_graph": call_graph,
        "modules": modules,
        "summary": summary,
    }


def _serialize_parsed(pf: dict) -> dict:
    """Make parsed file data JSON-serializable."""
    return {
        "functions": [
            {k: v for k, v in f.items() if k != "node"}
            for f in pf["functions"]
        ],
        "structs": pf["structs"],
        "globals": pf["globals"],
        "macros": pf["macros"],
        "typedefs": pf["typedefs"],
        "includes": pf["includes"],
        "line_count": pf["line_count"],
    }


# ---------------------------------------------------------------------------
# Integrated pipeline — wires every Phase C check
# ---------------------------------------------------------------------------

@dataclass
class StageOutcome:
    stage: str
    ok: bool
    summary: str = ""
    details: str = ""


@dataclass
class TranslationReport:
    workspace_dir: Path
    outcomes: list[StageOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(o.ok for o in self.outcomes)

    def add(self, outcome: StageOutcome) -> None:
        self.outcomes.append(outcome)

    def first_failure(self) -> StageOutcome | None:
        for o in self.outcomes:
            if not o.ok:
                return o
        return None

    def summary(self) -> str:
        lines = []
        for o in self.outcomes:
            mark = "PASS" if o.ok else "FAIL"
            lines.append(f"[{mark:4}] {o.stage}: {o.summary}")
        return "\n".join(lines) + "\n" + ("OVERALL: PASS" if self.ok else "OVERALL: FAIL")


def run_architect_stage(
    source: Path,
    name: str,
    config: AlchemistConfig | None = None,
    *,
    enforce: bool = True,
) -> tuple[StageOutcome, "CrateArchitecture | None"]:
    """Run Stage 3 — design architecture AND validate it.

    If `enforce=True` and the validator finds any ERRORs, refuses to proceed
    and returns StageOutcome(ok=False).
    """
    from alchemist.architect.crate_designer import CrateDesigner
    from alchemist.architect.schemas import CrateArchitecture
    from alchemist.architect.validator import validate_architecture
    from alchemist.extractor.schemas import ModuleSpec

    specs_dir = source / ".alchemist" / "specs"
    if not specs_dir.exists():
        return StageOutcome(
            stage="architect",
            ok=False,
            summary=f"specs not found at {specs_dir}",
        ), None
    specs = [
        ModuleSpec.model_validate(json.loads(f.read_text(encoding="utf-8")))
        for f in sorted(specs_dir.glob("*.json"))
    ]
    designer = CrateDesigner(config or AlchemistConfig())
    arch = designer.design(specs, project_name=name, source_description=str(source))

    # Post-architect trait extraction: fill in traits for compatible-signature
    # families the architect might have missed. Phase 0.5 requirement 4.
    from alchemist.architect.trait_extractor import extract_traits
    new_traits = extract_traits(specs, arch)
    if new_traits:
        arch.traits = list(arch.traits) + new_traits
        console.print(
            f"[cyan]trait extractor: added {len(new_traits)} trait(s): "
            f"{', '.join(t.name for t in new_traits)}[/cyan]"
        )

    (source / ".alchemist" / "architecture.json").write_text(
        arch.model_dump_json(indent=2), encoding="utf-8"
    )

    report = validate_architecture(arch, specs)
    details = "\n".join(str(i) for i in report.issues)
    if report.has_errors and enforce:
        return StageOutcome(
            stage="architect",
            ok=False,
            summary=f"validator rejected architecture: {report.summary()}",
            details=details,
        ), arch
    return StageOutcome(
        stage="architect",
        ok=True,
        summary=f"architecture validated: {report.summary()}",
        details=details,
    ), arch


def run_implement_stage(
    source: Path,
    output: Path,
    *,
    tdd: bool = True,
    config: AlchemistConfig | None = None,
) -> StageOutcome:
    """Run Stage 4 — generate Rust code.

    When `tdd=True` (default), uses the TDD generator with skeleton,
    test emission, per-function loop, and API completeness gate. Also
    runs the field scanner to pre-populate shared type schemas.
    """
    from alchemist.architect.field_scanner import scan_specs_for_fields
    from alchemist.architect.schemas import CrateArchitecture
    from alchemist.extractor.schemas import ModuleSpec

    specs_dir = source / ".alchemist" / "specs"
    arch_path = source / ".alchemist" / "architecture.json"
    if not arch_path.exists():
        return StageOutcome(
            stage="implement",
            ok=False,
            summary="architecture.json missing — run Stage 3 first",
        )
    specs = [
        ModuleSpec.model_validate(json.loads(f.read_text(encoding="utf-8")))
        for f in sorted(specs_dir.glob("*.json"))
    ]
    # Spec completer: merge orphan per-function specs from
    # specs/_functions/<module>/*.json into each module's algorithms list.
    # Spec extraction sometimes produces a function spec but fails to fold
    # it into the aggregated module, so the architect never sees it and
    # generated code that references the helper fails to compile.
    fn_dir = specs_dir / "_functions"
    if fn_dir.exists():
        added_total = 0
        for module in specs:
            per_fn_dir = fn_dir / module.name
            if not per_fn_dir.exists():
                continue
            present = {a.name for a in module.algorithms or []}
            new_algs = list(module.algorithms or [])
            for fn_json in sorted(per_fn_dir.glob("*.json")):
                data = json.loads(fn_json.read_text(encoding="utf-8"))
                name = data.get("name") or ""
                if not name or name in present:
                    continue
                data.setdefault("display_name", name)
                data.setdefault("description",
                                data.get("purpose") or data.get("algorithm_notes") or name)
                try:
                    from alchemist.extractor.schemas import AlgorithmSpec
                    new_algs.append(AlgorithmSpec.model_validate(data))
                    present.add(name)
                    added_total += 1
                except Exception:
                    continue
            if len(new_algs) != len(module.algorithms or []):
                module.algorithms = new_algs
        if added_total:
            console.print(
                f"[cyan]spec completer: merged {added_total} orphan function spec(s)[/cyan]"
            )
    # Normalize parameter types before generation. Fixes classes of extractor
    # drift (Vec<u8> output buffers → &mut [u8], u64 length pointers → &mut usize).
    from alchemist.extractor.normalizer import normalize_all
    specs, norm_notes = normalize_all(specs)
    if norm_notes:
        console.print(
            f"[cyan]spec normalizer: rewrote {len(norm_notes)} parameter(s)[/cyan]"
        )
    # Spec auditor: cross-check specs against the actual C source signatures.
    # Catches extractor-level errors (wrong state type, missing mutability)
    # that the normalizer's pattern rules don't reach. Auto-fixes safe cases.
    from alchemist.extractor.spec_auditor import (
        audit_all as audit_specs, apply_auto_fixes,
    )
    audit_report = audit_specs(specs, source)
    if audit_report.findings:
        console.print(
            f"[cyan]spec auditor: {audit_report.summary()}[/cyan]"
        )
        fixable = [f for f in audit_report.findings if f.auto_fix]
        if fixable:
            specs = apply_auto_fixes(specs, audit_report)
            console.print(
                f"[cyan]spec auditor: auto-fixed {len(fixable)} finding(s)[/cyan]"
            )
    # Constants auto-extractor: pull C #define / enum / static const into
    # each module's spec.constants so the skeleton can inject them as
    # `pub const` before the LLM sees the function stubs. Removes the
    # whole class of "undefined identifier" compile failures from LLM
    # referencing C constants it can't reproduce.
    try:
        from alchemist.extractor.constants_extractor import extract_from_path
        c_sources: dict[str, Path] = {
            p.stem: p for p in source.rglob("*.c")
            if "test" not in p.name.lower() and "example" not in p.name.lower()
        }
        total_consts = 0
        for module in specs:
            if module.constants:
                continue  # already populated (e.g., loaded from cache)
            c_file = c_sources.get(module.name)
            if c_file is None:
                continue
            try:
                report = extract_from_path(c_file)
                module.constants = report.extracted
                total_consts += report.count
            except Exception:  # noqa: BLE001
                continue
        if total_consts:
            console.print(
                f"[cyan]constants extractor: {total_consts} consts across "
                f"{len([m for m in specs if m.constants])} modules[/cyan]"
            )
    except Exception as e:  # noqa: BLE001
        console.print(f"[yellow]constants extractor skipped: {e}[/yellow]")
    arch = CrateArchitecture.model_validate(
        json.loads(arch_path.read_text(encoding="utf-8"))
    )

    # Field scanner: pre-populate shared-type field schemas.
    # The scanner's output is available to the TDD generator via the
    # ModuleSpec.shared_types list (augmented below).
    field_schemas = scan_specs_for_fields(specs, arch)
    if field_schemas:
        console.print(
            f"[cyan]field scanner: {len(field_schemas)} type schemas pre-scanned[/cyan]"
        )

    if tdd:
        from alchemist.implementer.tdd_generator import TDDGenerator
        gen = TDDGenerator(config=config)
        result = gen.generate_workspace(specs, arch, output, source_root=source)
        ok = bool(result.ok)
        summary = (
            f"TDD: {sum(1 for a in result.attempts if a.tests_passed)}/"
            f"{len(result.attempts)} fns pass tests; "
            f"API {'ok' if result.api_report and result.api_report.ok else 'incomplete'}"
        )
        return StageOutcome(stage="implement", ok=ok, summary=summary)
    else:
        from alchemist.implementer.code_generator import CodeGenerator
        gen = CodeGenerator(config=config)
        results = gen.generate_workspace(specs, arch, output)
        ok = all(r.get("success") for r in results.values()) if results else False
        return StageOutcome(
            stage="implement", ok=ok,
            summary=f"compiled {sum(1 for r in results.values() if r.get('success'))}/{len(results)} crates",
        )


def run_verify_stage(
    c_source_dir: Path,
    output: Path,
    diff_config=None,
    *,
    refuse_without_diff: bool = True,
) -> StageOutcome:
    """Run Stage 5 — mandatory differential verification gate.

    When diff_config is None and refuse_without_diff=True (the production
    default), the differential gate FAILS with reason 'no config'. This
    enforces the 'refuse success without verification' rule.
    """
    from alchemist.verifier.differential_tester import verify_workspace
    report = verify_workspace(
        output, diff_config=diff_config, refuse_without_diff=refuse_without_diff
    )
    ok = report.passed
    first = report.first_failure
    summary = "all gates PASS" if ok else (
        f"gate {first.name} FAILED: {first.summary}" if first else "unknown failure"
    )
    return StageOutcome(stage="verify", ok=ok, summary=summary)


def run_translate_all(
    source: Path,
    name: str,
    output: Path | None = None,
    *,
    config: AlchemistConfig | None = None,
    stages: tuple[int, int] = (1, 6),
    diff_config=None,
    enforce_validator: bool = True,
    refuse_without_diff: bool = True,
) -> TranslationReport:
    """Integrated `alchemist translate` flow.

    Wires in every Phase C gate:
      * Stage 3 validator (refuses to proceed on errors if enforce_validator).
      * Stage 4 field scanner + TDD generator + API completeness.
      * Stage 5 mandatory differential gate (refuses success if diff_config
        is missing, when refuse_without_diff=True).

    Returns a TranslationReport whose `.ok` field is True only if EVERY
    stage passed.
    """
    config = config or AlchemistConfig()
    source = Path(source).resolve()
    checkpoint = source / ".alchemist"
    checkpoint.mkdir(parents=True, exist_ok=True)
    out = output or (checkpoint / "output")
    report = TranslationReport(workspace_dir=out)

    # Phase 0 Bug #8: workspace mutex. Two concurrent pipelines on the
    # same subject race on output/, wins/, and target/. Acquire an
    # advisory lock; fail loudly if another live process holds it.
    from alchemist.workspace_lock import workspace_lock, WorkspaceLockError
    try:
        _lock_cm = workspace_lock(source, timeout=10.0)
        _lock_cm.__enter__()
    except WorkspaceLockError as e:
        report.add(StageOutcome(
            stage="lock", ok=False,
            summary=f"workspace lock acquisition failed: {e}",
        ))
        return report

    try:
        return _run_translate_all_locked(
            source, name, out, checkpoint, report, config,
            stages, diff_config, enforce_validator, refuse_without_diff,
        )
    finally:
        try:
            _lock_cm.__exit__(None, None, None)
        except Exception:
            pass


def _run_translate_all_locked(
    source: Path,
    name: str,
    out: Path,
    checkpoint: Path,
    report,
    config,
    stages: tuple[int, int],
    diff_config,
    enforce_validator: bool,
    refuse_without_diff: bool,
):
    """Body of run_translate_all, executed under workspace_lock."""
    start_stage, end_stage = stages

    # --- Stage 1: Analyze ---
    if start_stage <= 1 <= end_stage:
        try:
            analysis = run_analyze(source, config=config)
            (checkpoint / "analysis.json").write_text(
                json.dumps(analysis, indent=2, default=str), encoding="utf-8"
            )
            report.add(StageOutcome(
                stage="analyze", ok=True,
                summary=(
                    f"{analysis['summary']['total_files']} files, "
                    f"{analysis['summary']['total_functions']} fns, "
                    f"{len(analysis['modules'])} modules"
                ),
            ))
        except SystemExit as e:
            report.add(StageOutcome(
                stage="analyze", ok=False, summary=f"failed: {e}"
            ))
            return report

    # --- Stage 2: Extract ---
    if start_stage <= 2 <= end_stage:
        from alchemist.extractor.spec_extractor import SpecExtractor
        from alchemist.extractor.spec_validator import validate_specs as _validate_specs
        from alchemist.extractor.variant_resolver import (
            make_llm_tiebreaker,
            resolve_specs,
        )
        from alchemist.extractor.schemas import ModuleSpec
        from alchemist.llm.client import AlchemistLLM

        specs_dir = checkpoint / "specs"
        specs_dir.mkdir(exist_ok=True)
        try:
            analysis_data = json.loads((checkpoint / "analysis.json").read_text(encoding="utf-8"))
            extractor = SpecExtractor(config=config)
            specs = extractor.extract_all(analysis_data, output_dir=specs_dir)
        except Exception as e:
            report.add(StageOutcome(stage="extract", ok=False, summary=f"extract failed: {e}"))
            return report

        # Variant disambiguation — resolve multi-variant families (CRC, AES, SHA)
        # to a single canonical variant BEFORE implementation sees the spec.
        try:
            llm = AlchemistLLM(config)
            tiebreaker = make_llm_tiebreaker(llm)
            resolutions = resolve_specs(specs, llm_tiebreaker=tiebreaker)
            resolved_count = sum(1 for r in resolutions if r.resolved)
            ambiguous_unresolved = [r for r in resolutions if not r.resolved and r.candidates]
            if resolved_count:
                console.print(
                    f"[cyan]variant resolver: resolved {resolved_count} algorithms; "
                    f"{len(ambiguous_unresolved)} unresolved[/cyan]"
                )
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]variant resolver skipped: {e}[/yellow]")

        # Re-save specs after resolution since apply_resolution mutates them
        for s in specs:
            (specs_dir / f"{s.name}.json").write_text(
                s.model_dump_json(indent=2), encoding="utf-8"
            )

        # Spec validator — second-pass plausibility check.
        val_report = _validate_specs(specs)
        msg = val_report.summary()
        if not val_report.ok:
            msg += " — errors: " + "; ".join(i.message for i in val_report.errors[:3])
            report.add(StageOutcome(stage="extract", ok=False, summary=msg))
            return report
        report.add(StageOutcome(stage="extract", ok=True, summary=msg))

    # --- Stage 3: Architect (w/ validator gate) ---
    if start_stage <= 3 <= end_stage:
        outcome, arch = run_architect_stage(
            source, name, config=config, enforce=enforce_validator,
        )
        report.add(outcome)
        if not outcome.ok:
            return report

    # --- Stage 4: Implement (field scanner + TDD) ---
    if start_stage <= 4 <= end_stage:
        outcome = run_implement_stage(source, out, tdd=True, config=config)
        report.add(outcome)
        if not outcome.ok:
            return report

    # --- Stage 5: Verify (mandatory differential gate) ---
    if start_stage <= 5 <= end_stage:
        # Auto-select a diff_config based on subject name when caller
        # didn't supply one. zlib has a pre-built config; other subjects
        # will gain configs as they're added (mbedTLS, lwIP, ...).
        resolved_diff_config = diff_config
        if resolved_diff_config is None:
            subject_name = source.name.lower()
            if "zlib" in subject_name:
                from alchemist.verifier.zlib_config import zlib_diff_config
                resolved_diff_config = zlib_diff_config(c_source_dir=source)
                console.print(
                    "[cyan]Stage 5: auto-selected zlib differential config[/cyan]"
                )
        outcome = run_verify_stage(
            source, out,
            diff_config=resolved_diff_config,
            refuse_without_diff=refuse_without_diff,
        )
        report.add(outcome)
        if not outcome.ok:
            return report

    # --- Stage 6: Report ---
    if start_stage <= 6 <= end_stage:
        try:
            from alchemist.reporter.metrics import MetricsCollector
            collector = MetricsCollector(out, source)
            metrics = collector.collect_all()
            (out / "alchemist-report.json").write_text(
                json.dumps(metrics, indent=2, default=str), encoding="utf-8"
            )
            report.add(StageOutcome(stage="report", ok=True, summary="metrics written"))
        except Exception as e:
            report.add(StageOutcome(stage="report", ok=False, summary=f"report gen failed: {e}"))

    return report
