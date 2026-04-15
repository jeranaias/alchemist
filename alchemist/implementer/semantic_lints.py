"""Per-algorithm-family semantic lints.

The anti-stub detector catches generic "compiled but empty" output
(`unimplemented!()`, "we don't have the algorithm" comments). It can't
catch generated code that is syntactically real Rust AND compiles AND
runs, but is mathematically wrong for the algorithm's invariants.

Examples of what these lints catch:

  * CRC-32 that mixes a reflected polynomial with MSB-first traversal,
    or non-reflected with LSB-first. Both compile; only one matches the
    spec's variant tag.
  * Adler-32 where `s1` is initialized to 0 instead of 1 (hidden bug —
    tests for a single byte still pass if tests happen to use seed=1,
    but tests with empty input fail silently).
  * AES implementations where the round count doesn't match the key size.
  * SHA-256 where the length-padding is little-endian (that's MD5).
  * Hash functions that never use one of their declared inputs.

Each lint returns a list of `SemanticFinding` with file, line, rule
name, severity, and a message. Findings are fed back into the TDD
prompt as "previous attempt violated this invariant" hints, and a
final sweep blocks success when any finding is severity=error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from alchemist.extractor.schemas import AlgorithmSpec


@dataclass
class SemanticFinding:
    rule: str
    severity: str          # "error" | "warning"
    message: str
    file: str = ""
    line: int = 0
    snippet: str = ""

    def __str__(self) -> str:
        loc = f"{self.file}:{self.line}" if self.file else ""
        return f"[{self.severity}] {self.rule} {loc}: {self.message}"


# ---------------------------------------------------------------------------
# Per-family lints
# ---------------------------------------------------------------------------

def lint_crc32(source: str, alg: AlgorithmSpec) -> list[SemanticFinding]:
    """Verify CRC-32 polynomial / traversal consistency.

    Variant rules:
      - variant:ieee_reflected → MUST use 0xEDB88320 AND shift-right (c >> 1).
      - variant:ieee_non_reflected → MUST use 0x04C11DB7 AND shift-left (c << 1).
      - variant:castagnoli → MUST use 0x82F63B78.
    """
    findings: list[SemanticFinding] = []
    variant = _variant_of(alg)
    has_reflected_poly = bool(re.search(r"0x[eE][dD][bB]8_?8320", source))
    has_non_reflected_poly = bool(re.search(r"0x04_?[cC]1_?1[dD][bB]7", source))
    has_castagnoli_poly = bool(re.search(r"0x82[fF]6_?3[bB]78", source))
    has_shift_right = bool(re.search(r"c\s*(?:>>=?|>>)\s*1", source)) or \
                      bool(re.search(r"c\s*=\s*(?:0x[0-9a-fA-F]+\s*\^\s*)?\(?\s*c\s*>>\s*1", source))
    has_shift_left = bool(re.search(r"c\s*(?:<<=?|<<)\s*1", source)) or \
                     bool(re.search(r"c\s*=\s*\(?\s*c\s*<<\s*1", source))

    if variant == "ieee_reflected":
        if not has_reflected_poly:
            findings.append(SemanticFinding(
                rule="crc32_wrong_polynomial",
                severity="error",
                message="variant:ieee_reflected requires polynomial 0xEDB88320",
            ))
        if has_shift_left and not has_shift_right:
            findings.append(SemanticFinding(
                rule="crc32_traversal_direction_mismatch",
                severity="error",
                message=(
                    "variant:ieee_reflected uses LSB-first (shift right), "
                    "but source shifts left. Reflected polynomial + shift-left "
                    "produces garbage output — pick one variant consistently."
                ),
            ))
    elif variant == "ieee_non_reflected":
        if not has_non_reflected_poly:
            findings.append(SemanticFinding(
                rule="crc32_wrong_polynomial",
                severity="error",
                message="variant:ieee_non_reflected requires polynomial 0x04C11DB7",
            ))
        if has_shift_right and not has_shift_left:
            findings.append(SemanticFinding(
                rule="crc32_traversal_direction_mismatch",
                severity="error",
                message="variant:ieee_non_reflected uses MSB-first (shift left), but source shifts right",
            ))
    elif variant == "castagnoli":
        if not has_castagnoli_poly:
            findings.append(SemanticFinding(
                rule="crc32_wrong_polynomial",
                severity="error",
                message="variant:castagnoli requires polynomial 0x82F63B78",
            ))

    # Generic: mixing BOTH polynomials in one function is always wrong.
    if has_reflected_poly and has_non_reflected_poly:
        findings.append(SemanticFinding(
            rule="crc32_polynomial_mixed",
            severity="error",
            message=(
                "source contains BOTH 0xEDB88320 and 0x04C11DB7 polynomials. "
                "Pick one — the variant resolver should have resolved this."
            ),
        ))
    return findings


def lint_adler32(source: str, alg: AlgorithmSpec) -> list[SemanticFinding]:
    findings: list[SemanticFinding] = []
    # BASE must be 65521 — accept either the literal or a named constant
    # (ADLER_BASE, BASE, ADLER32_BASE are all valid if they're defined as 65521)
    has_literal = bool(re.search(r"\b65_?521\b", source))
    has_named_const = bool(re.search(r"\b(?:ADLER_?BASE|BASE|ADLER32_BASE)\b", source))
    if not has_literal and not has_named_const:
        findings.append(SemanticFinding(
            rule="adler32_wrong_base",
            severity="error",
            message="Adler-32 must use BASE = 65521 (largest prime < 2^16)",
        ))
    # If we find BASE = <other number>, that's a stronger signal.
    for m in re.finditer(r"\b(?:BASE|base|ADLER_BASE)\s*[:=]\s*(\d+)", source):
        if m.group(1) != "65521":
            findings.append(SemanticFinding(
                rule="adler32_wrong_base",
                severity="error",
                message=f"Adler-32 BASE declared as {m.group(1)}; must be 65521",
            ))
    # s1 must be initialized from seed (for RFC 1950 use seed=1 as default),
    # NOT from 0 unconditionally.
    bad_init = re.search(r"(?:s1|s_1)\s*[:=]\s*0[^x0-9]", source)
    if bad_init and "seed" not in source[max(0, bad_init.start() - 100):bad_init.start()].lower():
        findings.append(SemanticFinding(
            rule="adler32_s1_zero_init",
            severity="error",
            message=(
                "Adler-32 s1 must start from seed (typically 1), not 0. "
                "Empty-input test vector Adler-32(b'') == 0x00000001 relies on this."
            ),
        ))
    return findings


def lint_sha256(source: str, alg: AlgorithmSpec) -> list[SemanticFinding]:
    findings: list[SemanticFinding] = []
    # SHA-256 uses big-endian length padding; spotting to_le_bytes on a 64-bit
    # length is a red flag that the author confused MD5 with SHA.
    if "to_le_bytes" in source and "bit_len" in source:
        findings.append(SemanticFinding(
            rule="sha256_le_length_padding",
            severity="error",
            message="SHA-256 length padding must be big-endian. to_le_bytes is MD5, not SHA.",
        ))
    # Initial hash words — one of H0[0] must appear
    if "0x6a09_e667" not in source and "0x6a09e667" not in source.lower().replace("_", ""):
        findings.append(SemanticFinding(
            rule="sha256_missing_h0",
            severity="error",
            message="SHA-256 initial hash value H0[0] = 0x6a09e667 not found",
        ))
    return findings


def lint_md5(source: str, alg: AlgorithmSpec) -> list[SemanticFinding]:
    findings: list[SemanticFinding] = []
    # MD5 uses little-endian length padding
    if "to_be_bytes" in source and "bit_len" in source:
        findings.append(SemanticFinding(
            rule="md5_be_length_padding",
            severity="error",
            message="MD5 length padding must be little-endian. to_be_bytes is wrong for MD5.",
        ))
    return findings


def lint_aes(source: str, alg: AlgorithmSpec) -> list[SemanticFinding]:
    findings: list[SemanticFinding] = []
    variant = _variant_of(alg)
    expected_rounds = {"aes128_ecb": 10, "aes192_ecb": 12, "aes256_ecb": 14}
    if variant in expected_rounds:
        want = expected_rounds[variant]
        # Look for Nr = N, rounds = N, NUM_ROUNDS = N (with optional type annotation)
        pat = re.compile(
            r"\b(?:Nr|NR|rounds?|NUM_ROUNDS|NR_ROUNDS)\b"
            r"(?:\s*:\s*\w+)?"      # optional type annotation (Nr: u32)
            r"\s*=\s*(\d+)",
            re.IGNORECASE,
        )
        for m in pat.finditer(source):
            declared = int(m.group(1))
            if declared != want:
                findings.append(SemanticFinding(
                    rule="aes_wrong_round_count",
                    severity="error",
                    message=f"{variant} must have {want} rounds; source declares {declared}",
                ))
    return findings


def lint_unused_input(source: str, alg: AlgorithmSpec) -> list[SemanticFinding]:
    """If a spec input named `buf`/`data`/etc isn't referenced in the body, flag it."""
    findings: list[SemanticFinding] = []
    meaningful_names = {"buf", "data", "input", "bytes", "msg"}
    for p in alg.inputs or []:
        name = p.name.lstrip("_").lower()
        if name not in meaningful_names:
            continue
        # Require the name to appear as an rvalue somewhere (not just in the signature)
        sig_match = re.search(r"\bpub fn \w+\s*\(([^)]*)\)", source)
        if not sig_match:
            continue
        after_sig = source[sig_match.end():]
        if not re.search(rf"\b{re.escape(p.name)}\b", after_sig):
            findings.append(SemanticFinding(
                rule="unused_input",
                severity="error",
                message=(
                    f"parameter {p.name!r} of type {p.rust_type!r} is declared but never "
                    f"referenced in the body. This is a silent-stub pattern."
                ),
            ))
    return findings


