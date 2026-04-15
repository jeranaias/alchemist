"""Phase C integration tests — validator gate, field scanner, Stage 5 refusal.

These test the pipeline wiring in isolation (without the LLM actually
running extraction/architect/impl). They confirm the safety nets fire.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alchemist.architect.schemas import (
    CrateArchitecture,
    CrateSpec,
)
from alchemist.extractor.schemas import (
    AlgorithmSpec,
    ModuleSpec,
    Parameter,
)
from alchemist.pipeline import (
    StageOutcome,
    TranslationReport,
    run_implement_stage,
    run_verify_stage,
)


def _prep_subject(tmp_path: Path, arch: CrateArchitecture, specs: list[ModuleSpec]) -> Path:
    """Make a `.alchemist/` checkpoint tree ready for Stage 4."""
    ck = tmp_path / ".alchemist"
    (ck / "specs").mkdir(parents=True)
    for s in specs:
        (ck / "specs" / f"{s.name}.json").write_text(
            s.model_dump_json(indent=2), encoding="utf-8"
        )
    (ck / "architecture.json").write_text(
        arch.model_dump_json(indent=2), encoding="utf-8"
    )
    return tmp_path


# ---------- Stage 5 refusal ----------

def test_verify_stage_refuses_without_diff_config(tmp_path):
    # Minimal compile-ready workspace
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "dummy"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("pub fn foo() -> u32 { 42 }\n", encoding="utf-8")

    outcome = run_verify_stage(tmp_path, tmp_path, diff_config=None, refuse_without_diff=True)
    assert not outcome.ok
    # Stage 5 explicitly refuses rather than claim success
    assert "FAIL" in outcome.summary.upper() or "refus" in outcome.summary.lower() or "skipped" in outcome.summary.lower() or "no differential" in outcome.summary.lower() or "differential" in outcome.summary.lower()


# ---------- Stage 3 validator gate ----------

def test_architect_validator_rejects_spec_coverage_error(tmp_path):
    """If the architecture references a module that has no spec, validator errors
    must prevent the pipeline from proceeding."""
    from alchemist.architect.validator import validate_architecture

    arch = CrateArchitecture(
        workspace_name="zlib_rs",
        description="",
        crates=[
            CrateSpec(
                name="zlib-bogus",
                description="",
                modules=["missing_module"],  # no spec!
                is_no_std=False,
            ),
        ],
    )
    report = validate_architecture(arch, specs=[])
    assert report.has_errors
    rule_names = {i.rule for i in report.errors}
    assert "spec_coverage" in rule_names


# ---------- Stage 4 with TDD + fake LLM (offline) ----------

class _FakeLLM:
    def __init__(self):
        self.total_cost = 0.0

    def create_cached_context(self, system_text, project_context=""):
        from alchemist.llm.client import CachedContext
        return CachedContext(system_prompt=system_text, project_context=project_context)

    def call_structured(self, messages, tool_name, tool_schema, **kwargs):
        from alchemist.llm.client import LLMResponse
        impl = (
            "pub fn adler32(input: &[u8]) -> u32 {\n"
            "    let mut s1: u32 = 1;\n"
            "    let mut s2: u32 = 0;\n"
            "    for b in input { s1 = (s1 + *b as u32) % 65521; s2 = (s2 + s1) % 65521; }\n"
            "    (s2 << 16) | s1\n"
            "}"
        )
        return LLMResponse(content="", structured={"content": impl})


@pytest.mark.skipif(
    __import__("shutil").which("cargo") is None,
    reason="cargo not on PATH",
)
def test_stage4_tdd_fakellm_produces_working_crate(tmp_path, monkeypatch):
    """Stage 4 (TDD) with a fake LLM produces a crate that compiles and passes."""
    alg = AlgorithmSpec(
        name="adler32",
        display_name="Adler-32",
        category="checksum",
        description="RFC 1950 Adler-32 checksum.",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="bytes")],
        return_type="u32",
        source_functions=["adler32"],
        referenced_standards=["RFC 1950"],
    )
    module = ModuleSpec(
        name="checksum", display_name="Checksums", description="",
        algorithms=[alg],
    )
    arch = CrateArchitecture(
        workspace_name="zlib_rs",
        description="",
        crates=[
            CrateSpec(name="zlib-checksum", description="",
                       modules=["checksum"], is_no_std=False),
        ],
    )

    source_dir = _prep_subject(tmp_path, arch, [module])
    out = tmp_path / "output"

    # Swap in fake LLM
    import alchemist.implementer.tdd_generator as tdd_mod
    monkeypatch.setattr(tdd_mod, "AlchemistLLM", lambda *a, **kw: _FakeLLM())

    outcome = run_implement_stage(source_dir, out, tdd=True)
    assert outcome.ok, f"Stage 4 failed: {outcome.summary}"


# ---------- Summary / shape ----------

def test_translation_report_shape():
    report = TranslationReport(workspace_dir=Path("."))
    report.add(StageOutcome(stage="analyze", ok=True, summary="x"))
    report.add(StageOutcome(stage="extract", ok=False, summary="y"))
    assert not report.ok
    ff = report.first_failure()
    assert ff is not None
    assert ff.stage == "extract"
    s = report.summary()
    assert "PASS" in s and "FAIL" in s
