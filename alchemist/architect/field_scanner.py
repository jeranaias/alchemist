"""Field schema pre-scanner.

Solves the field whack-a-mole problem: when shared types are first generated
with minimal fields, every dependent crate generation hits "no field X"
errors. The fix loop reactively adds fields one at a time.

This module pre-scans ALL specs to collect every field access on every
shared type, so the Implementer generates types with COMPLETE field sets
on first attempt.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from alchemist.architect.schemas import CrateArchitecture
from alchemist.extractor.schemas import AlgorithmSpec, ModuleSpec, Parameter, StateVariable


# Common Rust types we infer from variable names + context
TYPE_HINTS = {
    # Counters and sizes
    "size": "usize",
    "len": "usize",
    "length": "u32",
    "count": "u32",
    "total": "u64",
    "total_in": "u64",
    "total_out": "u64",
    # Bit/byte buffers
    "bits": "u32",
    "buffer": "alloc::vec::Vec<u8>",
    "window": "alloc::vec::Vec<u8>",
    "data": "alloc::vec::Vec<u8>",
    "next_in": "Option<alloc::vec::Vec<u8>>",
    "next_out": "Option<alloc::vec::Vec<u8>>",
    # State / mode
    "mode": "u32",
    "state": "u32",
    "flags": "i32",
    "wrap": "i32",
    # Booleans
    "fastest": "bool",
    "last": "bool",
    "havedict": "bool",
    "have_dict": "bool",
    "sane": "bool",
    "half": "bool",
    # Indexes / positions
    "strstart": "usize",
    "lookahead": "usize",
    "match_start": "usize",
    "block_start": "i64",
    "pos": "usize",
    "start": "usize",
    "head": "u32",
    "tail": "u32",
    "next": "u32",
    "back": "i32",
    # Hashes / tables
    "ins_h": "u32",
    "hash_size": "usize",
    "prev": "alloc::vec::Vec<usize>",
    "lens": "alloc::vec::Vec<u16>",
    "work": "alloc::vec::Vec<u16>",
    # Window parameters
    "w_size": "usize",
    "w_bits": "u32",
    "w_mask": "usize",
    "wsize": "u32",
    "wbits": "u32",
    "whave": "u32",
    "wnext": "u32",
    "window_bits": "u32",
    "window_size": "u32",
    # Adler/CRC checksums
    "adler": "u32",
    "check": "u32",
    "dmax": "u32",
    "dict_id": "u32",
    # Compression params
    "level": "i32",
    "strategy": "i32",
    "mem_level": "i32",
    "lenfix_width": "u32",
    "distfix_width": "u32",
    "lenbits": "u32",
    "distbits": "u32",
    # Misc
    "msg": "Option<&'static str>",
    "hold": "u32",
    "length_extra": "u32",
    "extra": "u32",
    "ncode": "u32",
    "nlen": "u32",
    "ndist": "u32",
    "was": "u32",
    "match_length": "u32",
    "distance": "u32",
    "offset": "u32",
    "codes": "u32",
    "pending": "alloc::vec::Vec<u8>",
}


@dataclass
class FieldSchema:
    """Complete field schema for a struct, derived from spec analysis."""
    type_name: str
    fields: dict[str, str] = field(default_factory=dict)  # name -> rust_type
    sources: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def add_field(self, name: str, rust_type: str, source: str) -> None:
        """Record a field access. Type inference happens later."""
        if name not in self.fields:
            self.fields[name] = rust_type
        elif self.fields[name] == "?" and rust_type != "?":
            # Upgrade unknown type if we now have a guess
            self.fields[name] = rust_type
        self.sources[name].add(source)


def scan_specs_for_fields(
    specs: list[ModuleSpec],
    architecture: CrateArchitecture | None = None,
) -> dict[str, FieldSchema]:
    """Scan all algorithm specs and infer fields needed on shared types.

    Strategy:
      1. Collect all StateVariables across specs — these are explicit fields
      2. Collect all Parameters whose rust_type references a shared type
      3. Cross-reference shared_types in module specs

    Returns dict mapping type_name → FieldSchema.
    """
    schemas: dict[str, FieldSchema] = {}

    # Pass 1: explicit shared types
    for module in specs:
        for st in module.shared_types:
            schema = schemas.setdefault(st.name, FieldSchema(type_name=st.name))
            for f in st.fields:
                schema.add_field(f.name, f.rust_type, source=f"shared_types[{module.name}]")

    # Pass 2: state variables on each algorithm
    for module in specs:
        for algo in module.algorithms:
            for sv in algo.state:
                # Convention: state vars become fields on the algorithm's "state" struct
                stype_name = _state_struct_name(algo.name)
                schema = schemas.setdefault(stype_name, FieldSchema(type_name=stype_name))
                schema.add_field(sv.name, sv.rust_type, source=f"state[{algo.name}]")

    return schemas


def scan_generated_code_for_fields(
    code_files: dict[str, str],
    type_names: Iterable[str],
) -> dict[str, FieldSchema]:
    """Scan generated Rust code for `state.X` style field accesses.

    Use this AFTER a first generation pass to find fields the LLM
    referenced but didn't define. Returns inferred schemas to add.
    """
    schemas: dict[str, FieldSchema] = {}

    # Match `<varname>.<field>` where varname suggests one of the target types
    # We use a simple approach: scan for `.X` field accesses on common variable names
    # and bucket them by the inferred type
    field_pattern = re.compile(r"\b(state|self|stream|strm|s)\b(?:\.as_mut\(\)\.unwrap\(\))?\.([a-z_][a-z0-9_]*)")

    # Method names to exclude (these aren't fields)
    method_blacklist = {
        "as_mut", "as_ref", "is_none", "is_some", "unwrap", "clone",
        "default", "new", "to_string", "len", "is_empty", "iter",
        "push", "pop", "extend_from_slice", "to_vec", "fmt", "into",
        "from", "borrow", "borrow_mut", "as_str", "as_bytes",
        "next", "rev", "min", "max", "contains", "starts_with",
    }

    for filepath, content in code_files.items():
        for m in field_pattern.finditer(content):
            field_name = m.group(2)
            if field_name in method_blacklist:
                continue
            # Default-bucket all unknown varname fields under a "state" target type
            target_type = "state"  # placeholder — caller maps this
            schema = schemas.setdefault(target_type, FieldSchema(type_name=target_type))
            inferred = TYPE_HINTS.get(field_name, "u32")  # default to u32 for unknown
            schema.add_field(field_name, inferred, source=filepath)

    return schemas


def render_struct(schema: FieldSchema, derives: list[str] | None = None) -> str:
    """Render a FieldSchema as a Rust struct definition."""
    derives = derives or ["Debug", "Default"]
    lines = []
    lines.append(f"#[derive({', '.join(derives)})]")
    lines.append(f"pub struct {schema.type_name} {{")
    for fname in sorted(schema.fields):
        ftype = schema.fields[fname]
        lines.append(f"    pub {fname}: {ftype},")
    lines.append("}")
    return "\n".join(lines)


def merge_schemas(*schemas_list: dict[str, FieldSchema]) -> dict[str, FieldSchema]:
    """Merge multiple schema dicts. Later schemas additive on earlier."""
    merged: dict[str, FieldSchema] = {}
    for schemas in schemas_list:
        for name, schema in schemas.items():
            if name not in merged:
                merged[name] = FieldSchema(type_name=name)
            for fname, ftype in schema.fields.items():
                merged[name].add_field(fname, ftype, source="merged")
    return merged


def _state_struct_name(algo_name: str) -> str:
    """Convention: algorithm 'inflate' has state struct 'inflate_state'."""
    return f"{algo_name}_state"


# ─── Tests baked in (run as: python -m alchemist.architect.field_scanner) ──

if __name__ == "__main__":
    # Quick smoke test
    from alchemist.extractor.schemas import (
        AlgorithmSpec, ModuleSpec, StateVariable, SharedType, TypeField, Parameter
    )

    spec = ModuleSpec(
        name="inflate",
        display_name="Inflate",
        description="DEFLATE decompression",
        algorithms=[
            AlgorithmSpec(
                name="inflate",
                display_name="Inflate",
                category="decompression",
                description="Inflates DEFLATE",
                state=[
                    StateVariable(name="window", rust_type="alloc::vec::Vec<u8>", description="sliding window"),
                    StateVariable(name="hold", rust_type="u32", description="bit accumulator"),
                ],
            ),
        ],
        shared_types=[
            SharedType(
                name="z_stream",
                rust_definition="struct z_stream { ... }",
                description="zlib stream state",
                fields=[
                    TypeField(name="next_in", rust_type="Option<Vec<u8>>", description="input buffer"),
                    TypeField(name="adler", rust_type="u32", description="checksum"),
                ],
            ),
        ],
    )

    schemas = scan_specs_for_fields([spec])
    for name, schema in schemas.items():
        print(f"\n=== {name} ===")
        print(render_struct(schema))
