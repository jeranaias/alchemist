"""Phase D: Stage 5 must catch every bug the previous zlib run shipped.

These tests exercise the mandatory verification gate against the existing
`subjects/zlib/.alchemist/output/` generated workspace. The gate MUST fail:

  - Anti-stub gate → ≥18 stubs present (compress, uncompress, deflate).
  - Architecturally, the gate REFUSES success without a diff_config anyway.

That proves Stage 5 would have blocked the prior session from claiming
success on a broken zlib — the production bar is now enforced.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alchemist.verifier.differential_tester import (
    DifferentialTester,
    verify_workspace,
)
from alchemist.verifier.zlib_config import (
    zlib_diff_config,
    zlib_harnesses,
    zlib_public_signatures,
)


ZLIB_OUTPUT = Path(__file__).parent.parent / "subjects" / "zlib" / ".alchemist" / "output"
ZLIB_SOURCE = Path(__file__).parent.parent / "subjects" / "zlib"


# ---------- Config shape ----------

def test_zlib_harnesses_include_adler_crc_deflate():
    harnesses = zlib_harnesses()
    names = {h.algorithm for h in harnesses}
    assert {"adler32", "crc32", "deflate"} <= names


def test_zlib_public_signatures_shape():
    sigs = zlib_public_signatures()
    names = {s.name for s in sigs}
    assert {"adler32", "crc32", "compress", "uncompress"} <= names
    # Adler-32 must take uLong initial + buf + uInt len
    adler = next(s for s in sigs if s.name == "adler32")
    assert ("adler", "uLong") in adler.params


def test_zlib_diff_config_builds(tmp_path):
    # Build against current zlib source (if present)
    if not ZLIB_SOURCE.exists():
        pytest.skip("subjects/zlib not present")
    cfg = zlib_diff_config(ZLIB_SOURCE)
    assert cfg.c_public_signatures
    assert cfg.harnesses
    assert cfg.c_sources  # should have discovered at least some .c files


# ---------- Anti-stub against existing zlib output ----------

@pytest.mark.skipif(not ZLIB_OUTPUT.exists(), reason="zlib output not present")
def test_stage5_anti_stub_gate_fails_on_current_zlib():
    """Stage 5's anti-stub gate must flag the existing zlib output as broken."""
    tester = DifferentialTester(ZLIB_OUTPUT)
    anti_stub = tester.gate_anti_stub()
    assert not anti_stub.passed
    assert anti_stub.anti_stub_report is not None
    # Previous session catalogued ≥18 stubs across compress / uncompress / deflate
    assert len(anti_stub.anti_stub_report.violations) >= 18


@pytest.mark.skipif(not ZLIB_OUTPUT.exists(), reason="zlib output not present")
def test_stage5_refuses_success_on_current_zlib():
    """End-to-end: gate returns passed=False on current zlib output.

    Even if cargo check/test pass (they do — it compiles), anti-stub blocks.
    """
    report = verify_workspace(ZLIB_OUTPUT, diff_config=None, refuse_without_diff=True)
    assert not report.passed
    ff = report.first_failure
    assert ff is not None
    # Expect the failure to be one of the hard gates (anti-stub or differential)
    assert ff.name in ("anti-stub", "differential", "test", "compile")


# ---------- Standards-catalog sanity for zlib algorithms ----------

def test_zlib_catalog_algorithms_present():
    from alchemist.standards import lookup_test_vectors
    assert lookup_test_vectors("adler32"), "adler32 must have standards vectors"
    assert lookup_test_vectors("crc32"), "crc32 must have standards vectors"
    assert lookup_test_vectors("deflate"), "deflate must have standards vectors"
