"""Algorithm variant disambiguator.

Many algorithms have multiple canonical forms that look nearly identical in
prose but produce completely different outputs. A spec extracted by LLM
often mentions both — CRC-32 descriptions regularly include both the
reflected polynomial 0xEDB88320 and the non-reflected 0x04C11DB7, which
must be paired with opposite traversal orders. If the implementer sees
"both" it picks one arbitrarily and gets wrong answers half the time.

This module runs AFTER extraction and BEFORE implementation. For every
algorithm whose canonical name matches a known multi-variant family, it:

  1. Detects which variants are mentioned / hinted at in the spec.
  2. If more than one, either (a) resolves from catalog test vectors
     (if present, they uniquely determine the variant), or (b) issues
     a targeted LLM call that must pick exactly one variant.
  3. Records the chosen variant on the AlgorithmSpec via the mathematical
     description and a synthetic test vector if needed.

The output is a spec where the variant is unambiguous, so the implementer
— and the reference-impl registry — can route to the right canonical
Rust without guessing.
"""

from __future__ import annotations

import binascii
import re
from dataclasses import dataclass, field
from typing import Callable

from alchemist.extractor.schemas import (
    AlgorithmSpec,
    ModuleSpec,
    TestVector,
)
from alchemist.standards import TestVector as StdTestVector, lookup_test_vectors


# ---------------------------------------------------------------------------
# Family definitions
# ---------------------------------------------------------------------------

@dataclass
class Variant:
    """A single algorithmic variant with its identifying fingerprints."""
    name: str
    description: str
    # Textual fingerprints — strings in the spec that strongly suggest this variant.
    fingerprints: list[str] = field(default_factory=list)
    # Optional catalog algorithm name that validates this variant's outputs.
    catalog_algorithm: str | None = None
    # Notes to append to the algorithm's mathematical_description if chosen.
    notes: str = ""


@dataclass
class VariantFamily:
    """A set of algorithmic variants that share a name but differ in details."""
    family: str          # e.g. "crc32", "aes", "md5"
    variants: list[Variant]
    # Regex that identifies when an AlgorithmSpec belongs to this family.
    name_patterns: list[str] = field(default_factory=list)

    def matches_algorithm(self, alg: AlgorithmSpec) -> bool:
        blobs = [
            alg.name.lower(),
            (alg.description or "").lower(),
            (alg.mathematical_description or "").lower(),
            " ".join(alg.referenced_standards or []).lower(),
        ]
        for p in self.name_patterns:
            for blob in blobs:
                if re.search(p, blob):
                    return True
        return False


# ---------------------------------------------------------------------------
# Built-in families
# ---------------------------------------------------------------------------

CRC32_FAMILY = VariantFamily(
    family="crc32",
    name_patterns=[r"crc[_-]?32"],
    variants=[
        Variant(
            name="ieee_reflected",
            description="CRC-32 IEEE 802.3 / zlib / gzip (reflected polynomial, LSB-first traversal)",
            fingerprints=[
                "0xedb88320", "0xEDB88320",
                "reflected", "zlib", "gzip", "PNG", "IEEE 802.3",
                "shift right", "c >> 1", "c >>= 1",
            ],
            catalog_algorithm="crc32",
            notes=(
                "Canonical variant: IEEE 802.3 / zlib / gzip CRC-32. "
                "Polynomial 0xEDB88320 (reflected form of 0x04C11DB7). "
                "Use LSB-first traversal (shift right, XOR on low bit). "
                "Initial 0xFFFFFFFF, final XOR 0xFFFFFFFF."
            ),
        ),
        Variant(
            name="ieee_non_reflected",
            description="CRC-32 MSB-first with non-reflected polynomial",
            fingerprints=[
                "0x04c11db7", "0x04C11DB7",
                "non-reflected", "shift left", "c << 1", "c <<= 1",
                "0x80000000", "0x8000_0000",
            ],
            notes=(
                "MSB-first CRC-32 variant. Polynomial 0x04C11DB7. "
                "Shift left, check high bit. Rarely used — zlib / gzip / PNG all use the reflected variant."
            ),
        ),
        Variant(
            name="castagnoli",
            description="CRC-32C (Castagnoli) — iSCSI / SCTP / SSE4.2",
            fingerprints=[
                "0x82f63b78", "0x82F63B78", "castagnoli",
                "iSCSI", "SCTP", "SSE4.2", "CRC-32C",
            ],
            catalog_algorithm="crc32c",
            notes="Castagnoli polynomial 0x82F63B78. Different from IEEE 802.3.",
        ),
    ],
)


