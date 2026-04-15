"""Tests for alchemist.pipeline_incremental."""

from __future__ import annotations

import json
from pathlib import Path

from alchemist.pipeline_incremental import (
    ChangeSet,
    IncrementalState,
    diff_states,
    fingerprint_file,
    fingerprint_source_dir,
    invalidate_checkpoints,
)


def test_fingerprint_file(tmp_path):
    f = tmp_path / "a.c"
    f.write_text("int x = 1;\n", encoding="utf-8")
    fp = fingerprint_file(f)
    assert fp.sha256
    assert fp.mtime > 0


def test_fingerprint_source_dir(tmp_path):
    (tmp_path / "a.c").write_text("int a;\n", encoding="utf-8")
    (tmp_path / "b.h").write_text("int b;\n", encoding="utf-8")
    (tmp_path / "readme.md").write_text("ignore me\n", encoding="utf-8")
    state = fingerprint_source_dir(tmp_path)
    assert len(state.fingerprints) == 2  # .c and .h only


def test_diff_states_detects_changes(tmp_path):
    (tmp_path / "a.c").write_text("v1\n", encoding="utf-8")
    old = fingerprint_source_dir(tmp_path)

    (tmp_path / "a.c").write_text("v2\n", encoding="utf-8")
    (tmp_path / "b.c").write_text("new\n", encoding="utf-8")
    new = fingerprint_source_dir(tmp_path)

    cs = diff_states(old, new)
    assert len(cs.modified) == 1
    assert any("a.c" in m for m in cs.modified)
    assert len(cs.added) == 1
    assert any("b.c" in a for a in cs.added)
    assert cs.any_changes


def test_diff_states_detects_deletions(tmp_path):
    (tmp_path / "a.c").write_text("x\n", encoding="utf-8")
    old = fingerprint_source_dir(tmp_path)

    (tmp_path / "a.c").unlink()
    new = fingerprint_source_dir(tmp_path)

    cs = diff_states(old, new)
    assert len(cs.deleted) == 1


def test_diff_states_no_changes(tmp_path):
    (tmp_path / "a.c").write_text("same\n", encoding="utf-8")
    s = fingerprint_source_dir(tmp_path)
    cs = diff_states(s, s)
    assert not cs.any_changes


def test_state_save_load_roundtrip(tmp_path):
    state = IncrementalState()
    from alchemist.pipeline_incremental import FileFingerprint
    state.fingerprints["a.c"] = FileFingerprint(path="a.c", sha256="abc123", mtime=1.0)
    path = tmp_path / "state.json"
    state.save(path)
    loaded = IncrementalState.load(path)
    assert loaded.fingerprints["a.c"].sha256 == "abc123"


def test_invalidate_checkpoints_deletes_affected(tmp_path):
    # Set up checkpoint structure
    ck = tmp_path / ".alchemist"
    fn_dir = ck / "specs" / "_functions" / "checksum"
    fn_dir.mkdir(parents=True)
    (fn_dir / "adler32.json").write_text("{}", encoding="utf-8")
    (fn_dir / "crc32.json").write_text("{}", encoding="utf-8")
    spec = ck / "specs" / "checksum.json"
    spec.write_text(json.dumps({
        "algorithms": [{"source_functions": ["adler32", "crc32"]}],
    }), encoding="utf-8")

    analysis = {
        "files": {
            "adler32.c": {"functions": [{"name": "adler32"}]},
            "crc32.c": {"functions": [{"name": "crc32"}]},
        },
    }
    cs = ChangeSet(modified=["adler32.c"])
    deleted = invalidate_checkpoints(cs, analysis, ck)
    assert any("adler32" in d for d in deleted)
    assert not (fn_dir / "adler32.json").exists()
    # crc32 should be untouched
    assert (fn_dir / "crc32.json").exists()


def test_change_set_summary():
    cs = ChangeSet(added=["a.c"], modified=["b.c", "c.c"], deleted=[])
    assert "1 added" in cs.summary()
    assert "2 modified" in cs.summary()
