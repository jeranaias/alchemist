"""Global state rewriter — deterministic Rust strategy for every C global.

C codebases are full of `static` globals: lookup tables, lazy-init
singletons, mutable counters. The LLM is bad at deciding which pattern
to use for each — it either makes everything `static mut` (unsafe) or
everything `const` (wrong when the value can't be computed at compile
time).

This module classifies each C global and emits a deterministic Rust
strategy before the LLM ever sees it. The architect embeds the decision
in the CrateArchitecture's `ownership_decisions` list, and the skeleton
generator uses it when building type definitions.

Classification rules (priority order):

  1. **const-computable table** — value depends only on loop indices and
     literal constants (CRC tables, S-boxes). → `const fn` or `const`
     array computed at compile time.

  2. **read-only after init** — initialized once (often via an `_init()`
     function), never mutated after that. → `std::sync::LazyLock` /
     `once_cell::sync::Lazy` (or `OnceCell` on no_std with spin).

  3. **thread-local counter** — a mutable integer bumped per call but
     not shared across threads. → owned field on a state struct passed
     by `&mut`.

  4. **truly shared mutable** — must be visible to multiple callers AND
     mutated. → `Arc<Mutex<T>>` or `AtomicU32` depending on type.

  5. **extern/opaque** — declared `extern` or pointer-to-incomplete-type.
     → opaque newtype wrapping `*mut c_void` behind a safe API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class GlobalStrategy(str, Enum):
    const_table = "const_table"
    lazy_init = "lazy_init"
    struct_field = "struct_field"
    atomic = "atomic"
    arc_mutex = "arc_mutex"
    opaque_extern = "opaque_extern"


@dataclass
class GlobalClassification:
    """Classification of a single C global variable."""
    name: str
    c_type: str
    strategy: GlobalStrategy
    rust_type: str
    rust_init: str
    rationale: str

    def as_ownership_decision(self) -> dict:
        """Produce a dict compatible with CrateArchitecture.ownership_decisions."""
        return {
            "c_pattern": f"static {self.c_type} {self.name}",
            "rust_pattern": f"{self.rust_type} ({self.strategy.value})",
            "rationale": self.rationale,
        }


# ---------------------------------------------------------------------------
# Heuristic classifiers
# ---------------------------------------------------------------------------

_INTEGRAL_TYPES = {
    "int", "unsigned", "unsigned int", "long", "unsigned long",
    "short", "unsigned short", "char", "unsigned char",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int8_t", "int16_t", "int32_t", "int64_t",
    "size_t", "ssize_t", "uintptr_t",
}

_ARRAY_RE = re.compile(r"^(.*?)\s*\[\s*(\d+)\s*\]$")


def _is_const_computable(name: str, c_type: str, init_code: str) -> bool:
    """True if the value can be computed purely from literals + loop indices.

    Heuristic: init code contains only:
      * numeric literals (0x..., decimal)
      * loop counters (for, while with <256 / <8 style bounds)
      * bitwise ops (^, |, &, >>, <<)
      * the variable's own name (self-referential table build)
    """
    if not init_code:
        return False
    # Presence of function calls (other than the table-init itself) → not const
    # Quick heuristic: if there's a `(` preceded by a word char, might be a call
    calls = re.findall(r"\b([a-zA-Z_]\w*)\s*\(", init_code)
    allowed_calls = {name, "min", "max"}
    if any(c not in allowed_calls for c in calls):
        return False
    # Must reference only literals and indices
    return True


def _is_array(c_type: str) -> tuple[bool, str, int]:
    """Returns (is_array, base_type, size)."""
    m = _ARRAY_RE.match(c_type.strip())
    if m:
        return True, m.group(1).strip(), int(m.group(2))
    return False, c_type, 0


def classify_global(
    name: str,
    c_type: str,
    is_static: bool = True,
    is_const: bool = False,
    is_extern: bool = False,
    init_code: str = "",
    has_init_function: bool = False,
    is_mutated_after_init: bool = False,
) -> GlobalClassification:
    """Classify a single C global and pick a Rust strategy."""

    is_arr, base_type, arr_size = _is_array(c_type)

    # Rule 5: extern / opaque
    if is_extern or "void" in c_type:
        return GlobalClassification(
            name=name, c_type=c_type,
            strategy=GlobalStrategy.opaque_extern,
            rust_type="*mut c_void /* behind safe wrapper */",
            rust_init="std::ptr::null_mut()",
            rationale="extern or opaque type — wrap in safe newtype",
        )

    # Rule 1: const-computable table
    if is_arr and not is_mutated_after_init:
        if is_const or _is_const_computable(name, c_type, init_code):
            rust_elem = _c_to_rust_scalar(base_type)
            return GlobalClassification(
                name=name, c_type=c_type,
                strategy=GlobalStrategy.const_table,
                rust_type=f"[{rust_elem}; {arr_size}]",
                rust_init=f"const {name.upper()}: [{rust_elem}; {arr_size}] = {{ /* computed at compile time */ }};",
                rationale=(
                    "Array initialized from literals/indices only — compute at compile time "
                    "via const fn or const block. Zero runtime cost, zero unsafe."
                ),
            )

    # Rule 2: read-only after init (lazy)
    if has_init_function and not is_mutated_after_init:
        rust_inner = _c_to_rust_type(c_type)
        return GlobalClassification(
            name=name, c_type=c_type,
            strategy=GlobalStrategy.lazy_init,
            rust_type=f"std::sync::LazyLock<{rust_inner}>",
            rust_init=f"static {name.upper()}: LazyLock<{rust_inner}> = LazyLock::new(|| init_{name}());",
            rationale=(
                "Initialized once via init function, never mutated after. "
                "LazyLock ensures thread-safe one-time initialization."
            ),
        )

    # Rule 3: thread-local / struct field / atomic
    if is_mutated_after_init and not is_arr and base_type in _INTEGRAL_TYPES:
        rust_scalar = _c_to_rust_scalar(base_type)
        return GlobalClassification(
            name=name, c_type=c_type,
            strategy=GlobalStrategy.atomic,
            rust_type=f"AtomicU{_bits(rust_scalar)}",
            rust_init=f"AtomicU{_bits(rust_scalar)}::new(0)",
            rationale="Small mutable integer — use atomic for thread safety without locks.",
        )

    # Rule 4: shared mutable (fallback)
    if is_mutated_after_init:
        rust_inner = _c_to_rust_type(c_type)
        return GlobalClassification(
            name=name, c_type=c_type,
            strategy=GlobalStrategy.arc_mutex,
            rust_type=f"Arc<Mutex<{rust_inner}>>",
            rust_init=f"Arc::new(Mutex::new(Default::default()))",
            rationale="Shared mutable state — Arc<Mutex<T>> for safe concurrent access.",
        )

    # Default: const / struct field for non-mutated scalars
    rust_t = _c_to_rust_type(c_type)
    return GlobalClassification(
        name=name, c_type=c_type,
        strategy=GlobalStrategy.struct_field if is_mutated_after_init else GlobalStrategy.const_table,
        rust_type=rust_t,
        rust_init=f"const {name.upper()}: {rust_t} = 0;",
        rationale="Simple scalar — const if never mutated, struct field otherwise.",
    )


# ---------------------------------------------------------------------------
# Type mapping helpers
# ---------------------------------------------------------------------------

_SCALAR_MAP = {
    "int": "i32", "unsigned": "u32", "unsigned int": "u32",
    "long": "i64", "unsigned long": "u64",
    "short": "i16", "unsigned short": "u16",
    "char": "i8", "unsigned char": "u8",
    "uint8_t": "u8", "uint16_t": "u16", "uint32_t": "u32", "uint64_t": "u64",
    "int8_t": "i8", "int16_t": "i16", "int32_t": "i32", "int64_t": "i64",
    "size_t": "usize", "ssize_t": "isize",
    "float": "f32", "double": "f64",
}


def _c_to_rust_scalar(base: str) -> str:
    return _SCALAR_MAP.get(base.strip(), "u32")


def _c_to_rust_type(c_type: str) -> str:
    is_arr, base, size = _is_array(c_type)
    if is_arr:
        return f"[{_c_to_rust_scalar(base)}; {size}]"
    return _c_to_rust_scalar(c_type)


def _is_small_counter(c_type: str) -> bool:
    return not _is_array(c_type)[0]


def _bits(rust_scalar: str) -> int:
    m = re.search(r"(\d+)$", rust_scalar)
    return int(m.group(1)) if m else 32


# ---------------------------------------------------------------------------
# Batch classifier
# ---------------------------------------------------------------------------

def classify_globals(
    globals_list: Iterable[dict],
) -> list[GlobalClassification]:
    """Classify a list of globals from the Stage 1 analysis output.

    Each dict should have at minimum: name, type. Optional: is_static,
    is_const, is_extern, init_code, has_init_function, is_mutated_after_init.
    """
    out = []
    for g in globals_list:
        out.append(classify_global(
            name=g.get("name", ""),
            c_type=g.get("type", "int"),
            is_static=g.get("is_static", True),
            is_const=g.get("is_const", False),
            is_extern=g.get("is_extern", False),
            init_code=g.get("init_code", ""),
            has_init_function=g.get("has_init_function", False),
            is_mutated_after_init=g.get("is_mutated_after_init", False),
        ))
    return out
