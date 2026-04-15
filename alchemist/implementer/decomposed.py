"""Decomposed function generation.

Instead of asking the LLM to emit a whole Rust function in one shot —
types, constants, loops, edge cases, finalization all in one monolithic
generation — decompose the task into smaller sub-problems and verify
after each one.

Pattern:
    1. CONSTANTS  — emit named constants (polynomial, BASE, lookup table, etc)
                    Verify: compiles, constants are sensible values.
    2. SHAPE      — emit the function signature + main control flow,
                    with unimplemented!() placeholders inside the hot loops.
                    Verify: compiles.
    3. BODY       — fill in the hot-loop bodies.
                    Verify: compiles + catalog tests pass.
    4. EDGE CASES — fix any remaining test failures with targeted prompts
                    that include the specific failing vectors.

Each step has a narrower correction surface than "write the whole fn from
scratch". When a step fails, the iteration re-prompts just that step,
leaving earlier validated work intact.

This module is invoked by TDDGenerator when the single-shot pipeline
exhausts its non-multi-sample budget on a function whose complexity
warrants stepwise generation (CRC, compression, crypto).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class StepResult:
    step: str                       # "constants", "shape", "body", "edge_cases"
    success: bool = False
    produced: str = ""              # the Rust fragment produced by this step
    error: str = ""
    iterations: int = 0


@dataclass
class DecomposedResult:
    algorithm: str
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if not self.steps:
            return False
        # constants and shape must succeed strictly; body and edge_cases can
        # co-operate — if the final edge_cases step succeeds, the function is
        # correct even if `body` reported test failures mid-flight.
        last = self.steps[-1]
        non_recoverable = [s for s in self.steps if s.step in ("constants", "shape")]
        if any(not s.success for s in non_recoverable):
            return False
        return last.success

    def last_error(self) -> str:
        for s in reversed(self.steps):
            if not s.success and s.error:
                return s.error
        return ""


# ---------------------------------------------------------------------------
# Prompt templates for each decomposition step
# ---------------------------------------------------------------------------

_CONSTANTS_PROMPT = """You are producing JUST the named constants for a Rust
function. Do NOT produce the function itself, only the constants that
surround it.

## Algorithm
Name: {name}
Description: {description}
Mathematical notes: {math}
Referenced standards: {standards}

## Task
Emit every named constant this algorithm needs. Examples:
  - Adler-32: `const ADLER_BASE: u32 = 65521; const ADLER_NMAX: usize = 5552;`
  - CRC-32 IEEE: the 256-entry lookup table as a `const CRC_TABLE: [u32; 256]`
  - SHA-256: H0 initial state, K round constants

Use `const` (compile-time). No `static`. No functions.

{reference_block}

Return ONLY the Rust const declarations as JSON {{"content": "..."}}.
"""


_SHAPE_PROMPT = """You are producing JUST the function SHAPE — signature + main
control flow — for a Rust function. Leave the hot-loop body as
`unimplemented!("fill in body")` or `todo!()` — those stubs will be
replaced by a subsequent prompt. DO include: signature, local variable
initialization, loop structure, return expression.

## Algorithm
Name: {name}
Signature (must match exactly):
```rust
{signature}
```

## Available constants (already in scope)
{constants}

## Description
{description}

## Return
Emit the function including signature and the overall control flow
(loops, conditionals, pre/post-processing). Inside each hot-loop body,
leave a `todo!("body N")` marker — do not attempt to fill it yet.

Return ONLY the Rust function as JSON {{"content": "..."}}.
"""


_BODY_PROMPT = """You are filling in the hot-loop bodies of a Rust function.
The function shape already exists — only replace the `todo!` placeholders.

## Algorithm
Name: {name}
Description: {description}
Mathematical notes: {math}

## Current shape
```rust
{shape}
```

## Available constants
{constants}

{reference_block}

## Task
For each `todo!(...)` placeholder in the shape above, replace it with the
actual body. Keep the surrounding structure identical. Return the
complete function (signature + body, with no placeholders) as JSON
{{"content": "..."}}.
"""


_EDGE_CASE_PROMPT = """The function compiles but one or more tests fail. Targeted
fix: examine the failing vector and correct the specific bug without
rewriting the whole function.

## Current function
```rust
{current}
```

## Failing test output
```
{failure}
```

## Hint
Focus on edge cases: empty input, single byte, boundary conditions,
endianness, seed handling. Keep the overall structure; patch only what's
demonstrably wrong from the failure signature.

