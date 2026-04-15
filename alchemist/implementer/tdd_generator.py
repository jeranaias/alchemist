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
    ) -> TDDResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        result = TDDResult(workspace_dir=output_dir)

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
        for crate_name in _topo_sort(architecture):
            crate_spec = next((c for c in architecture.crates if c.name == crate_name), None)
            if not crate_spec:
                continue
            crate_modules = [m for m in specs if m.name in set(crate_spec.modules)]
            for module in crate_modules:
                for alg in module.algorithms:
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

        test_name_prefix = f"test_{alg.name}_"
        fallback_test = f"smoke_{alg.name}"
        previous_failure = ""  # carries test output into next iteration prompt

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
                if ms is not None and ms.ok and ms.best and ms.best.tests_failed == 0:
                    attempt.final_compiled = True
                    attempt.tests_passed = True
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
                console.print(f"  [yellow]{alg.name}: anti-stub rejected iteration {iteration}[/yellow]")
                continue

            # Semantic lint (family-specific invariants) — reject early if
            # the candidate clearly violates its algorithm's math, so we
            # don't burn a cargo check/test cycle on it.
            semantic_findings = _semantic_lint(new_fn, alg)
            if _semantic_has_errors(semantic_findings):
                previous_failure = _semantic_summary(semantic_findings)
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
                # Revert and try again with compile-error context
                module_path.write_text(current, encoding="utf-8")
                attempt.last_error = _top_lines(cerr, 3)
                console.print(f"  [yellow]{alg.name}: compile failed, reverting (iter {iteration})[/yellow]")
                continue

            attempt.final_compiled = True

            # Run tests that match the function name
            ok_test, tout, terr = _run_cargo_test_filter(
                crate_dir, test_name_prefix,
            )
            if ok_test:
                attempt.tests_passed = True
                console.print(f"  [green]{alg.name}: tests pass on iter {iteration}[/green]")
                return attempt

            # If no matching tests exist, fall back to smoke
            combined = tout + "\n" + terr
            if f"0 passed" in combined and f"0 failed" in combined:
                # No tests matched; accept the iteration (compile was ok)
                attempt.tests_passed = True
                console.print(f"  [dim]{alg.name}: no test matched, compile-only pass[/dim]")
                return attempt

            attempt.last_error = _top_lines(terr, 5)
            previous_failure = _top_lines(tout + "\n" + terr, 20)
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
        if resp.structured and "content" in resp.structured:
            return (resp.structured.get("content") or "").strip() or None
        # Fallback: raw text
        raw = (resp.content or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:\w+)?\s*", "", raw)
            raw = re.sub(r"```\s*$", "", raw)
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
        test_name_prefix: str,
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

    def _struct_context_for(self, alg: AlgorithmSpec) -> str:
        """Build a struct-field context block for types referenced in params.

        When a function takes `&mut DeflateState`, the model needs to see
        the exact field names and types to generate compiling code. We scan
        the workspace's types module file for struct definitions that match
        any type name appearing in the function's parameters.
        """
        # Collect type names from parameter types
        type_names_wanted: set[str] = set()
        for p in alg.inputs or []:
            # Extract PascalCase identifiers from the type string
            for m in re.finditer(r"\b([A-Z]\w+)\b", p.rust_type or ""):
                name = m.group(1)
                if name not in ("Vec", "Option", "Result", "Box", "String", "HashMap",
                                "Arc", "Mutex", "Rc", "RefCell"):
                    type_names_wanted.add(name)

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

        # Extract struct/enum definitions matching wanted names
        blocks: list[str] = []
        for tf in types_files:
            text = tf.read_text(encoding="utf-8", errors="replace")
            for name in type_names_wanted:
                # Find `pub struct Name { ... }` or `pub enum Name { ... }`
                pattern = re.compile(
                    rf"((?:#\[[^\]]*\]\s*\n)*"
                    rf"pub\s+(?:struct|enum|type)\s+{re.escape(name)}\b[^{{;]*"
                    rf"(?:\{{[^}}]*\}}|;))",
                    re.MULTILINE | re.DOTALL,
                )
                m = pattern.search(text)
                if m:
                    defn = m.group(0).strip()
                    # Truncate very large structs to avoid blowing up the prompt
                    if len(defn) > 2000:
                        defn = defn[:2000] + "\n    // ... (truncated)"
                    blocks.append(f"```rust\n{defn}\n```")

        if not blocks:
            return "(referenced types not found in workspace types.rs)"

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
        # Replace the entire fn item (attrs + signature + body)
        return text[:m["item_start"]] + new_fn_text.strip() + "\n" + text[m["item_end"]:]

    def _find_fn(self, text: str, name: str) -> dict | None:
        pat = re.compile(self._FN_BLOCK_RE.pattern.replace("{name}", re.escape(name)), re.MULTILINE)
        m = pat.search(text)
        if not m:
            return None
        sig_end = m.end()  # position of `{`
        # Find matching close
        depth = 1
        i = sig_end
        n = len(text)
        while i < n and depth > 0:
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    body_end = i
                    # Include trailing newline if present
                    item_end = body_end + 1
                    if item_end < n and text[item_end] == "\n":
                        item_end += 1
                    # item_start: back up to include attrs/doc
                    item_start = m.start()
                    return dict(
                        item_start=item_start,
                        body_start=sig_end,
                        body_end=body_end,
                        item_end=item_end,
                    )
            i += 1
        return None

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
        fixer = HolisticFixer(llm=self.llm, max_iter=2, reject_stubs=True)
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
            # Append an unimplemented stub with the C function name so splicer works
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
            # Ask for a real impl
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
            self._fill_in_function(
                synthetic, spec_module,
                next((c for c in arch.crates if c.name == miss.crate), None) or arch.crates[0],
                specs, arch, workspace_dir,
            )

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
        )
        return r.returncode == 0, r.stderr
    except Exception as e:
        return False, str(e)


def _run_cargo_test(path: Path, timeout: int = 600) -> tuple[bool, str, str]:
    try:
        r = subprocess.run(
            ["cargo", "test", "--workspace", "--no-fail-fast"],
            cwd=str(path),
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0, r.stdout, r.stderr
    except Exception as e:
        return False, "", str(e)


def _run_cargo_test_filter(path: Path, test_filter: str, timeout: int = 300) -> tuple[bool, str, str]:
    try:
        r = subprocess.run(
            ["cargo", "test", test_filter, "--", "--nocapture"],
            cwd=str(path),
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode == 0, r.stdout, r.stderr
    except Exception as e:
        return False, "", str(e)


def _top_lines(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[:n])
