"""Classify extracted functions into algorithm vs. build-utility categories.

zlib (and many C libraries) ship code-generation utilities alongside
the actual algorithms — e.g. ``write_table()``, ``gen_crc_header()``.
These should not be translated into Rust; they exist only to produce
C source or header files at build time.

This module provides:
  - ``classify_function`` — returns a classification label for one function.
  - ``filter_build_utilities`` — strips non-algorithm functions from a list
    of ``ModuleSpec`` objects so the implementer only sees real algorithms.
"""

from __future__ import annotations

import re
from typing import Literal

from alchemist.extractor.schemas import FunctionSpec, ModuleSpec

FunctionClass = Literal["algorithm", "build_utility", "test_harness", "main"]

# Phrases in the purpose/description that indicate build-time codegen
_BUILD_PHRASES = re.compile(
    r"generates?\s+C\s+source|writes?\s+to\s+file|code\s+generation",
    re.IGNORECASE,
)


def classify_function(name: str, spec: FunctionSpec) -> FunctionClass:
    """Return the classification of a single function.

    Rules (evaluated in order):
      1. ``name == "main"`` -> ``"main"``
      2. Name starts with ``write_table`` / ``gen_`` and contains ``header``
         -> ``"build_utility"``
      3. Name contains ``test`` or ``example`` -> ``"test_harness"``
      4. Purpose/description matches build-time codegen phrases
         -> ``"build_utility"``
      5. Everything else -> ``"algorithm"``
    """
    if name == "main":
        return "main"

    lower = name.lower()
    if (lower.startswith("write_table") or lower.startswith("gen_")) and "header" in lower:
        return "build_utility"

    if "test" in lower or "example" in lower:
        return "test_harness"

    purpose = (spec.purpose or "").strip()
    if purpose and _BUILD_PHRASES.search(purpose):
        return "build_utility"

    return "algorithm"


def filter_build_utilities(specs: list[ModuleSpec]) -> list[ModuleSpec]:
    """Return a copy of *specs* with build-utility / test / main algorithms removed.

    Each ``ModuleSpec`` is shallow-copied with its ``algorithms`` list
    filtered.  Modules that end up with zero algorithms are dropped entirely.
    """
    out: list[ModuleSpec] = []
    for module in specs:
        kept = [
            alg for alg in module.algorithms
            if classify_function(alg.name, _algo_to_fnspec(alg)) == "algorithm"
        ]
        if kept:
            out.append(module.model_copy(update={"algorithms": kept}))
    return out


def _algo_to_fnspec(alg) -> FunctionSpec:
    """Lightweight adapter: AlgorithmSpec -> FunctionSpec for classification."""
    return FunctionSpec(
        name=alg.name,
        purpose=alg.description or "",
        category=alg.category if alg.category in (
            "compression", "decompression", "checksum", "hash",
            "cipher", "filter", "controller", "data_structure",
            "protocol", "utility", "other",
        ) else "other",
        inputs=alg.inputs,
        return_type=alg.return_type,
        referenced_standards=alg.referenced_standards,
    )
