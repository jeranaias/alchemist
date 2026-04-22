"""File-integrity guarantees for run_multi_sample.

Phase 0 Bug #2 fix: every candidate evaluation runs under atomic splice +
atomic restore, and any end-state divergence from the original source
raises FileIntegrityError.

These tests exercise fault paths:
  1. Evaluator raises mid-candidate — file must be restored
  2. Sampler succeeds but splicer fails — file unchanged
  3. Candidate succeeds — winner's body is written, verification passes
  4. Evaluator times out (simulated by raising inside) — restore still runs
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from alchemist.implementer.multi_sample import (
    FileIntegrityError,
    MultiSampleResult,
    SampleScore,
    _atomic_write,
    _sha256_bytes,
    run_multi_sample,
)


ORIGINAL = """pub fn my_fn(x: u32) -> u32 {
    unimplemented!("skeleton")
}
"""


def _make_file(tmp_path: Path) -> Path:
    p = tmp_path / "module.rs"
    p.write_text(ORIGINAL, encoding="utf-8")
    return p


def test_atomic_write_is_observable_only_complete(tmp_path: Path) -> None:
    p = _make_file(tmp_path)
    new_content = "// totally different\npub fn my_fn() {}\n"
    _atomic_write(p, new_content)
    assert p.read_text(encoding="utf-8") == new_content


def test_original_hash_is_stable() -> None:
    h1 = _sha256_bytes(ORIGINAL.encode("utf-8"))
    h2 = _sha256_bytes(ORIGINAL.encode("utf-8"))
    assert h1 == h2 and len(h1) == 64


def test_no_candidates_restores_original(tmp_path: Path) -> None:
    p = _make_file(tmp_path)
    # Simulate "every candidate rejected as stub" — sampler returns None
    result = run_multi_sample(
        sampler=lambda i: None,
        splicer=lambda body: True,
        evaluator=lambda: (True, 0, 0, ""),
        original_source=ORIGINAL,
        file_path=p,
        n_samples=2,
    )
    assert result.all_failed
    assert p.read_text(encoding="utf-8") == ORIGINAL


def test_evaluator_exception_does_not_corrupt_file(tmp_path: Path) -> None:
    p = _make_file(tmp_path)
    call_count = {"n": 0}

    def raising_evaluator() -> tuple[bool, int, int, str]:
        call_count["n"] += 1
        # Raise on the first candidate. Sort order of futures is not
        # deterministic so the splicer may have written ANY of the 3
        # candidates before this raises. The fix must still restore.
        raise RuntimeError("simulated cargo timeout")

    def splicer(body: str) -> bool:
        # Writes the candidate body into the file (simulates the real splicer).
        p.write_text(f"pub fn my_fn() {{ {body} }}\n", encoding="utf-8")
        return True

    # Expect the exception to propagate OUT of run_multi_sample (via the
    # try/finally); the file must still end up as ORIGINAL.
    with pytest.raises(RuntimeError, match="simulated cargo timeout"):
        run_multi_sample(
            sampler=lambda i: f"let _ = {i};",
            splicer=splicer,
            evaluator=raising_evaluator,
            original_source=ORIGINAL,
            file_path=p,
            n_samples=3,
        )

    # File must be restored to original despite the raised exception.
    assert p.read_text(encoding="utf-8") == ORIGINAL


def test_all_candidates_fail_splice_still_restores(tmp_path: Path) -> None:
    p = _make_file(tmp_path)
    result = run_multi_sample(
        sampler=lambda i: f"body_{i}",
        splicer=lambda body: False,  # splicer always fails
        evaluator=lambda: (False, 0, 0, "n/a"),
        original_source=ORIGINAL,
        file_path=p,
        n_samples=3,
    )
    assert result.all_failed
    assert p.read_text(encoding="utf-8") == ORIGINAL


def test_winner_body_is_written_and_hash_verified(tmp_path: Path) -> None:
    p = _make_file(tmp_path)
    winning_body = "42_u32.wrapping_add(0)"

    def splicer(body: str) -> bool:
        # Simulates real splicer: writes a fn with the body.
        p.write_text(
            f"pub fn my_fn(x: u32) -> u32 {{ {body} }}\n",
            encoding="utf-8",
        )
        return True

    def evaluator() -> tuple[bool, int, int, str]:
        src = p.read_text(encoding="utf-8")
        if winning_body in src:
            return True, 3, 0, ""
        return True, 0, 1, ""

    result = run_multi_sample(
        sampler=lambda i: winning_body if i == 1 else f"let _ = {i};",
        splicer=splicer,
        evaluator=evaluator,
        original_source=ORIGINAL,
        file_path=p,
        n_samples=3,
    )
    assert result.ok
    assert result.best is not None
    assert result.best.body == winning_body
    # Winner must be present in the final file.
    assert winning_body in p.read_text(encoding="utf-8")


def test_file_tampered_mid_run_raises_integrity_error(tmp_path: Path) -> None:
    """If something external modifies the file between the last restore
    and the final verify, we must raise FileIntegrityError.
    """
    p = _make_file(tmp_path)
    # Use a splicer that does NOT actually write to p (bypasses the normal
    # restore path) and tamper with p to simulate external corruption.
    def tamper_splicer(body: str) -> bool:
        return True  # success but didn't write
    def passing_evaluator() -> tuple[bool, int, int, str]:
        # Simulate external corruption here so it happens inside the
        # candidate loop, before the final verify.
        p.write_text("/* tampered */\n", encoding="utf-8")
        return False, 0, 0, ""

    # The restore in finally will re-write ORIGINAL, so the tamper alone
    # doesn't corrupt. This test instead verifies that if tampering
    # persists through the final verify, we raise.
    # To simulate persistence, make restore a no-op via monkey-patching.
    import alchemist.implementer.multi_sample as ms_mod
    orig_atomic = ms_mod._atomic_write
    ms_mod._atomic_write = lambda fp, content: None  # no-op to simulate
    try:
        with pytest.raises(FileIntegrityError, match="restore FAILED"):
            run_multi_sample(
                sampler=lambda i: "body",
                splicer=tamper_splicer,
                evaluator=passing_evaluator,
                original_source=ORIGINAL,
                file_path=p,
                n_samples=1,
            )
    finally:
        ms_mod._atomic_write = orig_atomic
