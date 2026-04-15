"""Second-pass spec validator.

After Stage 2 extraction, every AlgorithmSpec passes through this validator
which cross-checks:

  1. If the spec claims a referenced standard (RFC / NIST), compare any
     declared constants / test vectors against the authoritative values
     loaded from alchemist.standards. This is the check that would have
     flagged Adler-32's BASE=255 vs RFC 1950's BASE=65521.
  2. Check mathematical plausibility: is the category consistent with
     the return type? (e.g., a `checksum` should return a fixed-width
     integer; a `compression` returns bytes).
  3. Optionally, ask an LLM reviewer for a sanity opinion.

Returns SpecValidationReport with issue severity and actionable messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from alchemist.extractor.schemas import AlgorithmSpec, ModuleSpec
from alchemist.standards import (
    TestVector as StandardsVector,
    lookup_test_vectors,
    match_algorithm,
)


class IssueSeverity(str, Enum):
    error = "ERROR"
    warning = "WARNING"
    info = "INFO"


@dataclass
class SpecIssue:
    algorithm: str
    rule: str
    severity: IssueSeverity
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.value}] {self.algorithm} / {self.rule}: {self.message}"


@dataclass
class SpecValidationReport:
    issues: list[SpecIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[SpecIssue]:
        return [i for i in self.issues if i.severity == IssueSeverity.error]

    @property
    def warnings(self) -> list[SpecIssue]:
        return [i for i in self.issues if i.severity == IssueSeverity.warning]

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return "Spec validation: clean"
        return (
            f"Spec validation: {len(self.errors)} errors, "
            f"{len(self.warnings)} warnings"
        )


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------

# Category → expected return types (regex fragments). A spec whose return
# type doesn't match any of these triggers a WARNING, not an ERROR.
CATEGORY_RETURN_HINTS: dict[str, list[str]] = {
    "checksum": [r"^u\d+$", r"^usize$"],
    "hash": [r"^\[?u8\s*;?\s*\d*\]?$", r"^Vec<u8>$", r"^\[u8;\s*\d+\]$", r"alloc::vec::Vec<u8>"],
    "cipher": [r"Vec<u8>", r"alloc::vec::Vec<u8>", r"Result<.*>"],
    "compression": [r"Vec<u8>", r"alloc::vec::Vec<u8>", r"Result<.*>"],
    "decompression": [r"Vec<u8>", r"alloc::vec::Vec<u8>", r"Result<.*>"],
}


def _constants_in_math(math: str) -> list[tuple[str, int]]:
    """Extract `NAME = 0xNN | \\d+` pairs from free-text math description."""
    out: list[tuple[str, int]] = []
    # Uppercase identifier = number
    for m in re.finditer(r"\b([A-Z_]{2,})\s*=\s*(0x[0-9a-fA-F]+|\d+)", math or ""):
        name, value = m.group(1), m.group(2)
        try:
            iv = int(value, 16) if value.startswith("0x") else int(value)
            out.append((name, iv))
        except ValueError:
            pass
    return out


# Known standard constants by canonical algorithm key.
# Each entry: (constant_name, expected_value, diagnostic_message_if_mismatch).
STANDARD_CONSTANTS: dict[str, list[tuple[str, int, str]]] = {
    "adler32": [
        ("BASE", 65521, "RFC 1950 specifies BASE = 65521 (largest prime < 2^16). BASE=255 is wrong."),
        ("NMAX", 5552, "RFC 1950 specifies NMAX = 5552."),
    ],
    "crc32": [
        ("POLY", 0xEDB88320,
         "CRC-32 (IEEE 802.3 / zlib) uses reflected polynomial 0xEDB88320."),
    ],
}


def _category_return_matches(category: str, return_type: str) -> bool:
    patterns = CATEGORY_RETURN_HINTS.get(category)
    if not patterns:
        return True  # no hint — don't complain
    return any(re.search(p, return_type or "") for p in patterns)


def _check_constants_against_standard(alg: AlgorithmSpec) -> list[SpecIssue]:
    issues: list[SpecIssue] = []
    canonical = match_algorithm(alg.name)
    if not canonical:
        # Try referenced_standards
        for s in alg.referenced_standards:
            canonical = match_algorithm(s.replace(" ", "").replace("RFC", "").strip()) or canonical
    if not canonical:
        return issues
    expected = STANDARD_CONSTANTS.get(canonical, [])
    if not expected:
        return issues
    declared = dict(_constants_in_math(alg.mathematical_description or ""))
    for name, want, msg in expected:
        if name in declared and declared[name] != want:
            issues.append(SpecIssue(
                algorithm=alg.name,
                rule="standard_constant_mismatch",
                severity=IssueSeverity.error,
                message=f"{name}={declared[name]} but standard requires {want}. {msg}",
            ))
    return issues


def _check_test_vectors_against_standard(alg: AlgorithmSpec) -> list[SpecIssue]:
    issues: list[SpecIssue] = []
    canonical = match_algorithm(alg.name)
    if not canonical:
        return issues
    catalog_vectors = lookup_test_vectors(canonical)
    if not catalog_vectors:
        return issues
    # Build a lookup: input bytes → expected hex (from catalog)
    catalog_lookup: dict[bytes, str] = {v.input_bytes: v.expected_hex for v in catalog_vectors}
    for i, tv in enumerate(alg.test_vectors or []):
        # Try to reconstruct input bytes from tv.inputs — only works if a single
        # parameter maps to a hex string or ASCII literal.
        declared_in = _extract_input_bytes(tv.inputs)
        if declared_in is None:
            continue
        if declared_in in catalog_lookup:
            expected_hex = catalog_lookup[declared_in]
            # Compare declared expected to standard expected
            declared_out = tv.expected_output.strip().lower()
            # Strip 0x / leading zeros for comparison
            declared_norm = declared_out.replace("0x", "").lstrip("0") or "0"
            expected_norm = expected_hex.lower().lstrip("0") or "0"
            if declared_norm != expected_norm:
                issues.append(SpecIssue(
                    algorithm=alg.name,
                    rule="test_vector_mismatch",
                    severity=IssueSeverity.error,
                    message=(
                        f"Extracted test vector {i} says output={declared_out} but "
                        f"standards catalog says {expected_hex} for the same input."
                    ),
                ))
    return issues


def _extract_input_bytes(inputs: dict[str, str]) -> bytes | None:
    """Best-effort: pull byte literal from the spec's tv.inputs dict."""
    for v in inputs.values():
        vs = v.strip()
        m = re.match(r'^b"(.+)"$', vs)
        if m:
            return m.group(1).encode("latin-1")
        if re.match(r"^&?\[?(?:0x[0-9a-fA-F]+,?\s*)+\]?$", vs):
            hexes = re.findall(r"0x([0-9a-fA-F]+)", vs)
            try:
                return bytes(int(h, 16) for h in hexes)
            except ValueError:
                return None
        m2 = re.match(r'^"([^"]+)"$', vs)
        if m2:
            return m2.group(1).encode()
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_spec(alg: AlgorithmSpec) -> SpecValidationReport:
    """Run all deterministic checks on a single algorithm spec."""
    report = SpecValidationReport()
    # Category / return type alignment
    if not _category_return_matches(alg.category, alg.return_type):
        report.issues.append(SpecIssue(
            algorithm=alg.name,
            rule="return_type_category_mismatch",
            severity=IssueSeverity.warning,
            message=(
                f"return_type {alg.return_type!r} looks unusual for category {alg.category!r}"
            ),
        ))
    # Standards constant check
    report.issues.extend(_check_constants_against_standard(alg))
    # Standards test vector check
    report.issues.extend(_check_test_vectors_against_standard(alg))
    # Basic sanity
    if not alg.inputs and alg.category not in ("utility", "data_structure"):
        report.issues.append(SpecIssue(
            algorithm=alg.name,
            rule="no_inputs",
            severity=IssueSeverity.warning,
            message="algorithm has no declared inputs",
        ))
    if not alg.source_functions:
        report.issues.append(SpecIssue(
            algorithm=alg.name,
            rule="no_source_functions",
            severity=IssueSeverity.warning,
            message="algorithm has no source_functions — API completeness can't enforce fns",
        ))
    return report


def validate_module(module: ModuleSpec) -> SpecValidationReport:
    combined = SpecValidationReport()
    for alg in module.algorithms:
        sub = validate_spec(alg)
        combined.issues.extend(sub.issues)
    return combined


def validate_specs(specs: list[ModuleSpec]) -> SpecValidationReport:
    combined = SpecValidationReport()
    for m in specs:
        sub = validate_module(m)
        combined.issues.extend(sub.issues)
    return combined