AES_FAMILY = VariantFamily(
    family="aes",
    name_patterns=[r"\baes[_-]?\d{3}\b", r"\baes[_\-a-z]*\b", r"advanced encryption standard"],
    variants=[
        Variant(
            name="aes128_ecb",
            description="AES-128 in ECB single-block mode",
            fingerprints=["aes-128", "aes_128", "ECB", "single block", "nk = 4", "nr = 10"],
            catalog_algorithm="aes128",
        ),
        Variant(
            name="aes192_ecb",
            description="AES-192 in ECB single-block mode",
            fingerprints=["aes-192", "aes_192", "nk = 6", "nr = 12"],
            catalog_algorithm="aes192",
        ),
        Variant(
            name="aes256_ecb",
            description="AES-256 in ECB single-block mode",
            fingerprints=["aes-256", "aes_256", "nk = 8", "nr = 14"],
            catalog_algorithm="aes256",
        ),
        Variant(
            name="aes_cbc",
            description="AES in CBC (cipher block chaining) mode",
            fingerprints=["CBC", "cipher block chaining", "IV XOR plaintext"],
        ),
        Variant(
            name="aes_ctr",
            description="AES in CTR (counter) mode",
            fingerprints=["CTR", "counter mode", "nonce counter"],
        ),
    ],
)


SHA_FAMILY = VariantFamily(
    family="sha",
    name_patterns=[r"\bsha[-_]?(1|224|256|384|512)\b", r"\bsha\b"],
    variants=[
        Variant(name="sha1",   description="SHA-1 (160-bit)",
                fingerprints=["sha1", "sha-1", "160-bit", "5 state words"],
                catalog_algorithm="sha1"),
        Variant(name="sha224", description="SHA-224 (truncated SHA-256)",
                fingerprints=["sha224", "sha-224", "224-bit", "truncated"],
                catalog_algorithm="sha224"),
        Variant(name="sha256", description="SHA-256 (8 32-bit words)",
                fingerprints=["sha256", "sha-256", "256-bit", "8 state words"],
                catalog_algorithm="sha256"),
        Variant(name="sha384", description="SHA-384 (truncated SHA-512)",
                fingerprints=["sha384", "sha-384", "384-bit"],
                catalog_algorithm="sha384"),
        Variant(name="sha512", description="SHA-512 (8 64-bit words)",
                fingerprints=["sha512", "sha-512", "512-bit", "64-bit words"],
                catalog_algorithm="sha512"),
    ],
)


DEFAULT_FAMILIES: list[VariantFamily] = [CRC32_FAMILY, AES_FAMILY, SHA_FAMILY]


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class ResolutionResult:
    algorithm: str
    family: str
    chosen: Variant | None
    candidates: list[Variant]  # variants whose fingerprints matched the spec
    rationale: str = ""

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1

    @property
    def resolved(self) -> bool:
        return self.chosen is not None


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def _spec_text(alg: AlgorithmSpec) -> str:
    """Concatenate every free-text field of the spec for fingerprint matching."""
    parts = [
        alg.description or "",
        alg.mathematical_description or "",
        " ".join(alg.referenced_standards or []),
    ]
    for inv in alg.invariants or []:
        parts.append(inv.description or "")
    return " ".join(parts).lower()


def _matches_fingerprints(text: str, variant: Variant) -> bool:
    for fp in variant.fingerprints:
        if fp.lower() in text:
            return True
    return False


def _variant_catalog_agrees_with_spec(
    alg: AlgorithmSpec, variant: Variant,
) -> bool:
    """Positive validation: variant has a catalog entry AND at least one
    spec.test_vector matches that entry's expected output for the same input.
    """
    if not variant.catalog_algorithm or not alg.test_vectors:
        return False
    catalog_vectors = lookup_test_vectors(variant.catalog_algorithm)
    if not catalog_vectors:
        return False
    catalog_map: dict[bytes, str] = {
        v.input_bytes: v.expected_hex for v in catalog_vectors
    }
    for tv in alg.test_vectors:
        in_bytes = _extract_spec_input(tv)
        if in_bytes is None or in_bytes not in catalog_map:
            continue
        declared = _normalize_hex(tv.expected_output.strip().lower())
        expected = catalog_map[in_bytes].lower().lstrip("0") or "0"
        if declared and declared == expected:
            return True
    return False


