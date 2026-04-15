"""Auto-generate Rust FFI bindings for a C library.

Given a set of C public functions (name + return type + params) and the
directory containing their implementation sources, this module produces:

1. A compiled shared library (via gcc/MinGW `gcc -shared`).
2. A Rust source file with `unsafe extern "C"` declarations.
3. Safe Rust wrappers where straightforward mappings exist.
4. A `build.rs` that tells cargo where to find the DLL.

The emitted bindings follow the same pattern used in verify/diff_test/ which
has been proven correct against zlib1.dll.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# C → Rust type mapping
# ---------------------------------------------------------------------------

# Scalar type mapping. Pointers and typedefs handled separately.
C_SCALAR_MAP: dict[str, str] = {
    "void": "()",
    "char": "c_char",
    "signed char": "c_schar",
    "unsigned char": "c_uchar",
    "short": "c_short",
    "short int": "c_short",
    "unsigned short": "c_ushort",
    "unsigned short int": "c_ushort",
    "int": "c_int",
    "signed int": "c_int",
    "unsigned": "c_uint",
    "unsigned int": "c_uint",
    "long": "c_long",
    "long int": "c_long",
    "unsigned long": "c_ulong",
    "unsigned long int": "c_ulong",
    "long long": "c_longlong",
    "long long int": "c_longlong",
    "unsigned long long": "c_ulonglong",
    "float": "c_float",
    "double": "c_double",
    "size_t": "usize",
    "ssize_t": "isize",
    "ptrdiff_t": "isize",
    "intptr_t": "isize",
    "uintptr_t": "usize",
    "int8_t": "i8",
    "int16_t": "i16",
    "int32_t": "i32",
    "int64_t": "i64",
    "uint8_t": "u8",
    "uint16_t": "u16",
    "uint32_t": "u32",
    "uint64_t": "u64",
    # zlib-specific
    "Byte": "c_uchar",
    "Bytef": "c_uchar",
    "uInt": "c_uint",
    "uIntf": "c_uint",
    "uLong": "c_ulong",
    "uLongf": "c_ulong",
    "voidp": "*mut c_void",
    "voidpf": "*mut c_void",
    "voidpc": "*const c_void",
    "z_off_t": "c_long",
    # Booleans (C99)
    "_Bool": "bool",
    "bool": "bool",
}


@dataclass
class CType:
    """Parsed C type: base name + const-ness + pointer depth + array-ness."""
    base: str
    is_const: bool = False
    pointer_depth: int = 0
    is_array: bool = False
    raw: str = ""

    @classmethod
    def parse(cls, type_str: str) -> "CType":
        raw = type_str.strip()
        s = raw
        # Strip trailing array markers — treat as pointer
        is_array = False
        while s.endswith("]"):
            idx = s.rfind("[")
            if idx < 0:
                break
            s = s[:idx].strip()
            is_array = True
        is_const = False
        # Leading / trailing const
        # Normalize `const T *` and `T const *`
        if s.startswith("const "):
            is_const = True
            s = s[len("const "):]
        elif re.match(r"\w[\w\s]*\sconst\b", s):
            is_const = True
            s = re.sub(r"\sconst\b", "", s, count=1)
        # Pointer depth
        pointer_depth = 0
        while s.endswith("*"):
            pointer_depth += 1
            s = s[:-1].strip()
            # handle `char * const` too — skip const qualifier after star
            if s.endswith("const"):
                s = s[:-len("const")].strip()
        base = s.strip()
        return cls(
            base=base,
            is_const=is_const,
            pointer_depth=pointer_depth + (1 if is_array else 0),
            is_array=is_array,
            raw=raw,
        )


@dataclass
class CSignature:
    """A C function signature."""
    name: str
    return_type: str
    params: list[tuple[str, str]] = field(default_factory=list)
    # (param_name, param_type_string)


@dataclass
class TypedefMap:
    """Explicit overrides: C typedef name → Rust type expression."""
    entries: dict[str, str] = field(default_factory=dict)

    def resolve(self, c_type: str) -> str | None:
        return self.entries.get(c_type)


def map_c_type_to_rust(
    type_str: str,
    typedefs: TypedefMap | None = None,
    opaque_types: set[str] | None = None,
) -> str:
    """Translate a C type string to a Rust FFI type expression."""
    typedefs = typedefs or TypedefMap()
    opaque_types = opaque_types or set()
    parsed = CType.parse(type_str)
    base = parsed.base

    # Strip struct/union keywords
    for kw in ("struct ", "union ", "enum "):
        if base.startswith(kw):
            base = base[len(kw):].strip()

    # Pointer to void is always c_void
    if base == "void" and parsed.pointer_depth > 0:
        rust = "c_void"
    elif base in C_SCALAR_MAP:
        rust = C_SCALAR_MAP[base]
    elif typedefs.resolve(base):
        rust = typedefs.resolve(base) or base
    elif base in opaque_types:
        rust = base
    else:
        # Unknown typedef — emit as opaque type name (caller must provide a
        # `pub enum X {}` if they want a truly opaque handle, otherwise we
        # fall back to c_void for pointer-only usages).
        if parsed.pointer_depth > 0:
            rust = "c_void"
        else:
            rust = base  # will likely fail to compile if truly unknown — good

    # Apply pointer depth
    for _ in range(parsed.pointer_depth):
        rust = f"{'*const' if parsed.is_const else '*mut'} {rust}"
        # After first pointer, inner const-ness no longer applies at top level
        parsed = CType(base=rust, is_const=False, pointer_depth=0)

    # Void return type is `()` — omit or represent as `()`
    if rust == "()":
        return rust
    return rust.strip()


# ---------------------------------------------------------------------------
# C source parsing (minimal — enough for zlib-style public APIs)
# ---------------------------------------------------------------------------

_FN_PROTO = re.compile(
    r"""
    (?P<ret>[\w\s\*]+?)\s+           # return type (non-greedy)
    (?P<name>[A-Za-z_]\w*)\s*        # fn name
    \(\s*(?P<params>[^;{}]*?)\s*\)   # params
    \s*;                              # terminating semicolon (prototype only)
    """,
    re.VERBOSE | re.MULTILINE,
)


def parse_header(header_text: str, *, only_names: set[str] | None = None) -> list[CSignature]:
    """Extract C function prototypes from a header file.

    Only top-level declarations are parsed. Macro-heavy or function-like
    macros are skipped. Use `only_names` to restrict to specific fns.
    """
    # Strip line/block comments before parsing
    stripped = _strip_comments(header_text)
    signatures: list[CSignature] = []
    for m in _FN_PROTO.finditer(stripped):
        name = m.group("name")
        if only_names and name not in only_names:
            continue
        ret = m.group("ret").strip()
        # Skip storage specifiers
        for kw in ("extern", "static", "inline", "ZEXTERN", "ZEXPORT", "OF", "ZLIB_EXTERN"):
            ret = re.sub(rf"\b{kw}\b", "", ret).strip()
        params_raw = m.group("params").strip()
        params = _parse_params(params_raw)
        signatures.append(CSignature(name=name, return_type=ret, params=params))
    return signatures


def _strip_comments(text: str) -> str:
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return text


def _parse_params(params_str: str) -> list[tuple[str, str]]:
    if not params_str or params_str.strip() in ("", "void"):
        return []
    parts = _split_on_top_level_commas(params_str)
    out: list[tuple[str, str]] = []
    for idx, p in enumerate(parts):
        p = p.strip()
        if not p:
            continue
        # last word = param name (if it's an identifier)
        m = re.match(r"(.*?)([A-Za-z_]\w*)$", p)
        if m and m.group(1).strip():
            ctype = m.group(1).strip()
            name = m.group(2).strip()
            # handle `int buf[]` — strip trailing `[]` from name
            name = re.sub(r"\[\s*\d*\s*\]$", "", name)
            out.append((name, ctype))
        else:
            # unnamed param — synthesize
            out.append((f"arg{idx}", p))
    return out


def _split_on_top_level_commas(s: str) -> list[str]:
    depth = 0
    buf: list[str] = []
    out: list[str] = []
    for ch in s:
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


# ---------------------------------------------------------------------------
# DLL build
# ---------------------------------------------------------------------------

@dataclass
class DllBuildResult:
    success: bool
    dll_path: Path | None = None
    import_lib: Path | None = None
    stdout: str = ""
    stderr: str = ""
    command: list[str] = field(default_factory=list)


def build_c_dll(
    c_sources: list[Path],
    output_dll: Path,
    *,
    include_dirs: list[Path] | None = None,
    extra_cflags: list[str] | None = None,
    compiler: str = "gcc",
    timeout: int = 120,
) -> DllBuildResult:
    """Compile a set of .c files into a shared library.

    On Windows with MinGW, also writes an import library alongside the DLL
    using `-Wl,--out-implib`.
    """
    include_dirs = include_dirs or []
    extra_cflags = extra_cflags or []
    output_dll.parent.mkdir(parents=True, exist_ok=True)

    if shutil.which(compiler) is None:
        return DllBuildResult(
            success=False,
            stderr=f"compiler {compiler!r} not found on PATH",
        )

    cmd: list[str] = [compiler, "-shared", "-O2", "-fPIC"]
    if extra_cflags:
        cmd.extend(extra_cflags)
    for inc in include_dirs:
        cmd.extend(["-I", str(inc)])
    cmd.extend(str(s) for s in c_sources)
    cmd.extend(["-o", str(output_dll)])

    # Request an import library on Windows. gcc ignores this flag on non-Windows.
    if output_dll.suffix.lower() in (".dll", ""):
        import_lib = output_dll.with_suffix(".dll.a")
        cmd.extend(["-Wl,--out-implib," + str(import_lib)])
    else:
        import_lib = None

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        success = result.returncode == 0 and output_dll.exists()
        return DllBuildResult(
            success=success,
            dll_path=output_dll if success else None,
            import_lib=import_lib if (import_lib and import_lib.exists()) else None,
            stdout=result.stdout,
            stderr=result.stderr,
            command=cmd,
        )
    except subprocess.TimeoutExpired as e:
        return DllBuildResult(success=False, stderr=f"compile timeout: {e}")
    except Exception as e:
        return DllBuildResult(success=False, stderr=f"compile failure: {e}")


# ---------------------------------------------------------------------------
# Rust FFI code emission
# ---------------------------------------------------------------------------

_RUST_KEYWORDS = {
    "as", "break", "const", "continue", "crate", "else", "enum", "extern",
    "false", "fn", "for", "if", "impl", "in", "let", "loop", "match", "mod",
    "move", "mut", "pub", "ref", "return", "self", "Self", "static", "struct",
    "super", "trait", "true", "type", "unsafe", "use", "where", "while",
    "async", "await", "dyn", "abstract", "become", "box", "do", "final",
    "macro", "override", "priv", "typeof", "unsized", "virtual", "yield", "try",
}


def _rust_safe_ident(name: str) -> str:
    if name in _RUST_KEYWORDS:
        return f"r#{name}"
    return name


def emit_ffi_module(
    signatures: list[CSignature],
    *,
    typedefs: TypedefMap | None = None,
    opaque_types: set[str] | None = None,
    module_doc: str | None = None,
) -> str:
    """Produce a Rust source string containing extern declarations and safe-ish wrappers."""
    typedefs = typedefs or TypedefMap()
    opaque_types = opaque_types or set()

    lines: list[str] = []
    if module_doc:
        for l in module_doc.splitlines():
            lines.append(f"//! {l}")
        lines.append("")
    lines.append("#![allow(non_camel_case_types, non_snake_case, dead_code)]")
    lines.append("")
    lines.append("use std::os::raw::{c_char, c_double, c_float, c_int, c_long, c_longlong, c_schar, c_short, c_uchar, c_uint, c_ulong, c_ulonglong, c_ushort, c_void};")
    lines.append("")

    # Opaque types
    for t in sorted(opaque_types):
        lines.append(f"#[repr(C)]")
        lines.append(f"pub struct {t} {{ _private: [u8; 0] }}")
    if opaque_types:
        lines.append("")

    # extern "C" block
    lines.append('unsafe extern "C" {')
    for sig in signatures:
        ret_rust = map_c_type_to_rust(sig.return_type, typedefs, opaque_types)
        params_rust: list[str] = []
        for p_name, p_type in sig.params:
            t = map_c_type_to_rust(p_type, typedefs, opaque_types)
            safe_name = _rust_safe_ident(p_name) if p_name else f"arg{len(params_rust)}"
            params_rust.append(f"{safe_name}: {t}")
        ret_part = f" -> {ret_rust}" if ret_rust not in ("()", "") else ""
        lines.append(f"    pub fn {sig.name}({', '.join(params_rust)}){ret_part};")
    lines.append("}")
    lines.append("")

    return "\n".join(lines) + "\n"


def emit_build_rs(dll_search_dir: Path, lib_name: str) -> str:
    """Produce a build.rs that points cargo at the DLL and its import library."""
    # Use forward slashes for cross-platform paths in the emitted source
    search = str(dll_search_dir).replace("\\", "/")
    return (
        "fn main() {\n"
        f'    println!("cargo:rustc-link-search=native={search}");\n'
        f'    println!("cargo:rustc-link-lib=dylib={lib_name}");\n'
        "}\n"
    )


def emit_cargo_toml(
    package_name: str,
    *,
    rust_deps: list[tuple[str, str]] | None = None,
    edition: str = "2021",
) -> str:
    rust_deps = rust_deps or []
    lines = [
        "[package]",
        f'name = "{package_name}"',
        'version = "0.1.0"',
        f'edition = "{edition}"',
        "",
        "[dependencies]",
    ]
    for name, spec in rust_deps:
        lines.append(f'{name} = {spec}')
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# High-level convenience entry
# ---------------------------------------------------------------------------

@dataclass
class AutoFfiRequest:
    c_sources: list[Path]
    include_dirs: list[Path]
    public_signatures: list[CSignature]
    output_dir: Path
    crate_name: str = "c_reference"
    lib_name: str = "c_reference"
    typedefs: TypedefMap = field(default_factory=TypedefMap)
    opaque_types: set[str] = field(default_factory=set)


@dataclass
class AutoFfiResult:
    build: DllBuildResult
    rust_path: Path
    build_rs_path: Path
    cargo_toml_path: Path


def generate_ffi_crate(req: AutoFfiRequest) -> AutoFfiResult:
    """Build a complete, self-contained FFI crate that links to the compiled C library."""
    req.output_dir.mkdir(parents=True, exist_ok=True)
    dll_path = req.output_dir / f"{req.lib_name}.dll"
    build_result = build_c_dll(req.c_sources, dll_path, include_dirs=req.include_dirs)

    src_dir = req.output_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    ffi_code = emit_ffi_module(
        req.public_signatures,
        typedefs=req.typedefs,
        opaque_types=req.opaque_types,
        module_doc=(
            f"FFI bindings for C library '{req.lib_name}'. "
            "Auto-generated by alchemist.verifier.auto_ffi."
        ),
    )
    rust_path = src_dir / "lib.rs"
    rust_path.write_text(ffi_code, encoding="utf-8")

    build_rs = req.output_dir / "build.rs"
    build_rs.write_text(emit_build_rs(req.output_dir, req.lib_name), encoding="utf-8")

    cargo = req.output_dir / "Cargo.toml"
    cargo.write_text(emit_cargo_toml(req.crate_name), encoding="utf-8")

    return AutoFfiResult(
        build=build_result,
        rust_path=rust_path,
        build_rs_path=build_rs,
        cargo_toml_path=cargo,
    )
