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

## Principles

1. **One concern per crate.** Each crate should have a clear, single purpose. \
Checksums go in one crate, compression in another.

2. **Dependency order matters.** Crates should form a DAG. Leaf crates (no internal deps) \
come first: types, error definitions, checksums. Complex crates depend on simpler ones.

3. **Ownership at boundaries.** Every piece of data has exactly one owner. Shared state \
becomes either: a struct field (owned), a reference parameter (borrowed), or a channel \
message (transferred). NEVER use Arc<Mutex<>> unless genuinely needed for concurrent access.

4. **Traits for interfaces.** Public module boundaries should use traits so consumers \
can swap implementations (e.g., different compression strategies).

5. **no_std by default.** Unless a crate genuinely needs std (file I/O, networking), \
mark it no_std with optional std feature.

6. **Zero unsafe target.** Pure algorithms should have zero unsafe blocks. Only SIMD \
intrinsics or hardware register access justify unsafe.

7. **Error types are first-class.** Design a proper error hierarchy, not stringly-typed errors.

8. **Don't replicate C's API.** Design a Rust-idiomatic API. C's `z_stream` with raw \
pointers becomes a Rust struct implementing `Read`/`Write`. C's error codes become \
`Result<T, E>`.

## Output

Return a CrateArchitecture with all crates, traits, error types, ownership decisions, \
and feature flags fully specified.\
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
