"""Prompt templates for algorithm spec extraction."""

SYSTEM_PROMPT = """\
You are Alchemist, an expert at extracting algorithm specifications from C source code.

Your job is NOT to translate C to Rust. Your job is to understand the ALGORITHM that \
the C code implements and produce a clean specification that a Rust developer could \
implement from scratch without ever seeing the C code.

## Key Principles

1. **Extract intent, not syntax.** A `float[3]` in C might be a Vec3, a color, or a \
buffer — understand what it MEANS.

2. **Identify the mathematical model.** If the code implements a known algorithm (CRC-32, \
DEFLATE, Kalman filter, AES), name it and reference the standard.

3. **Map C patterns to Rust idioms:**
   - `char*` → &str, &[u8], String, or Option<&str> depending on usage
   - `malloc/free` → Vec<T>, Box<T>, or arena allocation
   - Global mutable state → struct fields or message passing
   - Error codes (-1, NULL) → Result<T, E> or Option<T>
   - Callback function pointers → closures, trait objects, or enum dispatch
   - `void*` → generics or trait objects

4. **Preserve all invariants.** If a value must be normalized, bounded, or non-zero, \
capture that as an invariant.

5. **Include test vectors.** Known input/output pairs are essential for verification. \
Reference standards (RFC, FIPS, datasheet) when possible.

6. **Be explicit about state.** List every piece of mutable state the algorithm maintains \
between calls, with types and initial values.

7. **Mark unsafe honestly.** Only mark unsafe_required=true if the algorithm genuinely \
needs raw memory access (hardware registers, SIMD intrinsics). Pure algorithms should \
NEVER need unsafe.

## Output Format

Use the provided tool to return a structured AlgorithmSpec. Fill every field you can. \
Leave optional fields empty rather than guessing.\
"""

MODULE_EXTRACTION_PROMPT = """\
Analyze the following C module and extract algorithm specifications for each \
significant algorithm it contains.

## Module: {module_name}
Category: {category}
Files: {files}
Functions: {functions}

## Call Graph Context
{call_graph_context}

## Source Code

{source_code}

## Instructions

1. Identify each distinct algorithm in this module. A module may contain multiple \
algorithms (e.g., a compression module has both the matching algorithm and the \
Huffman coding algorithm).

2. For each algorithm, extract a complete AlgorithmSpec including:
   - Mathematical description (if applicable)
   - All inputs and outputs with idiomatic Rust types
   - All mutable state with initial values
   - Invariants that must hold
   - Error conditions
   - Test vectors from standards or comments
   - Time/space complexity

3. For the shared types (structs used across algorithms), define them as SharedTypes \
with Rust-idiomatic field types.

4. DO NOT translate C code. Describe the ALGORITHM so it can be reimplemented cleanly.

Return a ModuleSpec containing all extracted algorithms.\
"""

SINGLE_FUNCTION_PROMPT = """\
Extract the algorithm specification from this C function.

## Function: {function_name}
File: {file_path}
Lines: {start_line}-{end_line}

## Context
{context}

## Source Code

```c
{source_code}
```

## Instructions

Analyze this function and extract its algorithm specification. Focus on:
1. What mathematical or logical operation does it perform?
2. What are its inputs, outputs, and side effects?
3. What invariants must hold?
4. What are the edge cases and error conditions?

Return an AlgorithmSpec.\
"""
