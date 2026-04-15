"""Test-vector amplifier.

After a generated function passes the curated catalog vectors, amplify
with randomized inputs — 100K random byte slices of varying lengths —
run them through both the Rust output and the C reference, and look for
the first mismatch. That mismatch becomes a new test vector folded back
into the spec, and the function is regenerated with the new constraint
in its test suite.

The curated RFC/NIST vectors prove correctness on a handful of
well-studied inputs; the amplifier catches subtle bugs like:

  * seed-handling off-by-one (works for seed=1 but breaks for seed=0)
  * chunk-boundary issues when input length crosses NMAX for Adler-32
  * integer overflow that manifests only at specific input sizes
  * reflected-vs-not CRC variants that happen to agree on ASCII inputs
    but diverge on bytes with the high bit set

This module exposes:
  * CRustRunner — an abstract interface for "run both impls, compare"
  * RandomInputStrategy — generators for bytes / structured inputs
  * amplify() — the main driver
  * Mismatch → TestVector conversion for spec.test_vectors injection
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from typing import Callable, Iterator, Protocol

from alchemist.extractor.schemas import TestVector


# ---------------------------------------------------------------------------
# Input strategies
# ---------------------------------------------------------------------------

@dataclass
class InputStrategy:
    """A bounded source of random inputs."""
    name: str
    min_len: int = 0
    max_len: int = 4096
    # If set, the generator will include these inputs first (useful for
    # catching regressions on known-bad edge cases).
    preset: list[bytes] = field(default_factory=list)

    def generate(self, n: int, seed: int | None = None) -> Iterator[bytes]:
        """Yield n inputs. Presets first, then random."""
        yield from self.preset[:n]
        remaining = max(0, n - len(self.preset))
        rng = _seeded_rng(seed)
        for _ in range(remaining):
            length = rng.randrange(self.min_len, self.max_len + 1)
            yield bytes(rng.getrandbits(8) for _ in range(length))


def _seeded_rng(seed: int | None):
    import random
    r = random.Random()
    if seed is not None:
        r.seed(seed)
    else:
        r.seed(int.from_bytes(secrets.token_bytes(8), "big"))
    return r


# Pre-made strategies for common algorithm categories.
CHECKSUM_STRATEGY = InputStrategy(
    name="checksum_default",
    min_len=0, max_len=8192,
    preset=[
        b"", b"a", b"ab", b"abc", b"0123456789",
        b"\x00", b"\xff", b"\x00" * 256, b"\xff" * 256,
        # Bytes with high bit set — catches reflected/non-reflected CRC divergence
        bytes(range(256)),
        # ASCII to non-ASCII transition
        bytes(range(0x20, 0x80)) + bytes(range(0x80, 0xFF)),
    ],
)

HASH_STRATEGY = InputStrategy(
    name="hash_default",
    min_len=0, max_len=2048,
    preset=[
        b"", b"a", b"abc", b"The quick brown fox jumps over the lazy dog",
        b"\x00" * 64, b"\x00" * 65, b"\x00" * 127, b"\x00" * 128,
    ],
)


# ---------------------------------------------------------------------------
# Runner protocol
# ---------------------------------------------------------------------------

class CRustRunner(Protocol):
    """Runs both the C reference and the Rust translation on a single input.

    Must return byte-length-encoded outputs for comparison. Implementations
    live in Stage 5's FFI layer (see alchemist.verifier.differential_tester).
    """
    def run_c(self, data: bytes) -> bytes: ...
    def run_rust(self, data: bytes) -> bytes: ...


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class Mismatch:
    input_bytes: bytes
    c_output: bytes
    rust_output: bytes
    index: int                  # which of N iterations found it

    def as_test_vector(self, input_param: str = "input") -> TestVector:
        """Convert to a spec TestVector for regen feedback."""
        input_lit = _bytes_to_rust_literal(self.input_bytes)
        expected_lit = _bytes_to_rust_literal(self.c_output)
        return TestVector(
            description=(
                f"Amplifier mismatch at iteration {self.index}; "
                f"C reference output is {expected_lit[:60]}..."
            ),
            inputs={input_param: input_lit},
            expected_output=expected_lit,
            tolerance="exact",
            source="test-vector amplifier",
        )


@dataclass
class AmplifyReport:
    iterations_run: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)
    stopped_early: bool = False
    elapsed_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def summary(self) -> str:
        if self.ok:
            return (
                f"amplifier: {self.iterations_run} inputs, 0 mismatches "
                f"({self.elapsed_seconds:.1f}s)"
            )
        first = self.mismatches[0]
        return (
            f"amplifier: FAIL after {first.index}/{self.iterations_run} inputs; "
            f"first mismatch input={len(first.input_bytes)}B → "
            f"C={first.c_output.hex()[:16]} vs Rust={first.rust_output.hex()[:16]}"
        )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def amplify(
    runner: CRustRunner,
    strategy: InputStrategy,
    *,
    iterations: int = 10_000,
    seed: int | None = None,
    stop_after_mismatches: int = 1,
) -> AmplifyReport:
    """Run both impls on random inputs; return the first N mismatches.

    Stops early once `stop_after_mismatches` have been collected, since the
    canonical use is "find one counterexample and fold it into the spec."
    """
    import time
    start = time.monotonic()
    report = AmplifyReport()
    for idx, data in enumerate(strategy.generate(iterations, seed=seed)):
        report.iterations_run = idx + 1
        try:
            c_out = runner.run_c(data)
            r_out = runner.run_rust(data)
        except Exception:  # noqa: BLE001
            # Runner crashed on this input — treat as a mismatch with empty outputs
            report.mismatches.append(Mismatch(
                input_bytes=data, c_output=b"", rust_output=b"", index=idx,
            ))
            if len(report.mismatches) >= stop_after_mismatches:
                report.stopped_early = True
                break
            continue
        if c_out != r_out:
            report.mismatches.append(Mismatch(
                input_bytes=data, c_output=c_out, rust_output=r_out, index=idx,
            ))
            if len(report.mismatches) >= stop_after_mismatches:
                report.stopped_early = True
                break
    report.elapsed_seconds = time.monotonic() - start
    return report


# ---------------------------------------------------------------------------
# Rust-literal helper
# ---------------------------------------------------------------------------

def _bytes_to_rust_literal(data: bytes) -> str:
    """Produce a `&[u8]` Rust literal for embedding in test sources."""
    if not data:
        return "&[]"
    body = ", ".join(f"0x{b:02x}" for b in data)
    return f"&[{body}]"


# ---------------------------------------------------------------------------
# Fold mismatches back into specs
# ---------------------------------------------------------------------------

def fold_mismatches_into_spec(
    spec_test_vectors: list[TestVector],
    mismatches: list[Mismatch],
    *,
    input_param: str = "input",
    max_to_add: int = 5,
) -> list[TestVector]:
    """Return a new list of test vectors with mismatches appended.

    Doesn't mutate the input list. Deduplicates against existing vectors
    by raw input bytes.
    """
    out = list(spec_test_vectors)
    existing_inputs: set[bytes] = set()
    for tv in spec_test_vectors:
        for v in tv.inputs.values():
            # Best-effort: try to read back the input bytes. This is loose —
            # we only need to avoid adding EXACT duplicates.
            existing_inputs.add(v.encode() if isinstance(v, str) else bytes(v))

    added = 0
    for mm in mismatches:
        if added >= max_to_add:
            break
        lit = _bytes_to_rust_literal(mm.input_bytes)
        if lit.encode() in existing_inputs:
            continue
        out.append(mm.as_test_vector(input_param=input_param))
        added += 1
    return out
