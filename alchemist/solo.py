"""Surgical per-function iteration.

Full pipeline runs all ~75 functions sequentially (~90 min). Most of
that time is burned on functions we've already won and on repetitive
iteration over stubborn ones. `solo` bypasses the full pipeline and
laser-focuses on one named function at high iter count + high temp
sampling, with all the context the LLM needs.

Usage:
    alchemist solo adler32_z --subject subjects/zlib
    alchemist solo inflate_fast --subject subjects/zlib --iters 30 --temp 0.6

On success the winning body lands in the wins cache. Next full run
restores it with zero LLM calls.

Parallel invocations are safe across different modules (they touch
different .rs files). Within the same module a file lock serializes.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console

from alchemist.architect.schemas import CrateArchitecture, CrateSpec
from alchemist.config import AlchemistConfig
from alchemist.extractor.schemas import ModuleSpec
from alchemist.implementer.tdd_generator import TDDGenerator, FunctionAttempt
from alchemist.llm.client import AlchemistLLM

console = Console(force_terminal=True, legacy_windows=False)


@contextmanager
def _module_lock(subject: Path, module_file: str):
    """Cross-process serialization on one module file.

    Two parallel solos targeting different fns in the SAME module would
    race on splice/revert. Serialize with a lock file; different modules
    run concurrently.
    """
    lock_dir = subject / ".alchemist" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{module_file}.lock"
    # Simple polling lock — Windows-compatible, no fcntl.
    start = time.time()
    while True:
        try:
            fd = open(lock_path, "x")
            break
        except FileExistsError:
            if time.time() - start > 600:
                raise RuntimeError(
                    f"timed out waiting for lock on {module_file}"
                )
            time.sleep(2)
    try:
        fd.write(str(time.time()))
        fd.flush()
        yield
    finally:
        fd.close()
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _load_specs_and_arch(
    subject: Path,
) -> tuple[list[ModuleSpec], CrateArchitecture, Path]:
    alch_dir = subject / ".alchemist"
    specs_dir = alch_dir / "specs"
    arch_path = alch_dir / "architecture.json"
    output_dir = alch_dir / "output"
    if not specs_dir.exists() or not arch_path.exists():
        raise RuntimeError(
            f"{subject}: specs or architecture missing. Run a full pipeline "
            f"at least once to populate .alchemist/specs and architecture.json."
        )
    specs = [
        ModuleSpec.model_validate(json.loads(f.read_text(encoding="utf-8")))
        for f in sorted(specs_dir.glob("*.json"))
    ]
    # Apply same pre-flight fixes the pipeline uses so solo sees corrected specs.
    from alchemist.extractor.normalizer import normalize_all
    specs, _ = normalize_all(specs)
    try:
        from alchemist.extractor.spec_auditor import audit_all, apply_auto_fixes
        report = audit_all(specs, subject)
        if any(f.auto_fix for f in report.findings):
            specs = apply_auto_fixes(specs, report)
    except Exception:
        pass
    # Spec completer for orphans
    fn_dir = specs_dir / "_functions"
    if fn_dir.exists():
        from alchemist.extractor.schemas import AlgorithmSpec
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
                                data.get("purpose") or name)
                try:
                    new_algs.append(AlgorithmSpec.model_validate(data))
                    present.add(name)
                except Exception:
                    continue
            module.algorithms = new_algs
    arch = CrateArchitecture.model_validate(
        json.loads(arch_path.read_text(encoding="utf-8"))
    )
    return specs, arch, output_dir


def _locate_fn(
    fn_name: str,
    specs: list[ModuleSpec],
    arch: CrateArchitecture,
) -> tuple[ModuleSpec, CrateSpec, "AlgorithmSpec"] | None:
    for module in specs:
        for alg in module.algorithms or []:
            if alg.name == fn_name:
                for crate in arch.crates:
                    if module.name in set(crate.modules):
                        return module, crate, alg
    return None


def run_solo(
    subject: Path,
    fn_name: str,
    *,
    iters: int = 15,
    temperature: float = 0.5,
    multi_sample_n: int = 6,
    skip_if_cached: bool = False,
) -> FunctionAttempt:
    """Iterate one function until it passes or exhausts iters.

    Parameters tuned for surgical mode: more iterations, higher temp,
    larger fan-out than the full pipeline's defaults. The idea is to
    spend more compute per function in exchange for dropping the
    overhead of 74 other functions.
    """
    specs, arch, output_dir = _load_specs_and_arch(subject)
    found = _locate_fn(fn_name, specs, arch)
    if not found:
        raise RuntimeError(
            f"function `{fn_name}` not found in any module's algorithms list"
        )
    module, crate, alg = found
    console.print(
        f"[cyan]solo target: {crate.name}::{module.name}::{fn_name}[/cyan]"
    )

    if skip_if_cached:
        cache_path = (
            output_dir.parent / "wins" / crate.name / module.name /
            f"{fn_name}.rs"
        )
        if cache_path.exists():
            console.print(
                f"[green]solo: cached win already exists at {cache_path} — "
                f"skip requested[/green]"
            )
            return FunctionAttempt(
                algorithm=fn_name, crate=crate.name, module=module.name,
                final_compiled=True, tests_passed=True,
            )

    config = AlchemistConfig()
    llm = AlchemistLLM(config)
    gen = TDDGenerator(
        config=config,
        llm=llm,
        max_iter_per_fn=iters,
        holistic_after=max(3, iters // 3),
        multi_sample_after=2,
        multi_sample_n=multi_sample_n,
        multi_sample_temperature=temperature,
    )
    # Initialize workspace-level context required by _fill_in_function.
    # These are normally set in generate_workspace; solo skips that.
    gen._source_root = subject
    gen._probe_refs = {}
    gen._probe_attempted = set()
    gen._workspace_dir = output_dir
    # Fuzz backfill populates alg.test_vectors for state-mutator /
    # byte-transform / pure-fn shapes. Without this, every solo fails
    # with "no test vectors" because the spec files don't persist the
    # vectors from a prior Phase B.
    try:
        gen._backfill_fuzz_vectors(specs)
    except Exception as e:
        console.print(f"[yellow]solo: fuzz backfill skipped ({e})[/yellow]")
    gen._cached_ctx = llm.create_cached_context(
        system_text=(
            "You are implementing one Rust function at a time from a spec. "
            "Output must compile AND pass its tests byte-exactly. "
            "Never emit stubs, placeholders, or ellipsis comments."
        ),
        project_context=gen._build_project_context(specs, arch),
    )

    module_file = f"{module.name}.rs"
    with _module_lock(subject, module_file):
        attempt = gen._fill_in_function(
            alg, module, crate, specs, arch, output_dir,
        )
    status = (
        "[green]PASS[/green]" if attempt.tests_passed
        else ("[yellow]COMPILED[/yellow]" if attempt.final_compiled
              else "[red]FAIL[/red]")
    )
    console.print(
        f"{status} {fn_name}: iters={attempt.iterations} "
        f"err={attempt.last_error[:120] if attempt.last_error else ''}"
    )
    return attempt
