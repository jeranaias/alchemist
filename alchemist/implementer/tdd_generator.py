"""Phase 4C: test-driven code generation.

Orchestration:

    Phase A (skeleton)         — emit compile-ready types + `unimplemented!()`
                                  bodies, confirm `cargo check --workspace`.
    Phase B (tests)            — append test blocks sourced from
                                  spec.test_vectors + standards catalog.
                                  `cargo check` must still pass (tests compile).
                                  `cargo test` is expected to FAIL (stubs panic).
    Phase C (per-fn fill-in)   — for each algorithm, in dependency order:
        * Ask the LLM for an implementation of just that function.
        * Deterministically scrub + reject if the result trips the anti-stub
          detector. Re-prompt with stricter instructions.
        * Run the test(s) for that function. If they pass, move on; otherwise
          re-prompt with the test failure output as supervisor signal.
        * After N failing iterations, escalate to the holistic fixer.
    Phase D (completeness gate)— verify every spec.source_functions has a
                                  `pub fn`. Re-prompt if missing.

The rewritten `CodeGenerator.generate_workspace` in the sibling module
`code_generator.py` delegates to this when `tdd_mode=True`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from alchemist.architect.schemas import CrateArchitecture, CrateSpec
from alchemist.config import AlchemistConfig
from alchemist.extractor.schemas import AlgorithmSpec, ModuleSpec
from alchemist.implementer.anti_stub import ScanReport, scan_text
from alchemist.implementer.semantic_lints import (
    has_errors as _semantic_has_errors,
    lint_function as _semantic_lint,
    summarize_for_reprompt as _semantic_summary,
)
from alchemist.implementer.api_completeness import (
    ApiCompletenessReport,
    check_workspace as check_api,
    missing_to_reprompt_context,
)
from alchemist.implementer.init_templates import try_init_template
from alchemist.implementer.scrubber import scrub_rust
from alchemist.implementer.skeleton import (
    WorkspaceSkeletonResult,
    generate_workspace_skeleton,
    _topo_sort,
    _run_cargo_check,
)
from alchemist.implementer.test_generator import generate_tests_for_workspace
from alchemist.llm.client import AlchemistLLM, CachedContext

console = Console(force_terminal=True, legacy_windows=False)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class FunctionAttempt:
    algorithm: str
    crate: str
    module: str
    iterations: int = 0
    final_compiled: bool = False
    tests_passed: bool = False
    escalated_to_holistic: bool = False
    last_error: str = ""


@dataclass
class TDDResult:
    workspace_dir: Path
    skeleton: WorkspaceSkeletonResult | None = None
    attempts: list[FunctionAttempt] = field(default_factory=list)
    api_report: ApiCompletenessReport | None = None
    workspace_compiles: bool = False
    workspace_tests_passed: bool = False
    final_stdout: str = ""
    final_stderr: str = ""

    @property
    def ok(self) -> bool:
        return (
            self.workspace_compiles
            and self.workspace_tests_passed
            and (self.api_report.ok if self.api_report else True)
        )


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an expert Rust systems engineer translating C algorithms
into idiomatic safe Rust. You will be given a precise specification for a
single function and must produce ONLY the Rust source for that one function
(or a drop-in replacement of its body). Your output MUST satisfy:

  1. Implement the ACTUAL algorithm described by the spec — no stubs, no
     simulation, no "we don't have the algorithm" comments, no placeholders.
  2. Use only safe Rust. No `unsafe` blocks unless the spec explicitly says
     unsafe is required.
  3. No markdown fences. No explanation. Return ONLY valid Rust.
  4. Match the exact function signature from the spec.
  5. Assume `extern crate alloc;` is in scope when the crate is `no_std`.
"""

_IMPL_PROMPT = """Implement this single Rust function. Do NOT stub, simulate, or
write scaffolding that compiles but doesn't work. Implement the actual
algorithm described by the spec.

## Algorithm spec
Name: {name}
Category: {category}
Description: {description}
Mathematical notes: {math}
Referenced standards: {standards}

## Signature (must match exactly)
```rust
{signature}
```

## Inputs
{inputs}

## Return type
{return_type}

## Invariants / preconditions
{invariants}

## Known test vectors (your implementation MUST satisfy these)
{test_vectors}

## Standards catalog vectors (authoritative, MUST satisfy)
{catalog_vectors}
{reference_impls}

## Shared type definitions (already in scope via `use zlib_types::*;`)
{struct_context}

## Current stub to replace
```rust
{current_body}
```
{previous_failure}

Return the COMPLETE function definition (signature + body) that replaces
the stub. Adapt the reference implementation (if provided) to match the
signature required above. Return ONLY the function — no const/static/use
declarations, no markdown, no explanation.
"""


# ---------------------------------------------------------------------------
# TDD generator
# ---------------------------------------------------------------------------

