"""Post-extraction spec normalization.

The LLM extractor sometimes produces parameter signatures that are
technically valid Rust types but semantically wrong for the C API
contract. Examples seen in zlib:

  - `dest: Vec<u8>` with direction=inout  →  should be `&mut [u8]`
    (C's `unsigned char *dest` is a caller-owned buffer, not an owned vec).
  - `destLen: u64` with direction=inout   →  should be `&mut usize`
    (C's `uLong *destLen` is an output-length pointer).
  - `destLen: Option<usize>` with direction=inout  →  `&mut usize`.

These errors cascade: the skeleton emits the wrong signature, the LLM
then writes code against the wrong signature, and the wrappers around
compress/uncompress all fail to compile in a chain.

This module rewrites specs in-place before they reach the skeleton
generator. Rules are conservative — we only rewrite cases where the
mismatch is unambiguous from the direction annotation.
"""

from __future__ import annotations

import re

from alchemist.extractor.schemas import (
    AlgorithmSpec,
    ModuleSpec,
    ParamDirection,
    Parameter,
)


# Names that scream "I'm an output length pointer", regardless of
# declared rust_type. Used to catch `destLen: u64` drift.
_LENGTH_PTR_NAME_RE = re.compile(
    r"^(?:"
    r"dest_?[Ll]en|destLen|out_?len|out_?_len|"
    r"total_out|have|avail_out"
    r")$"
)


def normalize_spec(spec: AlgorithmSpec) -> tuple[AlgorithmSpec, list[str]]:
    """Rewrite a single algorithm spec's parameters. Returns (new, notes)."""
    notes: list[str] = []
    new_inputs: list[Parameter] = []
    for p in spec.inputs or []:
        t = (p.rust_type or "").strip()
        d = p.direction

        # Case 1: Vec<u8> with inout/output — should be &mut [u8].
        if d in (ParamDirection.inout, ParamDirection.output) and \
                re.fullmatch(r"Vec\s*<\s*u8\s*>", t):
            new_t = "&mut [u8]"
            notes.append(f"{spec.name}::{p.name}: Vec<u8> {d.value} → {new_t}")
            new_inputs.append(p.model_copy(update={"rust_type": new_t}))
            continue

        # Case 2: Vec<u8> with input — should be &[u8].
        if d == ParamDirection.input and re.fullmatch(r"Vec\s*<\s*u8\s*>", t):
            new_t = "&[u8]"
            notes.append(f"{spec.name}::{p.name}: Vec<u8> input → {new_t}")
            new_inputs.append(p.model_copy(update={"rust_type": new_t}))
            continue

        # Case 3: length-pointer pattern: names like destLen/dest_len with
        # a numeric type and inout direction → &mut usize.
        if d == ParamDirection.inout and _LENGTH_PTR_NAME_RE.match(p.name) and \
                t in ("u32", "u64", "usize", "i32", "i64", "Option<usize>"):
            new_t = "&mut usize"
            notes.append(f"{spec.name}::{p.name}: {t} inout → {new_t}")
            new_inputs.append(p.model_copy(update={"rust_type": new_t}))
            continue

        # Case 4: length pointer with output direction.
        if d == ParamDirection.output and _LENGTH_PTR_NAME_RE.match(p.name) and \
                t in ("u32", "u64", "usize", "i32", "i64"):
            new_t = "&mut usize"
            notes.append(f"{spec.name}::{p.name}: {t} output → {new_t}")
            new_inputs.append(p.model_copy(update={"rust_type": new_t}))
            continue

        # Case 5: stale length fields typed as u64 where usize is idiomatic
        # and the function name is a compressed-payload wrapper. Non-inout
        # u64 lengths (plain inputs) are left alone — they may be intentional.
        # Skipped to stay conservative.

        new_inputs.append(p)

    # Return type: Result<u64, _> where destLen was u64 → Result<usize, _>.
    # Same reasoning: zlib-style APIs use size_t; u64 is a stale artifact.
    ret = spec.return_type or ""
    new_ret = ret
    if re.search(r"\bResult\s*<\s*u64\b", ret):
        # Only rewrite if the function has an inout length param we just normalized.
        if any(p.rust_type == "&mut usize" for p in new_inputs):
            new_ret = re.sub(r"\bResult\s*<\s*u64\b", "Result<usize", ret)
            notes.append(f"{spec.name}: return Result<u64,_> → Result<usize,_>")

    updates: dict = {"inputs": new_inputs}
    if new_ret != ret:
        updates["return_type"] = new_ret
    if updates:
        return spec.model_copy(update=updates), notes
    return spec, notes


def normalize_module(mod: ModuleSpec) -> tuple[ModuleSpec, list[str]]:
    """Normalize every algorithm in a module. Returns (new_module, notes)."""
    notes: list[str] = []
    new_algs: list[AlgorithmSpec] = []
    for a in mod.algorithms or []:
        new_a, alg_notes = normalize_spec(a)
        new_algs.append(new_a)
        notes.extend(alg_notes)
    if not notes:
        return mod, []
    return mod.model_copy(update={"algorithms": new_algs}), notes


def normalize_all(modules: list[ModuleSpec]) -> tuple[list[ModuleSpec], list[str]]:
    """Normalize a whole spec set. Returns (new_modules, all_notes)."""
    notes: list[str] = []
    new_mods: list[ModuleSpec] = []
    for m in modules:
        new_m, mod_notes = normalize_module(m)
        new_mods.append(new_m)
        notes.extend(mod_notes)
    return new_mods, notes
