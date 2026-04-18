"""Post-architect trait extractor.

Scans algorithm specs across the workspace and proposes TraitSpec entries
for every family of functions with compatible signatures. Runs as a
post-processing step between the architect's LLM output and the Stage 4
skeleton emission.

The architect's prompt already asks for traits, but the LLM often misses
obvious groupings (Adler32 + CRC32 → Checksum). This module is the
deterministic backstop that fills the gap, guaranteeing that compatible-
signature families always get a shared trait.

Heuristic: two functions share a "shape" if they have the same return
type and the same sequence of parameter Rust types. Functions with
identical shapes in the same domain category form a trait family.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from alchemist.architect.schemas import (
    CrateArchitecture,
    TraitSpec,
    TraitMethod,
)
from alchemist.extractor.schemas import AlgorithmSpec, ModuleSpec


@dataclass(frozen=True)
class _Shape:
    category: str
    param_types: tuple[str, ...]
    return_type: str

    def trait_name(self) -> str:
        """Derive a trait name from the category."""
        name_by_cat = {
            "checksum": "Checksum",
            "hash": "Hasher",
            "cipher": "Cipher",
            "signature": "Signer",
            "key_exchange": "KeyExchange",
            "compression": "Compressor",
            "decompression": "Decompressor",
            "filter": "Filter",
        }
        return name_by_cat.get(self.category, self.category.title())


def _normalize_type(t: str) -> str:
    """Collapse whitespace and standardize a Rust type for shape comparison."""
    return re.sub(r"\s+", "", t).replace("&mut", "&mut ").replace("&", "&").strip()


def _shape_for(alg: AlgorithmSpec) -> _Shape | None:
    """Compute the shape of an algorithm spec."""
    if not alg.inputs:
        return None
    return _Shape(
        category=alg.category or "",
        param_types=tuple(_normalize_type(p.rust_type) for p in alg.inputs),
        return_type=_normalize_type(alg.return_type or "()"),
    )


def extract_traits(
    specs: list[ModuleSpec],
    arch: CrateArchitecture,
    *,
    min_implementors: int = 2,
) -> list[TraitSpec]:
    """Propose traits for every compatible-signature family with enough members.

    Only emits traits that don't already exist in arch.traits (avoid
    duplicating what the architect already produced).

    Args:
        specs: all module specs in the workspace.
        arch: the architect's output (used to read existing traits + crate map).
        min_implementors: family size threshold (2+ implementors → trait).

    Returns new TraitSpec entries to ADD to arch.traits. Does not mutate arch.
    """
    existing_names = {t.name for t in arch.traits}

    # Group algorithms by shape
    by_shape: dict[_Shape, list[AlgorithmSpec]] = defaultdict(list)
    alg_to_crate: dict[str, str] = {}
    for mod in specs:
        # Find which crate owns this module
        owning_crate = next(
            (c.name for c in arch.crates if mod.name in set(c.modules)),
            arch.crates[0].name if arch.crates else "unknown",
        )
        for alg in mod.algorithms or []:
            shape = _shape_for(alg)
            if shape is None:
                continue
            by_shape[shape].append(alg)
            alg_to_crate[alg.name] = owning_crate

    new_traits: list[TraitSpec] = []
    for shape, members in by_shape.items():
        if len(members) < min_implementors:
            continue
        trait_name = shape.trait_name()
        if trait_name in existing_names:
            continue
        # Pick the owning crate as the one where most implementors live.
        crate_votes: dict[str, int] = defaultdict(int)
        for a in members:
            crate_votes[alg_to_crate.get(a.name, "unknown")] += 1
        owning_crate = max(crate_votes.items(), key=lambda x: x[1])[0]
        # Synthesize a single trait method based on the shared shape.
        method = TraitMethod(
            name="compute",
            signature=f"fn compute({_shape_as_params(shape)}) -> {shape.return_type}",
            description=(
                f"Computes the {shape.category} value for the given input. "
                f"Shared across {len(members)} implementors."
            ),
        )
        new_traits.append(TraitSpec(
            name=trait_name,
            description=(
                f"Common interface for {shape.category} functions with matching "
                f"signature shape."
            ),
            methods=[method],
            crate=owning_crate,
            implementors=sorted(a.name for a in members),
        ))
    return new_traits


def _shape_as_params(shape: _Shape) -> str:
    """Emit a comma-separated param list for a trait method signature."""
    out = []
    for i, t in enumerate(shape.param_types):
        # self-like first-borrow params: promote to &self / &mut self.
        if i == 0 and t.startswith("&mut "):
            out.append("&mut self")
            continue
        if i == 0 and t.startswith("&") and not t.startswith("&mut"):
            out.append("&self")
            continue
        out.append(f"arg{i}: {t}")
    return ", ".join(out)
