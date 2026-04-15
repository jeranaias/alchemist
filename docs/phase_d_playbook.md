# Phase D playbook: proving a translation is verified-correct

Phase D is the acceptance bar for "Alchemist works on this codebase." It verifies the full pipeline (analyze → extract → architect → implement → verify) produces Rust that passes every gate against a C reference.

This playbook documents how to run Phase D for any C library.

## 1. Get the source under `subjects/`

```bash
git clone https://github.com/… subjects/mylib
# or just copy files in
```

Alchemist will not modify the C source — it only reads it.

## 2. Sanity-check with `alchemist inspect`

```bash
alchemist inspect subjects/mylib
```

If `Modules detected: 0`, the module detector couldn't find algorithmic clusters. Either the library is too trivial (single file, few fns) or all your code lives under `test/` / `contrib/` (which Alchemist filters by default).

## 3. Run Stages 1-3 first (cheap, LLM-only)

```bash
alchemist translate subjects/mylib --name mylib-rs --stages 1-3
```

This runs analyze + extract + architect. Budget: ~1 min of LLM time per ~500 lines of C.

Check `subjects/mylib/.alchemist/architecture.json` — make sure the crate layout makes sense for your library. If the architect produced a weird design, edit the JSON manually (it's the source of truth for Stage 4) or re-run `--stages 3-3`.

## 4. Run Stage 4 (TDD code generation)

```bash
alchemist translate subjects/mylib --name mylib-rs --stages 4-4
```

Budget per function in the typical case:
- 1 LLM call generates the implementation (~5-15 s)
- `cargo check` on the crate (~5 s)
- `cargo test` filtered to that fn's tests (~5 s)
- If tests fail, up to `max_iter_per_fn=5` more iterations
- If still failing, one holistic-fix escalation

Practical total: 30 s - 3 min per function, so a 10-algorithm library is ~5-30 min.

## 5. Build a `DifferentialConfig` for your library

Stage 5's differential gate refuses success without one. For zlib, `alchemist.verifier.zlib_config.zlib_diff_config()` exists. For your library, write its analog:

```python
# mylib_config.py
from pathlib import Path
from alchemist.verifier.auto_ffi import CSignature
from alchemist.verifier.differential_tester import DifferentialConfig
from alchemist.verifier.proptest_gen import AlgorithmHarness

def mylib_diff_config(c_source_dir: Path) -> DifferentialConfig:
    return DifferentialConfig(
        c_sources=sorted(c_source_dir.glob("*.c")),
        c_include_dirs=[c_source_dir],
        c_public_signatures=[
            CSignature(name="my_fn", return_type="uint32_t",
                        params=[("buf", "const uint8_t *"), ("len", "size_t")]),
        ],
        harnesses=[
            AlgorithmHarness(
                algorithm="my_fn", category="checksum",
                rust_call="rust_my_fn(&input)",
                c_call="c_my_fn(&input)",
            ),
        ],
        ffi_crate_name="c_mylib_ref",
    )
```

## 6. Run Stage 5 + 6

```python
from pathlib import Path
from alchemist.pipeline import run_verify_stage
from mylib_config import mylib_diff_config

source = Path("subjects/mylib")
output = source / ".alchemist" / "output"

outcome = run_verify_stage(
    c_source_dir=source,
    output=output,
    diff_config=mylib_diff_config(source),
    refuse_without_diff=True,
)
print(outcome.summary)
```

Expected: `ok=True` only when every gate passes — compile clean, no anti-stub violations, `cargo test` green, ≥10K random inputs through the differential harness all match the C reference.

## 7. Troubleshoot

| Symptom | Where to look |
|---------|---------------|
| Analyze found 0 modules | Source layout doesn't match detector heuristics. Point at the right dir. |
| Spec validator errors | `.alchemist/specs/_functions/<mod>/<fn>.json` — may have hallucinated constants. |
| Architect validator errors | `.alchemist/architecture.json` — wrong module assignments. |
| Stage 4 stuck on one fn | LLM is fighting the test — check `spec.test_vectors` and `mathematical_description`, add more detail. Try `--stages 4-4` to resume. |
| Anti-stub rejects everything | Raise `max_iter_per_fn` in Python; or the spec is too vague — flesh out `algorithm_notes`. |
| Differential fails on byte-1 cases | Off-by-one in seed handling — common LLM mistake. Check `spec.test_vectors` for empty-input case. |

## 8. Acceptance criteria (Phase D is met when)

For your chosen library, all of the following are TRUE in a single `run_translate_all` invocation:

- [x] `analyze` found ≥1 module
- [x] `extract` produced specs, `spec_validator` OK
- [x] `architect` architecture validates without errors
- [x] `implement` (TDD) — 100% of `spec.source_functions` have `pub fn`
- [x] `implement` — anti-stub gate passes (0 violations)
- [x] `verify` — compile clean on workspace
- [x] `verify` — every `cargo test` passes
- [x] `verify` — 10K-case differential vs C passes every algorithm

When all eight are green, the `TranslationReport.ok` is True and the Rust is "verified correct" to the production bar.

## 9. Known reference runs

| Library | Status | Notes |
|---------|--------|-------|
| `subjects/tinychk/` | reference | Tiny 3-algorithm test subject (adler32, crc32, fletcher16). Use for smoke runs. |
| `subjects/zlib/` | Phase D pending full run | Pre-Phase-A output exists with known stubs. Stage 5 gate against that output is an acceptance test (see `tests/test_phase_d_zlib.py`). |
| `subjects/mbedtls/` | Phase E target | Not yet translated. Will exercise the `crypto` plugin's CAVP + constant-time lint. |