class TDDGenerator:
    def __init__(
        self,
        config: AlchemistConfig | None = None,
        llm: AlchemistLLM | None = None,
        *,
        max_iter_per_fn: int = 5,
        holistic_after: int = 3,
        multi_sample_after: int = 2,
        multi_sample_n: int = 4,
        multi_sample_temperature: float = 0.35,
    ):
        self.config = config or AlchemistConfig()
        self.llm = llm or AlchemistLLM(self.config)
        self.max_iter_per_fn = max_iter_per_fn
        self.holistic_after = holistic_after
        self.multi_sample_after = multi_sample_after
        self.multi_sample_n = multi_sample_n
        self.multi_sample_temperature = multi_sample_temperature

    # --- Main entry ---

    def generate_workspace(
        self,
        specs: list[ModuleSpec],
        architecture: CrateArchitecture,
        output_dir: Path,
        *,
        source_root: Path | None = None,
    ) -> TDDResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        result = TDDResult(workspace_dir=output_dir)
        # Remember the source root so the reference probe can locate
        # C function bodies for transliteration. Probe silently skips
        # if source_root is None.
        self._source_root = source_root
        self._probe_refs: dict = {}
        self._probe_attempted: set[str] = set()

        # Phase A: skeleton
        console.print("[bold cyan]TDD Phase A: skeleton generation[/bold cyan]")
        skel = generate_workspace_skeleton(specs, architecture, output_dir, cargo_check=True)
        result.skeleton = skel
        if not skel.ok:
            console.print(f"[red]skeleton failed to compile:\n{skel.workspace_stderr[:2000]}[/red]")
            result.workspace_compiles = False
            return result

        # Phase B: tests (append test blocks)
        console.print("[bold cyan]TDD Phase B: test emission[/bold cyan]")
        # Fuzz-vector backfill: for every algorithm without extracted vectors
        # AND without catalog vectors, try to synthesize vectors by calling
        # the C reference library. Closes the P2 loophole where functions
        # with no vectors would silently compile-only-pass.
        self._backfill_fuzz_vectors(specs)
        test_results = generate_tests_for_workspace(specs, architecture, output_dir)
        total_tests = sum(t.tests_written for t in test_results)
        console.print(f"  emitted {total_tests} tests across {len(test_results)} crates")

        # Verify tests COMPILE (even if they fail)
        ok, stderr = _run_cargo_check(output_dir, timeout=300)
        if not ok:
            # Re-run with --all-targets to surface test compile errors
            ok_at, stderr_at = _run_cargo_all_targets(output_dir)
            if not ok_at:
                console.print(f"[red]test block compile failed:\n{stderr_at[:2000]}[/red]")
                result.workspace_compiles = False
                return result

        # Phase C: per-function TDD
        console.print("[bold cyan]TDD Phase C: per-function implementation loop[/bold cyan]")
        self._workspace_dir = output_dir  # for struct context lookup
        self._cached_ctx = self.llm.create_cached_context(
            system_text=_SYSTEM_PROMPT,
            project_context=self._build_project_context(specs, architecture),
        )
        # Reference probe is lazy: synthesized per-function during Phase C
        # when (a) the function has no curated ref AND (b) the first
        # generation iteration failed. This avoids spending 50+ min up-front
        # probing every function when most succeed without a probe-generated
        # template. See self._ensure_probe_ref in _fill_in_function.
        for crate_name in _topo_sort(architecture):
            crate_spec = next((c for c in architecture.crates if c.name == crate_name), None)
            if not crate_spec:
                continue
            crate_modules = [m for m in specs if m.name in set(crate_spec.modules)]
            for module in crate_modules:
                # Topologically sort algorithms within the module so leaf
                # functions are generated before wrappers. A wrapper like
                # `compress` depends on `deflate` compiling correctly; if we
                # generate wrappers first, they fail because the leaves are
                # still `unimplemented!()` stubs.
                ordered_algs = self._topo_sort_algorithms(module.algorithms)
                for alg in ordered_algs:
                    attempt = self._fill_in_function(
                        alg, module, crate_spec, specs, architecture, output_dir,
                    )
                    result.attempts.append(attempt)

        # Phase D: API completeness
        console.print("[bold cyan]TDD Phase D: API completeness check[/bold cyan]")
        api = check_api(specs, architecture, output_dir)
        result.api_report = api
        if not api.ok:
            # Attempt a targeted fix: re-prompt for each missing
            self._fix_missing(api, specs, architecture, output_dir)
            api = check_api(specs, architecture, output_dir)
            result.api_report = api

        # Final: workspace-wide cargo test
        console.print("[bold cyan]TDD final: cargo test --workspace[/bold cyan]")
        tests_ok, tout, terr = _run_cargo_test(output_dir)
        result.final_stdout = tout
        result.final_stderr = terr
        result.workspace_tests_passed = tests_ok

        check_ok, _ = _run_cargo_check(output_dir, timeout=300)
        result.workspace_compiles = check_ok

        return result

    # --- Phase C helpers ---

    def _wins_cache_path(
        self, workspace_dir: Path, crate: str, module: str, fn_name: str,
    ) -> Path:
        # Stored alongside output, not under output/, so a target/ clean
        # doesn't wipe accumulated progress. Path is stable across runs.
        return (
            workspace_dir.parent / "wins" / crate / module / f"{fn_name}.rs"
        )

    def _hardport_path(
        self, subject_hint: str, crate: str, module: str, fn_name: str,
    ) -> Path | None:
        """Locate a hand-port for (subject, crate, module, fn) if one exists.

        Hand-ports live in-package at
          alchemist/references/impls/<subject>_hardports/<crate>/<module>/<fn>.rs

        Subject hint is derived from the workspace name or crate prefix —
        e.g., "zlib" for crate "zlib-checksum". Returns None if no match.
        """
        from alchemist.references import registry as _reg
        base = _reg.REFERENCES_DIR
        # Try subject-specific hardports dir
        for key in (subject_hint, crate.split("-", 1)[0]):
            if not key:
                continue
            p = base / f"{key}_hardports" / crate / module / f"{fn_name}.rs"
            if p.exists():
                return p
        return None

    def _load_cached_win(
        self, workspace_dir: Path, crate: str, module: str, fn_name: str,
    ) -> str | None:
        """Return the Rust body text to try first for this fn.

        Priority:
          1. Hand-port (alchemist/references/impls/<subject>_hardports/...)
             — authored by humans, versioned with the package, always up to date.
          2. Workspace wins cache (<workspace>/.alchemist/wins/...)
             — LLM-produced wins from prior runs of THIS subject.
        """
        subject_hint = workspace_dir.parent.parent.name if workspace_dir else ""
        hardport = self._hardport_path(subject_hint, crate, module, fn_name)
        if hardport is not None:
            try:
                return hardport.read_text(encoding="utf-8")
            except Exception:
                pass
        p = self._wins_cache_path(workspace_dir, crate, module, fn_name)
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    def _save_cached_win(
        self, workspace_dir: Path, crate: str, module: str, fn_name: str,
        fn_body: str,
    ) -> None:
        """Persist a verified-winning impl to the cache.

        Previously swallowed all exceptions silently — disk full, perms,
        or path issues would silently stop the cache from accumulating.
        Now: log + re-raise on failure, with round-trip verify to catch
        partial writes.
        """
        p = self._wins_cache_path(workspace_dir, crate, module, fn_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(fn_body, encoding="utf-8")
        # Round-trip verify: the wins cache is load-bearing for monotonic
        # progress. A silently-truncated write means next-run cache restore
        # fails → iterate → maybe-regress. Loud failure is better.
        readback = p.read_text(encoding="utf-8")
        if readback != fn_body:
            raise RuntimeError(
                f"wins cache write verify FAILED: {p} "
                f"(wrote {len(fn_body)} bytes, read back {len(readback)})"
            )

    def _verify_cached_win_twice(
        self, crate_dir: Path, test_name_prefix: str | list[str],
    ) -> tuple[bool, int]:
        """Run the test filter twice. Returns (ok, total_tests_observed).

        A cached win that passes once but fails once is flaky — caching
        it pollutes the cache and poisons later runs. Require 2-for-2.
        """
        total_tests = 0
        for attempt_idx in range(2):
            ok, tout, terr = _run_cargo_test_filter(crate_dir, test_name_prefix)
            combined = (tout or "") + "\n" + (terr or "")
            import re as _re
            ran_counts = [int(m) for m in _re.findall(
                r"running\s+(\d+)\s+tests?", combined
            )]
            if not ok or not any(n > 0 for n in ran_counts):
                return False, total_tests
            total_tests = max(total_tests, sum(ran_counts))
        return True, total_tests

    def _fill_in_function(
        self,
        alg: AlgorithmSpec,
        module: ModuleSpec,
        crate_spec: CrateSpec,
        all_specs: list[ModuleSpec],
        arch: CrateArchitecture,
        workspace_dir: Path,
    ) -> FunctionAttempt:
        crate_dir = workspace_dir / crate_spec.name
        module_path = crate_dir / "src" / f"{module.name}.rs"
        attempt = FunctionAttempt(
            algorithm=alg.name, crate=crate_spec.name, module=module.name,
        )
        if not module_path.exists():
            attempt.last_error = f"module file missing: {module_path}"
            return attempt

        # Wins cache fast-path: if a prior run got this function passing,
        # try that impl first. If it still compiles + tests pass under the
        # current skeleton/spec, take the win with zero LLM calls. Honest
        # because the test suite still runs and verifies correctness.
        cached_win = self._load_cached_win(
            workspace_dir, crate_spec.name, module.name, alg.name,
        )
        if cached_win:
            current = module_path.read_text(encoding="utf-8")
            replaced = self._replace_fn_in_source(current, alg.name, cached_win)
            if replaced:
                module_path.write_text(replaced, encoding="utf-8")
                ok_compile, _ = _run_cargo_check(crate_dir, timeout=180)
                if ok_compile:
                    test_name_prefix = _test_filters_for_fn(alg.name)
                    # 2x verify at restore: a flaky cached impl poisons
                    # progress. Both runs must pass with >0 tests observed.
                    ok_twice, total_tests = self._verify_cached_win_twice(
                        crate_dir, test_name_prefix,
                    )
                    if ok_twice and total_tests > 0:
                        attempt.iterations = 0
                        attempt.final_compiled = True
                        attempt.tests_passed = True
                        console.print(
                            f"  [green]{alg.name}: cached win restored "
                            f"(0 LLM calls, 2x verified, {total_tests} tests)[/green]"
                        )
                        return attempt
                # Cached win didn't hold — revert and fall through to iteration
                module_path.write_text(current, encoding="utf-8")

        # Short-circuit: if the function has no verifiable test vectors
        # upfront, skip iteration entirely. Iterating would burn LLM calls
        # on code we can't verify. P2 surfaces the gap rather than silently
        # accepting it.
        from alchemist.standards import lookup_test_vectors
        has_spec_vectors = bool(alg.test_vectors)
        has_catalog_vectors = bool(lookup_test_vectors(alg.name))
        if not has_spec_vectors and not has_catalog_vectors:
            attempt.last_error = (
                "no verifiable test vectors — add spec.test_vectors, extend "
                "fuzz_vectors bindings, or add to standards catalog"
            )
            console.print(
                f"  [red]{alg.name}: no test vectors — skipping (cannot verify)[/red]"
            )
            return attempt

        test_name_prefix = _test_filters_for_fn(alg.name)
        fallback_test = f"smoke_{alg.name}"
        previous_failure = ""  # carries test output into next iteration prompt

        # Fast path: deterministic template for init/reset functions.
        # These fail LLM generation consistently — bypass the loop.
        # CRITICAL: we still run the correctness test after the template
        # lands. Per P2, a template compiling is not a correctness proof.
        # If tests exist and pass, great. If no tests exist, fall back to
        # the compile-only accept (init fns with no observable output
        # cannot be verified without state-mutator bindings).
        template_code = try_init_template(alg)
        if template_code:
            current = module_path.read_text(encoding="utf-8")
            replaced = self._replace_fn_in_source(current, alg.name, template_code)
            if replaced:
                module_path.write_text(replaced, encoding="utf-8")
                ok_compile, _err = _run_cargo_check(crate_dir, timeout=180)
                if ok_compile:
                    # Run the test filter to see if we have real tests
                    ok_test, tout, terr = _run_cargo_test_filter(
                        crate_dir, test_name_prefix,
                    )
                    combined = tout + "\n" + terr
                    import re as _re
                    ran_counts = [int(m) for m in _re.findall(
                        r"running\s+(\d+)\s+tests?", combined
                    )]
                    had_real_tests = any(n > 0 for n in ran_counts)
                    if ok_test and had_real_tests:
                        # 2x verify before caching — one flaky pass
                        # poisons next-run restore
                        ok_twice, _ = self._verify_cached_win_twice(
                            crate_dir, test_name_prefix,
                        )
                        if not ok_twice:
                            console.print(
                                f"  [yellow]{alg.name}: init template passed once "
                                f"but not twice — treating as iter fail[/yellow]"
                            )
                            module_path.write_text(current, encoding="utf-8")
                        else:
                            attempt.iterations = 0
                            attempt.final_compiled = True
                            attempt.tests_passed = True
                            try:
                                self._save_cached_win(
                                    workspace_dir, crate_spec.name, module.name,
                                    alg.name, template_code,
                                )
                            except Exception as e:  # noqa: BLE001
                                console.print(
                                    f"  [red]{alg.name}: cache save failed: {e}[/red]"
                                )
                            console.print(
                                f"  [green]{alg.name}: init template + 2x tests pass[/green]"
                            )
                            return attempt
                    if not ok_test:
                        # Template compiled but tests failed — revert and
                        # let LLM iteration take over
                        module_path.write_text(current, encoding="utf-8")
                    elif had_real_tests:
                        # Tests passed (unreachable — already handled above)
                        pass
                    else:
                        # No tests exist. Template is a tombstone. Keep it
                        # but mark the attempt as failed: P2 forbids compile-
                        # only pass.
                        attempt.last_error = (
                            "init template accepted but no tests exist to "
                            "verify correctness (P2 forbids compile-only pass)"
                        )
                        console.print(
                            f"  [yellow]{alg.name}: init template landed but unverified[/yellow]"
                        )
                        return attempt
                else:
                    # Template didn't compile — revert and fall through to LLM
                    module_path.write_text(current, encoding="utf-8")

        for iteration in range(1, self.max_iter_per_fn + 1):
            attempt.iterations = iteration
            current = module_path.read_text(encoding="utf-8")
            current_body = self._extract_fn_body(current, alg.name)

            # Multi-sample fan-out at iteration >= multi_sample_after.
            # If sampling finds a compile+test-pass candidate, take the win.
            if iteration >= self.multi_sample_after:
                ms = self._multi_sample_attempt(
                    alg, module_path, current, current_body or "unimplemented!()",
                    previous_failure=previous_failure, crate_dir=crate_dir,
                    test_name_prefix=test_name_prefix,
                )
                # A multi-sample win requires BOTH zero failures AND at
                # least one passing test. Otherwise cargo's "0 passed; 0
                # failed" success counts as a win even when no test
                # actually ran — the same P2 loophole as the main loop.
                if (
                    ms is not None and ms.ok and ms.best
                    and ms.best.tests_failed == 0
                    and ms.best.tests_passed >= 1
                ):
                    # 2x verify the multi-sample winner before declaring
                    # success. ms's evaluator only tested once per candidate.
                    ok_twice, _ = self._verify_cached_win_twice(
                        crate_dir, test_name_prefix,
                    )
                    if not ok_twice:
                        console.print(
                            f"  [yellow]{alg.name}: multi-sample passed once "
                            f"but not twice — iter continues[/yellow]"
                        )
                        continue
                    attempt.final_compiled = True
                    attempt.tests_passed = True
                    # Snapshot the winning FULL fn item (signature + body) from
                    # the module file. Prior bug: saved body-only, which the
                    # splice guard (5cf38f3) then rejects at restore time,
                    # making the cache entry unusable. Save full fn instead.
                    try:
                        final_src = module_path.read_text(encoding="utf-8")
                        m = self._find_fn(final_src, alg.name)
                        final_fn = (
                            final_src[m["item_start"]:m["item_end"]]
                            if m else None
                        )
                        if final_fn:
                            self._save_cached_win(
                                workspace_dir, crate_spec.name, module.name,
                                alg.name, final_fn,
                            )
                    except Exception as e:  # noqa: BLE001
                        console.print(
                            f"  [red]{alg.name}: cache save failed: {e}[/red]"
                        )
                    console.print(
                        f"  [green]{alg.name}: multi-sample win on iter {iteration} "
                        f"(candidate #{ms.best.candidate_idx})[/green]"
                    )
                    return attempt

            # Generate replacement (context-aware: includes prev failure)
            new_fn = self._prompt_for_impl(
                alg, current_body or "unimplemented!()",
                previous_failure=previous_failure,
            )
            if not new_fn:
                attempt.last_error = "LLM returned empty"
                continue

            # Deterministic cleanup
            new_fn, _ = scrub_rust(new_fn)

            # Strip leaked module-level items (use/static/const) the LLM
            # may have emitted above the function definition.
            new_fn = self._strip_module_items(new_fn)

            # Anti-stub check
            stub_violations = scan_text("pending.rs", new_fn)
            if stub_violations:
                violation_summary = "; ".join(
                    f"{v.pattern}: {v.snippet.strip()[:80]}" for v in stub_violations[:3]
                )
                previous_failure = (
                    f"## Previous iteration was REJECTED as a stub.\n\n"
                    f"You wrote:\n```rust\n{new_fn[:1500]}\n```\n\n"
                    f"Rejection reasons: {violation_summary}\n\n"
                    f"Write REAL working code. No unimplemented!(), no todo!(), "
                    f"no 'for this spec we simulate' comments, no empty Ok(()) "
                    f"bodies. Implement the actual algorithm."
                )
                console.print(f"  [yellow]{alg.name}: anti-stub rejected iteration {iteration}[/yellow]")
                continue

            # Semantic lint (family-specific invariants) — reject early if
            # the candidate clearly violates its algorithm's math, so we
            # don't burn a cargo check/test cycle on it.
            semantic_findings = _semantic_lint(new_fn, alg)
            if _semantic_has_errors(semantic_findings):
                previous_failure = (
                    f"## Previous iteration was REJECTED by the semantic linter.\n\n"
                    f"You wrote:\n```rust\n{new_fn[:2500]}\n```\n\n"
                    f"Lint findings:\n{_semantic_summary(semantic_findings)}\n\n"
                    f"These are algorithm correctness issues, not syntax. Check "
                    f"your constants, polynomials, and endianness against the "
                    f"referenced standards."
                )
                console.print(
                    f"  [yellow]{alg.name}: semantic lint rejected iter {iteration} — "
                    f"{len([f for f in semantic_findings if f.severity == 'error'])} errors[/yellow]"
                )
                continue

            # Splice into module
            replaced = self._replace_fn_in_source(current, alg.name, new_fn)
            if not replaced:
                attempt.last_error = f"could not splice new body for fn {alg.name}"
                continue
            module_path.write_text(replaced, encoding="utf-8")

            # Compile check (crate only)
            ok_compile, cerr = _run_cargo_check(crate_dir, timeout=180)
            if not ok_compile:
                # Revert and try again with compile-error context + the
                # LLM's own failed attempt so it can see what it wrote.
                # Without the attempt in context, the next prompt shows the
                # reverted stub (unimplemented!()) and the LLM can't tell
                # which of its choices broke — it just rewrites similar code.
                module_path.write_text(current, encoding="utf-8")
                attempt.last_error = _top_lines(cerr, 3)
                previous_failure = (
                    f"## Previous iteration FAILED to compile.\n\n"
                    f"You wrote this:\n```rust\n{new_fn[:2500]}\n```\n\n"
                    f"Cargo error output:\n```\n{_top_lines(cerr, 40)}\n```\n\n"
                    f"Fix the specific compile errors above. Do NOT rewrite from "
                    f"scratch — identify the offending fields/types/imports and "
                    f"correct only those. Keep the overall algorithm the same. "
                    f"Pay special attention to: field names on struct params, "
                    f"import paths, and type mismatches."
                )
                console.print(f"  [yellow]{alg.name}: compile failed, reverting (iter {iteration})[/yellow]")
                # After iter 1's compile failure, synthesize a reference from
                # the C source and make it available for iter 2+. The probe
                # runs only once per algorithm per run — this is the lazy
                # generalization primitive.
                if iteration == 1:
                    self._ensure_probe_ref(alg)
                continue

            attempt.final_compiled = True

            # Run tests that match the function name
            ok_test, tout, terr = _run_cargo_test_filter(
                crate_dir, test_name_prefix,
            )
            combined = (tout or "") + "\n" + (terr or "")
            # Cargo returns exit 0 even when ZERO tests matched the filter.
            # Detect that case explicitly: if the output shows "0 passed; 0
            # failed" WITHOUT any "running N tests" line reporting N>0, then
            # no tests ran. Per P2 (no compile-only pass) we must fail here
            # rather than claim success.
            import re as _re
            ran_counts = [int(m) for m in _re.findall(
                r"running\s+(\d+)\s+tests?", combined
            )]
            had_real_tests = any(n > 0 for n in ran_counts)
            if ok_test and had_real_tests:
                # 2x verify before caching — prevents flaky passes from
                # polluting the cache. Both runs must return ok + >0 tests.
                ok_twice, _ = self._verify_cached_win_twice(
                    crate_dir, test_name_prefix,
                )
                if not ok_twice:
                    console.print(
                        f"  [yellow]{alg.name}: iter {iteration} passed once "
                        f"but not twice — iter continues[/yellow]"
                    )
                    continue
                attempt.tests_passed = True
                # Persist the winning body so the next run skips iteration
                # for this function (huge accumulation effect across runs).
                if new_fn:
                    try:
                        self._save_cached_win(
                            workspace_dir, crate_spec.name, module.name,
                            alg.name, new_fn,
                        )
                    except Exception as e:  # noqa: BLE001
                        console.print(
                            f"  [red]{alg.name}: cache save failed: {e}[/red]"
                        )
                console.print(f"  [green]{alg.name}: tests pass on iter {iteration} (2x verified)[/green]")
                return attempt

            # No tests matched this function's prefix — P2 violation to
            # accept. Mark as failed so the user sees which functions are
            # unverifiable. Fuzz-vector backfill already ran; if it produced
            # nothing, the only path forward is to extend fuzz bindings.
            if ok_test and not had_real_tests:
                attempt.last_error = (
                    "no correctness test available for this function; "
                    "compile-only acceptance is forbidden by the "
                    "zero-shortcut policy"
                )
                previous_failure = (
                    "## No tests run — compile-only acceptance is forbidden\n\n"
                    "This function needs a test vector but none exists in the "
                    "spec or standards catalog. The pipeline will not accept "
                    "an implementation that cannot be verified."
                )
                console.print(
                    f"  [red]{alg.name}: no test vectors — cannot verify correctness[/red]"
                )
                continue

            attempt.last_error = _top_lines(terr, 5)
            previous_failure = (
                f"## Previous iteration compiled but FAILED tests.\n\n"
                f"You wrote:\n```rust\n{new_fn[:2500]}\n```\n\n"
                f"Test output:\n```\n{_top_lines((tout or '') + chr(10) + (terr or ''), 40)}\n```\n\n"
                f"The code compiles but produces wrong output. Check the "
                f"expected values in the test and fix the algorithm logic. "
                f"Most likely causes: off-by-one, wrong initial state, "
                f"wrong constant, incorrect bit ordering."
            )
            console.print(f"  [yellow]{alg.name}: tests failed on iter {iteration}[/yellow]")

            # Escalate to holistic
            if iteration >= self.holistic_after and iteration < self.max_iter_per_fn:
                attempt.escalated_to_holistic = True
                self._holistic_fix(crate_dir, alg, attempt.last_error)

        return attempt

    def _prompt_for_impl(
        self, alg: AlgorithmSpec, current_body: str, *,
        previous_failure: str = "",
        temperature: float = 0.15,
    ) -> str | None:
        from alchemist.standards import lookup_test_vectors
        from alchemist.references import find_references
        from alchemist.references.registry import references_for_standards
        schema = {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        }
        params = "\n".join(
            f"- {p.name}: {p.rust_type}  — {p.description}"
            for p in alg.inputs
        ) or "(no inputs)"
        tvecs = "\n".join(
            f"- inputs={v.inputs} → expected={v.expected_output}  ({v.description})"
            for v in (alg.test_vectors or [])
        ) or "(no extracted vectors — ensure the implementation matches the referenced standards)"
        # Standards catalog vectors (authoritative)
        catalog = lookup_test_vectors(alg.name) or []
        catalog_vec_text = "\n".join(
            f"- {v.name}: input={v.input_hex or '(empty)'} → expected={v.expected_hex}"
            for v in catalog[:5]
        ) or "(no catalog entry for this algorithm)"
        prev_failure_section = (
            f"\n## Previous iteration's FAILING test output\n```\n{previous_failure[:2000]}\n```\n"
            "The implementation you produced last time FAILED against these tests. "
            "Fix what was wrong."
            if previous_failure
            else ""
        )
        # Reference implementation injection — adapts, never reinvents.
        reference_block = self._reference_prompt_block(alg)
        # Struct context — inject field definitions for shared types that
        # appear in the function's parameter types. Without this, the model
        # guesses field names on 40-field structs and gets them wrong.
        struct_context = self._struct_context_for(alg)
        signature = self._signature_for(alg)
        prompt = _IMPL_PROMPT.format(
            name=alg.name,
            category=alg.category,
            description=alg.description,
            math=alg.mathematical_description or "(none)",
            standards=", ".join(alg.referenced_standards) or "(none)",
            signature=signature,
            inputs=params,
            return_type=alg.return_type or "()",
            invariants="\n".join(
                f"- {inv.description}" for inv in alg.invariants
            ) or "(none)",
            test_vectors=tvecs,
            catalog_vectors=catalog_vec_text,
            reference_impls=reference_block,
            struct_context=struct_context,
            current_body=current_body,
            previous_failure=prev_failure_section,
        )
        resp = self.llm.call_structured(
            messages=[{"role": "user", "content": prompt}],
            tool_name="impl",
            tool_schema=schema,
            cached_context=self._cached_ctx,
            max_tokens=6000,
            temperature=temperature,
        )
        # Hard gate: if the LLM call failed (503, timeout, etc.), return None
        # so the caller SKIPS splicing. Without this, the error text was being
        # spliced as code, producing uncompilable Rust with ERROR: strings.
        if getattr(resp, "error", ""):
            return None
        if resp.structured and "content" in resp.structured:
            content = (resp.structured.get("content") or "").strip()
            # Some LLMs wrap their structured output in the schema itself:
            # `content` ends up being a JSON string describing the schema,
            # not the Rust code. Detect and drill in.
            content = _unwrap_llm_schema_leak(content)
            return content or None
        # Fallback: raw text
        raw = (resp.content or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:\w+)?\s*", "", raw)
            raw = re.sub(r"```\s*$", "", raw)
        raw = _unwrap_llm_schema_leak(raw)
        return raw or None

    def _multi_sample_attempt(
        self,
        alg: AlgorithmSpec,
        module_path: Path,
        original_source: str,
        current_body: str,
        *,
        previous_failure: str,
        crate_dir: Path,
        test_name_prefix: str | list[str],
    ):
        """Fan out multi_sample_n candidates, evaluate, pick best.

        Returns MultiSampleResult or None (when sampling can't make progress).
        """
        from alchemist.implementer.multi_sample import (
            make_cargo_evaluator,
            run_multi_sample,
        )

        def sampler(_idx: int) -> str | None:
            return self._prompt_for_impl(
                alg, current_body,
                previous_failure=previous_failure,
                temperature=self.multi_sample_temperature,
            )

        def splicer(body: str) -> bool:
            body, _ = scrub_rust(body)
            body = self._strip_module_items(body)
            if scan_text("pending.rs", body):
                return False
            replaced = self._replace_fn_in_source(original_source, alg.name, body)
            if not replaced:
                return False
            module_path.write_text(replaced, encoding="utf-8")
            return True

        def reject_stub(body: str) -> bool:
            return bool(scan_text("pending.rs", body))

        evaluator = make_cargo_evaluator(crate_dir, test_name_prefix)
        try:
            return run_multi_sample(
                sampler=sampler,
                splicer=splicer,
                evaluator=evaluator,
                original_source=original_source,
                file_path=module_path,
                n_samples=self.multi_sample_n,
                reject_stub=reject_stub,
            )
        except Exception as e:  # noqa: BLE001
            console.print(f"  [dim]multi-sample skipped: {e}[/dim]")
            # Always restore original source on error
            module_path.write_text(original_source, encoding="utf-8")
            return None

    def _topo_sort_algorithms(self, algs: list[AlgorithmSpec]) -> list[AlgorithmSpec]:
        """Sort algorithms so dependencies come before dependents.

        Heuristic: an algorithm A depends on B if A's description or
        mathematical_description mentions B's name as a function call
        (identifier followed by '('). Functions with no dependencies go
        first, then their dependents, etc.
        """
        names = {a.name for a in algs}
        alg_by_name = {a.name: a for a in algs}

        def deps_of(a: AlgorithmSpec) -> set[str]:
            text = (a.description or "") + " " + (a.mathematical_description or "")
            found: set[str] = set()
            for n in names:
                if n == a.name:
                    continue
                # Look for call-site patterns: `name(` or `name (`
                if re.search(rf"\b{re.escape(n)}\s*\(", text):
                    found.add(n)
            return found

        # Compute in-degrees and do Kahn's algorithm
        deps = {a.name: deps_of(a) for a in algs}
        ordered: list[AlgorithmSpec] = []
        remaining = set(names)
        while remaining:
            # Find algs whose deps are all resolved
            ready = [n for n in remaining if not (deps[n] & remaining)]
            if not ready:
                # Cycle — just append remaining in original order
                ordered.extend(a for a in algs if a.name in remaining)
                break
            # Stable order: sort by original index
            ready.sort(key=lambda n: next(i for i, a in enumerate(algs) if a.name == n))
            for n in ready:
                ordered.append(alg_by_name[n])
                remaining.discard(n)
        return ordered

    def _struct_context_for(self, alg: AlgorithmSpec) -> str:
        """Build a struct-field context block for types referenced in params.

        When a function takes `&mut DeflateState`, the model needs to see
        the exact field names and types to generate compiling code. We scan
        the workspace's types module file for struct definitions that match
        any type name appearing in the function's parameters.

        Follows type references transitively: if DeflateStream has a field
        `state: DeflateState`, both structs are included. Depth-limited to
        avoid runaway inclusion of every shared type.
        """
        _STDLIB_GENERIC_NAMES = {
            "Vec", "Option", "Result", "Box", "String", "HashMap",
            "Arc", "Mutex", "Rc", "RefCell", "Cow", "Rc", "Cell",
            "BTreeMap", "BTreeSet", "HashSet", "VecDeque",
        }

        def _collect_type_names(type_str: str) -> set[str]:
            names: set[str] = set()
            for m in re.finditer(r"\b([A-Z]\w+)\b", type_str or ""):
                name = m.group(1)
                if name not in _STDLIB_GENERIC_NAMES:
                    names.add(name)
            return names

        # Collect type names from parameter types
        type_names_wanted: set[str] = set()
        for p in alg.inputs or []:
            type_names_wanted |= _collect_type_names(p.rust_type)

        if not type_names_wanted:
            return "(no shared types referenced by this function's parameters)"

        # Find the types module file in the workspace
        if not hasattr(self, '_workspace_dir'):
            return "(struct context unavailable — no workspace dir)"

        types_files: list[Path] = []
        for rs in Path(self._workspace_dir).rglob("types.rs"):
            if "target" not in str(rs):
                types_files.append(rs)

        if not types_files:
            return "(no types.rs found in workspace)"

        # Walk the type-reference graph, collecting all referenced structs.
        # Depth-limited so we don't include every shared type in the workspace.
        MAX_DEPTH = 3
        all_text = "\n".join(
            tf.read_text(encoding="utf-8", errors="replace") for tf in types_files
        )
        found_defs: dict[str, str] = {}
        to_visit: list[tuple[str, int]] = [(n, 0) for n in type_names_wanted]
        while to_visit:
            name, depth = to_visit.pop()
            if name in found_defs or depth > MAX_DEPTH:
                continue
            pattern = re.compile(
                rf"((?:#\[[^\]]*\]\s*\n)*"
                rf"pub\s+(?:struct|enum|type)\s+{re.escape(name)}\b[^{{;]*"
                rf"(?:\{{[^}}]*\}}|;))",
                re.MULTILINE | re.DOTALL,
            )
            m = pattern.search(all_text)
            if not m:
                continue
            defn = m.group(0).strip()
            if len(defn) > 2000:
                defn = defn[:2000] + "\n    // ... (truncated)"
            found_defs[name] = defn
            # Recurse into types referenced inside this struct's body.
            for sub in _collect_type_names(defn):
                if sub not in found_defs:
                    to_visit.append((sub, depth + 1))

        if not found_defs:
            return "(referenced types not found in workspace types.rs)"
        # Stable order — primary type first, then alphabetical.
        primary = sorted(type_names_wanted & found_defs.keys())
        rest = sorted(k for k in found_defs if k not in type_names_wanted)
        blocks = [f"```rust\n{found_defs[n]}\n```" for n in primary + rest]

        return (
            "The following types are used in this function's parameters. "
            "Use EXACTLY these field names and types:\n\n"
            + "\n\n".join(blocks)
        )

    def _reference_prompt_block(self, alg: AlgorithmSpec) -> str:
        """Pull any matching reference implementations from the library.

        Tries two lookup paths:
          1. Direct — by algorithm name (alias-tolerant).
          2. By cited standards — e.g. spec says "RFC 1950" → Adler-32 ref.
        """
        from alchemist.references import find_references
        from alchemist.references.registry import references_for_standards

        candidates = []
        # Ephemeral probe refs take priority — they were synthesized for
        # THIS specific function from THIS specific C source, so they're
        # more precise than any category-level curated match.
        probe_ref = getattr(self, "_probe_refs", {}).get(alg.name)
        if probe_ref is not None:
            candidates.append(probe_ref)
        # Try direct name match (alias-tolerant)
        direct = find_references(alg.name)
        if direct.ok:
            candidates.extend(direct.impls)
        # Try by cited standards
        if alg.referenced_standards:
            candidates.extend(references_for_standards(alg.referenced_standards))
        # Try by category-qualified name (e.g. "adler32" from "adler32_z")
        base_name = alg.name.rstrip("_").rsplit("_", 1)[0] if "_" in alg.name else alg.name
        if base_name != alg.name:
            fallback = find_references(base_name)
            if fallback.ok:
                candidates.extend(fallback.impls)

        # Dedup keeping order
        seen: set[tuple[str, str]] = set()
        unique: list = []
        for impl in candidates:
            key = (impl.algorithm, impl.variant)
            if key not in seen:
                seen.add(key)
                unique.append(impl)

        if not unique:
            return ""
        # Limit to at most 2 variants (avoid prompt explosion on multi-variant entries)
        snippets = [impl.as_prompt_snippet() for impl in unique[:2]]
        header = (
            "\n## Reference implementation(s) — adapt to the signature above\n"
            "The following Rust is a known-good implementation of this exact algorithm. "
            "Use it as the template; change only names and signature to match the spec.\n\n"
            "CRITICAL: Do NOT redefine any constants (CRC32_TABLE, ADLER_BASE, etc.) — "
            "they are already imported via `use zlib_types::*;` and `use crate::*;`. "
            "Just USE them in your function body. Return ONLY the function definition, "
            "no const declarations, no module-level items.\n"
        )
        return header + "\n\n".join(snippets)

    def _signature_for(self, alg: AlgorithmSpec) -> str:
        params: list[str] = [f"{p.name}: {p.rust_type}" for p in alg.inputs]
        ret = alg.return_type or "()"
        if ret in ("", "()"):
            return f"pub fn {alg.name}({', '.join(params)})"
        return f"pub fn {alg.name}({', '.join(params)}) -> {ret}"

    # --- Module-item stripping ---

    @staticmethod
    def _strip_module_items(code: str) -> str:
        """Remove module-level items (use/static/const) that the LLM emits
        above the function definition.

        The LLM sometimes prefixes its response with lines like
        ``use crate::foo;`` or ``static X: u32 = ...;``.  These leak into
        the file even after a revert because ``_replace_fn_in_source`` only
        replaces from the function signature onwards.

        This helper strips such lines that appear *before* the first
        ``pub fn`` / ``fn`` line.  ``const fn`` is kept (it is a function
        qualifier, not a module-level const binding).
        """
        lines = code.splitlines(keepends=True)
        first_fn_idx: int | None = None
        for idx, line in enumerate(lines):
            stripped = line.lstrip()
            # Match "pub fn", "pub(crate) fn", "fn", "pub const fn", etc.
            if re.match(r"(?:pub\s*(?:\([^)]*\)\s*)?)?(?:async\s+|unsafe\s+|const\s+)*fn\s", stripped):
                first_fn_idx = idx
                break

        if first_fn_idx is None:
            # No function found — return as-is (caller will reject anyway)
            return code

        kept: list[str] = []
        for idx, line in enumerate(lines):
            if idx < first_fn_idx:
                stripped = line.lstrip()
                # Drop lines that are module-level items
                if re.match(r"use\s", stripped):
                    continue
                if re.match(r"static\s", stripped):
                    continue
                # "const " but NOT "const fn"
                if re.match(r"const\s", stripped) and not re.match(r"const\s+fn\s", stripped):
                    continue
            kept.append(line)
        return "".join(kept)

    # --- Source splicing ---

    _FN_BLOCK_RE = re.compile(
        r"(?P<attrs>(?:#\[[^\]]*\]\s*)*)"
        r"(?P<doc>(?:///[^\n]*\n)*)"
        r"(?P<sig>(?:pub\s+(?:\([^\)]*\)\s*)?)?"
        r"(?:async\s+|const\s+|unsafe\s+)*"
        r"fn\s+{name}\s*(?:<[^>]*>)?\s*\([^)]*\)[^{{;]*)"
        r"\{"
    )

    def _extract_fn_body(self, text: str, name: str) -> str | None:
        m = self._find_fn(text, name)
        if not m:
            return None
        start = m["body_start"]
        end = m["body_end"]
        return text[start:end]

    def _replace_fn_in_source(self, text: str, name: str, new_fn_text: str) -> str | None:
        m = self._find_fn(text, name)
        if not m:
            return None
        # Protection against body-only replacements (legacy cache format).
        # `new_fn_text` MUST be a full fn item: either starts with attrs/docs,
        # has `pub fn NAME`, `fn NAME`, or similar. If it's body-only, the
        # splice corrupts the module (observed: cache hit for crc32_combine_op
        # stored just the body, splice produced a naked body in place of the
        # fn declaration, crate lost 14+ functions in the blast radius).
        stripped = new_fn_text.strip()
        looks_like_body_only = (
            not re.search(r"\bfn\s+" + re.escape(name) + r"\b", stripped)
            and not stripped.startswith(("pub ", "#[", "///", "//!"))
        )
        if looks_like_body_only:
            return None
        # Replace the entire fn item (attrs + signature + body)
        replaced = text[:m["item_start"]] + stripped + "\n" + text[m["item_end"]:]
        # Integrity guard: LLM sometimes returns whole-file contents that
        # corrupt the module when spliced (recently observed: lib.rs loses
        # all `pub mod X;` declarations, modules lose the skeleton header).
        # Guard in two tiers:
        #   1. If the text carried required boilerplate (skeleton markers,
        #      use statements, pub mod items) — the splice MUST preserve it.
        #      Erasing a marker is a clear corruption signal.
        #   2. Length guard only applies when the original was substantially
        #      populated (>=500 chars). Below that, small test modules and
        #      freshly-scaffolded stubs are legitimately small.
        required_markers = ("#![allow(unused", "use crate::")
        for marker in required_markers:
            if marker in text and marker not in replaced:
                return None
        if len(text) >= 500 and len(replaced) < max(120, len(text) // 3):
            return None
        return replaced

    def _find_fn(self, text: str, name: str) -> dict | None:
        pat = re.compile(self._FN_BLOCK_RE.pattern.replace("{name}", re.escape(name)), re.MULTILINE)
        m = pat.search(text)
        if not m:
            return None
        sig_end = m.end()  # position just past the `{` matched by _FN_BLOCK_RE
        # _FN_BLOCK_RE's last token is `{`, so sig_end - 1 is the `{`.
        # Use string/comment-aware brace matching via the shared helper
        # (Phase 0 Bug #4: naive counter miscounted braces inside strings).
        from alchemist.implementer.scrubber import find_matching_brace
        open_brace = sig_end - 1
        body_end = find_matching_brace(text, open_brace)
        if body_end < 0:
            return None
        # Include trailing newline if present
        item_end = body_end + 1
        if item_end < len(text) and text[item_end] == "\n":
            item_end += 1
        return dict(
            item_start=m.start(),
            body_start=sig_end,
            body_end=body_end,
            item_end=item_end,
        )

    def _holistic_fix(self, crate_dir: Path, alg: AlgorithmSpec, error_ctx: str) -> None:
        """Escalation tier: whole-crate fix via the holistic fixer."""
        from alchemist.implementer.holistic import HolisticFixer
        console.print(f"  [magenta]{alg.name}: holistic escalation[/magenta]")
        spec_ctx = (
            f"Algorithm: {alg.name}\n"
            f"Category: {alg.category}\n"
            f"Description: {alg.description}\n"
            f"Standards: {', '.join(alg.referenced_standards) or '(none)'}"
        )
        # Cap holistic at 1 iteration. The fixer often returns empty patches
        # when the LLM can't diagnose the issue, and retrying wastes calls
        # without improving the state.
        fixer = HolisticFixer(llm=self.llm, max_iter=1, reject_stubs=True)
        fixer.fix_crate(crate_dir, spec_context=spec_ctx, extra_error_ctx=error_ctx)

    def _fix_missing(
        self,
        api: ApiCompletenessReport,
        specs: list[ModuleSpec],
        arch: CrateArchitecture,
        workspace_dir: Path,
    ) -> None:
        """Re-prompt for each missing function in isolation."""
        for miss in api.missing:
            crate_dir = workspace_dir / miss.crate
            # Locate the spec for this algorithm
            spec_module = next((m for m in specs if m.name == miss.module), None)
            if not spec_module:
                continue
            alg = next((a for a in spec_module.algorithms if a.name == miss.algorithm), None)
            if not alg:
                continue
            # Append a pub fn stub at the bottom of the module and try filling it
            module_path = crate_dir / "src" / f"{miss.module}.rs"
            if not module_path.exists():
                continue
            existing = module_path.read_text(encoding="utf-8")
            if f"pub fn {miss.c_function}" in existing:
                continue
            # Append a temporary stub purely so the splicer has a target to
            # replace. CRITICAL: after _fill_in_function runs, the stub must
            # either be replaced with real code OR be reverted. A surviving
            # stub in the output violates P1 (no placeholder bodies).
            sig = f"pub fn {miss.c_function}(input: &[u8]) -> u32"
            stub = (
                f"\n\n/// Auto-added stub to satisfy API completeness check.\n"
                f"/// Re-prompt will fill this in.\n"
                f"{sig} {{\n"
                f"    let _ = input;\n"
                f"    unimplemented!(\"missing fn: {miss.c_function}\")\n"
                f"}}\n"
            )
            module_path.write_text(existing + stub, encoding="utf-8")
            synthetic = AlgorithmSpec(
                name=miss.c_function,
                display_name=miss.c_function,
                category=alg.category,
                description=alg.description,
                inputs=alg.inputs,
                return_type=alg.return_type,
                referenced_standards=alg.referenced_standards,
                test_vectors=alg.test_vectors,
                mathematical_description=alg.mathematical_description,
            )
            fill_attempt = self._fill_in_function(
                synthetic, spec_module,
                next((c for c in arch.crates if c.name == miss.crate), None) or arch.crates[0],
                specs, arch, workspace_dir,
            )
            # If the fill failed, the stub is still in the file. Revert it
            # rather than leave an unimplemented!() body in the output.
            #
            # Prior bug: exact-string match on the stub message missed
            # - LLM rewording to `todo!("fixme")` or similar
            # - Whitespace changes from rustfmt
            # - Multi-statement bodies wrapping the stub
            # has_stub_for_fn handles all three via regex + canonical match.
            if not fill_attempt.tests_passed:
                after = module_path.read_text(encoding="utf-8")
                from alchemist.implementer.anti_stub import has_stub_for_fn
                if has_stub_for_fn(after, miss.c_function):
                    # Stub survived — strip it and restore prior content
                    module_path.write_text(existing, encoding="utf-8")
                    console.print(
                        f"  [red]{miss.c_function}: fill failed, stub "
                        f"removed from output (P1: no placeholder bodies)[/red]"
                    )

    def _ensure_probe_ref(self, alg: AlgorithmSpec) -> bool:
        """Lazily synthesize a reference impl for this algorithm if needed.

        Called per-function during Phase C when iter-1 fails. Skipped if:
          - a curated disk reference exists (via find_references)
          - a probe has already been attempted for this algorithm this run

        Returns True iff a new probe ref was registered (so the caller can
        re-prompt with the fresh reference on iter 2+).
        """
        if alg.name in self._probe_attempted:
            return False
        self._probe_attempted.add(alg.name)
        from alchemist.implementer.reference_probe import (
            probe_algorithm, probe_result_as_reference,
        )
        from alchemist.references.registry import find_references
        if find_references(alg.name).ok:
            return False
        if self._source_root is None:
            return False
        sig = self._signature_for(alg)
        probe = probe_algorithm(
            alg,
            source_root=self._source_root,
            llm=self.llm,
            signature=sig,
            struct_context=self._struct_context_for(alg),
            cached_context=self._cached_ctx,
        )
        ref = probe_result_as_reference(probe, sig)
        if ref is None:
            return False
        self._probe_refs[alg.name] = ref
        console.print(f"    [cyan]{alg.name}: probe-synthesized ref injected[/cyan]")
        return True

    def _backfill_fuzz_vectors(self, specs: list[ModuleSpec]) -> None:
        """For every algorithm with no test_vectors, try fuzz generation.

        Uses the C reference DLL (if discoverable) to compute ground-truth
        outputs for random inputs. Vectors are appended in-place so Phase B
        will emit real correctness tests.

        Currently supports: checksum-category functions against zlib1.dll.
        Extensible to hash/cipher/compression as bindings are added.
        """
        from alchemist.standards import lookup_test_vectors
        # Look up the C DLL for this translation. Convention: subjects
        # directory contains a reference DLL the extractor can see.
        # For zlib we use verify/zlib1.dll.
        dll_path = self._locate_c_dll()
        if dll_path is None or not dll_path.exists():
            return
        try:
            from alchemist.extractor.fuzz_vectors import (
                load_zlib_dll, ZLIB_BINDINGS, ZLIB_PURE_REFERENCES, fuzz_for_spec,
                ZLIB_BYTE_TRANSFORMS, fuzz_byte_transform,
            )
            from alchemist.extractor.state_mutator import (
                ZLIB_STATE_MUTATORS, fuzz_state_mutator,
            )
            from alchemist.extractor.c_shim_fuzz import (
                ZLIB_SHIM_BINDINGS, ZLIB_SHIM_PURE_BINDINGS,
                ZLIB_SHIM_OBSERVER_BINDINGS,
                locate_zlib_shim, _load_shim,
                fuzz_with_shim, fuzz_pure_shim, fuzz_observer_shim,
            )
            dll = load_zlib_dll(dll_path)
            # Load the C shim DLL if present — its C-reference-based
            # state-mutator vectors take priority over Python reference ports.
            shim_path = locate_zlib_shim()
            shim_dll = _load_shim(shim_path) if shim_path else None
        except Exception as e:  # noqa: BLE001
            console.print(f"  [dim]fuzz backfill skipped: {e}[/dim]")
            return
        added = 0
        for mod in specs:
            for alg in mod.algorithms:
                if alg.test_vectors:
                    continue
                # C-shim pure-function path
                if shim_dll is not None and alg.name in ZLIB_SHIM_PURE_BINDINGS:
                    vectors = fuzz_pure_shim(
                        shim_dll, alg, ZLIB_SHIM_PURE_BINDINGS[alg.name],
                    )
                    if vectors:
                        alg.test_vectors = vectors
                        added += len(vectors)
                    continue
                # C-shim state-mutator path
                if shim_dll is not None and alg.name in ZLIB_SHIM_BINDINGS:
                    vectors = fuzz_with_shim(
                        shim_dll, alg, ZLIB_SHIM_BINDINGS[alg.name],
                    )
                    if vectors:
                        alg.test_vectors = vectors
                        added += len(vectors)
                    continue
                # C-shim state-observer path (state_in -> scalar return)
                # DISABLED 2026-04-22: emitter writes state.<flat_field_name>
                # but DeflateState uses structured fields (e.g., dyn_ltree:
                # Vec<(u16,u16)>, not dyn_ltree_freq: Vec<u16>). Also hardport
                # returns u8 while binding declares i32. Re-enable once
                # CShimField supports a rust_write_template / field mapping.
                # See detect_data_type for the test case.
                if False and shim_dll is not None and alg.name in ZLIB_SHIM_OBSERVER_BINDINGS:
                    vectors = fuzz_observer_shim(
                        shim_dll, alg, ZLIB_SHIM_OBSERVER_BINDINGS[alg.name],
                    )
                    if vectors:
                        alg.test_vectors = vectors
                        added += len(vectors)
                    continue
                # Byte-buffer transformation path — fn mutates a slice
                # argument or compares two slices. Runs BEFORE the generic
                # pure-reference/catalog paths so zmem* don't fall into
                # the scalar-only fuzz_for_spec codepath that skips them.
                if alg.name in ZLIB_BYTE_TRANSFORMS:
                    vectors = fuzz_byte_transform(
                        alg, ZLIB_BYTE_TRANSFORMS[alg.name],
                    )
                    if vectors:
                        alg.test_vectors = vectors
                        added += len(vectors)
                    continue
                # State-mutator path (fallback): Python reference port
                state_binding = ZLIB_STATE_MUTATORS.get(alg.name)
                if state_binding is not None:
                    vectors = fuzz_state_mutator(alg, state_binding)
                    if vectors:
                        alg.test_vectors = vectors
                        added += len(vectors)
                    continue
                # Only skip catalog lookup when catalog vectors will
                # actually be emitted — i.e., the function can consume
                # a byte-slice input. Scalar-arg functions like
                # crc32_combine_gen64 match the catalog by name but
                # can't use the byte-slice tests, so they still need
                # fuzz-generated vectors.
                from alchemist.implementer.test_generator import (
                    _can_accept_byte_slice,
                )
                if _can_accept_byte_slice(alg) and lookup_test_vectors(alg.name):
                    continue
                vectors = fuzz_for_spec(
                    dll, alg, ZLIB_BINDINGS,
                    pure_references=ZLIB_PURE_REFERENCES,
                )
                if vectors:
                    alg.test_vectors = vectors
                    added += len(vectors)
        if added:
            console.print(f"  [cyan]fuzz backfill: generated {added} vectors[/cyan]")

    def _locate_c_dll(self) -> Path | None:
        """Find a C reference library for fuzz-vector generation.

        Today: hardcoded to verify/zlib1.dll for the zlib subject. Phase 1
        will replace this with auto-build via `build_c_dll.py`.
        """
        candidates = [
            Path("verify/zlib1.dll"),
            Path("verify/zlib.so"),
            Path("verify/libz.so"),
        ]
        for c in candidates:
            if c.exists():
                return c.resolve()
        return None

    def _build_project_context(
        self, specs: list[ModuleSpec], architecture: CrateArchitecture,
    ) -> str:
        lines = [
            f"## Workspace: {architecture.workspace_name}",
            f"## Crates",
        ]
        for c in architecture.crates:
            lines.append(f"  - {c.name}: {c.description[:150]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cargo helpers
# ---------------------------------------------------------------------------

def _run_cargo_all_targets(path: Path, timeout: int = 300) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["cargo", "check", "--all-targets"],
            cwd=str(path),
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return r.returncode == 0, r.stderr
    except Exception as e:
        return False, str(e)


def _is_windows_link_lock(stderr: str) -> bool:
    """LNK1104 on Windows means the linker couldn't open a target .exe
    because another process (antivirus, still-exiting test binary) has
    a file lock. Not a real test failure — retry after a brief pause."""
    return "LNK1104" in (stderr or "") or "cannot open file" in (stderr or "")


def _kill_target_zombies(workspace_dir: Path) -> int:
    """Kill any running process whose image lives under workspace_dir/target/.

    libtest can leave worker-process orphans when a cargo test is killed
    or times out. Those orphans hold file locks on the test .exe, causing
    LNK1104 on the next re-link. Proactive kill before each cargo run.
    Only implemented on Windows where this is a real issue.
    """
    if not sys.platform.startswith("win"):
        return 0
    try:
        target = (workspace_dir / "target").resolve()
    except Exception:
        return 0
    target_prefix = str(target).lower()
    try:
        import psutil  # type: ignore
    except ImportError:
        return 0
    killed = 0
    for p in psutil.process_iter(["pid", "exe"]):
        try:
            exe = (p.info.get("exe") or "").lower()
            if exe.startswith(target_prefix):
                p.kill()
                killed += 1
        except Exception:
            pass
    return killed


def _cargo_with_link_retry(argv: list[str], path: Path, timeout: int,
                           max_retries: int = 3) -> tuple[int, str, str]:
    """Run a cargo command with Windows LNK1104 retry-with-backoff.

    Before each attempt, proactively kills any lingering test-binary
    zombies from prior runs that would hold file locks.
    """
    import time
    last = (1, "", "")
    for attempt in range(max_retries):
        _kill_target_zombies(path)
        try:
            r = subprocess.run(
                argv, cwd=str(path),
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
        except Exception as e:
            return 1, "", str(e)
        if r.returncode == 0 or not _is_windows_link_lock(r.stderr):
            return r.returncode, r.stdout, r.stderr
        # Transient link lock; back off 0.5s, 1.5s, 3.0s
        time.sleep(0.5 * (3 ** attempt))
        last = (r.returncode, r.stdout, r.stderr)
    return last


def _run_cargo_test(path: Path, timeout: int = 600) -> tuple[bool, str, str]:
    rc, stdout, stderr = _cargo_with_link_retry(
        ["cargo", "test", "--workspace", "--no-fail-fast"], path, timeout,
    )
    return rc == 0, stdout, stderr


def _run_cargo_test_filter(path: Path, test_filter: str | list[str], timeout: int = 300) -> tuple[bool, str, str]:
    """Run `cargo test` filtered to specific name prefixes.

    `test_filter` may be a single string or a list of strings. On Windows,
    cargo only accepts one positional TESTNAME before `--`; multiple
    positional args are rejected. The Rust test binary, however, treats
    non-flag args as OR'd filters. So we pass all filters after `--`.

    Using multiple filters lets us target several non-overlapping test-
    name families simultaneously — important when the short name of fn A
    is a prefix of fn B (e.g., `crc32` vs `crc32_combine_op`). Without
    this, `test_crc32_` would sweep in `test_crc32_combine_op_*` and
    falsely attribute sibling failures to the function under test.
    """
    if isinstance(test_filter, str):
        filters = [test_filter]
    else:
        filters = [f for f in test_filter if f]
    cmd = ["cargo", "test", "--", *filters, "--nocapture"]
    rc, stdout, stderr = _cargo_with_link_retry(cmd, path, timeout)
    return rc == 0, stdout, stderr


def _test_filters_for_fn(fn_name: str) -> list[str]:
    """Return the set of cargo test substring filters that match THIS
    function's tests and no sibling function's tests.

    Test-name schemes in use:
      test_<fn>_vec_<desc>     — catalog vectors
      test_<fn>_spec_<idx>     — spec.test_vectors
      test_<fn>_state_<idx>    — state-mutator vectors
      test_<fn>_observer_<idx> — observer vectors
      smoke_<fn>               — legacy smoke test (rare)
    """
    return [
        f"test_{fn_name}_vec_",
        f"test_{fn_name}_spec_",
        f"test_{fn_name}_state_",
        f"test_{fn_name}_observer_",
        f"smoke_{fn_name}",
    ]


def _top_lines(text: str, n: int) -> str:
    return "\n".join((text or "").splitlines()[:n])


def _unwrap_llm_schema_leak(s: str) -> str:
    """Strip JSON-schema wrappers an LLM sometimes emits verbatim.

    Maverick occasionally responds with the raw tool_schema JSON
    (`{"type": "object", "properties": {"content": {"value": "..."}}}`)
    instead of the structured output itself. Parse for the embedded
    `pub fn ...` body, or the `value` / `content` leaf.
    """
    if not s or "pub fn" in s and not s.lstrip().startswith("{"):
        return s
    # Attempt JSON parse
    stripped = s.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            import json as _json
            obj = _json.loads(stripped)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            # Schema-leak shape: {"properties": {"content": {"value": "..."}}}
            props = obj.get("properties")
            if isinstance(props, dict):
                for key in ("content", "impl", "fn"):
                    node = props.get(key)
                    if isinstance(node, dict):
                        v = node.get("value") or node.get("default") or node.get("example")
                        if isinstance(v, str) and "pub fn" in v:
                            return v.strip()
            # Direct value: {"content": "pub fn ..."}
            for key in ("content", "impl", "fn"):
                v = obj.get(key)
                if isinstance(v, str) and "pub fn" in v:
                    return v.strip()
    # Last resort: regex-extract the first `pub fn ...` block.
    import re as _re
    m = _re.search(r"pub\s+fn\s+\w+", s)
    if m:
        return s[m.start():].strip()
    return s
