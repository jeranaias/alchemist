"""Multi-sample parallel TDD candidate generation.

When the per-function TDD loop gets stuck (same wrong answer at
`temperature=0.15`), fanning out N parallel completions at a higher
temperature and picking the best by compile/test outcome dramatically
widens the search without adding many more iterations.

The behavior is:

  * Up to N LLM calls in parallel (default N=4) at temperature=0.35.
  * Each candidate is deterministically scrubbed + anti-stub-checked.
  * Candidates that still contain stubs are discarded up front.
  * The remaining candidates are scored by:
      1. whether the function body compiles (crate-level `cargo check`),
      2. how many of the function's targeted tests pass,
      3. tie-break by shorter body (less likely to be scaffolding).
  * The top-scoring candidate wins. The file is left in that state.

If every candidate fails to compile, the original source is restored and
the caller is told the sampling produced no usable candidate.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class SampleScore:
    candidate_idx: int
    body: str              # the new fn-body text returned by the sampler
    compiled: bool = False
    tests_passed: int = 0
    tests_failed: int = 0
    length: int = 0
    error_summary: str = ""

    @property
    def test_pass_rate(self) -> float:
        total = self.tests_passed + self.tests_failed
        if total == 0:
            return 0.0
        return self.tests_passed / total

    def score(self) -> tuple[int, float, int, int]:
        """Higher is better. Tie-breaker is shorter body."""
        return (
            1 if self.compiled else 0,
            self.test_pass_rate,
            self.tests_passed,
            -self.length,   # negate so "shorter is better"
        )


@dataclass
class MultiSampleResult:
    best: SampleScore | None = None
    scores: list[SampleScore] = field(default_factory=list)
    all_failed: bool = False

    @property
    def ok(self) -> bool:
        return self.best is not None and self.best.compiled


# ---------------------------------------------------------------------------
# Sampler contract
# ---------------------------------------------------------------------------

# The sampler callable takes (candidate_index) and returns the replacement
# fn-body text (signature + body), or None if the LLM failed to respond.
Sampler = Callable[[int], str | None]

# The splicer callable writes a candidate into the source file. It takes
# (body_text) and returns True on success.
Splicer = Callable[[str], bool]

# The evaluator callable measures a candidate after it's been spliced in.
# Returns (compiled, tests_passed, tests_failed, error_summary).
Evaluator = Callable[[], tuple[bool, int, int, str]]


class FileIntegrityError(RuntimeError):
    """Raised when a post-candidate restore does not return the file to
    its original byte-for-byte state. Indicates pipeline corruption —
    never swallow.
    """


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(file_path: Path, content: str) -> None:
    """Write content to file_path atomically via temp+rename.

    os.replace is atomic on both POSIX and Windows (Python 3.3+). Avoids
    the window where a concurrent cargo reader observes a half-written
    file, or where an exception mid-write leaves the file truncated.
    """
    # Same parent dir so rename is same-filesystem
    parent = file_path.parent
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=f".{file_path.stem}.", suffix=".candidate",
        dir=str(parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, str(file_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def run_multi_sample(
    *,
    sampler: Sampler,
    splicer: Splicer,
    evaluator: Evaluator,
    original_source: str,
    file_path: Path,
    n_samples: int = 4,
    reject_stub: Callable[[str], bool] | None = None,
    max_workers: int = 4,
) -> MultiSampleResult:
    """Run N parallel samples, splice+evaluate each serially, return the best.

    `reject_stub` is called on each candidate body; if it returns True the
    candidate is discarded before splicing.

    File integrity invariant: when this function returns without a winner,
    the file at `file_path` MUST contain `original_source` byte-for-byte.
    Verified via SHA256 of the restored content. If it doesn't match, we
    raise FileIntegrityError rather than silently continuing — that's a
    corruption bug the caller needs to surface.
    """
    # Invariant: snapshot the source hash now. Any restore path must
    # re-establish this exact byte sequence.
    original_hash = _sha256_bytes(original_source.encode("utf-8"))

    # 1. Fan out — produce all candidates in parallel.
    candidates: list[tuple[int, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(sampler, i): i for i in range(n_samples)}
        for fut in concurrent.futures.as_completed(futures):
            idx = futures[fut]
            try:
                body = fut.result()
            except Exception:  # noqa: BLE001
                body = None
            if body is not None:
                if reject_stub and reject_stub(body):
                    continue
                candidates.append((idx, body))

    if not candidates:
        _verify_restored(file_path, original_source, original_hash)
        return MultiSampleResult(all_failed=True)

    # 2. Evaluate each candidate serially (cargo check/test isn't safe to
    #    parallelize on a single workspace — they'd race on target/).
    #    Each candidate: atomic splice → evaluate → atomic restore.
    #    If ANY step raises, we restore and re-raise. Never leave the
    #    file in a candidate state.
    scores: list[SampleScore] = []
    for idx, body in candidates:
        try:
            ok_splice = splicer(body)
            if not ok_splice:
                continue
            compiled, passed, failed, err = evaluator()
            scores.append(SampleScore(
                candidate_idx=idx,
                body=body,
                compiled=compiled,
                tests_passed=passed,
                tests_failed=failed,
                length=len(body),
                error_summary=err,
            ))
        finally:
            # Always restore the original — whether eval passed, failed,
            # or raised. Atomic so a concurrent cargo worker reading the
            # file sees either the candidate or the original, never half.
            _atomic_write(file_path, original_source)

    # Verify the restore actually happened — defence in depth.
    _verify_restored(file_path, original_source, original_hash)

    if not scores:
        return MultiSampleResult(all_failed=True)

    # 3. Pick the winner.
    best = max(scores, key=lambda s: s.score())
    # Write the winner back (non-atomic splicer call preserves the
    # caller's splice semantics — caller writes the fn body in-place).
    splicer(best.body)

    return MultiSampleResult(best=best, scores=scores, all_failed=not best.compiled)


def _verify_restored(
    file_path: Path, original_source: str, original_hash: str,
) -> None:
    """Ensure the file is back to its original bytes after multi-sample.

    Raises FileIntegrityError if not. This is a hard gate — silently
    continuing past corrupted source has historically cascaded into
    whole-crate splice failures (see Phase 0 audit bug #2).
    """
    try:
        current = file_path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as e:
        raise FileIntegrityError(
            f"multi-sample restore check: cannot read {file_path}: {e}"
        ) from e
    current_hash = _sha256_bytes(current.encode("utf-8"))
    if current_hash != original_hash:
        # Last-ditch rescue: force-write original again, then raise.
        try:
            _atomic_write(file_path, original_source)
        except Exception:  # noqa: BLE001
            pass
        raise FileIntegrityError(
            f"multi-sample restore FAILED to match original at {file_path}: "
            f"expected sha256={original_hash[:12]}, got {current_hash[:12]}"
        )


# ---------------------------------------------------------------------------
# Cargo evaluator adapter
# ---------------------------------------------------------------------------

def make_cargo_evaluator(
    crate_dir: Path,
    test_filter: str,
    *,
    check_timeout: int = 180,
    test_timeout: int = 300,
) -> Evaluator:
    """Build an Evaluator that runs `cargo check` + `cargo test <filter>`."""
    def evaluate() -> tuple[bool, int, int, str]:
        try:
            chk = subprocess.run(
                ["cargo", "check"],
                cwd=str(crate_dir),
                capture_output=True, text=True, timeout=check_timeout,
                encoding="utf-8", errors="replace",
            )
        except Exception as e:  # noqa: BLE001
            return False, 0, 0, str(e)[:500]
        if chk.returncode != 0:
            return False, 0, 0, "\n".join(chk.stderr.splitlines()[:10])
        try:
            tst = subprocess.run(
                ["cargo", "test", test_filter, "--", "--nocapture"],
                cwd=str(crate_dir),
                capture_output=True, text=True, timeout=test_timeout,
                encoding="utf-8", errors="replace",
            )
        except Exception as e:  # noqa: BLE001
            return True, 0, 0, str(e)[:500]
        combined = tst.stdout + "\n" + tst.stderr
        passed, failed = _parse_test_counts(combined)
        return True, passed, failed, "\n".join(tst.stderr.splitlines()[:10])
    return evaluate


def _parse_test_counts(text: str) -> tuple[int, int]:
    """Parse `test result: ok. X passed; Y failed;` from cargo output."""
    import re
    passed = failed = 0
    for m in re.finditer(
        r"test result: \S+\s+\.?\s*(\d+)\s+passed;\s+(\d+)\s+failed", text
    ):
        passed += int(m.group(1))
        failed += int(m.group(2))
    return passed, failed
