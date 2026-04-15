# API Reference

Alchemist is a CLI plus a Python library. Most users run `alchemist translate`, but everything is scriptable.

## Top-level: `alchemist.pipeline`

### `run_translate_all(source, name, output, stages=(1,6), enforce_validator=True, refuse_without_diff=True)`
End-to-end pipeline. Returns `TranslationReport` whose `.ok` is `True` only if every stage passes.

```python
from pathlib import Path
from alchemist.pipeline import run_translate_all

report = run_translate_all(
    source=Path("./zlib"),
    name="zlib-rs",
    output=Path("./zlib/.alchemist/output"),
)
print(report.summary())
```

### `run_analyze(source, config=None)`
Stage 1 only. Returns `{"files": ..., "call_graph": ..., "modules": ...}`.

### `run_architect_stage(source, name, config=None, enforce=True)`
Stage 3. Returns `(outcome, arch)`. When `enforce=True`, validator errors fail the outcome.

### `run_implement_stage(source, output, tdd=True, config=None)`
Stage 4. `tdd=True` selects the TDD generator (default). `tdd=False` falls back to the legacy per-file generator.

### `run_verify_stage(c_source_dir, output, diff_config=None, refuse_without_diff=True)`
Stage 5 gate. Without `diff_config`, `refuse_without_diff=True` forces failure.

## Stage 5: `alchemist.verifier`

### `DifferentialConfig`
Carries: C sources, includes, public signatures, typedefs, opaque types, and per-algorithm `AlgorithmHarness` list.

### `verify_workspace(rust_workspace, diff_config=None, refuse_without_diff=True) -> VerificationReport`
Four gates run in order: compile → anti-stub → test → differential. `report.passed` is `True` only if all four pass.

### `AutoFfiRequest` / `generate_ffi_crate(request) -> AutoFfiResult`
Builds a Rust FFI crate that links to a compiled C shared library. Used internally by `verify_workspace` but safe to call directly.

### `zlib_diff_config(c_source_dir, include_dirs=None) -> DifferentialConfig`
Pre-configured `DifferentialConfig` for zlib (`alchemist.verifier.zlib_config`).

## Stage 4: `alchemist.implementer`

### `skeleton.generate_workspace_skeleton(specs, arch, output_dir, cargo_check=True) -> WorkspaceSkeletonResult`
Deterministic: produces types + sigs + `unimplemented!()` bodies. `result.ok` is `True` only if `cargo check --workspace` succeeds.

### `test_generator.generate_tests_for_workspace(specs, arch, workspace_dir, enable_smoke=False)`
Appends `#[cfg(test)]` blocks sourced from `spec.test_vectors` + the standards catalog.

### `tdd_generator.TDDGenerator(config, llm, max_iter_per_fn=5, holistic_after=3)`
Per-function TDD loop. `generate_workspace(specs, arch, output_dir) -> TDDResult`.

### `anti_stub.scan_workspace(workspace_dir) -> ScanReport`
Scans every `.rs` in the workspace for stub markers. Report has `.ok`, `.violations`, `.summary()`.

### `api_completeness.check_workspace(specs, arch, workspace_dir) -> ApiCompletenessReport`
Verifies every `spec.source_functions` has a `pub fn` somewhere in its crate.

### `holistic.HolisticFixer(llm, max_iter=3, reject_stubs=True).fix_crate(crate_dir, spec_context="", extra_error_ctx="")`
Whole-crate escalation fixer. Invoked automatically after N missed iterations; can also be called directly.

## Stage 2: `alchemist.extractor`

### `spec_validator.validate_specs(specs) -> SpecValidationReport`
Cross-checks declared constants + test vectors against the standards catalog. Catches BASE=255 pre-implementation.

## Standards: `alchemist.standards`

### `lookup_test_vectors(algorithm) -> list[TestVector]`
Returns authoritative test vectors. Algorithm name matching is case/alias/snake-case-insensitive.

### `list_algorithms() -> list[str]`
Every canonical algorithm with at least one catalog vector.

### `match_algorithm(name) -> str | None`
Map an extractor-supplied name to the canonical catalog key.

## Plugins: `alchemist.plugins`

### `DomainPlugin(name, description, post_extract=None, post_skeleton=None, lints=None)`
Plugin contract. All fields except `name`/`description` are optional.

### `register(plugin)`, `get(name)`, `list_plugins()`, `clear()`
Registry operations.

### `load_builtins()`, `load_entry_points()`, `load_all()`
Discovery — `load_all()` is what the CLI calls.

### `run_lints(workspace_dir, specs) -> list[tuple]`
Runs every registered plugin's lints and aggregates findings. Plugin crashes are captured as findings, not re-raised.

## LLM: `alchemist.llm.client`

### `AlchemistLLM(config=None).call_structured(messages, tool_name, tool_schema, cached_context, max_tokens, temperature)`
Structured JSON call. Auto-injects cache-buster nonce. Parses JSON out of markdown fences or thinking tags. Repairs truncated JSON.

### `wait_for_server(max_wait=300, check_interval=10) -> bool`
Blocks until the local vLLM server responds.

## Return-report shapes

| Shape | Field | Meaning |
|-------|-------|---------|
| `TranslationReport` | `.ok`, `.outcomes`, `.first_failure()`, `.summary()` | Top-level pipeline result |
| `VerificationReport` | `.passed`, `.compile/.anti_stub/.test/.differential`, `.first_failure`, `.summary()` | Stage 5 result |
| `ScanReport` | `.ok`, `.violations`, `.by_pattern()`, `.summary()` | Anti-stub result |
| `ApiCompletenessReport` | `.ok`, `.expected`, `.found`, `.missing` | API check result |
| `TDDResult` | `.ok`, `.skeleton`, `.attempts`, `.api_report`, `.workspace_compiles`, `.workspace_tests_passed` | TDD loop result |
| `ValidationReport` | `.has_errors`, `.errors`, `.warnings`, `.summary()` | Architecture validator |
| `SpecValidationReport` | `.ok`, `.errors`, `.warnings` | Spec validator |
