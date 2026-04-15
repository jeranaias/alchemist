"""Tests for alchemist.implementer.unsafe_fence."""

from __future__ import annotations

from pathlib import Path

import pytest

from alchemist.implementer.unsafe_fence import (
    UnsafeFenceConfig,
    UnsafeFenceReport,
    scan_workspace,
)


def _write_rs(tmp_path, crate, name, content):
    d = tmp_path / crate / "src"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(content, encoding="utf-8")


def test_no_toml_allows_nothing(tmp_path):
    cfg = UnsafeFenceConfig.load(tmp_path)
    assert not cfg.allow_patterns
    assert not cfg.is_allowed("any/file.rs")


def test_toml_parsing(tmp_path):
    (tmp_path / "alchemist.toml").write_text(
        '[unsafe]\nallow = ["hal/*", "ffi/bindings.rs"]\n',
        encoding="utf-8",
    )
    cfg = UnsafeFenceConfig.load(tmp_path)
    assert len(cfg.allow_patterns) == 2
    assert cfg.is_allowed("hal/gpio.rs")
    assert cfg.is_allowed("ffi/bindings.rs")
    assert not cfg.is_allowed("core/lib.rs")


def test_scan_flags_unauthorized_unsafe(tmp_path):
    _write_rs(tmp_path, "core", "lib.rs", "pub fn x() { unsafe { } }\n")
    report = scan_workspace(tmp_path, config=UnsafeFenceConfig())
    assert not report.ok
    assert len(report.violations) == 1
    assert "core" in report.violations[0].file


def test_scan_allows_whitelisted_files(tmp_path):
    _write_rs(tmp_path, "ffi", "bindings.rs", "pub fn x() { unsafe { } }\n")
    cfg = UnsafeFenceConfig(allow_patterns=["ffi/*"])
    report = scan_workspace(tmp_path, config=cfg)
    assert report.ok


def test_scan_ignores_unsafe_in_comments(tmp_path):
    _write_rs(tmp_path, "core", "lib.rs", "// unsafe { }\npub fn x() {}\n")
    report = scan_workspace(tmp_path, config=UnsafeFenceConfig())
    assert report.ok


def test_scan_ignores_unsafe_in_strings(tmp_path):
    _write_rs(tmp_path, "core", "lib.rs", 'pub fn x() { let s = "unsafe { }"; }\n')
    report = scan_workspace(tmp_path, config=UnsafeFenceConfig())
    assert report.ok


def test_scan_clean_workspace_ok(tmp_path):
    _write_rs(tmp_path, "core", "lib.rs", "pub fn safe() -> u32 { 42 }\n")
    report = scan_workspace(tmp_path)
    assert report.ok
    assert report.files_scanned == 1


def test_report_summary():
    r = UnsafeFenceReport(files_scanned=5)
    assert "0 violations" in r.summary()
