"""Holistic crate fixer — whole-crate context fix escalation tier.

Used when the per-function TDD loop hits repeated failures on the same
function. Sends every `.rs` file plus the full `cargo check` / `cargo test`
error output to the LLM and asks for a multi-file patch.

Ported from `holistic_fix.py` (root script) into the package so pipeline
code can invoke it programmatically.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from alchemist.implementer.anti_stub import scan_text
from alchemist.implementer.scrubber import scrub_rust, scrub_toml
from alchemist.llm.client import AlchemistLLM

console = Console(force_terminal=True, legacy_windows=False)


SYSTEM_PROMPT = """\
You are a Rust expert fixing a crate that has compilation or test failures. You will receive:

1. All .rs / .toml files in the crate.
2. The full cargo check and cargo test error output.
3. Optionally, the algorithm spec and any referenced standards.

Return a JSON object of shape {"files": {"src/lib.rs": "<full new content>", ...}}
containing ONLY the files you're modifying. Follow these rules:

  - NO stub placeholders. If an algorithm is missing, implement the actual algorithm
    from the spec / standard, not `unimplemented!()` or fake heuristics.
  - NO `unsafe` unless the spec explicitly requires it.
  - Keep changes minimal — only touch files that need to change.
  - Add missing types / constants / methods rather than silently removing references.
  - Output must compile cleanly against the given test block.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class HolisticResult:
    iterations_run: int = 0
    success: bool = False
    files_changed: list[str] = field(default_factory=list)
    final_stderr: str = ""


def cargo_check(crate_dir: Path, timeout: int = 180) -> tuple[bool, str]:
    r = subprocess.run(
        ["cargo", "check"], cwd=str(crate_dir),
        capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode == 0, r.stderr


def cargo_test(crate_dir: Path, filter: str | None = None, timeout: int = 300) -> tuple[bool, str, str]:
    cmd = ["cargo", "test"]
    if filter:
        cmd.append(filter)
    cmd.extend(["--", "--nocapture"])
    r = subprocess.run(
        cmd, cwd=str(crate_dir),
        capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode == 0, r.stdout, r.stderr


def build_crate_context(crate_dir: Path, max_chars: int = 40_000) -> str:
    parts: list[str] = []
    toml = crate_dir / "Cargo.toml"
    if toml.exists():
        parts.append(f"// === Cargo.toml ===\n{toml.read_text(encoding='utf-8', errors='replace')}")
    src = crate_dir / "src"
    if src.exists():
        for rs in sorted(src.rglob("*.rs")):
            rel = rs.relative_to(crate_dir).as_posix()
            parts.append(f"// === {rel} ===\n{rs.read_text(encoding='utf-8', errors='replace')}")
    out = "\n\n".join(parts)
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n\n// ... (truncated at {max_chars} chars)"
    return out


def extract_errors_only(stderr: str, max_chars: int = 6_000) -> str:
    """Keep only `error` blocks, drop `warning` blocks."""
    blocks: list[str] = []
    current: list[str] = []
    for line in stderr.splitlines():
        if re.match(r"^warning(?:\[\w+\])?:", line):
            if current and current[0].startswith("error"):
                blocks.append("\n".join(current))
            current = ["__warn__"]
        elif re.match(r"^error(?:\[\w+\])?:", line):
            if current and current[0].startswith("error"):
                blocks.append("\n".join(current))
            current = [line]
        elif current and current[0].startswith("error"):
            current.append(line)
        elif current and current[0] == "__warn__":
            continue
    if current and current[0].startswith("error"):
        blocks.append("\n".join(current))
    joined = "\n\n".join(blocks)
    return joined[:max_chars]


# ---------------------------------------------------------------------------
# Main fixer
# ---------------------------------------------------------------------------

@dataclass
class HolisticFixer:
    llm: AlchemistLLM
    max_iter: int = 3
    # If True, reject the LLM's proposed patch when it contains stub markers.
    reject_stubs: bool = True

    def fix_crate(
        self,
        crate_dir: Path,
        *,
        spec_context: str = "",
        extra_error_ctx: str = "",
    ) -> HolisticResult:
        """Run up to `max_iter` holistic fix passes on a crate."""
        result = HolisticResult()
        cached = self.llm.create_cached_context(system_text=SYSTEM_PROMPT)

        for iteration in range(self.max_iter):
            result.iterations_run = iteration + 1
            ok_chk, stderr = cargo_check(crate_dir)
            error_text = extract_errors_only(stderr)
            if ok_chk and not extra_error_ctx:
                # Possibly tests still failing, but check passes
                # Let caller decide whether that's success
                result.success = True
                return result

            effective_errors = (error_text + "\n\n" + extra_error_ctx).strip()
            if not effective_errors:
                result.success = ok_chk
                result.final_stderr = stderr
                return result

            context_blob = build_crate_context(crate_dir)
            prompt = (
                f"Fix compilation / test errors in this Rust crate.\n\n"
                f"## Errors\n```\n{effective_errors}\n```\n\n"
                + (f"## Spec context\n{spec_context}\n\n" if spec_context else "")
                + f"## Crate files\n{context_blob}\n\n"
                f"Return JSON {{'files': {{'<path>': '<full content>'}}}}."
            )
            schema = {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["files"],
            }
            resp = self.llm.call_structured(
                messages=[{"role": "user", "content": prompt}],
                tool_name="fixed_crate",
                tool_schema=schema,
                cached_context=cached,
                max_tokens=16000,
                temperature=0.15,
            )
            patch = self._extract_file_map(resp)
            if not patch:
                console.print(f"  [yellow]holistic iter {iteration + 1}: empty patch[/yellow]")
                continue

            changed = self._apply_patch(crate_dir, patch)
            result.files_changed.extend(changed)
            if not changed:
                console.print(f"  [yellow]holistic iter {iteration + 1}: no changes applied[/yellow]")

        # Final compile status after all iterations
        ok_chk, final_stderr = cargo_check(crate_dir)
        result.success = ok_chk
        result.final_stderr = final_stderr
        return result

    # --- Internals ---

    def _extract_file_map(self, resp) -> dict[str, str]:
        if resp.structured and isinstance(resp.structured, dict) and "files" in resp.structured:
            files = resp.structured["files"]
            if isinstance(files, dict):
                return files
        raw = (resp.content or "").strip()
        if not raw:
            return {}
        # Try direct JSON parse
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "files" in parsed:
                return parsed["files"]
        except json.JSONDecodeError:
            pass
        # Markdown-fenced
        m = re.search(r"```json\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
                if "files" in parsed:
                    return parsed["files"]
            except json.JSONDecodeError:
                pass
        return {}

    def _apply_patch(self, crate_dir: Path, patch: dict[str, str]) -> list[str]:
        changed: list[str] = []
        for rel, content in patch.items():
            if not isinstance(content, str):
                continue
            # Block stub-bearing patches when requested
            if self.reject_stubs and rel.endswith(".rs"):
                violations = scan_text(rel, content)
                if violations:
                    console.print(f"  [yellow]holistic: rejecting patch for {rel} — contains stubs[/yellow]")
                    continue
            if rel.endswith(".rs"):
                content, _ = scrub_rust(content)
            elif rel.endswith(".toml"):
                content, _ = scrub_toml(content)
            content = content.replace("\r\n", "\n").replace("\r", "\n")
            target = crate_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            changed.append(rel)
        return changed
