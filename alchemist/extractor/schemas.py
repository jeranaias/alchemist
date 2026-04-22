"""Pydantic schemas for extracted algorithm specifications.

These schemas define the "bridge" between C code and Rust implementation.
The LLM extracts algorithm specs from C, and the implementer generates
Rust code from these specs — never directly from C.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ParamDirection(str, Enum):
    input = "input"
    output = "output"
    inout = "inout"


class Parameter(BaseModel):
    """A function/algorithm parameter."""
    name: str
    rust_type: str = Field(description="Idiomatic Rust type (e.g., &[u8], u32, &mut Vec<u8>)")
    description: str
    direction: ParamDirection = ParamDirection.input
    constraints: str = Field(default="", description="Value constraints (e.g., 'must be > 0', 'length <= 32768')")


class StateVariable(BaseModel):
    """A piece of mutable state maintained by the algorithm."""
    name: str
    rust_type: str
    description: str
    initial_value: str = Field(default="", description="Initial value expression in Rust")
    ownership: str = Field(default="owned", description="owned | borrowed | shared (Arc/Rc)")


class Invariant(BaseModel):
    """A property that must always hold."""
    description: str
    expression: str = Field(default="", description="Rust-like boolean expression if expressible")
    category: Literal["safety", "correctness", "performance"] = "correctness"


class ErrorCondition(BaseModel):
    """An error that the algorithm can produce."""
    name: str
    description: str
    rust_type: str = Field(default="", description="Suggested Rust error variant")


class TestVector(BaseModel):
    """A known input/output pair for verification."""
    __test__ = False  # not a pytest test class

    description: str
    inputs: dict[str, str] = Field(description="Parameter name -> value as Rust literal")
    expected_output: str
    tolerance: str = Field(default="exact", description="'exact' or epsilon like '1e-10'")
    source: str = Field(default="", description="Where this test case came from (RFC, datasheet, etc.)")


class AlgorithmSpec(BaseModel):
    """Complete specification of an extracted algorithm.

    This is the central data structure of Alchemist. It captures everything
    needed to reimplement an algorithm in Rust without looking at the C code.
    """
    name: str = Field(description="Algorithm name in snake_case (e.g., 'deflate_slow', 'adler32')")
    display_name: str = Field(description="Human-readable name (e.g., 'DEFLATE Slow Compression')")

    category: Literal[
        "compression", "decompression", "checksum", "hash",
        "cipher", "signature", "key_exchange",
        "filter", "controller", "transform",
        "data_structure", "protocol", "scheduler",
        "utility", "other",
    ]

    description: str = Field(description="1-3 sentence description of what the algorithm does")

    mathematical_description: str = Field(
        default="",
        description="Mathematical formulation if applicable (plain text, not LaTeX)"
    )

    # Interface
    inputs: list[Parameter] = Field(default_factory=list)
    outputs: list[Parameter] = Field(default_factory=list)
    return_type: str = Field(default="()", description="Rust return type")

    # State
    state: list[StateVariable] = Field(
        default_factory=list,
        description="Mutable state the algorithm maintains between calls"
    )

    # Correctness
    invariants: list[Invariant] = Field(default_factory=list)
    error_conditions: list[ErrorCondition] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)

    # Verification
    test_vectors: list[TestVector] = Field(default_factory=list)
    referenced_standards: list[str] = Field(
        default_factory=list,
        description="Standards this algorithm implements (e.g., 'RFC 1951', 'FIPS 197')"
    )

    # Implementation hints
    suggested_rust_traits: list[str] = Field(
        default_factory=list,
        description="Rust traits to implement (e.g., 'Read', 'Write', 'Iterator')"
    )
    no_std_compatible: bool = Field(
        default=True,
        description="Whether this can be implemented without std"
    )
    unsafe_required: bool = Field(
        default=False,
        description="Whether unsafe Rust is genuinely needed (e.g., hardware registers)"
    )
    unsafe_justification: str = Field(
        default="",
        description="If unsafe_required, explain why"
    )

    # Complexity
    time_complexity: str = Field(default="", description="Big-O time complexity")
    space_complexity: str = Field(default="", description="Big-O space complexity")

    # Provenance
    source_functions: list[str] = Field(
        default_factory=list,
        description="C function names this spec was extracted from"
    )
    source_files: list[str] = Field(default_factory=list)


class ConstantSpec(BaseModel):
    """A constant extracted from C source (#define, enum, static const).

    Phase 0 Bug — LLM cannot reliably reproduce long precomputed tables or
    named constants from C source. Extracting them deterministically and
    injecting into generated Rust removes a whole class of compile failures
    (undefined identifier, wrong value, truncated table).
    """
    name: str = Field(description="Constant name as emitted in Rust (e.g., 'CRC32_POLY')")
    rust_type: str = Field(description="Rust type annotation (e.g., 'u32', '[u32; 256]')")
    rust_expr: str = Field(description="RHS Rust expression (literal, array, etc.)")
    c_origin: str = Field(
        default="",
        description="Original C text (e.g., '#define NMAX 5552'). For audit.",
    )
    c_file: str = Field(default="", description="Source file path, for provenance")
    c_line: int = Field(default=0, description="Source line number")


class ModuleSpec(BaseModel):
    """Specification for a complete module (group of related algorithms)."""
    name: str
    display_name: str
    description: str
    algorithms: list[AlgorithmSpec]
    shared_types: list[SharedType] = Field(default_factory=list)
    module_invariants: list[str] = Field(default_factory=list)
    constants: list[ConstantSpec] = Field(
        default_factory=list,
        description="Constants extracted from the module's C source.",
    )


class SharedType(BaseModel):
    """A type shared between algorithms in a module."""
    name: str
    rust_definition: str = Field(description="Rust type definition (struct, enum, etc.)")
    description: str
    fields: list[TypeField] = Field(default_factory=list)


class TypeField(BaseModel):
    """A field in a shared type."""
    name: str
    rust_type: str
    description: str


class FunctionSpec(BaseModel):
    """Lightweight spec for a single function — used for per-function extraction.

    Much smaller than AlgorithmSpec. Designed for fast, reliable extraction
    of individual functions. Multiple FunctionSpecs are aggregated into an
    AlgorithmSpec on our side.
    """
    name: str = Field(description="Function name (e.g., 'adler32')")
    purpose: str = Field(description="1-2 sentence description of what this function does")
    category: Literal[
        "compression", "decompression", "checksum", "hash",
        "cipher", "filter", "controller", "data_structure",
        "protocol", "utility", "other",
    ]
    inputs: list[Parameter] = Field(default_factory=list)
    return_type: str = Field(default="()", description="Rust return type")
    algorithm_notes: str = Field(
        default="",
        description="Mathematical or algorithmic notes (formulas, invariants, edge cases)"
    )
    rust_strategy: str = Field(
        default="",
        description="How to implement this in idiomatic Rust (traits, patterns, ownership)"
    )
    unsafe_required: bool = Field(default=False)
    referenced_standards: list[str] = Field(default_factory=list)
