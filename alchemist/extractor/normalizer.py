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

# Alias rewrites for known C-level type names the extractor leaks through.
# Extractor sometimes keeps the C typedef (z_stream) or picks the wrong
# state type (InflateState in a deflate-only function). Rewrite on sight.
_TYPE_ALIAS: dict[str, str] = {
    "z_stream": "DeflateStream",
    "z_streamp": "&mut DeflateStream",
    "Bytef": "u8",
    "uchf": "u8",
    "uInt": "u32",
    "uLong": "u64",
    "voidpf": "*mut u8",
    "voidp": "*mut u8",
    "ZlibStream": "DeflateStream",
}

# Functions whose state param is mis-typed. Maps (fn_name, param_name) →
# correct type. Populated from observed bugs; additive only.
_STATE_TYPE_CORRECTION: dict[tuple[str, str], str] = {
    ("compress_block", "state"): "&mut DeflateState",
    ("compress_block", "s"): "&mut DeflateState",
}


def _apply_type_aliases(rust_type: str) -> str:
    """Replace known C-level type names with their Rust equivalents."""
    out = rust_type
    for alias, rust in _TYPE_ALIAS.items():
        # Word-boundary replacement so `z_stream` matches but not `z_streamp_foo`.
        out = re.sub(rf"\b{re.escape(alias)}\b", rust, out)
    return out


def _infer_module_state_type(module_name: str) -> str | None:
    """Given a module name, return the canonical state type for its functions.

    Purely mechanical — no LLM involved. The C source's file organization is
    the authoritative signal: trees.c / deflate.c operate on deflate_state,
    inffast.c / inftrees.c / inflate.c operate on inflate_state. The LLM
    extractor sometimes mislabels individual functions (e.g., marking
    `_tr_align` as an inflate helper because its block-alignment purpose
    sounded like decoder prep). This corrector overrides those mistakes
    from the module's file name, which can't be wrong.
    """
    m = module_name.lower()
    if any(tok in m for tok in ("inffast", "inftrees", "inflate", "inflat")):
        return "InflateState"
    if any(tok in m for tok in ("trees", "deflate", "deflat")):
        return "DeflateState"
    return None


def _module_state_correction(
    spec: AlgorithmSpec, rust_type: str, module_state_type: str | None,
) -> str:
    """If the module's canonical state type conflicts with a state type in
    `rust_type`, flip it. Applies to `DeflateState`/`InflateState` only."""
    if not module_state_type:
        return rust_type
    if module_state_type == "DeflateState" and "InflateState" in rust_type:
        return rust_type.replace("InflateState", "DeflateState")
    if module_state_type == "InflateState" and "DeflateState" in rust_type:
        return rust_type.replace("DeflateState", "InflateState")
    return rust_type


def normalize_spec(
    spec: AlgorithmSpec, module_state_type: str | None = None,
) -> tuple[AlgorithmSpec, list[str]]:
    """Rewrite a single algorithm spec's parameters. Returns (new, notes)."""
    notes: list[str] = []
    new_inputs: list[Parameter] = []
    for p in spec.inputs or []:
        t_raw = (p.rust_type or "").strip()
        # Apply type-alias rewrites first so subsequent rules see real Rust types.
        t = _apply_type_aliases(t_raw)
        if t != t_raw:
            notes.append(f"{spec.name}::{p.name}: {t_raw} → {t} (alias)")
        # Module-scoped state-type correction (file-path-driven, authoritative
        # for Deflate/Inflate confusion).
        t_before_mod = t
        t = _module_state_correction(spec, t, module_state_type)
        if t != t_before_mod:
            notes.append(
                f"{spec.name}::{p.name}: {t_before_mod} → {t} (module state)"
            )
        # Function-specific state-type corrections.
        corrected = _STATE_TYPE_CORRECTION.get((spec.name, p.name))
        if corrected and t != corrected:
            notes.append(f"{spec.name}::{p.name}: {t} → {corrected} (state correction)")
            t = corrected
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

        # Persist any alias/correction rewrites even if none of the above
        # cases matched.
        if t != t_raw:
            new_inputs.append(p.model_copy(update={"rust_type": t}))
        else:
            new_inputs.append(p)

    # Return type: apply type aliases first (z_stream, uLong, etc.), then
    # the Result<u64,_> heuristic.
    orig_ret = spec.return_type or ""
    new_ret = _apply_type_aliases(orig_ret)
    if new_ret != orig_ret:
        notes.append(f"{spec.name}: return {orig_ret} → {new_ret} (alias)")
    new_ret_before_mod = new_ret
    new_ret = _module_state_correction(spec, new_ret, module_state_type)
    if new_ret != new_ret_before_mod:
        notes.append(
            f"{spec.name}: return {new_ret_before_mod} → {new_ret} (module state)"
        )
    if re.search(r"\bResult\s*<\s*u64\b", new_ret):
        # Only rewrite if the function has an inout length param we just normalized.
        if any(p.rust_type == "&mut usize" for p in new_inputs):
            prev = new_ret
            new_ret = re.sub(r"\bResult\s*<\s*u64\b", "Result<usize", new_ret)
            notes.append(f"{spec.name}: return {prev} → {new_ret}")

    updates: dict = {"inputs": new_inputs}
    if new_ret != orig_ret:
        updates["return_type"] = new_ret
    if updates:
        return spec.model_copy(update=updates), notes
    return spec, notes


def normalize_module(mod: ModuleSpec) -> tuple[ModuleSpec, list[str]]:
    """Normalize every algorithm in a module. Returns (new_module, notes)."""
    notes: list[str] = []
    new_algs: list[AlgorithmSpec] = []
    module_state_type = _infer_module_state_type(mod.name)
    for a in mod.algorithms or []:
        new_a, alg_notes = normalize_spec(a, module_state_type=module_state_type)
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
