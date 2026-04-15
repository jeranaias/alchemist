# Alchemist Architecture

This document maps the pipeline stages to their key modules, the gates that enforce correctness, and the data that flows between them.

## Six stages

```
              ┌──────────────┐
source  ───►  │ 1. ANALYZE   │ ───► analysis.json (tree-sitter, call graph, modules)
              └──────────────┘
                     │
                     ▼
              ┌──────────────┐
              │ 2. EXTRACT   │ ───► specs/*.json (per-module FunctionSpecs + AlgorithmSpecs)
              └──────────────┘
                     │
                     ▼  spec_validator (catches BASE=255 et al.)
              ┌──────────────┐
              │ 3. ARCHITECT │ ───► architecture.json (crates, deps, traits, errors)
              └──────────────┘
                     │
                     ▼  architecture validator (dep DAG, orphan rule, ...)
              ┌──────────────┐
              │ 4. IMPLEMENT │ ───► output/ (Rust workspace)
              └──────────────┘
                     │   ├── 4A: skeleton  (deterministic, types + unimplemented!())
                     │   ├── 4B: tests     (from catalog + spec.test_vectors)
                     │   └── 4C: per-fn TDD loop (+ anti-stub + holistic escalation)
                     ▼
              ┌──────────────┐
              │ 5. VERIFY    │ ───► VerificationReport (compile / anti-stub / test / diff)
              └──────────────┘
                     │   ▲
                     ▼   └── Stage 5 REFUSES success without diff_config
              ┌──────────────┐
              │ 6. REPORT    │ ───► alchemist-report.json (metrics dashboard)
              └──────────────┘
```

## Modules at a glance

| Stage | Primary modules | Determinism |
|-------|-----------------|-------------|
| 1 Analyze | `analyzer/parser.py`, `analyzer/call_graph.py`, `analyzer/module_detector.py` | Deterministic (tree-sitter). No LLM. |
| 2 Extract | `extractor/spec_extractor.py`, `extractor/spec_validator.py` | LLM per function, then deterministic validation. |
| 3 Architect | `architect/crate_designer.py`, `architect/validator.py`, `architect/field_scanner.py` | LLM design, deterministic validation + field scan. |
| 4 Implement | `implementer/skeleton.py`, `implementer/test_generator.py`, `implementer/tdd_generator.py`, `implementer/anti_stub.py`, `implementer/api_completeness.py`, `implementer/scrubber.py`, `implementer/holistic.py` | Skeleton + tests deterministic; per-fn bodies LLM-driven; anti-stub + completeness deterministic. |
| 5 Verify | `verifier/auto_ffi.py`, `verifier/proptest_gen.py`, `verifier/differential_tester.py` | Deterministic: builds C DLL, emits Rust harness, runs `cargo test`. |
| 6 Report | `reporter/metrics.py` | Deterministic. |

## Gates (the "refuses success" rule)

Five gates must pass before `TranslationReport.ok == True`:

1. **Spec validator** — declared constants vs standards catalog (catches BASE=255).
2. **Architecture validator** — no dep cycles, no orphan-rule violations, every crate module resolves to a spec or an algorithm or an infrastructure name.
3. **Anti-stub** — zero `unimplemented!()`, zero "we don't have the algorithm" comments, zero silent `Ok(())` on fns that ignore their inputs.
4. **Test gate** — `cargo test --workspace` exits 0.
5. **Differential gate** — FFI-compiled C reference + generated proptest harness, ≥10K random inputs per algorithm match byte-exact (or within configured ULP tolerance for FP).

`run_translate_all` chains these; the first `ok=False` short-circuits the rest so you see the actual root cause, not cascaded failures.

## Data flow — types

```
AnalysisDict (stage 1 JSON)
  ├─ files[path] → { functions, structs, typedefs, globals, includes }
  ├─ call_graph
  └─ modules[] → { name, category, functions, files }

    │
    ▼  (spec_extractor, per-fn LLM calls)

list[ModuleSpec]
  └─ algorithms[] → AlgorithmSpec
        ├─ inputs[] → Parameter(name, rust_type, description, direction)
        ├─ return_type, category, referenced_standards
        ├─ test_vectors[] → TestVector(inputs: dict, expected_output, tolerance)
        └─ source_functions[] → ["adler32", "crc32", ...]

    │
    ▼  (crate_designer, LLM)

CrateArchitecture
  ├─ crates[] → CrateSpec(name, modules, dependencies, is_no_std, ...)
  ├─ traits[], error_types[]
  └─ ownership_decisions[]

    │
    ▼  (validator + field_scanner + TDDGenerator)

Rust workspace on disk
  └─ <crate>/src/<module>.rs (real implementations + #[cfg(test)] blocks)

    │
    ▼  (differential_tester + auto_ffi + proptest_gen)

VerificationReport
  └─ compile / anti_stub / test / differential gates
```

## LLM boundary

Alchemist uses the local LLM (`alchemist.llm.client.AlchemistLLM`) at three points:

1. **Stage 2** — one call per significant C function to produce a `FunctionSpec`.
2. **Stage 3** — one call per workspace to produce the `CrateArchitecture`.
3. **Stage 4C** — up to `max_iter_per_fn` calls per algorithm in the TDD loop, with test-failure context fed back in each iteration. Plus one escalation call to the holistic fixer after `holistic_after` iterations.

Stages 4A (skeleton) and 4B (tests) are fully deterministic. Stage 5 runs no LLM — it just exercises the generated code against C.

Key invariants:
- An 8-character nonce is prepended to every user message so server-side response caches miss.
- `temperature=0.15` is used everywhere to bypass vLLM prefix-cache-at-zero behavior.
- Both `message.content` and `message.reasoning` fields are read (Qwen3.5 reasoning-parser quirk).
- Per-function Stage 2 checkpoints land in `.alchemist/specs/_functions/<mod>/<fn>.json` — the pipeline is resumable across crashes.

## Extension points

- **New domain (crypto, networking, RTOS, FP math)** — write a `DomainPlugin` (see `docs/plugins.md`). Register via entry-points in your `pyproject.toml`.
- **New differential config** — subclass or instantiate `DifferentialConfig` with your library's FFI signatures and `AlgorithmHarness` list. `alchemist/verifier/zlib_config.py` is the reference.
- **New standards catalog entry** — drop a JSON file in `alchemist/standards/`, add the filename to `_CATALOG_FILES` in `catalog.py`, and every algorithm name alias gets routed to it automatically.

## What does NOT belong in the core

- Hand-tuned algorithm ports. If you've got a verified-correct Rust implementation, ship it as its own crate and skip Alchemist — that's what it's FOR producing.
- Anything that calls cloud APIs. The "local 122B only" invariant is non-negotiable.
- Language-specific hacks (adding string-mode patches, special-casing function names). The system works because every stage is driven by schemas, not string-matching. Adding a "when the function name contains X" hack anywhere is a signal the schema is missing a field.
