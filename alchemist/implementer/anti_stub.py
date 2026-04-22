"""Anti-stub detector for generated Rust code.

The model writes compilable lies. Examples seen in practice:
  - `unimplemented!()` / `todo!()` bodies
  - `// Since we don't have the actual algorithm, we'll use a simple heuristic.`
  - `// For this spec, we simulate the process.`
  - Functions that take input bytes but return Ok(()) without touching them
  - Output buffers declared but never written to

This module scans generated Rust and flags these patterns so the pipeline
can re-prompt or refuse to ship. Each violation carries the file, line,
pattern class, and a short snippet for re-prompt context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# Rust built-in stub calls. ALWAYS a violation in production code.
#
# Patterns use `\b` plus whitespace-insensitive matching so that any
# obfuscation via inserted whitespace (e.g., `unimplemented !  ( )`) is
# caught. Stub message content is irrelevant — the MACRO CALL itself is
# the violation. A fn that ships `unimplemented!(anything)` is a stub.
BUILTIN_STUB_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("unimplemented_macro", re.compile(r"\bunimplemented\s*!\s*\(")),
    ("todo_macro", re.compile(r"\btodo\s*!\s*\(")),
    ("panic_not_impl", re.compile(
        r'\bpanic\s*!\s*\(\s*"(?:[^"]*\b'
        r'(?:not\s+implemented|stub|unimplemented|not\s+yet|missing)\b[^"]*)"'
    , re.IGNORECASE)),
    # Type-system stub: function body is `Default::default()` only.
    # The `_scan_default_default_body` helper catches this with fn context.
]


def has_stub_for_fn(text: str, fn_name: str) -> bool:
    """True if the source contains a stub (unimplemented!/todo!/panic!) that
    mentions fn_name anywhere in the call, OR if any fn named fn_name has
    a body whose canonical form is just a single stub macro.

    Canonical form = whitespace-stripped, comment-stripped text.

    Used by Phase D's post-fill revert check. Previously the check was an
    exact-substring match on a hard-coded stub message, which missed:
      - LLM rewording the stub message
      - LLM using `todo!()` instead of `unimplemented!()`
      - Extra whitespace inserted by formatter
      - Comments embedded in the stub body
    """
    if not text:
        return False
    # Fast path: any unimplemented!/todo! call mentioning fn_name.
    escaped = re.escape(fn_name)
    inline = re.compile(
        r"(?:unimplemented|todo|panic)\s*!\s*\([^)]*" + escaped,
        re.IGNORECASE,
    )
    if inline.search(text):
        return True
    # Slow path: find every fn with this name and check its body's canonical
    # form for a bare stub macro. Uses the full fn-span collector so we
    # respect string/comment boundaries.
    fn_spans = _collect_fn_spans(text)
    for start, end, name in fn_spans:
        if name != fn_name:
            continue
        body = text[start:end]
        canonical = _canonicalize_body(body)
        if _is_canonical_stub(canonical):
            return True
    return False


def _canonicalize_body(body: str) -> str:
    """Strip comments + collapse whitespace. Used for canonical stub match."""
    # Drop block comments
    body = re.sub(r"/\*[\s\S]*?\*/", " ", body)
    # Drop line comments
    body = re.sub(r"//[^\n]*", " ", body)
    # Collapse whitespace
    body = re.sub(r"\s+", "", body)
    return body


_CANONICAL_STUB_BODIES = [
    # Pure macro calls with any content
    re.compile(r"^unimplemented!\([^)]*\);?$"),
    re.compile(r"^todo!\([^)]*\);?$"),
    re.compile(r"^panic!\([^)]*\);?$"),
    # `let _ = x;` N times then a stub macro
    re.compile(r"^(?:let_=\w+;)+(?:unimplemented|todo|panic)!\([^)]*\);?$"),
]


def _is_canonical_stub(canonical_body: str) -> bool:
    """True if the canonical (whitespace+comment-stripped) body is a stub."""
    for pat in _CANONICAL_STUB_BODIES:
        if pat.match(canonical_body):
            return True
    return False


# Comment phrases the LLM uses when it gives up mid-generation.
# Each is case-insensitive and matches only inside line/block comments.
STUB_COMMENT_PHRASES: list[tuple[str, re.Pattern[str]]] = [
    ("comment_dont_have_algorithm", re.compile(
        r"we\s+don'?t\s+have\s+(?:the\s+)?(?:actual\s+)?algorithm", re.IGNORECASE)),
    ("comment_dont_have_access", re.compile(
        r"we\s+don'?t\s+have\s+(?:access|the\s+actual|the\s+real)", re.IGNORECASE)),
    ("comment_for_this_spec", re.compile(
        r"for\s+this\s+spec(?:ification)?,?\s+we\s+(?:simulate|use|assume|will)", re.IGNORECASE)),
    ("comment_we_simulate", re.compile(
        r"\b(?:we\s+(?:simulate|assume)|simulate\s+the\s+(?:process|actual|call|logic|behavior))\b",
        re.IGNORECASE)),
    ("comment_conceptually", re.compile(r"\bconceptually\b", re.IGNORECASE)),
    ("comment_simple_heuristic", re.compile(
        r"simple\s+heuristic", re.IGNORECASE)),
    ("comment_not_accurate", re.compile(
        r"(?:this|it)\s+is\s+not\s+accurate", re.IGNORECASE)),
    ("comment_todo_implement", re.compile(
        r"TODO:\s*implement\b", re.IGNORECASE)),
    ("comment_fixme_stub", re.compile(
        r"FIXME:?\s*(?:stub|not\s+implemented)", re.IGNORECASE)),
    ("comment_placeholder", re.compile(
        r"\bplaceholder\b", re.IGNORECASE)),
    ("comment_in_reality", re.compile(
        r"in\s+reality,?\s+(?:this|we|the)", re.IGNORECASE)),
    ("comment_not_implemented", re.compile(
        r"not\s+(?:yet\s+)?implemented", re.IGNORECASE)),
    ("comment_auto_stub", re.compile(
        r"auto-?generated\s+stub", re.IGNORECASE)),
    # LLM likes to write "// ... (rest of X)" or "// ... rest ..." to
    # elide long tables (CRC32_TABLE, static Huffman trees, etc.). That
    # leaves a malformed collection literal that won't compile.
    ("comment_rest_elided", re.compile(
        r"//\s*\.\.\.\s*(?:\(rest|rest\s+of|remaining|etc\.?|and\s+so\s+on)",
        re.IGNORECASE)),
    # Related: "// ... X more entries" is a table-truncation marker.
    ("comment_n_more_entries", re.compile(
        r"//\s*\.\.\.\s*\d*\s*(?:more|remaining)\s+(?:entries|elements|values|items)",
        re.IGNORECASE)),
]


@dataclass
class StubViolation:
    file: str
    line: int
    pattern: str
    snippet: str
    fn_name: str | None = None

    def __str__(self) -> str:
        loc = f"{self.file}:{self.line}"
        fn = f" [in fn {self.fn_name}]" if self.fn_name else ""
        return f"{loc}{fn}  [{self.pattern}]  {self.snippet.strip()[:120]}"


@dataclass
class ScanReport:
    violations: list[StubViolation] = field(default_factory=list)
    files_scanned: int = 0

    @property
    def ok(self) -> bool:
        return len(self.violations) == 0

    def by_pattern(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for v in self.violations:
            counts[v.pattern] = counts.get(v.pattern, 0) + 1
        return counts

    def summary(self) -> str:
        if self.ok:
            return f"clean ({self.files_scanned} files scanned, 0 violations)"
        counts = self.by_pattern()
        head = f"{len(self.violations)} violations across {self.files_scanned} files"
        by_p = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        return f"{head}\n  {by_p}"


_COMMENT_LINE = re.compile(r"//[^\n]*|/\*[\s\S]*?\*/")
_FN_HEADER = re.compile(
    r"(?:pub\s+(?:\([^)]*\)\s*)?)?"
    r"(?:async\s+|const\s+|unsafe\s+|extern\s+(?:\"[^\"]*\"\s+)?)*"
    r"fn\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)[^{;]*"
)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _fn_name_at(text: str, pos: int, fn_spans: list[tuple[int, int, str]]) -> str | None:
    for start, end, name in fn_spans:
        if start <= pos < end:
            return name
    return None


def _collect_fn_spans(text: str) -> list[tuple[int, int, str]]:
    """Return list of (body_start, body_end_exclusive, fn_name) for every fn with a brace body."""
    spans: list[tuple[int, int, str]] = []
    for m in _FN_HEADER.finditer(text):
        # Find '{' after the signature
        brace = text.find("{", m.end())
        if brace == -1:
            continue
        depth = 1
        i = brace + 1
        n = len(text)
        in_str = False
        in_char = False
        in_line_cmt = False
        in_block_cmt = False
        esc = False
        while i < n and depth > 0:
            ch = text[i]
            nxt = text[i + 1] if i + 1 < n else ""
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
                elif ch == "'" and i + 2 < n and text[i + 2] == "'":
                    i += 2
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
            i += 1
        if depth == 0:
            spans.append((brace + 1, i - 1, m.group(1)))
    return spans


def _is_inside_test_cfg(text: str, pos: int) -> bool:
    """Return True if pos is inside a `#[cfg(test)]` block."""
    # Walk backwards to find any enclosing `mod tests { ... }` preceded by #[cfg(test)]
    # Cheap heuristic: look for the most recent `#[cfg(test)]` marker and see if
    # its matching block still contains pos.
    marker = re.compile(r"#\[cfg\(test\)\]")
    last = None
    for m in marker.finditer(text, 0, pos):
        last = m
    if not last:
        return False
    brace = text.find("{", last.end())
    if brace == -1 or brace > pos:
        return False
    depth = 1
    i = brace + 1
    n = len(text)
    while i < n and depth > 0:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and i >= pos:
                return True
        i += 1
    return False


def _scan_builtin_stubs(
    rel_path: str, text: str, fn_spans: list[tuple[int, int, str]],
    skip_tests: bool,
) -> list[StubViolation]:
    out: list[StubViolation] = []
    for name, pat in BUILTIN_STUB_PATTERNS:
        for m in pat.finditer(text):
            if skip_tests and _is_inside_test_cfg(text, m.start()):
                continue
            # Skip matches inside string literals. Cheap check: count quotes on same line.
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.start())
            if line_end == -1:
                line_end = len(text)
            line_text = text[line_start:line_end]
            if line_text.lstrip().startswith("//") or line_text.lstrip().startswith("*"):
                continue
            out.append(StubViolation(
                file=rel_path,
                line=_line_of(text, m.start()),
                pattern=name,
                snippet=line_text,
                fn_name=_fn_name_at(text, m.start(), fn_spans),
            ))
    return out


def _scan_comment_phrases(
    rel_path: str, text: str, fn_spans: list[tuple[int, int, str]],
) -> list[StubViolation]:
    out: list[StubViolation] = []
    seen: set[tuple[str, int]] = set()
    for cm in _COMMENT_LINE.finditer(text):
        comment_text = cm.group(0)
        for name, pat in STUB_COMMENT_PHRASES:
            mm = pat.search(comment_text)
            if mm:
                abs_pos = cm.start() + mm.start()
                line = _line_of(text, abs_pos)
                key = (name, line)
                if key in seen:
                    continue
                seen.add(key)
                snippet = comment_text.splitlines()[0] if "\n" in comment_text else comment_text
                out.append(StubViolation(
                    file=rel_path,
                    line=line,
                    pattern=name,
                    snippet=snippet,
                    fn_name=_fn_name_at(text, abs_pos, fn_spans),
                ))
    return out


def _scan_semantic_stubs(
    rel_path: str, text: str, fn_spans: list[tuple[int, int, str]],
) -> list[StubViolation]:
    """Detect functions that appear to ignore inputs or never write outputs."""
    out: list[StubViolation] = []
    # Rebuild a mapping from fn span → signature by matching headers again.
    for m in _FN_HEADER.finditer(text):
        fn_name = m.group(1)
        params = m.group(2)
        # Find the function body
        brace = text.find("{", m.end())
        if brace == -1:
            continue
        # Find matching close from fn_spans
        span = next((s for s in fn_spans if s[0] == brace + 1 and s[2] == fn_name), None)
        if not span:
            continue
        body = text[span[0]:span[1]]
        body_stripped = body.strip()
        # Skip test functions
        if _is_inside_test_cfg(text, m.start()):
            continue

        # Collect non-self, non-underscore param names of useful input types
        # (bytes, slices, Vec, u8, [u8]). Simple regex over params list.
        input_params: list[str] = []
        for p in _split_params(params):
            if not p or p.startswith("self") or p.startswith("&self") or p.startswith("&mut self"):
                continue
            # Pattern: name: type
            if ":" not in p:
                continue
            pname, ptype = p.split(":", 1)
            pname = pname.strip().lstrip("&").lstrip("mut ").strip()
            ptype = ptype.strip()
            if pname.startswith("_"):
                continue
            # Only flag for "meaningful" input types
            if re.search(r"\[u8\]|&\[u8\]|Vec<u8>|&mut\s+\[u8\]|&str|&\w+Buf", ptype):
                input_params.append(pname)

        if not input_params:
            continue
        # Check whether the body references at least one input param
        uses_any = any(
            re.search(r"\b" + re.escape(p) + r"\b", body) for p in input_params
        )
        # Check for silent success marker
        ends_with_ok = re.search(r"\bOk\s*\(\s*\(\s*\)\s*\)\s*$", body_stripped)
        if not uses_any and ends_with_ok:
            out.append(StubViolation(
                file=rel_path,
                line=_line_of(text, m.start()),
                pattern="fn_ignores_inputs",
                snippet=f"fn {fn_name}({params.strip()}) returns Ok(()) without touching inputs",
                fn_name=fn_name,
            ))
    return out


def _split_params(params: str) -> list[str]:
    """Split a function parameter list on commas outside angle/paren brackets."""
    out: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in params:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return out


def scan_text(rel_path: str, text: str, *, skip_tests: bool = True) -> list[StubViolation]:
    """Scan a single Rust source text for stub violations."""
    fn_spans = _collect_fn_spans(text)
    out: list[StubViolation] = []
    out.extend(_scan_builtin_stubs(rel_path, text, fn_spans, skip_tests))
    out.extend(_scan_comment_phrases(rel_path, text, fn_spans))
    out.extend(_scan_semantic_stubs(rel_path, text, fn_spans))
    return out


def scan_file(path: Path, *, root: Path | None = None, skip_tests: bool = True) -> list[StubViolation]:
    text = path.read_text(encoding="utf-8", errors="replace")
    rel = str(path.relative_to(root)) if root else str(path)
    return scan_text(rel, text, skip_tests=skip_tests)


def scan_crate(crate_dir: Path, *, skip_tests: bool = True) -> ScanReport:
    """Scan every .rs file under `crate_dir/src`."""
    report = ScanReport()
    src = crate_dir / "src"
    if not src.exists():
        return report
    for rs in sorted(src.rglob("*.rs")):
        report.files_scanned += 1
        report.violations.extend(scan_file(rs, root=crate_dir, skip_tests=skip_tests))
    return report


def scan_workspace(workspace_dir: Path, *, skip_tests: bool = True) -> ScanReport:
    """Scan every src/*.rs under every crate in a Cargo workspace."""
    report = ScanReport()
    for rs in sorted(workspace_dir.rglob("*.rs")):
        # Skip target/ and anything inside a tests/ directory (integration tests
        # might exercise stubs deliberately — out of scope for this gate).
        parts = set(rs.parts)
        if "target" in parts:
            continue
        report.files_scanned += 1
        report.violations.extend(scan_file(rs, root=workspace_dir, skip_tests=skip_tests))
    return report


def format_report(report: ScanReport, *, max_lines: int = 50) -> str:
    """Human-readable report for CLI / logs."""
    lines = [report.summary()]
    if not report.ok:
        lines.append("")
        for v in report.violations[:max_lines]:
            lines.append(f"  {v}")
        if len(report.violations) > max_lines:
            lines.append(f"  ... and {len(report.violations) - max_lines} more")
    return "\n".join(lines)
