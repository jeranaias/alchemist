# Troubleshooting

## `alchemist doctor` shows a FAIL

### `cargo` / `rustc` not found
Install via rustup:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

On Windows, use the `rustup-init.exe` installer from [rustup.rs](https://rustup.rs/).

### `gcc` not found
- **Windows**: Install [MSYS2](https://www.msys2.org/) and `pacman -S mingw-w64-ucrt-x86_64-gcc`, or use Strawberry Perl's bundled gcc.
- **Linux**: `apt install build-essential` / `dnf install gcc`
- **macOS**: `xcode-select --install`

### `local LLM server` unreachable
Start the RigRun server. Health check: `curl http://100.109.172.64:8090/health` must return `"deep": "up"`.

If the host IP differs for your setup, set `ALCHEMIST_ENDPOINT=http://your-host:8090/v1` before running Alchemist.

### `plugins` fails to load
Usually means a third-party plugin crashed during discovery. Run Python directly to surface the traceback:

```bash
python -c "from alchemist.plugins import load_all; load_all()"
```

## Stage 1 (Analyze) found 0 modules

Module detection filters by size and call graph connectivity. Possible causes:

- **All C files are under `test/` or `contrib/`** — Alchemist skips those by default. Move source to the top level or use `--source` to point at the real source dir.
- **All functions are tiny (<5 lines)** — the detector treats them as glue.
- **C source uses unusual preprocessor tricks** — run `alchemist inspect <src>` to see what the parser found.

## Stage 2 (Extract) — "spec validation failed: BASE=..."

The spec validator caught a constant that conflicts with the referenced standard. Example: the LLM extracted `BASE = 255` for Adler-32 while RFC 1950 specifies `BASE = 65521`.

This is a feature — the old session shipped this bug; the validator now blocks it. Two options:

1. **Re-run extraction**: `rm .alchemist/specs/<module>.json .alchemist/specs/_functions/<module>/<fn>.json` and re-run. The LLM is stochastic; a retry often produces a correct constant.
2. **Edit the spec manually**: Fix `.alchemist/specs/_functions/<module>/<fn>.json` and re-run from Stage 3.

## Stage 3 (Architect) — validator rejects with `spec_coverage` errors

The architect referenced a module name that doesn't match any ModuleSpec OR AlgorithmSpec. Common cause: the architect invented a helper module (`types`, `errors`, `traits`, `<algo>_table`) — those are now accepted as infrastructure. If you see a different name, it usually means the architect hallucinated a new algorithm.

Fix: manually edit `.alchemist/architecture.json` to drop the invalid module, or re-run Stage 3 (non-deterministic fix).

## Stage 4 (Implement) — "TDD: 0/N fns pass tests"

This means none of the per-function implementations passed their tests. Most likely causes in order:

1. **Skeleton didn't compile** — check the log; a skeleton compile failure short-circuits the rest. Almost always a spec with malformed `rust_type` (e.g. bare `&` instead of `&[u8]`). Fix the spec JSON and rerun.
2. **Anti-stub rejected every iteration** — the LLM kept producing `unimplemented!()` or stubby comments. Raise `max_iter_per_fn` via the Python API, or examine the per-function prompt context (inputs/standards) for missing detail.
3. **Tests compiled but failed mathematically** — the standards catalog test vector is right, the implementation is wrong. Check `.alchemist/output/<crate>/src/<module>.rs` to see what was generated, then help the model by adding a `spec.test_vectors` entry with a smaller input that demonstrates the bug.

## Stage 5 (Verify) — "FAIL: anti-stub … 18+ violations"

Phase A's anti-stub gate did its job — the generated Rust contains fake code. Do NOT use `--no-verify` to ship it. Instead, rerun Stage 4; the TDD loop with anti-stub rejection will catch the fakes at generation time now.

## Stage 5 (Verify) — "differential gate: no config"

You translated a library Alchemist doesn't have a built-in `DifferentialConfig` for. Write one (see `alchemist/verifier/zlib_config.py` for the template):

```python
from alchemist.verifier.zlib_config import DifferentialConfig
# Fill in c_sources, c_public_signatures, harnesses
my_cfg = DifferentialConfig(...)
```

Then call `run_translate_all(source, name, output, diff_config=my_cfg)` from Python.

## vLLM server responses are suspiciously fast and identical across calls

The server caches identical requests for ~15 minutes. Alchemist prepends an 8-char nonce and uses `temperature=0.15` to force cache misses — this is already in `alchemist/llm/client.py`. If you see it happening anyway, check that your fork hasn't removed the nonce.

## Windows: `UnicodeEncodeError: charmap can't encode`

Set `PYTHONIOENCODING=utf-8` before running Alchemist:

```bash
set PYTHONIOENCODING=utf-8        # cmd
$env:PYTHONIOENCODING = 'utf-8'    # PowerShell
export PYTHONIOENCODING=utf-8      # bash
```

Rich is configured with `force_terminal=True, legacy_windows=False` but some environments still ignore that.

## Generated Cargo workspace won't compile after apparent success

Run `cargo check --workspace` from the output directory and inspect the errors. If Alchemist's Stage 4 reported success but cargo fails, two possibilities:

1. **Outdated crate dependency** — regenerate the workspace Cargo.toml or run `cargo update`.
2. **File encoding corruption** — ensure `PYTHONIOENCODING=utf-8` was set during the run.

## The LLM loops on the same function

TDD is configured to escalate to the holistic fixer after 3 failed iterations. If you see the same function being retried past that, check:

- Server health (`alchemist doctor`). A saturated server will return nonsense.
- The `--stages 4-4` flag to resume only Stage 4 without re-running analyze/extract.
- Per-function checkpoint JSON in `.alchemist/specs/_functions/<module>/<fn>.json` — if it's malformed, delete it and re-extract.

## `I didn't run cargo test / differential — the report says PASS`

It doesn't. `TranslationReport.ok` is `True` only when every stage's outcome is `ok=True`. The differential gate under default settings refuses to pass without a `DifferentialConfig`. If you saw a PASS, one of these is the case:

- You used `--no-verify` (it will say so in the banner).
- Your custom pipeline code skipped Stage 5.
- You're looking at a partial-stage run (e.g. `--stages 1-4`).

The only path to a green `TranslationReport` on stages 1-6 is: valid specs → validated architecture → compiled crates → zero anti-stub violations → cargo test passes → differential tests pass against a C reference.
