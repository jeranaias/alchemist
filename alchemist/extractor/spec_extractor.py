"""Stage 2: Extract algorithm specifications from C code via LLM.

Takes analysis output (parsed files, call graph, modules) and produces
AlgorithmSpec / ModuleSpec for each detected module.
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from alchemist.config import AlchemistConfig
from alchemist.extractor.schemas import (
    AlgorithmSpec,
    FunctionSpec,
    ModuleSpec,
    SharedType,
    TypeField,
)
from alchemist.extractor.prompts.extract_algorithm import (
    SYSTEM_PROMPT,
    MODULE_EXTRACTION_PROMPT,
)
from alchemist.llm.client import AlchemistLLM, CachedContext
from alchemist.llm.structured import pydantic_to_tool_schema

console = Console(force_terminal=True, legacy_windows=False)


class SpecExtractor:
    """Extract algorithm specifications from analyzed C code."""

    def __init__(self, config: AlchemistConfig | None = None):
        self.config = config or AlchemistConfig()
        self.llm = AlchemistLLM(self.config)
        self._cached_context: CachedContext | None = None
        self._output_dir: Path | None = None

    def extract_all(
        self,
        analysis: dict,
        output_dir: Path | None = None,
    ) -> list[ModuleSpec]:
        """Extract specs for all detected modules.

        Args:
            analysis: Output from Stage 1 (analysis.json)
            output_dir: Where to write spec JSON files
        """
        self._output_dir = output_dir
        modules = analysis.get("modules", [])
        if not modules:
            console.print("[red]No modules found in analysis.[/red]")
            return []

        # Filter to algorithm modules only
        algo_modules = [m for m in modules if m["category"] == "algorithm"]
        glue_modules = [m for m in modules if m["category"] != "algorithm"]

        console.print(
            f"[cyan]Extracting specs for {len(algo_modules)} algorithm modules "
            f"(skipping {len(glue_modules)} glue modules)[/cyan]"
        )

        # Build cached context (system prompt + project overview)
        self._cached_context = self.llm.create_cached_context(
            system_text=SYSTEM_PROMPT,
            project_context=self._build_project_context(analysis),
        )

        specs = []
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task("Extracting specs...", total=len(algo_modules))

            for mod in algo_modules:
                progress.update(task, description=f"Extracting {mod['name']}...")
                # Skip if module spec already exists
                if output_dir:
                    existing = output_dir / f"{mod['name']}.json"
                    if existing.exists():
                        try:
                            cached = ModuleSpec.model_validate(
                                json.loads(existing.read_text())
                            )
                            specs.append(cached)
                            console.print(f"  [dim]Skipping {mod['name']} (already done)[/dim]")
                            progress.update(task, advance=1)
                            continue
                        except Exception:
                            pass
                spec = self._extract_module(mod, analysis)
                if spec:
                    specs.append(spec)

                    # Save incrementally
                    if output_dir:
                        out_file = output_dir / f"{mod['name']}.json"
                        out_file.write_text(spec.model_dump_json(indent=2))
                        console.print(f"  [green]Wrote {out_file}[/green]")

                progress.update(task, advance=1)

        # Print cost summary
        stats = self.llm.stats
        console.print(f"\n[cyan]LLM Stats:[/cyan]")
        console.print(f"  Calls: {stats['call_count']}")
        console.print(f"  Input tokens: {stats['total_input_tokens']:,}")
        console.print(f"  Output tokens: {stats['total_output_tokens']:,}")
        console.print(f"  Total cost: ${stats['total_cost_usd']:.4f}")

        return specs

    def _extract_module(self, module: dict, analysis: dict) -> ModuleSpec | None:
        """Extract spec for a single module via per-function extraction.

        Strategy: extract lightweight FunctionSpec for each significant function
        individually (small, reliable LLM calls), then aggregate into a ModuleSpec.
        """
        # Find all functions belonging to this module
        module_funcs = set(module.get("functions", []))
        files_dict = analysis.get("files", {})

        # Gather (function_name, file_path, source_code) tuples
        func_data = []
        for filepath, file_data in files_dict.items():
            if not filepath.endswith(".c"):
                continue
            try:
                source = Path(filepath).read_text(errors="replace")
            except FileNotFoundError:
                continue
            lines = source.split("\n")
            for func in file_data.get("functions", []):
                if func["name"] not in module_funcs:
                    continue
                start = func.get("start_line", 1) - 1
                end = func.get("end_line", start + 1)
                # Include a few lines before for comments
                ctx_start = max(0, start - 3)
                func_source = "\n".join(lines[ctx_start:end])
                func_data.append({
                    "name": func["name"],
                    "file": filepath,
                    "source": func_source,
                    "lines": func.get("line_count", 0),
                })

        if not func_data:
            console.print(f"  [yellow]No functions found for {module['name']}[/yellow]")
            return None

        # Skip very small (<5 lines) or very large (>500 lines) functions
        significant = [f for f in func_data if 5 <= f["lines"] <= 500]
        if not significant:
            significant = func_data[:5]

        # Checkpoint directory for per-function specs
        func_ckpt_dir = self._output_dir / "_functions" / module["name"] if self._output_dir else None
        if func_ckpt_dir:
            func_ckpt_dir.mkdir(parents=True, exist_ok=True)

        # Load any previously-extracted function specs
        func_specs: list[FunctionSpec] = []
        already_done: set[str] = set()
        if func_ckpt_dir:
            for fpath in func_ckpt_dir.glob("*.json"):
                try:
                    fs = FunctionSpec.model_validate(json.loads(fpath.read_text()))
                    func_specs.append(fs)
                    already_done.add(fs.name)
                except Exception:
                    pass

        remaining = [f for f in significant if f["name"] not in already_done]
        console.print(
            f"  [cyan]{module['name']}: {len(already_done)} already done, "
            f"extracting {len(remaining)} more (of {len(func_data)} total)[/cyan]"
        )

        # Extract FunctionSpec for each remaining function
        schema = pydantic_to_tool_schema(FunctionSpec)
        server_failures = 0
        for i, f in enumerate(remaining):
            # Check server before each call; wait if down
            if not self.llm.wait_for_server(max_wait=90, check_interval=10):
                console.print(
                    f"  [red]Server unavailable after waiting 90s — "
                    f"pausing module {module['name']}[/red]"
                )
                break

            prompt = (
                f"Analyze this C function and extract its specification.\n\n"
                f"Function: {f['name']} (file: {Path(f['file']).name})\n\n"
                f"```c\n{f['source']}\n```\n\n"
                f"Return a FunctionSpec describing what the algorithm does, "
                f"its inputs/outputs in idiomatic Rust types, and implementation notes. "
                f"Focus on the ALGORITHM, not the C syntax."
            )

            resp = self.llm.call_structured(
                messages=[{"role": "user", "content": prompt}],
                tool_name="function_spec",
                tool_schema=schema,
                cached_context=self._cached_context,
                max_tokens=1500,
            )

            if resp.structured:
                try:
                    spec = FunctionSpec.model_validate(resp.structured)
                    func_specs.append(spec)
                    # CHECKPOINT: save immediately
                    if func_ckpt_dir:
                        (func_ckpt_dir / f"{spec.name}.json").write_text(
                            spec.model_dump_json(indent=2)
                        )
                    console.print(
                        f"    [green]{spec.name}[/green] ({spec.category}) [{resp.duration_ms}ms]"
                    )
                    server_failures = 0
                except Exception as e:
                    console.print(f"    [yellow]{f['name']}: validation failed[/yellow]")
            else:
                content = (resp.content or "")[:80]
                console.print(f"    [red]{f['name']}: no JSON[/red]")
                if "ERROR" in (resp.content or "") or "Unavailable" in (resp.content or ""):
                    server_failures += 1
                    if server_failures >= 3:
                        console.print(
                            f"  [red]3 consecutive server failures — "
                            f"pausing module {module['name']}[/red]"
                        )
                        break

        if not func_specs:
            return None

        return self._aggregate_to_module_spec(module["name"], func_specs)

    def _aggregate_to_module_spec(
        self, name: str, func_specs: list[FunctionSpec]
    ) -> ModuleSpec:
        """Convert a list of FunctionSpecs into a ModuleSpec.

        One AlgorithmSpec per FunctionSpec. The earlier "group by category"
        strategy silently merged independent algorithms (e.g. adler32 + crc32
        + fletcher16) into a single spec with concatenated parameter lists,
        which broke skeleton generation downstream.
        """
        VALID_CATEGORIES = {
            "compression", "decompression", "checksum", "hash",
            "cipher", "filter", "controller", "data_structure",
            "protocol", "utility", "other",
        }

        algorithms: list[AlgorithmSpec] = []
        for fs in func_specs:
            cat = fs.category if fs.category in VALID_CATEGORIES else "other"
            algo = AlgorithmSpec(
                name=fs.name,
                display_name=fs.name.replace("_", " ").title(),
                category=cat,
                description=(fs.purpose or fs.name)[:500],
                mathematical_description=fs.algorithm_notes or "",
                inputs=fs.inputs,
                return_type=fs.return_type or "()",
                no_std_compatible=True,
                unsafe_required=bool(fs.unsafe_required),
                referenced_standards=sorted(fs.referenced_standards or []),
                source_functions=[fs.name],
            )
            algorithms.append(algo)

        algo_names = ", ".join(a.name for a in algorithms) or "(none)"
        return ModuleSpec(
            name=name,
            display_name=name.replace("_", " ").title(),
            description=(
                f"Module containing {len(func_specs)} function"
                f"{'' if len(func_specs) == 1 else 's'}: {algo_names}"
            ),
            algorithms=algorithms,
        )

    def _extract_module_OLD_BULK(self, module: dict, analysis: dict) -> ModuleSpec | None:
        """OLD: bulk extraction — kept for reference but not used."""
        source_parts = []
        total_chars = 0
        max_chars = 30_000
        c_files = [f for f in module.get("files", []) if f.endswith(".c")]
        for filepath in c_files:
            file_data = analysis.get("files", {}).get(filepath)
            if not file_data:
                continue
            try:
                code = Path(filepath).read_text(errors="replace")
                if total_chars + len(code) > max_chars and source_parts:
                    continue
                source_parts.append(f"// === {Path(filepath).name} ===\n{code}")
                total_chars += len(code)
            except FileNotFoundError:
                continue

        if not source_parts:
            return None

        source_code = "\n\n".join(source_parts)

        # Build call graph context
        cg = analysis.get("call_graph", {})
        func_calls = cg.get("function_calls", {})
        relevant_calls = {
            f: calls for f, calls in func_calls.items()
            if f in module.get("functions", [])
        }
        cg_context = json.dumps(relevant_calls, indent=2) if relevant_calls else "No call data."

        # Build the prompt
        prompt = MODULE_EXTRACTION_PROMPT.format(
            module_name=module["name"],
            category=module["category"],
            files=", ".join(Path(f).name for f in module.get("files", [])),
            functions=", ".join(module.get("functions", [])[:30]),
            call_graph_context=cg_context,
            source_code=source_code,
        )

        # Make the LLM call with structured output — retry once on failure
        schema = pydantic_to_tool_schema(ModuleSpec)
        response = None
        for attempt in range(2):
            response = self.llm.call_structured(
                messages=[{"role": "user", "content": prompt}],
                tool_name="module_spec",
                tool_schema=schema,
                cached_context=self._cached_context,
                max_tokens=8192,
            )
            if response.content and not response.content.startswith("ERROR"):
                break
            if attempt == 0:
                import time
                console.print(f"  [yellow]Retrying in 20s...[/yellow]")
                time.sleep(20)

        if response.structured:
            try:
                return ModuleSpec.model_validate(response.structured)
            except Exception as e:
                console.print(f"  [yellow]Pydantic validation failed for {module['name']}: {e}[/yellow]")
                return self._parse_partial(response.structured, module["name"])

        # Debug: show what we got back
        content_preview = (response.content or "")[:300]
        if content_preview.startswith("ERROR"):
            console.print(f"  [red]LLM error for {module['name']}: {content_preview}[/red]")
        elif content_preview:
            console.print(f"  [yellow]Got response but JSON parse failed for {module['name']}[/yellow]")
            console.print(f"  [dim]Preview: {content_preview[:150]}...[/dim]")
            # Try harder: the whole content might be valid JSON wrapped in thinking
            fallback = self._try_parse_content(response.content, module["name"])
            if fallback:
                return fallback
        else:
            console.print(f"  [red]Empty response for {module['name']}[/red]")
        return None

    def _try_parse_content(self, content: str, name: str) -> ModuleSpec | None:
        """Last-resort attempt to parse a ModuleSpec from raw content."""
        import re
        # Strip thinking tags
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

        # Try to find JSON object
        depth = 0
        start_idx = None
        for i, ch in enumerate(content):
            if ch == "{":
                if depth == 0:
                    start_idx = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start_idx is not None:
                    try:
                        data = json.loads(content[start_idx:i + 1])
                        data.setdefault("name", name)
                        data.setdefault("display_name", name)
                        data.setdefault("description", f"Extracted: {name}")
                        data.setdefault("algorithms", [])
                        spec = ModuleSpec.model_validate(data)
                        console.print(f"  [green]Recovered spec for {name} from raw content[/green]")
                        return spec
                    except Exception:
                        start_idx = None
        return None

    def _build_project_context(self, analysis: dict) -> str:
        """Build a project overview for the cached context."""
        summary = analysis.get("summary", {})
        modules = analysis.get("modules", [])

        lines = [
            "## Project Analysis Summary",
            f"Source: {analysis.get('source', 'unknown')}",
            f"Files: {summary.get('total_files', 0)}",
            f"Lines: {summary.get('total_lines', 0):,}",
            f"Functions: {summary.get('total_functions', 0)}",
            "",
            "## Detected Modules",
        ]
        for mod in modules:
            lines.append(
                f"- **{mod['name']}** ({mod['category']}): "
                f"{len(mod.get('functions', []))} functions, {mod.get('total_lines', 0)} lines"
            )

        return "\n".join(lines)

    def _parse_partial(self, data: dict, name: str) -> ModuleSpec | None:
        """Try to salvage a partially valid ModuleSpec."""
        try:
            # Fill in required fields with defaults
            data.setdefault("name", name)
            data.setdefault("display_name", name)
            data.setdefault("description", f"Extracted module: {name}")
            data.setdefault("algorithms", [])
            return ModuleSpec.model_validate(data)
        except Exception:
            return None
