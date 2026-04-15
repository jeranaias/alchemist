"""Stage 4: Generate Rust code from specs and architecture.

Generates crate-by-crate in dependency order, with an iterative compile-fix loop.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from rich.console import Console

from alchemist.architect.schemas import CrateArchitecture, CrateSpec
from alchemist.config import AlchemistConfig
from alchemist.extractor.schemas import ModuleSpec
from alchemist.implementer.prompts.implement_module import (
    IMPLEMENT_SYSTEM_PROMPT,
    IMPLEMENT_PROMPT,
    FIX_COMPILATION_PROMPT,
)
from alchemist.llm.client import AlchemistLLM, CachedContext
from alchemist.llm.structured import pydantic_to_tool_schema

console = Console(force_terminal=True, legacy_windows=False)


class CodeGenerator:
    """Generate Rust code from specs + architecture, with compile-fix loop."""

    def __init__(self, config: AlchemistConfig | None = None):
        self.config = config or AlchemistConfig()
        self.llm = AlchemistLLM(self.config)

    def generate_workspace(
        self,
        specs: list[ModuleSpec],
        architecture: CrateArchitecture,
        output_dir: Path,
    ) -> dict:
        """Generate complete Rust workspace.

        Returns dict with generation stats per crate.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        # DON'T write workspace Cargo.toml yet — it would reference crates
        # that don't exist yet, breaking per-crate `cargo check`.
        # We write it at the END after all crates are generated.

        # Build cached context
        cached = self.llm.create_cached_context(
            system_text=IMPLEMENT_SYSTEM_PROMPT,
            project_context=self._build_context(specs, architecture),
        )

        # Generate crates in dependency order (leaf first)
        order = self._topological_sort(architecture)
        console.print(f"[cyan]Generating {len(order)} crates in dependency order:[/cyan]")
        for name in order:
            console.print(f"  {name}")

        results = {}
        for crate_name in order:
            crate_spec = next(
                (c for c in architecture.crates if c.name == crate_name), None
            )
            if not crate_spec:
                continue

            console.print(f"\n[bold cyan]Generating crate: {crate_name}[/bold cyan]")

            # Find matching module specs
            crate_specs = [
                s for s in specs
                if s.name in crate_spec.modules
            ]

            result = self._generate_crate(
                crate_spec, crate_specs, architecture, output_dir, cached
            )
            results[crate_name] = result

        # Strip the [workspace] section from each crate's Cargo.toml
        # (added earlier to allow standalone compilation) before setting up
        # the actual workspace.
        import re
        for crate in architecture.crates:
            crate_toml = output_dir / crate.name / "Cargo.toml"
            if crate_toml.exists():
                content = crate_toml.read_text()
                # Remove [workspace] and any empty lines before it
                content = re.sub(r"\n*\[workspace\]\s*\n?", "\n", content).rstrip() + "\n"
                crate_toml.write_text(content)

        # NOW write the workspace Cargo.toml — all member crates exist
        self._write_workspace_toml(architecture, output_dir)

        # Final workspace check
        console.print("\n[cyan]Running workspace cargo check...[/cyan]")
        check_result = self._cargo_check(output_dir)
        if check_result["success"]:
            console.print("[green]Workspace compiles successfully![/green]")
        else:
            console.print("[yellow]Workspace has remaining compilation errors.[/yellow]")

        console.print(f"\n  Total LLM cost: ${self.llm.total_cost:.4f}")
        return results

    def _generate_crate(
        self,
        crate_spec: CrateSpec,
        module_specs: list[ModuleSpec],
        architecture: CrateArchitecture,
        output_dir: Path,
        cached: CachedContext,
    ) -> dict:
        """Generate a single crate PER-FILE — one file per LLM call.

        Avoids JSON escaping issues by asking for raw file content directly.
        Phases:
          1. Cargo.toml
          2. src/lib.rs
          3. Each declared module's src/X.rs
        """
        from alchemist.implementer.scrubber import scrub_rust, scrub_toml

        crate_dir = output_dir / crate_spec.name
        crate_dir.mkdir(parents=True, exist_ok=True)
        (crate_dir / "src").mkdir(exist_ok=True)

        siblings = [c.name for c in architecture.crates if c.name != crate_spec.name]
        module_specs_json = json.dumps(
            [s.model_dump() for s in module_specs], indent=2
        ) if module_specs else ""
        traits = [t for t in architecture.traits if t.crate == crate_spec.name]
        errors = [e for e in architecture.error_types if e.crate == crate_spec.name]

        files: dict[str, str] = {}

        # === Phase 1: Generate Cargo.toml ===
        # Build Cargo.toml deterministically — we don't need LLM for this.
        # The model kept ignoring the crate name and generating garbage.
        deps_lines = "\n".join(
            f'{dep} = {{ path = "../{dep}" }}'
            for dep in crate_spec.dependencies
        )
        cargo = (
            f"[package]\n"
            f'name = "{crate_spec.name}"\n'
            f'version = "0.1.0"\n'
            f'edition = "2021"\n'
            f"\n"
            f"[dependencies]\n"
            f"{deps_lines}\n"
            f"\n"
            f"[workspace]\n"
        )
        if not cargo:
            console.print(f"  [red]Failed to generate Cargo.toml for {crate_spec.name}[/red]")
            return {"success": False, "iterations": 0}
        cargo, _ = scrub_toml(cargo)
        if "[workspace]" not in cargo:
            cargo = cargo.rstrip() + "\n\n[workspace]\n"
        files["Cargo.toml"] = cargo
        (crate_dir / "Cargo.toml").write_text(cargo)
        console.print(f"  [dim]✓ Cargo.toml ({len(cargo)} chars)[/dim]")

        # === Phase 2: Generate src/lib.rs (kept intentionally minimal) ===
        # Decide submodule names from the module specs deterministically
        submodule_names = sorted({s.name for s in module_specs}) if module_specs else []
        mod_decls = "\n".join(f"pub mod {m};" for m in submodule_names)

        lib_prompt = (
            f"Generate a minimal src/lib.rs for the `{crate_spec.name}` crate.\n\n"
            f"## Crate purpose\n{crate_spec.description[:400]}\n\n"
            f"## Rules\n"
            f"- {'Start with `#![no_std]` and `extern crate alloc;`.' if crate_spec.is_no_std else 'Uses std by default.'}\n"
            f"- Include ONLY these module declarations (and NOTHING ELSE as `mod X;`):\n{mod_decls or '  (no modules)'}\n"
            f"- DO NOT declare these as `mod`, they are separate workspace crates: {', '.join(siblings)}\n"
            f"- Include an Error enum if specified in 'errors' below, otherwise define a simple `pub enum Error {{ ... }}`.\n"
            f"- Keep the file SHORT (<100 lines). Implementation goes in submodule files.\n"
            f"- Use `pub use submodule::TypeName;` sparingly for public re-exports of ONE key type per module.\n\n"
            f"## Errors to define\n{json.dumps([e.model_dump() for e in errors], indent=2) if errors else 'None.'}\n\n"
            f"Return ONLY the raw Rust code in the 'content' field of your JSON response. "
            f"No markdown fences, no explanation, no repeated lines."
        )
        lib_content = self._gen_single_file(lib_prompt, cached, max_tokens=4000)
        if not lib_content:
            console.print(f"  [red]Failed to generate lib.rs for {crate_spec.name}[/red]")
            return {"success": False, "iterations": 0}
        lib_content, _ = scrub_rust(lib_content)
        files["src/lib.rs"] = lib_content
        (crate_dir / "src" / "lib.rs").write_text(lib_content)
        console.print(f"  [dim]✓ src/lib.rs ({len(lib_content)} chars)[/dim]")

        # === Phase 3: Generate each declared submodule ===
        import re
        declared = re.findall(
            r"^\s*(?:pub\s+)?mod\s+([a-z_][a-z0-9_]*)\s*;", lib_content, re.MULTILINE
        )
        for mod_name in declared:
            mod_prompt = (
                f"Generate the src/{mod_name}.rs file for the `{crate_spec.name}` crate.\n\n"
                f"## Parent lib.rs content\n```rust\n{lib_content[:3000]}\n```\n\n"
                f"## Algorithm specs\n{module_specs_json[:5000]}\n\n"
                f"## Task\n"
                f"Implement the {mod_name} module. This file is at src/{mod_name}.rs.\n"
                f"- Do NOT include `#![no_std]` (that's in lib.rs)\n"
                f"- Use `use crate::...` to access parent items\n"
                f"- Include unit tests in a `#[cfg(test)] mod tests` block\n"
                f"- Use safe Rust (no unsafe unless truly required)\n\n"
                f"Return ONLY the raw Rust code for this ONE file in the 'content' field."
            )
            mod_content = self._gen_single_file(mod_prompt, cached, max_tokens=8000)
            if mod_content:
                mod_content, _ = scrub_rust(mod_content)
                files[f"src/{mod_name}.rs"] = mod_content
                (crate_dir / "src" / f"{mod_name}.rs").write_text(mod_content)
                console.print(f"  [dim]✓ src/{mod_name}.rs ({len(mod_content)} chars)[/dim]")
            else:
                # Create a stub so module declaration doesn't break compile
                stub = (
                    f"//! {mod_name} module (stub - generation failed)\n"
                    f"//! TODO: implement {mod_name}\n"
                )
                files[f"src/{mod_name}.rs"] = stub
                (crate_dir / "src" / f"{mod_name}.rs").write_text(stub)
                console.print(f"  [yellow]stub src/{mod_name}.rs (gen failed)[/yellow]")

        console.print(f"  [green]Generated {len(files)} files[/green]")

        # DETERMINISTIC SCRUBBING: fix typos and TOML issues before compiling
        from alchemist.implementer.scrubber import scrub_files, synthesize_missing_modules
        files, scrub_fixes = scrub_files(files)
        files = synthesize_missing_modules(files)

        # Ensure Cargo.toml has [workspace] section so it compiles standalone
        # (without this, cargo walks up and finds the parent workspace which
        # references not-yet-generated sibling crates).
        cargo_key = "Cargo.toml"
        if cargo_key in files and "[workspace]" not in files[cargo_key]:
            files[cargo_key] = files[cargo_key].rstrip() + "\n\n[workspace]\n"

        if scrub_fixes:
            console.print(f"  [dim]Scrubbed: {'; '.join(scrub_fixes)}[/dim]")

        # Write files
        self._write_crate_files(crate_dir, files)
        console.print(f"  [green]Generated {len(files)} files[/green]")

        # Compile-fix loop — targeted: parse errors by file, fix ONE file at a time
        max_iter = self.config.max_compile_iterations
        for iteration in range(max_iter):
            check = self._cargo_check(crate_dir)
            if check["success"]:
                console.print(f"  [green]Compiles on iteration {iteration}[/green]")
                return {"success": True, "iterations": iteration, "files": len(files)}

            # Parse errors by file
            errors_by_file = self._parse_cargo_errors(check["stderr"], crate_dir)
            if not errors_by_file:
                console.print(f"  [yellow]Iteration {iteration + 1}: errors not parseable[/yellow]")
                break

            console.print(
                f"  [yellow]Iteration {iteration + 1}/{max_iter}: "
                f"errors in {len(errors_by_file)} file(s)[/yellow]"
            )

            # Fix each affected file individually — smaller context, faster, no regressions
            any_change = False
            for rel_path, error_text in errors_by_file.items():
                full_path = crate_dir / rel_path
                if not full_path.exists():
                    continue
                current = full_path.read_text(errors="replace")

                targeted_prompt = (
                    f"Fix the compilation errors in this Rust file.\n\n"
                    f"File: {rel_path}\n\n"
                    f"Errors:\n```\n{error_text[:3000]}\n```\n\n"
                    f"Current content:\n```rust\n{current}\n```\n\n"
                    f"Return ONLY the fixed content of this one file as JSON: "
                    f'{{"content": "complete fixed file"}}. '
                    f"Do NOT introduce unsafe code. Do NOT change the public API."
                )

                single_file_schema = {
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                }

                fix_resp = self.llm.call_structured(
                    messages=[{"role": "user", "content": targeted_prompt}],
                    tool_name="fixed_file",
                    tool_schema=single_file_schema,
                    cached_context=cached,
                    max_tokens=8192,
                )

                new_content = None
                if fix_resp.structured and "content" in fix_resp.structured:
                    new_content = fix_resp.structured["content"]

                if new_content:
                    # Scrub the single file
                    scrubbed, fix_desc = scrub_files({rel_path: new_content})
                    new_content = scrubbed[rel_path]
                    full_path.write_text(new_content)
                    any_change = True
                    console.print(f"    [dim]fixed {rel_path}[/dim]")

            if not any_change:
                console.print(f"  [red]No changes made — stopping fix loop[/red]")
                break

        console.print(f"  [red]Failed to compile after {max_iter} iterations[/red]")
        return {"success": False, "iterations": max_iter, "files": len(files)}

    def _write_workspace_toml(self, arch: CrateArchitecture, output_dir: Path):
        """Write the workspace Cargo.toml with proper commas."""
        members = ",\n".join(f'    "{c.name}"' for c in arch.crates)
        content = f"""\
[workspace]
resolver = "2"
members = [
{members},
]
"""
        (output_dir / "Cargo.toml").write_text(content)

    def _gen_single_file(
        self, prompt: str, cached: CachedContext, max_tokens: int = 4000
    ) -> str | None:
        """Generate a single file's content via structured LLM call.

        Returns the raw file content as a string, or None on failure.
        Handles JSON envelope extraction including truncated responses.
        """
        schema = {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The complete file content"},
            },
            "required": ["content"],
        }
        resp = self.llm.call_structured(
            messages=[{"role": "user", "content": prompt}],
            tool_name="generated_file",
            tool_schema=schema,
            cached_context=cached,
            max_tokens=max_tokens,
            temperature=0.15,  # bypass response cache AND force varied sampling
        )

        # Primary path: structured output worked
        if resp.structured and isinstance(resp.structured, dict) and "content" in resp.structured:
            content = resp.structured["content"]
            if self._is_degenerate(content):
                return None
            return content

        # Fallback: extract from raw content
        raw = (resp.content or "").strip()
        if not raw:
            return None

        import re
        # Strip thinking tags
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

        # Strip markdown fences
        if raw.startswith("```"):
            after_fence = raw.split("\n", 1)
            if len(after_fence) == 2:
                raw = after_fence[1]
            if raw.endswith("```"):
                raw = raw.rsplit("```", 1)[0]
            raw = raw.strip()

        # If it looks like JSON with a content field, extract it
        if raw.startswith("{") and '"content"' in raw[:200]:
            # Try standard JSON parse
            try:
                import json
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and "content" in parsed:
                    content = parsed["content"]
                    if self._is_degenerate(content):
                        return None
                    return content
            except json.JSONDecodeError:
                pass

            # Manual extraction — handles truncated JSON.
            # Find the start of the content string value
            idx = raw.find('"content"')
            if idx >= 0:
                # Find the opening quote after "content":
                rest = raw[idx + len('"content"'):]
                colon_idx = rest.find(":")
                if colon_idx >= 0:
                    after_colon = rest[colon_idx + 1:].lstrip()
                    if after_colon.startswith('"'):
                        # Find matching close quote (not escaped)
                        body = after_colon[1:]
                        # Scan for unescaped closing quote
                        i = 0
                        content_chars = []
                        while i < len(body):
                            ch = body[i]
                            if ch == "\\" and i + 1 < len(body):
                                content_chars.append(body[i:i+2])
                                i += 2
                                continue
                            if ch == '"':
                                break
                            content_chars.append(ch)
                            i += 1
                        # Whether we found the end or ran out (truncated),
                        # decode what we have
                        partial = "".join(content_chars)
                        try:
                            decoded = partial.encode("utf-8").decode("unicode_escape", errors="replace")
                        except Exception:
                            decoded = partial
                        if self._is_degenerate(decoded):
                            return None
                        if len(decoded) > 20:
                            return decoded
            # JSON-looking but unparseable — don't return garbage
            return None

        # Not JSON at all — treat as raw code if non-trivial and not degenerate
        if len(raw) > 20 and not self._is_degenerate(raw):
            return raw
        return None

    def _is_degenerate(self, text: str) -> bool:
        """Detect if model response is stuck in a repetition loop.

        Models sometimes generate the same short line over and over when
        they lose coherence. We want to fail rather than write this junk.
        """
        if not text or len(text) < 500:
            return False
        # If any single non-empty line appears >= 10 times, it's degenerate
        from collections import Counter
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if not lines:
            return False
        counts = Counter(lines)
        most_common_line, freq = counts.most_common(1)[0]
        # If one line makes up more than 30% of the content, it's degenerate
        if freq >= 10 and freq / len(lines) > 0.30:
            return True
        return False

    def _parse_cargo_errors(self, stderr: str, crate_dir: Path) -> dict[str, str]:
        """Parse `cargo check` stderr into {relative_file_path: error_text}.

        Rust errors look like:
            error: <msg>
             --> src/lib.rs:42:10
              |
            ...

        Groups all errors/warnings by source file.
        """
        import re
        from collections import defaultdict

        errors_by_file: dict[str, list[str]] = defaultdict(list)
        lines = stderr.split("\n")
        current_block: list[str] = []
        current_file: str | None = None

        # Regex for file references in cargo output
        file_ref = re.compile(r"-->\s+([^:]+):(\d+):(\d+)")

        for line in lines:
            # Start of a new error/warning block
            if line.startswith("error") or line.startswith("warning"):
                # Flush previous block
                if current_file and current_block:
                    errors_by_file[current_file].append("\n".join(current_block))
                current_block = [line]
                current_file = None
            elif line.strip().startswith("-->"):
                match = file_ref.search(line)
                if match:
                    raw = match.group(1).strip()
                    # Normalize to relative path
                    try:
                        abs_path = Path(raw).resolve()
                        rel = abs_path.relative_to(crate_dir.resolve())
                        current_file = str(rel).replace("\\", "/")
                    except ValueError:
                        current_file = raw.replace("\\", "/")
                current_block.append(line)
            else:
                current_block.append(line)

        # Flush final block
        if current_file and current_block:
            errors_by_file[current_file].append("\n".join(current_block))

        # Only keep files that actually have ERRORS (not just warnings)
        result = {}
        for f, blocks in errors_by_file.items():
            error_blocks = [b for b in blocks if "error" in b.lower()]
            if error_blocks:
                result[f] = "\n\n".join(error_blocks)

        return result

    def _write_crate_files(self, crate_dir: Path, files: dict[str, str]):
        """Write generated files to disk."""
        for filepath, content in files.items():
            full_path = crate_dir / filepath
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)

    def _read_crate_files(self, crate_dir: Path) -> str:
        """Read all Rust source files in a crate for the fix prompt."""
        parts = []
        for f in sorted(crate_dir.rglob("*.rs")):
            rel = f.relative_to(crate_dir)
            parts.append(f"// === {rel} ===\n{f.read_text()}")
        toml = crate_dir / "Cargo.toml"
        if toml.exists():
            parts.append(f"// === Cargo.toml ===\n{toml.read_text()}")
        return "\n\n".join(parts)

    def _cargo_check(self, path: Path) -> dict:
        """Run cargo check and return results."""
        try:
            result = subprocess.run(
                ["cargo", "check"],
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=120,
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return {"success": False, "stdout": "", "stderr": str(e)}

    def _topological_sort(self, arch: CrateArchitecture) -> list[str]:
        """Sort crates in dependency order (leaf first)."""
        graph = {c.name: set(c.dependencies) for c in arch.crates}
        result = []
        visited = set()

        def visit(name: str):
            if name in visited:
                return
            visited.add(name)
            for dep in graph.get(name, set()):
                if dep in graph:
                    visit(dep)
            result.append(name)

        for name in graph:
            visit(name)
        return result

    def _extract_files_from_text(self, text: str) -> dict[str, str]:
        """Try to extract file contents from unstructured text response."""
        try:
            # Try direct JSON parse
            data = json.loads(text)
            if isinstance(data, dict):
                if "files" in data:
                    return data["files"]
                # Check if keys look like file paths
                if any("." in k for k in data):
                    return data
        except json.JSONDecodeError:
            pass

        # Try to find JSON block in markdown
        if "```json" in text:
            json_block = text.split("```json")[1].split("```")[0]
            try:
                data = json.loads(json_block)
                if isinstance(data, dict):
                    return data.get("files", data)
            except json.JSONDecodeError:
                pass

        return {}

    def _build_context(self, specs: list[ModuleSpec], arch: CrateArchitecture) -> str:
        """Build project context for the cached prompt."""
        lines = [
            f"## Architecture: {arch.workspace_name}",
            f"Crates: {len(arch.crates)}",
            f"Modules: {sum(len(s.algorithms) for s in specs)} algorithms across {len(specs)} modules",
            "",
        ]
        for crate in arch.crates:
            lines.append(f"- {crate.name}: {crate.description}")
        return "\n".join(lines)