Return the complete function as JSON {{"content": "..."}}.
"""


# ---------------------------------------------------------------------------
# Callbacks the caller supplies
# ---------------------------------------------------------------------------

SingleCallLLM = Callable[[str, str], str | None]
"""(prompt_text, step_name) -> rust_fragment or None."""

CrateChecker = Callable[[], tuple[bool, str]]
"""() -> (compiles_ok, error_summary)."""

TestRunner = Callable[[str], tuple[int, int, str]]
"""(test_filter) -> (passed, failed, output)."""


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------

@dataclass
class DecomposedGenerator:
    """Three-step stepwise generation: constants → shape → body (+ edge fix)."""
    call_llm: SingleCallLLM
    check_crate: CrateChecker
    run_tests: TestRunner
    splice_whole_fn: Callable[[str], bool]
    splice_constants: Callable[[str], bool]
    restore: Callable[[], None]
    reference_block: str = ""
    max_edge_iters: int = 2

    def generate(
        self,
        *,
        algorithm_name: str,
        description: str,
        math: str,
        standards: list[str],
        signature: str,
        test_filter: str,
    ) -> DecomposedResult:
        result = DecomposedResult(algorithm=algorithm_name)

        # ---- Step 1: constants ----
        step_consts = StepResult(step="constants")
        prompt = _CONSTANTS_PROMPT.format(
            name=algorithm_name,
            description=description,
            math=math or "(none)",
            standards=", ".join(standards) or "(none)",
            reference_block=self.reference_block or "",
        )
        consts = self.call_llm(prompt, "constants")
        step_consts.iterations = 1
        if consts:
            ok_splice = self.splice_constants(consts)
            if ok_splice:
                ok_check, err = self.check_crate()
                step_consts.success = ok_check
                step_consts.produced = consts
                step_consts.error = "" if ok_check else err
            else:
                step_consts.error = "splice failed"
        else:
            step_consts.error = "LLM returned empty"
        result.steps.append(step_consts)
        if not step_consts.success:
            self.restore()
            return result

        # ---- Step 2: shape ----
        step_shape = StepResult(step="shape")
        shape_prompt = _SHAPE_PROMPT.format(
            name=algorithm_name,
            signature=signature,
            constants=consts[:1500],
            description=description,
        )
        shape = self.call_llm(shape_prompt, "shape")
        step_shape.iterations = 1
        if shape:
            if self.splice_whole_fn(shape):
                ok_check, err = self.check_crate()
                step_shape.success = ok_check
                step_shape.produced = shape
                step_shape.error = "" if ok_check else err
            else:
                step_shape.error = "splice failed"
        else:
            step_shape.error = "LLM returned empty"
        result.steps.append(step_shape)
        if not step_shape.success:
            self.restore()
            return result

        # ---- Step 3: body ----
        step_body = StepResult(step="body")
        body_prompt = _BODY_PROMPT.format(
            name=algorithm_name,
            description=description,
            math=math or "(none)",
            shape=shape[:3000],
            constants=consts[:1500],
            reference_block=self.reference_block or "",
        )
        body = self.call_llm(body_prompt, "body")
        step_body.iterations = 1
        if body:
            if self.splice_whole_fn(body):
                ok_check, err = self.check_crate()
                if ok_check:
                    passed, failed, output = self.run_tests(test_filter)
                    step_body.success = (failed == 0 and passed > 0)
                    step_body.produced = body
                    step_body.error = "" if step_body.success else (
                        f"{failed} test failures\n{output[:500]}"
                    )
                else:
                    step_body.error = err
            else:
                step_body.error = "splice failed"
        else:
            step_body.error = "LLM returned empty"
        result.steps.append(step_body)

        # ---- Step 4: targeted edge-case fixes (only if body compiled) ----
        if step_body.produced and not step_body.success:
            for attempt in range(1, self.max_edge_iters + 1):
                current = step_body.produced
                edge_prompt = _EDGE_CASE_PROMPT.format(
                    current=current[:3000],
                    failure=step_body.error[:1500],
                )
                edge = self.call_llm(edge_prompt, "edge_cases")
                if not edge:
                    continue
                if not self.splice_whole_fn(edge):
                    continue
                ok_check, err = self.check_crate()
                if not ok_check:
                    step_body.error = err
                    continue
                passed, failed, output = self.run_tests(test_filter)
                if failed == 0 and passed > 0:
                    step_edge = StepResult(
                        step="edge_cases", success=True,
                        produced=edge, iterations=attempt,
                    )
                    result.steps.append(step_edge)
                    return result
                step_body.produced = edge
                step_body.error = f"{failed} test failures\n{output[:500]}"

        if not step_body.success:
            self.restore()
        return result


# ---------------------------------------------------------------------------
# Utility: detect when a function is complex enough to warrant decomposition
# ---------------------------------------------------------------------------

_DECOMPOSE_HINT_PATTERNS = [
    r"\bcrc\b", r"\bdeflate\b", r"\binflate\b", r"\bhuffman\b",
    r"\bsha\b", r"\baes\b", r"\bcipher\b",
    r"lookup table", r"compression", r"decompression",
]


def should_decompose(algorithm_name: str, description: str) -> bool:
    """True when an algorithm is complex enough that decomposed generation is worth it.

    Heuristic: matches a keyword list for known-complex families. The loop
    can also skip the heuristic and force decomposition via a kwarg.
    """
    blob = (algorithm_name + " " + (description or "")).lower()
    return any(re.search(p, blob) for p in _DECOMPOSE_HINT_PATTERNS)
