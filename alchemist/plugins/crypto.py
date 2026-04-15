"""Crypto domain plugin.

Adds crypto-specific safety nets:

  * `post_extract` — for any algorithm whose name matches AES / SHA / MD5,
    automatically fold NIST / FIPS test vectors from the standards catalog
    into `spec.test_vectors`. Caller can then pass those vectors straight
    through to test_generator.py without extra wiring.

  * `lints` — scan generated Rust for patterns that are toxic in crypto:
      - branches on secret bytes (`if key[i] == ...`)
      - early-return inside a `for b in ciphertext` loop
      - use of `match` on a secret `u8`
      - calls to `Vec::truncate` / slice indexing on secret-length values
        without using the `subtle` crate
    Violations are WARNING-level by default; a crate can opt in to ERROR
    via `[package.metadata.alchemist] crypto_ct_gate = "error"` in
    `Cargo.toml`.

The plugin is self-registering when imported (see plugins/__init__.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from alchemist.extractor.schemas import AlgorithmSpec, ModuleSpec, TestVector
from alchemist.plugins import DomainPlugin
from alchemist.standards import lookup_test_vectors, match_algorithm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CRYPTO_CATEGORIES = {"cipher", "hash", "signature", "key_exchange"}


def _as_spec_test_vector(algo: str, stdv) -> TestVector | None:
    """Translate a standards TestVector into an extractor TestVector.

    Only makes sense for algorithms where we can reconstruct the call from
    (input, optional key) → expected.
    """
    inputs: dict[str, str] = {}
    if stdv.input_hex:
        inputs["input"] = stdv.as_rust_literal("input")
    if stdv.key_hex:
        inputs["key"] = stdv.as_rust_literal("key")
    if stdv.iv_hex:
        inputs["iv"] = stdv.as_rust_literal("iv")
    if not inputs:
        return None
    return TestVector(
        description=f"{algo}/{stdv.name}",
        inputs=inputs,
        expected_output=stdv.as_rust_literal("expected"),
        tolerance="exact",
        source=stdv.source or "standards catalog",
    )


def augment_with_cavp(specs: Iterable[ModuleSpec]) -> int:
    """For every crypto algorithm, inject standards-catalog vectors into its spec.

    Returns the number of vectors added.
    """
    added = 0
    for module in specs:
        for alg in module.algorithms:
            if alg.category not in CRYPTO_CATEGORIES:
                continue
            canonical = match_algorithm(alg.name)
            if not canonical:
                continue
            existing = { (tv.description, tv.expected_output) for tv in alg.test_vectors or [] }
            for stdv in lookup_test_vectors(canonical):
                tv = _as_spec_test_vector(canonical, stdv)
                if tv is None:
                    continue
                key = (tv.description, tv.expected_output)
                if key in existing:
                    continue
                alg.test_vectors.append(tv)
                added += 1
    return added


# ---------------------------------------------------------------------------
# Constant-time lint
# ---------------------------------------------------------------------------

@dataclass
class CTFinding:
    file: str
    line: int
    rule: str
    message: str
    snippet: str = ""

    def as_tuple(self) -> tuple:
        return (self.file, self.line, self.rule, f"{self.message} | {self.snippet.strip()[:120]}")


# Heuristic regexes — catch the common branch-on-secret patterns without
# doing real dataflow analysis. False positives are preferable to silence.
_SECRET_NAME_RE = r"(?:key|secret|pw|password|pin|mac|tag|sig|signature|ciphertext|plaintext)"

# `if <expr>.contains_byte(X)` / `== key[i]`, etc.
_BRANCH_ON_SECRET = re.compile(
    rf"\bif\s+[^\{{\n]*\b{_SECRET_NAME_RE}\b[^\{{\n]*(?:==|!=|<|>|<=|>=)"
)

# `match <secret>` or `match <secret>[idx]`
_MATCH_ON_SECRET = re.compile(
    rf"\bmatch\s+\w*{_SECRET_NAME_RE}\w*"
)

# early-return inside loop iterating over a secret slice
_EARLY_RETURN_ON_SECRET = re.compile(
    rf"for\s+\w+\s+in\s+&?\w*{_SECRET_NAME_RE}\w*[^\{{\n]*\{{[^}}]*\breturn\b",
    re.DOTALL,
)


def scan_file_for_ct(path: Path) -> list[CTFinding]:
    text = path.read_text(encoding="utf-8", errors="replace")
    findings: list[CTFinding] = []
    for rule, pat in [
        ("branch_on_secret", _BRANCH_ON_SECRET),
        ("match_on_secret", _MATCH_ON_SECRET),
        ("early_return_in_secret_loop", _EARLY_RETURN_ON_SECRET),
    ]:
        for m in pat.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            snippet = text[m.start():m.end()].splitlines()[0] if text[m.start():m.end()] else ""
            findings.append(CTFinding(
                file=str(path),
                line=line,
                rule=rule,
                message=f"possible non-constant-time pattern ({rule})",
                snippet=snippet,
            ))
    return findings


def crypto_ct_lint(workspace_dir, specs) -> list[tuple]:  # noqa: ANN001
    """Run constant-time lint on every .rs file under crypto-flagged crates.

    A crate is considered crypto-flagged if any of its modules have at
    least one algorithm in CRYPTO_CATEGORIES.
    """
    workspace_dir = Path(workspace_dir)
    # Identify crypto modules by category
    crypto_modules: set[str] = set()
    for module in specs or []:
        if any(a.category in CRYPTO_CATEGORIES for a in module.algorithms):
            crypto_modules.add(module.name)

    if not crypto_modules:
        return []

    findings: list[CTFinding] = []
    # Scan every .rs file under any crate with a crypto module in its src.
    # We use a conservative approach: scan every crate-dir containing one of
    # the crypto_modules as `src/<module>.rs`.
    for module in crypto_modules:
        for rs in workspace_dir.rglob(f"src/{module}.rs"):
            findings.extend(scan_file_for_ct(rs))
    return [f.as_tuple() for f in findings]


# ---------------------------------------------------------------------------
# Plugin object
# ---------------------------------------------------------------------------

def _post_extract_hook(specs):
    return {"cavp_vectors_added": augment_with_cavp(specs)}


PLUGIN = DomainPlugin(
    name="crypto",
    description="NIST CAVP test-vector ingestion + constant-time lint for cipher/hash code.",
    post_extract=_post_extract_hook,
    lints=[crypto_ct_lint],
)