def _validate_variant_against_catalog(
    alg: AlgorithmSpec, variant: Variant,
) -> bool:
    """If the spec has test_vectors AND we know a catalog algorithm for this
    variant, check that the spec's (input → expected) pairs match the catalog's
    expected outputs for the same input. Catches extraction errors where the
    spec claims it's CRC-32 but declares CRC-32C vectors.
    """
    if not variant.catalog_algorithm or not alg.test_vectors:
        return True  # nothing to check against
    catalog_vectors = lookup_test_vectors(variant.catalog_algorithm)
    if not catalog_vectors:
        return True
    # Build input_bytes -> expected_hex map from catalog
    catalog_map: dict[bytes, str] = {
        v.input_bytes: v.expected_hex for v in catalog_vectors
    }
    for tv in alg.test_vectors:
        # Try to decode tv.inputs → bytes
        in_bytes = _extract_spec_input(tv)
        if in_bytes is None or in_bytes not in catalog_map:
            continue
        declared = tv.expected_output.strip().lower()
        declared_hex = _normalize_hex(declared)
        if not declared_hex:
            continue
        expected_hex = catalog_map[in_bytes].lower()
        if declared_hex != expected_hex.lstrip("0") and declared_hex != expected_hex:
            return False
    return True


def _extract_spec_input(tv: TestVector) -> bytes | None:
    for v in tv.inputs.values():
        vs = v.strip()
        m = re.match(r'^b"(.+)"$', vs)
        if m:
            try:
                return m.group(1).encode("latin-1")
            except UnicodeEncodeError:
                return None
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


def _normalize_hex(s: str) -> str:
    s = s.strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if re.fullmatch(r"[0-9a-f]+", s):
        return s.lstrip("0") or "0"
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_variant(
    alg: AlgorithmSpec,
    families: list[VariantFamily] | None = None,
    *,
    llm_tiebreaker: Callable[[AlgorithmSpec, list[Variant]], Variant | None] | None = None,
) -> ResolutionResult:
    """Determine the canonical variant of an AlgorithmSpec.

    Resolution order:
      1. Find the matching VariantFamily by name / standards.
      2. Score each variant's fingerprints against the spec's free-text fields.
      3. If exactly one variant matches: done.
      4. If multiple: try to disambiguate via catalog test-vector agreement.
      5. If still ambiguous and `llm_tiebreaker` is provided: let the LLM pick.
      6. Otherwise: return ambiguous with the candidates ranked.
    """
    families = families or DEFAULT_FAMILIES
    family = next((f for f in families if f.matches_algorithm(alg)), None)
    if family is None:
        return ResolutionResult(
            algorithm=alg.name, family="(none)", chosen=None, candidates=[],
            rationale="algorithm does not match any known variant family",
        )

    text = _spec_text(alg)
    candidates = [v for v in family.variants if _matches_fingerprints(text, v)]

    # If nothing matched, default to the first variant of the family if only one exists.
    if not candidates:
        if len(family.variants) == 1:
            only = family.variants[0]
            return ResolutionResult(
                algorithm=alg.name, family=family.family, chosen=only,
                candidates=[only],
                rationale="single-variant family — default applied",
            )
        return ResolutionResult(
            algorithm=alg.name, family=family.family, chosen=None, candidates=[],
            rationale="no variant fingerprints matched — cannot disambiguate without more context",
        )

    # Try to disambiguate via spec.test_vectors agreement with catalog outputs.
    if alg.test_vectors:
        # A variant "positively validates" if it has a catalog entry AND its
        # expected outputs match the spec's declared outputs for the same input.
        positively_validated: list[Variant] = []
        for c in candidates:
            if c.catalog_algorithm and _variant_catalog_agrees_with_spec(alg, c):
                positively_validated.append(c)
        # If one variant positively validates and others don't (either
        # disagree OR have no catalog to check), prefer the validators.
        if len(positively_validated) == 1:
            candidates = positively_validated
        elif len(positively_validated) > 1:
            candidates = positively_validated
        else:
            # None positively validate — fall back to filtering disagreements.
            candidates = [
                c for c in candidates
                if _validate_variant_against_catalog(alg, c)
            ] or candidates

    if len(candidates) == 1:
        chosen = candidates[0]
        return ResolutionResult(
            algorithm=alg.name, family=family.family, chosen=chosen,
            candidates=candidates,
            rationale=f"unique fingerprint match → {chosen.name}",
        )

    # Ambiguous. Escalate to tiebreaker if provided.
    if llm_tiebreaker is not None:
        pick = llm_tiebreaker(alg, candidates)
        if pick is not None:
            return ResolutionResult(
                algorithm=alg.name, family=family.family, chosen=pick,
                candidates=candidates,
                rationale=f"LLM tiebreaker selected {pick.name}",
            )

    # Can't resolve — return the list, caller decides what to do.
    return ResolutionResult(
        algorithm=alg.name, family=family.family, chosen=None,
        candidates=candidates,
        rationale=f"ambiguous — {len(candidates)} variants matched the spec",
    )


