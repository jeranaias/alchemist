"""Tests for the plugin registry and the built-in crypto plugin."""

from __future__ import annotations

from pathlib import Path

import pytest

from alchemist.extractor.schemas import (
    AlgorithmSpec,
    ModuleSpec,
    Parameter,
)
from alchemist.plugins import (
    DomainPlugin,
    clear,
    get,
    list_plugins,
    load_builtins,
    register,
    run_lints,
)
from alchemist.plugins.crypto import (
    PLUGIN as CRYPTO_PLUGIN,
    augment_with_cavp,
    crypto_ct_lint,
    scan_file_for_ct,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    clear()
    yield
    clear()


# ---------- Registry mechanics ----------

def test_register_and_get():
    plugin = DomainPlugin(name="x", description="")
    register(plugin)
    assert get("x") is plugin


def test_load_builtins_registers_crypto():
    load_builtins()
    assert get("crypto") is not None
    assert any(p.name == "crypto" for p in list_plugins())


def test_run_lints_aggregates_findings(tmp_path):
    calls = []

    def lint_a(ws, specs):
        calls.append("a")
        return [("file.rs", 1, "a", "x")]

    def lint_b(ws, specs):
        calls.append("b")
        return [("file.rs", 2, "b", "y")]

    register(DomainPlugin(name="A", description="", lints=[lint_a]))
    register(DomainPlugin(name="B", description="", lints=[lint_b]))

    findings = run_lints(tmp_path, [])
    assert len(findings) == 2
    assert set(calls) == {"a", "b"}


def test_run_lints_swallows_plugin_crashes(tmp_path):
    def crasher(ws, specs):
        raise RuntimeError("boom")

    register(DomainPlugin(name="crasher", description="", lints=[crasher]))
    findings = run_lints(tmp_path, [])
    # Crash is surfaced as a finding, not a raised exception
    assert any("lint_crash" in f[2] for f in findings)


# ---------- Crypto CAVP augmentation ----------

def test_augment_with_cavp_adds_aes_vectors():
    alg = AlgorithmSpec(
        name="aes128",
        display_name="AES-128",
        category="cipher",
        description="AES-128 ECB",
        inputs=[Parameter(name="input", rust_type="&[u8]", description="bytes")],
        return_type="Vec<u8>",
    )
    module = ModuleSpec(
        name="aes", display_name="", description="", algorithms=[alg],
    )
    added = augment_with_cavp([module])
    assert added > 0
    # FIPS 197 Appendix B key/input surface in the extracted test_vectors
    assert any(
        "fips197_appendix_b" in tv.description.lower()
        for tv in alg.test_vectors
    )


def test_augment_skips_non_crypto():
    alg = AlgorithmSpec(
        name="adler32",
        display_name="",
        category="checksum",
        description="",
        inputs=[],
        return_type="u32",
    )
    module = ModuleSpec(
        name="checksum", display_name="", description="", algorithms=[alg],
    )
    added = augment_with_cavp([module])
    assert added == 0


def test_augment_deduplicates():
    alg = AlgorithmSpec(
        name="sha256",
        display_name="",
        category="hash",
        description="",
        inputs=[],
        return_type="Vec<u8>",
    )
    module = ModuleSpec(
        name="hash", display_name="", description="", algorithms=[alg],
    )
    first = augment_with_cavp([module])
    second = augment_with_cavp([module])
    assert first > 0
    assert second == 0  # all vectors already present


# ---------- Constant-time lint ----------

def test_ct_lint_flags_branch_on_key(tmp_path):
    f = tmp_path / "cipher.rs"
    f.write_text(
        "pub fn decrypt(key: &[u8], ct: &[u8]) -> Vec<u8> {\n"
        "    if key[0] == 0x00 {\n"
        "        return vec![0; ct.len()];\n"
        "    }\n"
        "    ct.to_vec()\n"
        "}\n",
        encoding="utf-8",
    )
    findings = scan_file_for_ct(f)
    rules = {f.rule for f in findings}
    assert "branch_on_secret" in rules


def test_ct_lint_flags_match_on_secret(tmp_path):
    f = tmp_path / "pw.rs"
    f.write_text(
        "pub fn check(password: &[u8]) -> bool {\n"
        "    match password[0] {\n"
        "        0 => true,\n"
        "        _ => false,\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    findings = scan_file_for_ct(f)
    assert any(f.rule == "match_on_secret" for f in findings)


def test_ct_lint_clean_code_has_no_findings(tmp_path):
    f = tmp_path / "clean.rs"
    f.write_text(
        "pub fn xor_ct(dst: &mut [u8], key: &[u8]) {\n"
        "    for (d, k) in dst.iter_mut().zip(key.iter().cycle()) {\n"
        "        *d ^= *k;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    findings = scan_file_for_ct(f)
    assert findings == []


# ---------- Plugin-level lint runs across workspace ----------

def test_crypto_ct_lint_runs_when_crypto_module_present(tmp_path):
    # Fake workspace with a crate having a cipher module
    crate = tmp_path / "aes-core"
    (crate / "src").mkdir(parents=True)
    (crate / "src" / "aes").mkdir()
    (crate / "src" / "aes.rs").write_text(
        "pub fn aes_decrypt(key: &[u8], ct: &[u8]) -> Vec<u8> {\n"
        "    if key[0] == 0 { return vec![]; }\n"
        "    ct.to_vec()\n"
        "}\n",
        encoding="utf-8",
    )
    spec = ModuleSpec(
        name="aes",
        display_name="",
        description="",
        algorithms=[AlgorithmSpec(
            name="aes_decrypt", display_name="", category="cipher",
            description="", inputs=[], return_type="Vec<u8>",
        )],
    )
    findings = crypto_ct_lint(tmp_path, [spec])
    assert findings
    assert any("branch_on_secret" in f[2] for f in findings)


def test_crypto_plugin_object_shape():
    assert CRYPTO_PLUGIN.name == "crypto"
    assert CRYPTO_PLUGIN.lints
    assert CRYPTO_PLUGIN.post_extract is not None
