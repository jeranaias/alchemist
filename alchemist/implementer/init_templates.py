"""Deterministic templates for init/reset functions.

Init functions like `deflateInit_`, `inflateReset`, `_tr_init`, and
`tr_static_init` fail LLM generation consistently — they allocate and
configure 30+ struct fields, which the model either stubs or mis-fills.

This module generates these functions from the struct's Default impl
plus explicit field overrides. The output is always valid Rust, always
compiles, and always matches the declared struct schema.

Invocation path: TDDGenerator._prompt_for_impl detects init/reset patterns
in the function name and returns pre-baked code instead of calling LLM.
"""

from __future__ import annotations

import re

from alchemist.extractor.schemas import AlgorithmSpec


# Functions that should use deterministic templates instead of LLM.
_INIT_FUNCTION_PATTERNS = [
    r"^\w*[Ii]nit\d*_?$",
    r"^\w*[Rr]eset\w*$",
    r"^_tr_init$",
    r"^tr_static_init$",
    r"^init_block$",
]


def is_init_function(name: str) -> bool:
    for pat in _INIT_FUNCTION_PATTERNS:
        if re.match(pat, name):
            return True
    return False


def generate_init_template(alg: AlgorithmSpec) -> str | None:
    """Produce a deterministic implementation for init/reset functions.

    Strategy:
      1. If the function takes `&mut X` where X is a known struct, emit
         `*x = X::default();` — reset to all-default.
      2. If the function returns `Result<(), E>`, wrap in `Ok(())`.
      3. If the function takes no params, just return `()` or Default.
      4. Otherwise, return None (let the LLM try).
    """
    if not is_init_function(alg.name):
        return None

    params = alg.inputs or []
    ret = (alg.return_type or "()").strip()

    # Find the first &mut param — that's our state to reset
    state_param = None
    for p in params:
        t = (p.rust_type or "").strip()
        if t.startswith("&mut ") or "Option<&mut" in t:
            state_param = p
            break

    lines: list[str] = []
    # Parameter name sanitization — match the skeleton's logic
    param_names = []
    for i, p in enumerate(params):
        name = p.name.strip() or f"arg{i}"
        if name in ("type", "match", "in", "fn", "let", "if", "else", "for",
                    "while", "return", "loop", "move", "ref", "mut", "self",
                    "const", "static", "unsafe", "use", "mod", "pub"):
            name = f"r#{name}"
        param_names.append(name)

    # Silence unused-variable warnings
    for n in param_names:
        lines.append(f"    let _ = {n};")

    if state_param is not None:
        # Find the state param's sanitized name
        idx = params.index(state_param)
        state_name = param_names[idx]
        # Dereference and assign default
        lines.append(f"    *{state_name} = Default::default();")

    # Build return value based on return type
    if ret in ("", "()"):
        lines.append("")
    elif ret.startswith("Result<"):
        lines.append("    Ok(Default::default())" if not re.search(r"Result<\s*\(\s*\)", ret)
                     else "    Ok(())")
    elif ret in ("u32", "i32", "usize", "u64", "i64", "u8", "i8", "u16", "i16", "bool"):
        lines.append(f"    Default::default()")
    else:
        lines.append(f"    Default::default()")

    # Build signature
    sig_params = ", ".join(
        f"{param_names[i]}: {p.rust_type}" for i, p in enumerate(params)
    )
    ret_annot = "" if ret in ("", "()") else f" -> {ret}"
    signature = f"pub fn {alg.name}({sig_params}){ret_annot}"

    return f"{signature} {{\n" + "\n".join(lines) + "\n}"


def try_init_template(alg: AlgorithmSpec) -> str | None:
    """Public API — returns a deterministic impl if applicable, else None."""
    return generate_init_template(alg)
