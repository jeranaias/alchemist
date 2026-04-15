"""Deterministic post-processing for LLM-generated Rust code.

Fixes common typos and syntax errors BEFORE running cargo check, so the
compile-fix loop doesn't waste iterations on trivial LLM mistakes.
"""

from __future__ import annotations

import re
from pathlib import Path


# Common LLM typos — Rust-specific
TYPO_FIXES = [
    # ##! → #! (crate-level attribute typo; model sometimes doubles #)
    (re.compile(r"^##!", re.MULTILINE), "#!"),
    # ## → # for outer attributes
    (re.compile(r"^##\[", re.MULTILINE), "#["),
    # Stray `p ` before `pub` (observed: `p pub enum TreeError`)
    (re.compile(r"\bp\s+pub\b"), "pub"),
    # Stray single char before keyword — model inserts spurious letters
    (re.compile(r"\bp\s+fn\b"), "fn"),
    (re.compile(r"\bp\s+struct\b"), "struct"),
    (re.compile(r"\bp\s+enum\b"), "enum"),
    (re.compile(r"\bp\s+impl\b"), "impl"),
    (re.compile(r"\bp\s+trait\b"), "trait"),
    (re.compile(r"\bp\s+mod\b"), "mod"),
    (re.compile(r"\bp\s+use\b"), "use"),
    # pppub, ppub → pub (observed in 122B output)
    (re.compile(r"\bp{2,}ub\b"), "pub"),
    # ffn → fn
    (re.compile(r"\bf{2,}n\b"), "fn"),
    # iimpl → impl
    (re.compile(r"\bi{2,}mpl\b"), "impl"),
    # ssstruct → struct
    (re.compile(r"\bs{2,}truct\b"), "struct"),
    # eenum → enum
    (re.compile(r"\be{2,}num\b"), "enum"),
    # ttrait → trait
    (re.compile(r"\bt{2,}rait\b"), "trait"),
    # mmod → mod
    (re.compile(r"\bm{2,}od\b"), "mod"),
    # uuse → use
    (re.compile(r"\bu{2,}se\b"), "use"),
    # lllet → let
    (re.compile(r"\bl{2,}et\b"), "let"),
    # mmut → mut
    (re.compile(r"\bm{2,}ut\b"), "mut"),
    # rreturn → return
    (re.compile(r"\br{2,}eturn\b"), "return"),
    # sself → self
    (re.compile(r"\bs{2,}elf\b"), "self"),
    # SSelf → Self
    (re.compile(r"\bS{2,}elf\b"), "Self"),
    # Consts: CConst → Const
    (re.compile(r"\bcc{2,}onst\b"), "const"),
    # Unicode replacement char that sometimes slips in
    (re.compile(r"\ufffd"), ""),
]