def apply_resolution(alg: AlgorithmSpec, result: ResolutionResult) -> None:
    """Mutate the AlgorithmSpec in place to encode the chosen variant.

    Appends variant notes to `mathematical_description` and adds the variant
    name as a synthetic token in `referenced_standards` so the implementer
    and the reference-impl registry can route to it unambiguously.
    """
    if not result.resolved:
        return
    chosen = result.chosen
    prefix = f"[variant: {chosen.name}] "
    if chosen.notes and chosen.notes not in (alg.mathematical_description or ""):
        if alg.mathematical_description:
            alg.mathematical_description = (
                prefix + chosen.notes + "\n\n" + alg.mathematical_description
            )
        else:
            alg.mathematical_description = prefix + chosen.notes
    # Make variant discoverable via referenced_standards
    if chosen.name not in alg.referenced_standards:
        alg.referenced_standards = list(alg.referenced_standards) + [
            f"variant:{chosen.name}",
        ]


def resolve_specs(
    specs: list[ModuleSpec],
    *,
    families: list[VariantFamily] | None = None,
    llm_tiebreaker: Callable[[AlgorithmSpec, list[Variant]], Variant | None] | None = None,
) -> list[ResolutionResult]:
    """Resolve every algorithm across every module.

    Mutates each AlgorithmSpec in place via apply_resolution. Returns the list
    of ResolutionResult so the caller can log / report / prompt on failures.
    """
    out: list[ResolutionResult] = []
    for module in specs:
        for alg in module.algorithms:
            result = resolve_variant(alg, families=families, llm_tiebreaker=llm_tiebreaker)
            apply_resolution(alg, result)
            out.append(result)
    return out


# ---------------------------------------------------------------------------
# LLM tiebreaker factory
# ---------------------------------------------------------------------------

def make_llm_tiebreaker(llm) -> Callable[[AlgorithmSpec, list[Variant]], Variant | None]:
    """Build a tiebreaker callable that asks the LLM to pick one variant.

    The LLM sees only the variant names, descriptions, and the spec's free
    text. Returns the Variant whose name the LLM names, or None if parsing
    fails.
    """
    def tiebreaker(alg: AlgorithmSpec, candidates: list[Variant]) -> Variant | None:
        choices = "\n".join(
            f"- {c.name}: {c.description}"
            for c in candidates
        )
        prompt = (
            f"The algorithm `{alg.name}` was extracted with an ambiguous spec. "
            f"Which ONE of the following variants does the spec describe?\n\n"
            f"{choices}\n\n"
            f"## Spec description\n{alg.description}\n\n"
            f"## Mathematical notes\n{alg.mathematical_description or '(none)'}\n\n"
            f"## Referenced standards\n{', '.join(alg.referenced_standards) or '(none)'}\n\n"
            f"Respond with JSON: {{\"variant\": \"<name from list>\"}}."
        )
        schema = {
            "type": "object",
            "properties": {"variant": {"type": "string"}},
            "required": ["variant"],
        }
        try:
            resp = llm.call_structured(
                messages=[{"role": "user", "content": prompt}],
                tool_name="variant_choice",
                tool_schema=schema,
                max_tokens=200,
                temperature=0.1,
            )
            if resp.structured and "variant" in resp.structured:
                name = resp.structured["variant"]
                for c in candidates:
                    if c.name == name:
                        return c
        except Exception:  # noqa: BLE001
            pass
        return None
    return tiebreaker
