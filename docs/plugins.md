# Plugin authoring

Domain plugins extend Alchemist with:

- **Test vector catalogs** — authoritative (input, expected-output) pairs from RFCs, NIST CAVP, datasheets.
- **Post-generation lints** — checks that run after Stage 4 but before Stage 5 (constant-time, audit trails, numeric bounds).
- **Spec augmentation** — fold external knowledge into `spec.test_vectors` automatically at extract time.

The built-in `crypto` plugin is the canonical example.

## Minimal plugin

```python
# mypkg/algo_plugin.py
from pathlib import Path
from alchemist.plugins import DomainPlugin

def numeric_stability_lint(workspace_dir, specs):
    """Flag Rust files that use `f32` without a tolerance check."""
    findings = []
    for rs in Path(workspace_dir).rglob("*.rs"):
        text = rs.read_text(encoding="utf-8", errors="replace")
        if "f32" in text and "tolerance" not in text.lower():
            findings.append((str(rs), 1, "f32_no_tolerance",
                             "f32 usage without documented tolerance"))
    return findings

PLUGIN = DomainPlugin(
    name="numeric",
    description="Flags floating-point code without tolerance documentation.",
    lints=[numeric_stability_lint],
)
```

Register via entry-points in your `pyproject.toml`:

```toml
[project.entry-points."alchemist.plugins"]
numeric = "mypkg.algo_plugin:PLUGIN"
```

`pip install -e .` the package and `alchemist doctor` will show it in the plugin list.

## Full `DomainPlugin` contract

```python
@dataclass
class DomainPlugin:
    name: str
    description: str
    post_extract: Callable | None = None
    post_skeleton: Callable | None = None
    lints: list[LintRule] = field(default_factory=list)
```

### `post_extract(specs) -> dict`
Called after Stage 2. Mutate `specs` in place to inject test vectors or correct misdetections. Return a dict of diagnostic counters for logging.

```python
def augment_cavp(specs):
    added = 0
    for module in specs:
        for alg in module.algorithms:
            if alg.category == "cipher":
                # ... fold NIST CAVP vectors into alg.test_vectors
                added += 1
    return {"cavp_vectors": added}
```

### `post_skeleton(workspace_dir, specs) -> None`
Called after Phase 4A emits the skeleton. Use this to inject domain-specific setup: constant-time helper modules, audit-trail scaffolding, feature flags.

### `lints: list[LintRule]`
A `LintRule` is any callable with signature `(workspace_dir, specs) -> list[tuple]` where each tuple is `(file, line, rule_name, message)`.

Findings are informational by default. To make a lint BLOCK success, register it at the Stage 5 gate:

```python
from alchemist.verifier.differential_tester import DifferentialTester

class MyTester(DifferentialTester):
    def run_all(self):
        report = super().run_all()
        from alchemist.plugins import run_lints
        findings = run_lints(self.rust_workspace, self.specs)
        if findings:
            report.differential.passed = False
            report.differential.summary = f"{len(findings)} plugin lint violations"
        return report
```

## Guarantees

- Plugin crashes never propagate — they're captured as a `lint_crash` finding.
- Plugins load exactly once at CLI startup via `load_all()`. No lazy re-import inside loops.
- The registry is a simple `dict[str, DomainPlugin]`. Test-isolation via `alchemist.plugins.clear()`.

## Testing your plugin

```python
from alchemist.plugins import clear, register, run_lints
import mypkg.algo_plugin as mp

def test_my_lint(tmp_path):
    clear()
    register(mp.PLUGIN)
    # Set up a tmp workspace with a known-bad file
    (tmp_path / "bad.rs").write_text("fn x() -> f32 { 1.0 }")
    findings = run_lints(tmp_path, [])
    assert any("f32_no_tolerance" in f[2] for f in findings)
```

## Existing plugins

| Plugin | Location | What it does |
|--------|----------|--------------|
| `crypto` | `alchemist.plugins.crypto` | Injects NIST CAVP test vectors for AES / SHA / MD5. Constant-time lint (branch on secret, match on secret, early return in secret-iterating loops). |

See `alchemist/plugins/crypto.py` for the reference implementation.