def scrub_rust(code: str) -> tuple[str, list[str]]:
    """Apply regex fixes to Rust code. Returns (fixed_code, list_of_fixes_applied)."""
    fixes = []
    for pattern, replacement in TYPO_FIXES:
        new_code, n = pattern.subn(replacement, code)
        if n > 0:
            fixes.append(f"{pattern.pattern} → {replacement} ({n}x)")
            code = new_code

    # Strip module-level const/static re-definitions that conflict with imports.
    # The TDD generator tells the model not to redefine shared constants, but
    # it does anyway ~50% of the time. Stripping them prevents E0428.
    const_redef = re.compile(
        r"^(?:pub\s+)?const\s+(?:CRC32_TABLE|ADLER_BASE|ADLER_NMAX)\b[^;]*;?\s*$"
        r"(?:\n(?:[ \t].*\n)*)?",  # multi-line const (table with block)
        re.MULTILINE,
    )
    # For block-style consts: `const X: T = { ... };`
    new_code = code
    for m in const_redef.finditer(code):
        text = m.group(0)
        if "{" in text:
            # Find matching close brace
            start = code.find("{", m.start())
            if start >= 0:
                depth = 1
                i = start + 1
                while i < len(code) and depth > 0:
                    if code[i] == "{": depth += 1
                    elif code[i] == "}": depth -= 1
                    i += 1
                # Remove from const keyword to closing };
                end = code.find(";", i - 1)
                if end >= 0:
                    new_code = new_code.replace(code[m.start():end + 1], "")
                    fixes.append(f"stripped redefined const {text[:40].strip()}")
        else:
            new_code = new_code.replace(text, "")
            fixes.append(f"stripped redefined const {text[:40].strip()}")
    code = new_code

    # Remove stray markdown fences that sometimes leak through JSON.
    # Strip ANY line that is just ``` or ```lang anywhere in the file.
    code = re.sub(r"^\s*```(?:\w+)?\s*$\n?", "", code, flags=re.MULTILINE)
    # Also strip any standalone backticks (grave accents) used as fences mid-file
    code = re.sub(r"^\s*`{3,}\s*$\n?", "", code, flags=re.MULTILINE)

    # Detect broken test modules — strip entirely if they have obvious
    # truncation artifacts (dangling incomplete statements, byte literals
    # without string, etc.). Tests are optional for library compilation.
    test_pattern = re.compile(r"(#\[cfg\(test\)\]\s*\nmod\s+\w+\s*\{)", re.MULTILINE)
    test_match = test_pattern.search(code)
    if test_match:
        test_start = test_match.start()
        after = code[test_match.end():]
        # Find matching closing brace for the mod block
        brace_depth = 1
        i = 0
        while i < len(after) and brace_depth > 0:
            ch = after[i]
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
            i += 1
        test_body = after[:i] if brace_depth == 0 else after[:i]
        # Signs of truncation inside the test module:
        # - Line ending with `= b` (byte literal without content)
        # - Line ending with `"` (unclosed string)
        # - Incomplete assert_eq! call
        # - Unbalanced when we ran out of text
        truncation_signs = [
            r"=\s+b\s*$",           # let x = b  (byte string start, no content)
            r"=\s+b\s*\n",
            r"=\s+r#?\s*$",         # raw string start, no content
            r"assert[_a-z]*!\s*\(\s*$",  # assert!( at end
        ]
        is_truncated = brace_depth > 0 or any(
            re.search(p, test_body, re.MULTILINE) for p in truncation_signs
        )
        if is_truncated:
            code = code[:test_start].rstrip() + "\n"
            fixes.append("stripped broken test module")

    # Detect and remove functions with truncated bodies.
    # Pattern: function ending with `let X =`, `X =`, `,`, or `(` just
    # before a closing brace — mid-expression truncation.
    code, trunc_fixes = _strip_truncated_functions(code)
    if trunc_fixes:
        fixes.append(trunc_fixes)

    # Balance unclosed braces — common when token limit truncates generation
    code, brace_fixes = _balance_braces(code)
    if brace_fixes:
        fixes.append(brace_fixes)

    return code, fixes


