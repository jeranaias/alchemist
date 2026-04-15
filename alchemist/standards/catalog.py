"""Load and query the standards test-vector catalog.

Vectors live in sibling JSON files (rfc1950_adler32.json, ...). This module
provides the Python API that the extractor and verifier call to pull the
authoritative inputs/outputs for any algorithm Alchemist recognizes.
"""

from __future__ import annotations

import binascii
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


STANDARDS_DIR = Path(__file__).parent


# Map common algorithm name variants → canonical key used in JSON files.
# Extractor may produce names like `adler-32`, `adler32`, `Adler32`, etc.
_ALIASES: dict[str, str] = {
    "adler": "adler32",
    "adler32": "adler32",
    "adler-32": "adler32",
    "adler_32": "adler32",
    "crc": "crc32",
    "crc32": "crc32",
    "crc-32": "crc32",
    "crc_32": "crc32",
    "crc32-ieee": "crc32",
    "crc32_ieee": "crc32",
    "deflate": "deflate",
    "inflate": "deflate",
    "zlib": "deflate",
    "aes": "aes",
    "aes-128": "aes128",
    "aes128": "aes128",
    "aes-192": "aes192",
    "aes192": "aes192",
    "aes-256": "aes256",
    "aes256": "aes256",
    "sha": "sha256",
    "sha1": "sha1",
    "sha-1": "sha1",
    "sha224": "sha224",
    "sha-224": "sha224",
    "sha256": "sha256",
    "sha-256": "sha256",
    "sha384": "sha384",
    "sha-384": "sha384",
    "sha512": "sha512",
    "sha-512": "sha512",
    "md5": "md5",
    "md-5": "md5",
}


# Map canonical key → JSON file name.
_CATALOG_FILES: dict[str, str] = {
    "adler32": "rfc1950_adler32.json",
    "crc32": "rfc1952_crc32.json",
    "deflate": "rfc1951_deflate.json",
    "aes128": "fips197_aes.json",
    "aes192": "fips197_aes.json",
    "aes256": "fips197_aes.json",
    "sha1": "fips180_sha.json",
    "sha224": "fips180_sha.json",
    "sha256": "fips180_sha.json",
    "sha384": "fips180_sha.json",
    "sha512": "fips180_sha.json",
    "md5": "rfc1321_md5.json",
}


@dataclass(frozen=True)
class TestVector:
    """A single (input → expected output) correctness pair."""
    __test__ = False  # not a pytest test class

    algorithm: str
    name: str
    input_hex: str
    expected_hex: str
    # Optional fields for cipher test vectors.
    key_hex: str | None = None
    iv_hex: str | None = None
    mode: str | None = None
    source: str | None = None
    description: str | None = None

    @property
    def input_bytes(self) -> bytes:
        return binascii.unhexlify(self.input_hex) if self.input_hex else b""

    @property
    def expected_bytes(self) -> bytes:
        return binascii.unhexlify(self.expected_hex) if self.expected_hex else b""

    @property
    def key_bytes(self) -> bytes | None:
        return binascii.unhexlify(self.key_hex) if self.key_hex else None

    @property
    def iv_bytes(self) -> bytes | None:
        return binascii.unhexlify(self.iv_hex) if self.iv_hex else None

    def as_rust_literal(self, kind: str = "input") -> str:
        """Return `&[u8; N]` literal suitable for embedding in Rust source."""
        data = {
            "input": self.input_bytes,
            "expected": self.expected_bytes,
            "key": self.key_bytes or b"",
            "iv": self.iv_bytes or b"",
        }[kind]
        if not data:
            return "&[]"
        body = ", ".join(f"0x{b:02x}" for b in data)
        return f"&[{body}]"


def match_algorithm(name: str) -> str | None:
    """Map an extractor-supplied algorithm name to a canonical key."""
    if not name:
        return None
    n = name.strip().lower().replace(" ", "_").replace("-", "_")
    # Try exact alias
    if n in _ALIASES:
        return _ALIASES[n]
    # Try normalized variants
    n2 = n.replace("_", "").replace("-", "")
    if n2 in _ALIASES:
        return _ALIASES[n2]
    # Try prefix: `adler32_impl` → `adler32`
    for alias, canonical in sorted(_ALIASES.items(), key=lambda x: -len(x[0])):
        if n.startswith(alias):
            return canonical
    return None


@lru_cache(maxsize=None)
def _load_file(name: str) -> dict:
    path = STANDARDS_DIR / name
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def lookup_test_vectors(algorithm: str) -> list[TestVector]:
    """Return all known test vectors for the algorithm (or alias)."""
    canonical = match_algorithm(algorithm)
    if not canonical:
        return []
    file_name = _CATALOG_FILES.get(canonical)
    if not file_name:
        return []
    data = _load_file(file_name)
    if not data:
        return []
    vectors_raw = data.get("vectors", [])
    result: list[TestVector] = []
    for v in vectors_raw:
        # Some files mix variants (AES-128/192/256); filter by exact canonical
        # if a `variant` tag is present.
        if "variant" in v and v["variant"] != canonical:
            continue
        result.append(TestVector(
            algorithm=canonical,
            name=v.get("name", ""),
            input_hex=v.get("input_hex", "") or v.get("plaintext_hex", ""),
            expected_hex=v.get("expected_hex", "") or v.get("ciphertext_hex", "") or v.get("digest_hex", ""),
            key_hex=v.get("key_hex"),
            iv_hex=v.get("iv_hex"),
            mode=v.get("mode"),
            source=data.get("source") or v.get("source"),
            description=v.get("description"),
        ))
    return result


def list_algorithms() -> list[str]:
    """Return every canonical algorithm key that has at least one loaded vector."""
    out: set[str] = set()
    for canonical, file_name in _CATALOG_FILES.items():
        data = _load_file(file_name)
        if not data or not data.get("vectors"):
            continue
        variants = {v.get("variant") for v in data["vectors"] if v.get("variant")}
        if variants:
            if canonical in variants:
                out.add(canonical)
        else:
            out.add(canonical)
    return sorted(out)


def ascii_input(s: str) -> str:
    """Convenience: encode ASCII string into hex for manual vector construction."""
    return s.encode("ascii").hex()
