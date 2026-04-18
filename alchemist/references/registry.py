"""Reference implementation registry.

Each entry pairs a canonical algorithm key with a Rust reference snippet
and metadata (source, variant, signature hints). The TDD generator queries
this registry at generation time; if an AlgorithmSpec names a standard we
have a reference for, the reference is injected into the impl prompt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


REFERENCES_DIR = Path(__file__).parent / "impls"


@dataclass(frozen=True)
class ReferenceImpl:
    """A canonical reference implementation for a single algorithm variant."""
    algorithm: str                 # canonical key, e.g. "crc32_ieee", "adler32"
    variant: str                   # free-form variant tag, e.g. "reflected", "non_reflected"
    title: str                     # human-readable name
    rust_source: str               # the actual Rust code
    signature: str                 # canonical Rust signature, e.g. "pub fn adler32(seed: u32, buf: &[u8]) -> u32"
    standards: list[str] = field(default_factory=list)  # RFC / FIPS / IEEE citations
    source_url: str = ""           # where the implementation came from
    license: str = "Apache-2.0"    # license of the reference
    notes: str = ""                # invariants, pitfalls, anything the model should know

    def as_prompt_snippet(self) -> str:
        """Format the reference for injection into an LLM prompt."""
        lines = [
            f"## Reference implementation: {self.title}",
            f"Algorithm: {self.algorithm} (variant: {self.variant})",
        ]
        if self.standards:
            lines.append(f"Standards: {', '.join(self.standards)}")
        if self.notes:
            lines.append(f"Notes: {self.notes}")
        lines.append("")
        lines.append("```rust")
        lines.append(self.rust_source.strip())
        lines.append("```")
        return "\n".join(lines)


@dataclass
class ReferenceMatch:
    """Result of querying the registry: which references apply, ranked."""
    algorithm: str
    impls: list[ReferenceImpl] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.impls)

    def best(self, variant_hint: str | None = None) -> ReferenceImpl | None:
        if not self.impls:
            return None
        if variant_hint:
            for r in self.impls:
                if r.variant == variant_hint:
                    return r
        return self.impls[0]


# ---------------------------------------------------------------------------
# Normalization + alias routing
# ---------------------------------------------------------------------------

# Algorithm-name aliases pointing at a canonical registry key. Extractor
# names can vary (crc32, crc_32, CRC-32, crc32_ieee); this normalizes them.
_ALIAS_MAP: dict[str, str] = {
    "adler": "adler32",
    "adler32": "adler32",
    "adler_32": "adler32",
    "adler-32": "adler32",
    "crc": "crc32_ieee",
    "crc32": "crc32_ieee",
    "crc32_ieee": "crc32_ieee",
    "crc-32": "crc32_ieee",
    "crc_32": "crc32_ieee",
    "crc32c": "crc32c",
    "crc32_castagnoli": "crc32c",
    "castagnoli": "crc32c",
    "crc32_bzip2": "crc32_bzip2",
    "sha1": "sha1",
    "sha-1": "sha1",
    "sha_1": "sha1",
    "sha224": "sha224",
    "sha256": "sha256",
    "sha-256": "sha256",
    "sha_256": "sha256",
    "sha384": "sha384",
    "sha512": "sha512",
    "md5": "md5",
    "md_5": "md5",
    "md-5": "md5",
    "aes128": "aes128",
    "aes-128": "aes128",
    "aes_128": "aes128",
    "aes192": "aes192",
    "aes256": "aes256",
    "hmac_sha256": "hmac_sha256",
    "hmac-sha256": "hmac_sha256",
    "fletcher16": "fletcher16",
    "fletcher-16": "fletcher16",
    "fletcher32": "fletcher32",
    "fletcher-32": "fletcher32",
    "xxhash32": "xxhash32",
    "xxhash64": "xxhash64",
}


def _canonical(name: str) -> str | None:
    if not name:
        return None
    n = name.strip().lower().replace("-", "_").replace(" ", "_")
    if n in _ALIAS_MAP:
        return _ALIAS_MAP[n]
    n2 = n.replace("_", "")
    for alias, canonical in _ALIAS_MAP.items():
        if alias.replace("_", "") == n2:
            return canonical
    # Fallback: if the name matches a disk-loaded reference key directly,
    # accept it as canonical. This lets new ref JSON files work without
    # updating the alias map every time.
    loaded = _load_all_references()
    if n in loaded:
        return n
    for key in loaded:
        if key.lower().replace("_", "") == n2:
            return key
    return None


# ---------------------------------------------------------------------------
# Disk-backed loader
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_all_references() -> dict[str, list[ReferenceImpl]]:
    """Load every JSON reference under `impls/` and group by canonical key."""
    out: dict[str, list[ReferenceImpl]] = {}
    if not REFERENCES_DIR.exists():
        return out
    for path in sorted(REFERENCES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        entries = data if isinstance(data, list) else [data]
        for e in entries:
            try:
                impl = ReferenceImpl(
                    algorithm=e["algorithm"],
                    variant=e.get("variant", "default"),
                    title=e.get("title", e["algorithm"]),
                    rust_source=e["rust_source"],
                    signature=e.get("signature", ""),
                    standards=list(e.get("standards", [])),
                    source_url=e.get("source_url", ""),
                    license=e.get("license", "Apache-2.0"),
                    notes=e.get("notes", ""),
                )
            except KeyError:
                continue
            out.setdefault(impl.algorithm, []).append(impl)
    return out


# ---------------------------------------------------------------------------
# In-memory registration (for tests / third-party extensions)
# ---------------------------------------------------------------------------

_runtime_registry: dict[str, list[ReferenceImpl]] = {}


def register_reference(impl: ReferenceImpl) -> None:
    """Add an implementation to the runtime registry (overrides disk)."""
    _runtime_registry.setdefault(impl.algorithm, []).append(impl)


def clear_runtime_registry() -> None:
    """Test-isolation helper."""
    _runtime_registry.clear()


# ---------------------------------------------------------------------------
# Public query API
# ---------------------------------------------------------------------------

def find_references(algorithm: str) -> ReferenceMatch:
    """Look up Rust reference implementations for the given algorithm name.

    The lookup is alias-tolerant — `crc32`, `CRC-32`, `crc_32_ieee`, etc.
    all resolve to the same canonical entry.
    """
    canonical = _canonical(algorithm)
    if not canonical:
        return ReferenceMatch(algorithm=algorithm, impls=[])
    disk = _load_all_references().get(canonical, [])
    runtime = _runtime_registry.get(canonical, [])
    # Runtime overrides win (prepended), then disk entries.
    impls = list(runtime) + list(disk)
    return ReferenceMatch(algorithm=canonical, impls=impls)


def list_references() -> list[str]:
    """Every canonical algorithm with at least one reference impl."""
    disk_keys = set(_load_all_references().keys())
    runtime_keys = set(_runtime_registry.keys())
    return sorted(disk_keys | runtime_keys)


def references_for_standards(standards: list[str]) -> list[ReferenceImpl]:
    """Resolve references by `referenced_standards` entries.

    An AlgorithmSpec listing standards like `["RFC 1950", "Adler-32"]` will
    return whatever reference impls cite those standards. Also does fuzzy
    matching: if the spec says "Adler-32 checksum algorithm (as used in zlib)"
    and a reference impl cites "Adler-32", that's a match — any word from
    the impl's standard name appearing in the spec's standard string counts.
    """
    wanted_raw = [s.strip().lower() for s in standards if s]
    results: list[ReferenceImpl] = []
    seen: set[tuple[str, str]] = set()
    for impls in _load_all_references().values():
        for impl in impls:
            cited = [s.lower() for s in impl.standards]
            matched = False
            for c in cited:
                for w in wanted_raw:
                    # Exact match
                    if c == w or c in w or w in c:
                        matched = True
                        break
                    # Word-level: any significant word from the citation
                    # appears in the wanted string
                    c_words = {cw for cw in c.split() if len(cw) > 2}
                    if c_words and all(cw in w for cw in c_words):
                        matched = True
                        break
                if matched:
                    break
            if matched:
                key = (impl.algorithm, impl.variant)
                if key not in seen:
                    seen.add(key)
                    results.append(impl)
    return results