def _strip_truncated_functions(code: str) -> tuple[str, str]:
    """Remove function bodies that end mid-expression (token truncation).

    Detects `fn name(...) {` ... `<partial expr>` `}` patterns and replaces
    the function body with a stub `todo!()` so the file compiles.
    """
    # Find all fn definitions with brace bodies
    pattern = re.compile(
        r"((?:pub\s+)?(?:unsafe\s+)?fn\s+\w+[^{]*)\{",
        re.MULTILINE,
    )
    count = 0
    result_parts = []
    last_end = 0

    for m in pattern.finditer(code):
        fn_sig = m.group(1)
        body_start = m.end()  # position after {
        # Find matching close brace
        depth = 1
        i = body_start
        in_str = False
        in_char = False
        esc = False
        in_line_cmt = False
        in_block_cmt = False
        while i < len(code) and depth > 0:
            ch = code[i]
            nxt = code[i + 1] if i + 1 < len(code) else ""
            if in_line_cmt:
                if ch == "\n":
                    in_line_cmt = False
            elif in_block_cmt:
                if ch == "*" and nxt == "/":
                    in_block_cmt = False
                    i += 1
            elif in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif in_char:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == "'":
                    in_char = False
            else:
                if ch == "/" and nxt == "/":
                    in_line_cmt = True
                    i += 1
                elif ch == "/" and nxt == "*":
                    in_block_cmt = True
                    i += 1
                elif ch == '"':
                    in_str = True
                elif ch == "'" and i + 2 < len(code) and code[i+2] == "'":
                    # char literal — skip 3 chars
                    i += 2
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
            i += 1

        if depth != 0:
            # Unclosed function; leave for brace balancer
            continue

        body = code[body_start:i - 1]  # exclude closing brace
        # Check if body has truncation signs at end
        tail = body.rstrip()
        # Skip very short bodies
        if len(tail) < 5:
            continue
        last_chars = tail[-5:] if len(tail) >= 5 else tail
        truncated = False
        # Ends with assignment operator/comma/paren right before function close
        if re.search(r"[=,(]\s*$", tail):
            truncated = True
        # Ends with `let X =` pattern
        if re.search(r"\blet\s+\w+\s*=\s*$", tail):
            truncated = True
        # Ends with `X.method(` pattern
        if re.search(r"\w\s*\(\s*$", tail):
            truncated = True
        if truncated:
            result_parts.append(code[last_end:m.start()])
            # Replace body with a stub
            result_parts.append(fn_sig + "{\n    unimplemented!(\"stub — body truncated during generation\")\n}")
            last_end = i
            count += 1

    if count == 0:
        return code, ""

    result_parts.append(code[last_end:])
    return "".join(result_parts), f"replaced {count} truncated fn bodies with unimplemented!()"


def _balance_braces(code: str) -> tuple[str, str]:
    """Auto-close unclosed braces/brackets/parens at end of Rust code.

    Tracks depth while respecting strings and comments. If code ends with
    unclosed delimiters, appends closers.
    """
    depth_brace = 0
    depth_bracket = 0
    depth_paren = 0
    i = 0
    n = len(code)
    in_string = False
    in_char = False
    in_line_comment = False
    in_block_comment = False
    string_raw = False
    string_raw_hashes = 0

    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            if string_raw:
                if ch == '"':
                    # Check for matching hashes
                    j = i + 1
                    hashes = 0
                    while j < n and code[j] == '#' and hashes < string_raw_hashes:
                        hashes += 1
                        j += 1
                    if hashes == string_raw_hashes:
                        in_string = False
                        string_raw = False
                        i = j
                        continue
                i += 1
                continue
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
                i += 1
                continue
            i += 1
            continue
        if in_char:
            if ch == "\\":
                i += 2
                continue
            if ch == "'":
                in_char = False
            i += 1
            continue

        # Not inside any special context
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "r" and i + 1 < n and (code[i+1] in ('"', '#')):
            # Raw string start
            j = i + 1
            hashes = 0
            while j < n and code[j] == '#':
                hashes += 1
                j += 1
            if j < n and code[j] == '"':
                in_string = True
                string_raw = True
                string_raw_hashes = hashes
                i = j + 1
                continue
        if ch == "'":
            # Could be a lifetime or char literal
            # Cheap heuristic: if followed by single char + ', it's a char
            if i + 2 < n and code[i+2] == "'" and code[i+1] != "\\":
                i += 3
                continue
            if i + 3 < n and code[i+1] == "\\" and code[i+3] == "'":
                i += 4
                continue
            # Otherwise treat as lifetime — just skip '
            i += 1
            continue
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
        elif ch == "[":
            depth_bracket += 1
        elif ch == "]":
            depth_bracket -= 1
        elif ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren -= 1
        i += 1

    # Too few closers — add them
    added = []
    if depth_paren > 0:
        added.append(")" * depth_paren)
    if depth_bracket > 0:
        added.append("]" * depth_bracket)
    if depth_brace > 0:
        added.append("}" * depth_brace)

    if added:
        suffix = "".join(added)
        msg = f"auto-closed delimiters: {suffix}"
        return code.rstrip() + "\n" + suffix + "\n", msg

    # Too many closers — strip trailing bare } lines
    if depth_brace < 0 or depth_paren < 0 or depth_bracket < 0:
        # Remove trailing bare } lines repeatedly until balanced
        lines = code.rstrip().split("\n")
        excess = -depth_brace
        removed = 0
        while excess > 0 and lines:
            last = lines[-1].strip()
            if last == "}":
                lines.pop()
                excess -= 1
                removed += 1
            else:
                break
        if removed > 0:
            return "\n".join(lines) + "\n", f"stripped {removed} excess '}}'"

    return code, ""


