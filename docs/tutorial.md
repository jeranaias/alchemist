# Alchemist tutorial

This walk-through translates a small C library to verified-correct Rust in one command. We use `zlib` as the running example. The same flow works for any C codebase.

## 1. Prerequisites

Run the environment check first:

```bash
alchemist doctor
```

You should see OK for `cargo`, `rustc`, `gcc`, and the local LLM server. If the server is unreachable, start it; if `gcc` is missing, install MinGW (Windows) or `build-essential` (Linux). Everything else is bundled.

## 2. Point Alchemist at a codebase

```bash
alchemist translate ./path/to/zlib --name zlib-rs
```

That's it. The single command runs the full pipeline with every safety gate:

| Stage | What it does | Gate behavior |
|-------|--------------|---------------|
| 1 Analyze | tree-sitter parse, call graph, module detection | must find ≥1 module |
| 2 Extract | per-function LLM spec extraction, then spec validator | **blocks** on wrong constants (e.g. Adler-32 BASE=255 vs 65521) |
| 3 Architect | LLM designs Rust workspace, validator runs | **blocks** on dep cycles, orphan-rule violations, unassigned modules |
| 4 Implement | skeleton → failing tests → per-fn TDD loop | **blocks** on anti-stub matches, API completeness misses |
| 5 Verify | compile + anti-stub + cargo test + differential (10K cases vs C) | **blocks** unless all four gates pass |
| 6 Report | metrics dashboard | informational |

If any gate fails, `alchemist translate` exits non-zero with a report telling you exactly which gate caught the problem.

## 3. What gets produced

```
.alchemist/
├── analysis.json            # Stage 1
├── specs/<module>.json      # Stage 2
├── architecture.json        # Stage 3
└── output/                  # Stage 4 — generated Rust workspace
    ├── Cargo.toml
    ├── zlib-types/
    ├── zlib-checksum/
    ├── zlib-compression/
    └── ...
```

The generated workspace is a regular cargo workspace. You can `cd output && cargo test` independently.

## 4. Scoped re-runs

Each stage is checkpointed. To re-run a specific range:

```bash
alchemist translate ./zlib --name zlib-rs --stages 4-5
```

This skips analyze/extract/architect, re-runs Implement + Verify using cached earlier outputs.

## 5. Override gates (DEBUG ONLY)

The production default refuses success unless every gate passes. For debugging you can override:

```bash
alchemist translate ./zlib --force       # bypass validator
alchemist translate ./zlib --no-verify   # skip Stage 5 (NEVER ship without this)
alchemist translate ./zlib --no-tdd      # use the legacy pre-Phase-B generator
```

Shipping code produced with `--no-verify` defeats the point — use only for iterating on Alchemist itself.

## 6. Writing your own domain plugin

Crypto code has special requirements (constant-time, CAVP test vectors). Alchemist lets you plug these in without touching core. See `docs/plugins.md` for the full contract — short version:

```python
# mypkg/my_plugin.py
from alchemist.plugins import DomainPlugin

def my_lint(workspace_dir, specs):
    return [("src/file.rs", 42, "my_rule", "don't do that")]

PLUGIN = DomainPlugin(
    name="mydomain",
    description="...",
    lints=[my_lint],
)
```

Register it via pyproject.toml entry-points:

```toml
[project.entry-points."alchemist.plugins"]
mydomain = "mypkg.my_plugin:PLUGIN"
```

Alchemist auto-discovers it on startup.

## 7. Next steps

- `docs/api_reference.md` — programmatic API (call stages from Python)
- `docs/plugins.md` — plugin contract and examples
- `docs/troubleshooting.md` — common failure modes and fixes
