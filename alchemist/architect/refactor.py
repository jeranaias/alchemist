"""Multi-file refactoring pass — auto-propose type relocations.

When the skeleton or implementation phase hits orphan-rule violations
(E0116: "cannot define inherent impl for a type outside the crate where
the type is defined"), this module proposes moving the type to the crate
that wants to `impl` it. The proposal is deterministic and conservative:

  1. Parse `cargo check` output for E0116 errors.
  2. For each, identify (type_name, defined_in_crate, impl_in_crate).
  3. Propose moving the type definition to impl_in_crate, or merging
     the crates if they have a strong dependency anyway.

The pipeline applies proposals automatically during Stage 4's fix loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TypeRelocation:
    """Proposal to move a type from one crate to another."""
    type_name: str
    from_crate: str
    to_crate: str
    reason: str

    def __str__(self) -> str:
        return f"move `{self.type_name}` from {self.from_crate} → {self.to_crate}: {self.reason}"


@dataclass
class RefactorProposal:
    relocations: list[TypeRelocation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.relocations) == 0

    def summary(self) -> str:
        if self.ok:
            return "no refactoring needed"
        lines = [f"{len(self.relocations)} type relocation(s) proposed:"]
        for r in self.relocations:
            lines.append(f"  {r}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Error parsing
# ---------------------------------------------------------------------------

_E0116_RE = re.compile(r"error\[E0116\]")

_FILE_LOCATION_RE = re.compile(
    r"-->\s+([^\s:]+):(\d+):(\d+)",
)


@dataclass
class OrphanError:
    type_name: str
    impl_file: str  # the file that tried to impl the type
    impl_crate: str


def parse_orphan_errors(stderr: str, workspace_dir: Path) -> list[OrphanError]:
    """Extract E0116 orphan-rule errors from cargo check stderr."""
    errors: list[OrphanError] = []
    lines = stderr.splitlines()
    i = 0
    _IMPL_LINE = re.compile(r"impl\s+(?:<[^>]+>\s+)?(\w+)")
    while i < len(lines):
        if _E0116_RE.search(lines[i]):
            # Scan ahead for --> file:line:col and `impl TypeName`
            impl_file = ""
            impl_crate = ""
            type_name = ""
            for j in range(i, min(i + 10, len(lines))):
                fm = _FILE_LOCATION_RE.search(lines[j])
                if fm and not impl_file:
                    impl_file = fm.group(1)
                    try:
                        rel = Path(impl_file).resolve().relative_to(workspace_dir.resolve())
                        impl_crate = rel.parts[0] if rel.parts else ""
                    except ValueError:
                        parts = Path(impl_file).parts
                        impl_crate = parts[0] if parts else ""
                tm = _IMPL_LINE.search(lines[j])
                if tm and not type_name:
                    type_name = tm.group(1)
            if type_name:
                errors.append(OrphanError(
                    type_name=type_name,
                    impl_file=impl_file,
                    impl_crate=impl_crate,
                ))
        i += 1
    return errors


# ---------------------------------------------------------------------------
# Type-definition locator
# ---------------------------------------------------------------------------

def find_type_definition(
    type_name: str,
    workspace_dir: Path,
) -> tuple[str, str] | None:
    """Find where a type is defined. Returns (crate_name, file_path) or None."""
    pattern = re.compile(
        rf"^\s*pub\s+(?:struct|enum|type|union)\s+{re.escape(type_name)}\b",
        re.MULTILINE,
    )
    for crate_dir in sorted(workspace_dir.iterdir()):
        if not crate_dir.is_dir() or crate_dir.name == "target":
            continue
        src = crate_dir / "src"
        if not src.exists():
            continue
        for rs in src.rglob("*.rs"):
            text = rs.read_text(encoding="utf-8", errors="replace")
            if pattern.search(text):
                return crate_dir.name, str(rs)
    return None


# ---------------------------------------------------------------------------
# Proposal generator
# ---------------------------------------------------------------------------

def propose_refactoring(
    stderr: str,
    workspace_dir: Path,
) -> RefactorProposal:
    """Analyze cargo check stderr for E0116 errors, propose type relocations."""
    proposal = RefactorProposal()
    errors = parse_orphan_errors(stderr, workspace_dir)

    for err in errors:
        defn = find_type_definition(err.type_name, workspace_dir)
        if defn is None:
            continue
        from_crate, _ = defn
        to_crate = err.impl_crate
        if from_crate == to_crate:
            continue  # Same crate — probably a different error
        proposal.relocations.append(TypeRelocation(
            type_name=err.type_name,
            from_crate=from_crate,
            to_crate=to_crate,
            reason=(
                f"E0116: {to_crate} tries to impl {err.type_name} "
                f"but it's defined in {from_crate}"
            ),
        ))

    return proposal


def apply_relocation(
    relocation: TypeRelocation,
    workspace_dir: Path,
) -> bool:
    """Move a type definition from one crate to another.

    Conservative: moves the `pub struct/enum/type` block and adds a
    re-export in the original location. Returns True on success.
    """
    defn = find_type_definition(relocation.type_name, workspace_dir)
    if defn is None:
        return False
    from_crate, from_file = defn
    from_path = Path(from_file)
    to_dir = workspace_dir / relocation.to_crate / "src"
    if not to_dir.exists():
        return False

    # Extract the type block from the source file
    text = from_path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"((?:#\[[^\]]*\]\s*\n)*"
        rf"pub\s+(?:struct|enum|type|union)\s+{re.escape(relocation.type_name)}\b"
        rf"[^{{;]*(?:\{{[^}}]*\}}|;))",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(text)
    if not m:
        return False

    type_block = m.group(0)

    # Remove from source, add re-export
    new_from = text[:m.start()] + text[m.end():]
    # Add `pub use <to_crate>::<type_name>;` if the from_crate depends on to_crate
    reexport = f"pub use {relocation.to_crate.replace('-', '_')}::{relocation.type_name};\n"
    if reexport not in new_from:
        new_from = reexport + new_from
    from_path.write_text(new_from, encoding="utf-8")

    # Append to the destination crate's lib.rs
    to_lib = to_dir / "lib.rs"
    if to_lib.exists():
        existing = to_lib.read_text(encoding="utf-8")
    else:
        existing = ""
    if relocation.type_name not in existing:
        to_lib.write_text(existing + "\n" + type_block + "\n", encoding="utf-8")

    return True