def scrub_toml(content: str) -> tuple[str, list[str]]:
    """Fix common TOML generation errors (missing commas in arrays).

    The LLM often forgets commas in multi-line arrays like:
        members = [
            "a"
            "b"
        ]
    """
    fixes = []

    # Add commas between string elements on consecutive lines inside arrays
    # Match: "string"\n    "string"   (no comma)
    pattern = re.compile(
        r'("\s*\n\s*)(")',
    )
    new_content, n = pattern.subn(r'",\n    \2', content)
    if n > 0:
        fixes.append(f"TOML array commas ({n}x)")
        content = new_content

    # Validate by parsing
    try:
        import tomllib
        tomllib.loads(content)
    except ImportError:
        try:
            import tomli as tomllib  # Fallback for older Python
            tomllib.loads(content)
        except ImportError:
            pass
    except Exception as e:
        fixes.append(f"TOML still invalid: {e}")

    return content, fixes


def scrub_files(files: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Scrub all files in a generated crate dict. Returns (cleaned_files, all_fixes)."""
    cleaned = {}
    all_fixes = []
    for path, content in files.items():
        if path.endswith(".rs"):
            content, fixes = scrub_rust(content)
            if fixes:
                all_fixes.append(f"{path}: {', '.join(fixes)}")
        elif path.endswith(".toml"):
            content, fixes = scrub_toml(content)
            if fixes:
                all_fixes.append(f"{path}: {', '.join(fixes)}")
        cleaned[path] = content
    return cleaned, all_fixes


def synthesize_missing_modules(files: dict[str, str]) -> dict[str, str]:
    """If lib.rs declares `mod X;` but src/X.rs doesn't exist, create a stub.

    Prevents "file not found for module X" compile errors.
    """
    lib_rs = files.get("src/lib.rs") or files.get("lib.rs")
    if not lib_rs:
        return files

    # Find declared modules: `pub mod X;` or `mod X;` (not `mod X { ... }`)
    decl_pattern = re.compile(r"^\s*(?:pub\s+)?mod\s+([a-z_][a-z0-9_]*)\s*;", re.MULTILINE)
    declared = decl_pattern.findall(lib_rs)

    # Check which files exist
    existing_names = set()
    for path in files:
        p = Path(path)
        if p.suffix == ".rs" and p.name != "lib.rs" and p.name != "main.rs":
            existing_names.add(p.stem)

    # Create stubs for declared modules that don't exist
    new_files = dict(files)
    for mod_name in declared:
        target = f"src/{mod_name}.rs"
        alt_target = f"src/{mod_name}/mod.rs"
        if mod_name not in existing_names and target not in files and alt_target not in files:
            new_files[target] = (
                f"//! {mod_name} module (auto-generated stub)\n"
                f"//!\n"
                f"//! This module was declared in lib.rs but not generated.\n"
                f"//! TODO: implement {mod_name} functionality.\n"
            )

    return new_files
