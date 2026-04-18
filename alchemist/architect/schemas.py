"""Pydantic schemas for Rust architecture design."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CrateSpec(BaseModel):
    """Specification for a single Rust crate in the workspace."""
    name: str = Field(description="Crate name (e.g., 'zlib-deflate', 'zlib-checksum')")
    description: str
    is_no_std: bool = Field(default=True, description="Whether this crate should be no_std")
    dependencies: list[str] = Field(default_factory=list, description="Other workspace crate names this depends on")
    external_deps: list[ExternalDep] = Field(default_factory=list)
    modules: list[str] = Field(description="Module spec names this crate implements")
    public_api: list[str] = Field(default_factory=list, description="Public function/type names")


class ExternalDep(BaseModel):
    """An external crate dependency."""
    name: str
    version: str = Field(default="*")
    features: list[str] = Field(default_factory=list)
    optional: bool = False


class TraitSpec(BaseModel):
    """A Rust trait definition for module interfaces."""
    name: str
    description: str
    methods: list[TraitMethod]
    supertraits: list[str] = Field(default_factory=list)
    crate: str = Field(description="Which crate this trait lives in")
    implementors: list[str] = Field(
        default_factory=list,
        description="Struct names that implement this trait (e.g., ['Adler32', 'Crc32'] both impl Checksum)",
    )


class StateWrapperSpec(BaseModel):
    """A public struct that encapsulates a raw internal state type.

    Example: `Deflater` wraps `DeflateState` so callers never see the
    40-field struct directly. All DeflateState field access goes through
    methods on Deflater. This is Phase 0.5's encapsulation requirement.
    """
    public_name: str = Field(description="The encapsulating struct (e.g., Deflater)")
    inner_state: str = Field(description="The raw state type it wraps (e.g., DeflateState)")
    crate: str
    description: str = ""
    methods: list[str] = Field(
        default_factory=list,
        description="Rust signatures for public methods (e.g., 'pub fn write(&mut self, input: &[u8]) -> Result<usize, Error>')",
    )


class BuilderSpec(BaseModel):
    """A builder-pattern wrapper for parameterized init functions.

    Example: `DeflaterBuilder::new().level(6).window_bits(15).build()`
    replaces zlib's `deflateInit2_(strm, 6, Z_DEFLATED, 15, ...)`.
    """
    builder_name: str = Field(description="The builder type name (e.g., DeflaterBuilder)")
    built_type: str = Field(description="The type returned by .build() (e.g., Deflater)")
    crate: str
    parameters: list[str] = Field(
        default_factory=list,
        description="Fluent method signatures (e.g., 'pub fn level(self, level: i32) -> Self')",
    )
    build_signature: str = Field(
        default="pub fn build(self) -> Result<Self::Output, BuildError>",
        description="The build() method signature",
    )


class TraitMethod(BaseModel):
    """A method in a trait definition."""
    name: str
    signature: str = Field(description="Full Rust signature (e.g., 'fn compress(&mut self, input: &[u8]) -> Result<Vec<u8>, Error>')")
    description: str
    has_default: bool = False


class OwnershipDecision(BaseModel):
    """Documents an ownership/lifetime design decision."""
    c_pattern: str = Field(description="The C pattern being replaced (e.g., 'global mutable crc_table')")
    rust_pattern: str = Field(description="The Rust replacement (e.g., 'const CRC_TABLE: [u32; 256] computed at compile time')")
    rationale: str


class ErrorType(BaseModel):
    """An error type in the error hierarchy."""
    name: str
    variants: list[ErrorVariant]
    crate: str


class ErrorVariant(BaseModel):
    """A variant of an error enum."""
    name: str
    description: str
    fields: list[str] = Field(default_factory=list, description="Rust field types if any")


class CargoFeature(BaseModel):
    """A Cargo feature flag."""
    name: str
    description: str
    default: bool = False
    enables: list[str] = Field(default_factory=list, description="Other features this enables")


class CrateArchitecture(BaseModel):
    """Complete Rust workspace architecture."""
    workspace_name: str
    description: str

    crates: list[CrateSpec]
    dependency_graph: dict[str, list[str]] = Field(
        default_factory=dict,
        description="crate_name -> [dependency_crate_names]"
    )

    traits: list[TraitSpec] = Field(default_factory=list)
    error_types: list[ErrorType] = Field(default_factory=list)
    ownership_decisions: list[OwnershipDecision] = Field(default_factory=list)
    features: list[CargoFeature] = Field(default_factory=list)
    state_wrappers: list[StateWrapperSpec] = Field(
        default_factory=list,
        description="Public encapsulations of raw internal state types",
    )
    builders: list[BuilderSpec] = Field(
        default_factory=list,
        description="Builder-pattern wrappers for parameterized init",
    )

    unsafe_boundaries: list[str] = Field(
        default_factory=list,
        description="Where and why unsafe code is needed (target: empty)"
    )
