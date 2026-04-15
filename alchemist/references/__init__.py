"""Reference implementation library.

Canonical, verified Rust implementations of common algorithms. The TDD
generator injects these into the LLM prompt when `referenced_standards`
matches a known algorithm — the model adapts the reference to match the
spec's signature instead of reinventing the algorithm from prose.

Why this matters: on tinychk, CRC-32 failed to converge in 5 iterations
because the spec description mentions both polynomial conventions
(reflected 0xEDB88320 and non-reflected 0x04C11DB7) and the model kept
mixing them. With a reference impl in the prompt, the model stops
guessing between valid variants and starts translating.

All references are Apache-2.0-compatible. Sources and checksums recorded
in each entry's metadata.
"""

from alchemist.references.registry import (
    ReferenceImpl,
    ReferenceMatch,
    find_references,
    list_references,
    register_reference,
)

__all__ = [
    "ReferenceImpl",
    "ReferenceMatch",
    "find_references",
    "list_references",
    "register_reference",
]
