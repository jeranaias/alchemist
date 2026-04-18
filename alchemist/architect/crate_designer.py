"""Stage 3: Design Rust crate architecture from extracted specs.

Takes module specs and designs a Cargo workspace with proper ownership
boundaries, trait interfaces, and dependency structure.
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

from alchemist.architect.schemas import CrateArchitecture
from alchemist.config import AlchemistConfig
from alchemist.extractor.schemas import ModuleSpec
from alchemist.llm.client import AlchemistLLM, CachedContext
from alchemist.llm.structured import pydantic_to_tool_schema

console = Console(force_terminal=True, legacy_windows=False)

ARCHITECT_SYSTEM_PROMPT = """\
You are Alchemist's Architecture Designer. Given algorithm specifications extracted \
from a C codebase, you design an idiomatic Rust crate workspace.

## Non-negotiable principles (NO "good enough" allowed)

1. **One concern per crate.** Each crate has a single clear purpose.

2. **Dependency order forms a DAG.** Leaf crates (types, error definitions, pure \
primitives) come first; complex crates depend on simpler ones.

3. **Single ownership at every boundary.** Each piece of data has exactly one owner. \
Shared state is either owned (struct field), borrowed (reference parameter), or \
transferred (move). NEVER use Arc<Mutex<>> unless the code legitimately runs across \
threads.

4. **Traits for every shared shape.** If two or more functions have compatible \
signatures (e.g., Adler32 and CRC32 both `fn(&[u8]) -> u32`), they MUST share a \
trait. For this codebase you MUST produce at least these traits when applicable: \
`Checksum`, `Hasher`, `Compressor`, `Decompressor`, `Cipher`, `KeyDerivation`, \
`SignatureScheme`. Record implementors in TraitSpec.implementors.

5. **Encapsulate raw state structs behind a public wrapper.** If the extractor \
produces a state struct with more than ~10 fields (e.g., DeflateState, InflateState, \
AesContext, Sha256Context), the architecture MUST define a public StateWrapperSpec \
that hides those fields. Callers interact through methods on the wrapper, NOT by \
poking struct fields. The raw state is `pub(crate)` or private — never `pub`. \
Produce one StateWrapperSpec per raw state struct.

6. **Builder pattern for every parameterized init.** Any C function whose name \
matches `*Init*` and takes more than one parameter MUST be expressed as a builder. \
Example: `deflateInit2_(strm, level, method, windowBits, memLevel, strategy)` becomes \
`DeflaterBuilder::new().level(6).method(Deflated).window_bits(15).mem_level(8).strategy(Default).build()`. \
Produce one BuilderSpec per such init function.

7. **Structured error hierarchy per crate.** Each crate has its own error enum \
(e.g., `DeflateError`, `InflateError`). A top-level `Error` that covers all of them \
can exist but MUST NOT be the catch-all for crate-local failures.

8. **no_std by default.** Only crates that need std (file I/O, system time, \
networking) opt in via a `std` feature.

9. **#![forbid(unsafe_code)] in every crate.** Record any genuine unsafe boundary \
in unsafe_boundaries (ideally empty). If unsafe is needed, it lives in one \
isolated unsafe-blessed crate the user can audit separately.

10. **Don't replicate C's API. Design a Rust API.** C's `z_stream` with raw \
pointers becomes a Rust struct implementing `Read`/`Write`. C's error codes become \
`Result<T, E>`. C's `memcpy(dst, src, n)` becomes `dst[..n].copy_from_slice(&src[..n])`.

## Self-check before responding

Before returning, verify your output:
  - Every raw state struct with >10 fields has a StateWrapperSpec (requirement 5)
  - Every `*Init*` function with >1 param has a BuilderSpec (requirement 6)
  - At least one TraitSpec exists per compatible-signature family (requirement 4)
  - Every crate has its own ErrorType (requirement 7)
  - Zero unsafe_boundaries unless literally unavoidable (requirement 9)

If any check fails, iterate before returning.

## Output

Return a complete CrateArchitecture with crates, traits, error_types, \
state_wrappers, builders, ownership_decisions, and features all populated.\
"""

DESIGN_PROMPT = """\
Design a Rust workspace architecture for the following algorithm specifications.

## Project: {project_name}
Source: {source_description}
Total modules: {module_count}
Total algorithms: {algorithm_count}

## Module Specifications

{module_specs_json}

## Instructions

Design a complete Rust workspace that:
1. Groups algorithms into crates by domain (one concern per crate)
2. Defines trait interfaces at crate boundaries
3. Specifies a proper error type hierarchy
4. Documents every ownership decision (what was global/shared in C, what it becomes in Rust)
5. Marks no_std compatibility per crate
6. Lists feature flags for optional functionality

Return the complete CrateArchitecture.\
"""


class CrateDesigner:
    """Design Rust crate architecture from module specs."""

    def __init__(self, config: AlchemistConfig | None = None):
        self.config = config or AlchemistConfig()
        self.llm = AlchemistLLM(self.config)

    def design(
        self,
        specs: list[ModuleSpec],
        project_name: str = "translated",
        source_description: str = "",
    ) -> CrateArchitecture:
        """Design crate architecture from module specs."""
        console.print(f"[cyan]Designing architecture for {len(specs)} modules...[/cyan]")

        # Build cached context
        cached = self.llm.create_cached_context(
            system_text=ARCHITECT_SYSTEM_PROMPT,
        )

        # Serialize specs for the prompt
        specs_json = json.dumps(
            [s.model_dump() for s in specs],
            indent=2,
        )

        total_algos = sum(len(s.algorithms) for s in specs)

        prompt = DESIGN_PROMPT.format(
            project_name=project_name,
            source_description=source_description,
            module_count=len(specs),
            algorithm_count=total_algos,
            module_specs_json=specs_json,
        )

        schema = pydantic_to_tool_schema(CrateArchitecture)
        response = self.llm.call_structured(
            messages=[{"role": "user", "content": prompt}],
            tool_name="crate_architecture",
            tool_schema=schema,
            cached_context=cached,
            max_tokens=16384,
        )

        if response.structured:
            try:
                arch = CrateArchitecture.model_validate(response.structured)
                self._print_architecture(arch)
                return arch
            except Exception as e:
                console.print(f"[red]Failed to parse architecture: {e}[/red]")

        console.print("[red]Architecture design failed.[/red]")
        raise SystemExit(1)

    def _print_architecture(self, arch: CrateArchitecture):
        """Pretty-print the designed architecture."""
        console.print(f"\n[bold]Workspace: {arch.workspace_name}[/bold]")
        console.print(f"  {arch.description}\n")

        console.print("[cyan]Crates:[/cyan]")
        for crate in arch.crates:
            deps = f" -> [{', '.join(crate.dependencies)}]" if crate.dependencies else ""
            std = "no_std" if crate.is_no_std else "std"
            console.print(f"  [green]{crate.name}[/green] ({std}){deps}")
            console.print(f"    {crate.description}")

        if arch.traits:
            console.print(f"\n[cyan]Traits: {len(arch.traits)}[/cyan]")
            for trait in arch.traits:
                console.print(f"  [green]{trait.name}[/green] in {trait.crate}")

        if arch.ownership_decisions:
            console.print(f"\n[cyan]Ownership decisions: {len(arch.ownership_decisions)}[/cyan]")
            for dec in arch.ownership_decisions:
                console.print(f"  C: {dec.c_pattern}")
                console.print(f"  Rust: {dec.rust_pattern}")
                console.print(f"  Why: {dec.rationale}\n")

        console.print(f"\n  LLM cost: ${self.llm.total_cost:.4f}")
