"""Standards-based test vector catalog.

Use `lookup_test_vectors(algorithm)` to retrieve published test vectors for
a given algorithm. Each vector pairs a known input with the output the
official standard specifies. These are the non-negotiable correctness
anchors — any Rust implementation that fails them is wrong, full stop.
"""

from alchemist.standards.catalog import (
    TestVector,
    list_algorithms,
    lookup_test_vectors,
    match_algorithm,
)

__all__ = [
    "TestVector",
    "list_algorithms",
    "lookup_test_vectors",
    "match_algorithm",
]