# ---------------------------------------------------------------------------
# Family routing
# ---------------------------------------------------------------------------

_FAMILY_LINTS: dict[str, list[Callable[[str, AlgorithmSpec], list[SemanticFinding]]]] = {
    "crc32":    [lint_crc32, lint_unused_input],
    "adler32":  [lint_adler32, lint_unused_input],
    "sha":      [lint_sha256, lint_unused_input],
    "md5":      [lint_md5, lint_unused_input],
    "aes":      [lint_aes],
    # For everything else we still run lint_unused_input.
    "_default": [lint_unused_input],
}


def _family_key(alg: AlgorithmSpec) -> str:
    name = alg.name.lower()
    standards_blob = " ".join(alg.referenced_standards or []).lower()
    if re.search(r"crc[_-]?32", name + " " + standards_blob):
        return "crc32"
    if "adler" in name:
        return "adler32"
    if re.search(r"sha(?:1|256|512|224|384)?\b", name + " " + standards_blob):
        return "sha"
    if re.match(r"md[_-]?5", name) or "md5" in standards_blob:
        return "md5"
    if re.search(r"\baes\b", name + " " + standards_blob):
        return "aes"
    return "_default"


def _variant_of(alg: AlgorithmSpec) -> str | None:
    for s in alg.referenced_standards or []:
        if s.startswith("variant:"):
            return s.split(":", 1)[1]
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def lint_function(source: str, alg: AlgorithmSpec) -> list[SemanticFinding]:
    """Run every lint applicable to this algorithm's family."""
    family = _family_key(alg)
    lints = _FAMILY_LINTS.get(family, _FAMILY_LINTS["_default"])
    out: list[SemanticFinding] = []
    for lint in lints:
        try:
            out.extend(lint(source, alg))
        except Exception as e:  # noqa: BLE001
            out.append(SemanticFinding(
                rule="lint_crash",
                severity="warning",
                message=f"{lint.__name__} raised: {e}",
            ))
    return out


def has_errors(findings: list[SemanticFinding]) -> bool:
    return any(f.severity == "error" for f in findings)


def format_findings(findings: list[SemanticFinding]) -> str:
    if not findings:
        return "no semantic lint findings"
    return "\n".join(str(f) for f in findings)


def summarize_for_reprompt(findings: list[SemanticFinding]) -> str:
    """Condense findings into a paragraph the LLM can learn from."""
    errs = [f for f in findings if f.severity == "error"]
    if not errs:
        return ""
    lines = ["Your last attempt violated these algorithmic invariants:"]
    for f in errs[:8]:
        lines.append(f"  - [{f.rule}] {f.message}")
    lines.append("Fix these specifically; do not change anything else.")
    return "\n".join(lines)
