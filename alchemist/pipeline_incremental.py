"""Incremental re-runs — change one C file, re-extract only that file.

The checkpoint structure already supports this: per-function specs live
in `.alchemist/specs/_functions/<module>/<fn>.json`. We just need
invalidation plumbing: given a set of changed C files, determine which
functions are affected, delete their checkpoints, and re-run extract +
downstream stages only for those functions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileFingerprint:
    path: str
    sha256: str
    mtime: float


@dataclass
class IncrementalState:
    """Persisted fingerprints for invalidation tracking."""
    fingerprints: dict[str, FileFingerprint] = field(default_factory=dict)
    # path -> FileFingerprint

    def save(self, state_path: Path) -> None:
        data = {
            p: {"path": fp.path, "sha256": fp.sha256, "mtime": fp.mtime}
            for p, fp in self.fingerprints.items()
        }
        state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, state_path: Path) -> "IncrementalState":
        if not state_path.exists():
            return cls()
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            fps = {
                p: FileFingerprint(**v) for p, v in data.items()
            }
            return cls(fingerprints=fps)
        except Exception:
            return cls()


def fingerprint_file(path: Path) -> FileFingerprint:
    content = path.read_bytes()
    return FileFingerprint(
        path=str(path),
        sha256=hashlib.sha256(content).hexdigest(),
        mtime=path.stat().st_mtime,
    )


def fingerprint_source_dir(source_dir: Path, extensions: set[str] | None = None) -> IncrementalState:
    """Compute fingerprints for all C source files in a directory."""
    extensions = extensions or {".c", ".h"}
    state = IncrementalState()
    for f in sorted(source_dir.rglob("*")):
        if f.suffix in extensions and ".git" not in f.parts:
            fp = fingerprint_file(f)
            state.fingerprints[str(f)] = fp
    return state


@dataclass
class ChangeSet:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def changed_files(self) -> list[str]:
        return self.added + self.modified

    @property
    def any_changes(self) -> bool:
        return bool(self.added or self.modified or self.deleted)

    def summary(self) -> str:
        return (
            f"incremental: {len(self.added)} added, "
            f"{len(self.modified)} modified, "
            f"{len(self.deleted)} deleted"
        )


def diff_states(old: IncrementalState, new: IncrementalState) -> ChangeSet:
    """Compare two IncrementalStates and return the change set."""
    cs = ChangeSet()
    old_paths = set(old.fingerprints.keys())
    new_paths = set(new.fingerprints.keys())

    cs.added = sorted(new_paths - old_paths)
    cs.deleted = sorted(old_paths - new_paths)

    for p in sorted(old_paths & new_paths):
        if old.fingerprints[p].sha256 != new.fingerprints[p].sha256:
            cs.modified.append(p)

    return cs


def invalidate_checkpoints(
    change_set: ChangeSet,
    analysis: dict,
    checkpoint_dir: Path,
) -> list[str]:
    """Delete per-function checkpoints for functions in changed files.

    Returns the list of checkpoint paths deleted.
    """
    files_dict = analysis.get("files", {})
    # Map: file_path -> list of function names
    file_to_fns: dict[str, list[str]] = {}
    for fpath, fdata in files_dict.items():
        fns = [f["name"] for f in fdata.get("functions", [])]
        file_to_fns[fpath] = fns

    deleted_checkpoints: list[str] = []
    for changed_file in change_set.changed_files + change_set.deleted:
        fns = file_to_fns.get(changed_file, [])
        for fn_name in fns:
            # Per-function checkpoints: specs/_functions/<module>/<fn>.json
            # We need to find which module this function belongs to
            for module_dir in sorted((checkpoint_dir / "specs" / "_functions").iterdir()) \
                    if (checkpoint_dir / "specs" / "_functions").exists() else []:
                ckpt = module_dir / f"{fn_name}.json"
                if ckpt.exists():
                    ckpt.unlink()
                    deleted_checkpoints.append(str(ckpt))

    # Also invalidate the module-level spec if any function in it changed
    specs_dir = checkpoint_dir / "specs"
    if specs_dir.exists():
        for changed_file in change_set.changed_files + change_set.deleted:
            fns = file_to_fns.get(changed_file, [])
            if fns:
                # Find and delete module specs that contain these functions
                for spec_file in specs_dir.glob("*.json"):
                    if spec_file.name.startswith("_"):
                        continue
                    try:
                        spec_data = json.loads(spec_file.read_text(encoding="utf-8"))
                        spec_fns = set()
                        for alg in spec_data.get("algorithms", []):
                            spec_fns.update(alg.get("source_functions", []))
                        if spec_fns & set(fns):
                            spec_file.unlink()
                            deleted_checkpoints.append(str(spec_file))
                    except Exception:
                        continue

    return deleted_checkpoints
