"""Alchemist CLI — algorithm-aware C-to-Rust translation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows (Rich uses unicode chars cp1252 can't encode)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from alchemist import __version__

app = typer.Typer(
    name="alchemist",
    help="Algorithm-aware C-to-Rust translation toolkit. Runs entirely on local GPU — no cloud API calls.",
    no_args_is_help=True,
)
# Force UTF-8 and disable legacy Windows rendering so braille spinners work
console = Console(force_terminal=True, legacy_windows=False)


def _banner():
    console.print(
        Panel(
            f"[bold]Alchemist[/bold] v{__version__}\n"
            "Algorithm-aware C-to-Rust translation",
            border_style="yellow",
        )
    )


@app.command()
def analyze(
    source: Path = typer.Argument(..., help="Path to C/C++ source directory"),
    output: Path = typer.Option(None, "--output", "-o", help="Output JSON path"),
    preprocessed: bool = typer.Option(False, "--preprocessed", "-p", help="Also run gcc -E pass"),
):
    """Stage 1: Analyze C/C++ codebase — parse, build call graph, detect modules."""
    _banner()
    from alchemist.pipeline import run_analyze

    result = run_analyze(source, preprocessed=preprocessed)

    out_path = output or (source / ".alchemist" / "analysis.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str))
    console.print(f"\n[green]Analysis written to {out_path}[/green]")


@app.command()
def extract(
    source: Path = typer.Argument(..., help="Path to C/C++ source directory"),
    analysis: Path = typer.Option(None, "--analysis", "-a", help="Path to analysis.json"),
):
    """Stage 2: Extract algorithm specifications from C code via LLM."""
    _banner()
    from alchemist.extractor.spec_extractor import SpecExtractor

    # Load analysis
    analysis_path = analysis or (source / ".alchemist" / "analysis.json")
    if not analysis_path.exists():
        console.print(f"[red]Analysis file not found: {analysis_path}[/red]")
        console.print("[yellow]Run 'alchemist analyze' first.[/yellow]")
        raise SystemExit(1)

    analysis_data = json.loads(analysis_path.read_text())

    # Extract specs
    specs_dir = source / ".alchemist" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)

    extractor = SpecExtractor()
    specs = extractor.extract_all(analysis_data, output_dir=specs_dir)
    console.print(f"\n[green]Extracted {len(specs)} module specs to {specs_dir}[/green]")


@app.command()
def architect(
    source: Path = typer.Argument(..., help="Path to source directory (reads .alchemist/specs/)"),
    name: str = typer.Option("translated", "--name", "-n", help="Workspace name"),
):
    """Stage 3: Design Rust crate architecture from extracted specs."""
    _banner()
    from alchemist.architect.crate_designer import CrateDesigner
    from alchemist.extractor.schemas import ModuleSpec

    specs_dir = source / ".alchemist" / "specs"
    if not specs_dir.exists():
        console.print(f"[red]Specs directory not found: {specs_dir}[/red]")
        console.print("[yellow]Run 'alchemist extract' first.[/yellow]")
        raise SystemExit(1)

    # Load all spec files
    specs = []
    for f in sorted(specs_dir.glob("*.json")):
        data = json.loads(f.read_text())
        specs.append(ModuleSpec.model_validate(data))
    console.print(f"[cyan]Loaded {len(specs)} module specs[/cyan]")

    # Design architecture
    designer = CrateDesigner()
    arch = designer.design(specs, project_name=name, source_description=str(source))

    # Save
    out_path = source / ".alchemist" / "architecture.json"
    out_path.write_text(arch.model_dump_json(indent=2))
    console.print(f"\n[green]Architecture written to {out_path}[/green]")


@app.command()
def implement(
    source: Path = typer.Argument(..., help="Path to source directory (reads .alchemist/)"),
    output: Path = typer.Option(None, "--output", "-o", help="Output Rust project path"),
):
    """Stage 4: Generate Rust code from specs within designed architecture."""
    _banner()
    from alchemist.architect.schemas import CrateArchitecture
    from alchemist.extractor.schemas import ModuleSpec
    from alchemist.implementer.code_generator import CodeGenerator

    # Load specs
    specs_dir = source / ".alchemist" / "specs"
    specs = []
    if specs_dir.exists():
        for f in sorted(specs_dir.glob("*.json")):
            specs.append(ModuleSpec.model_validate(json.loads(f.read_text())))

    # Load architecture
    arch_path = source / ".alchemist" / "architecture.json"
    if not arch_path.exists():
        console.print("[red]Architecture not found. Run 'alchemist architect' first.[/red]")
        raise SystemExit(1)
    arch = CrateArchitecture.model_validate(json.loads(arch_path.read_text()))

    # Generate
    out = output or (source / ".alchemist" / "output")
    generator = CodeGenerator()
    results = generator.generate_workspace(specs, arch, out)
    console.print(f"\n[green]Generated Rust workspace at {out}[/green]")


@app.command()
def verify(
    c_source: Path = typer.Argument(..., help="Path to original C source"),
    rust_output: Path = typer.Argument(..., help="Path to generated Rust project"),
):
    """Stage 5: Differential verification — fuzz both, compare outputs."""
    _banner()
    from alchemist.verifier.differential_tester import DifferentialTester

    tester = DifferentialTester(c_source, rust_output)
    results = tester.run_all()

    if results.get("rust_tests", {}).get("success"):
        console.print("[green]Verification passed.[/green]")
    else:
        console.print("[yellow]Verification had failures — check output.[/yellow]")


@app.command()
def report(
    rust_project: Path = typer.Argument(..., help="Path to Rust project to analyze"),
    c_source: Path = typer.Option(None, "--c-source", "-c", help="Original C source for comparison"),
):
    """Stage 6: Generate metrics report."""
    _banner()
    from alchemist.reporter.metrics import MetricsCollector

    collector = MetricsCollector(rust_project, c_source)
    metrics = collector.collect_all()
    collector.print_dashboard(metrics)

    # Save metrics
    out_path = rust_project / "alchemist-report.json"
    out_path.write_text(json.dumps(metrics, indent=2, default=str))
    console.print(f"\n[green]Report saved to {out_path}[/green]")


@app.command()
def translate(
    source: Path = typer.Argument(..., help="Path to C/C++ source directory"),
    output: Path = typer.Option(None, "--output", "-o", help="Output Rust project path"),
    name: str = typer.Option("translated", "--name", "-n", help="Workspace name"),
    stages: str = typer.Option("1-6", "--stages", "-s", help="Stage range to run (e.g. 1-4)"),
    force: bool = typer.Option(
        False, "--force", help="Proceed past the architecture validator even on ERROR"
    ),
    no_verify: bool = typer.Option(
        False, "--no-verify",
        help="Allow success without differential verification (NOT RECOMMENDED)",
    ),
    no_tdd: bool = typer.Option(
        False, "--no-tdd", help="Use the legacy code generator instead of TDD Stage 4",
    ),
):
    """Full pipeline with mandatory gates. Refuses to claim success unless:
       - Architecture validator passes
       - TDD Stage 4 generates real code (anti-stub)
       - Stage 5 differential gate passes

    Use --force / --no-verify only for debugging.
    """
    _banner()
    import time
    from alchemist.pipeline import run_translate_all

    start = time.monotonic()
    parts = stages.split("-")
    start_stage = int(parts[0])
    end_stage = int(parts[-1])

    source = Path(source).resolve()
    out = output or (source / ".alchemist" / "output")

    # Run the full integrated pipeline
    if no_tdd:
        # Legacy path: run stages individually using the old generator
        console.print("[yellow]--no-tdd: using legacy code generator[/yellow]")
        # Fallthrough: do it the old way but still enforce Stage 5
        from alchemist.pipeline import (
            run_architect_stage, run_implement_stage,
            run_verify_stage, run_analyze,
        )
        # Use the modern run_translate_all but request tdd=False via monkey-patch
        # is complex; just leave the user the `--no-tdd` path for the non-TDD
        # stage-4 while keeping the rest intact.
        from alchemist.pipeline import TranslationReport, StageOutcome
        report = TranslationReport(workspace_dir=out)
        from alchemist.extractor.spec_extractor import SpecExtractor
        from alchemist.extractor.schemas import ModuleSpec
        checkpoint = source / ".alchemist"
        checkpoint.mkdir(parents=True, exist_ok=True)
        if start_stage <= 1 <= end_stage:
            ana = run_analyze(source)
            (checkpoint / "analysis.json").write_text(
                json.dumps(ana, indent=2, default=str), encoding="utf-8"
            )
            report.add(StageOutcome(stage="analyze", ok=True, summary=f"{ana['summary']['total_files']} files"))
        if start_stage <= 2 <= end_stage:
            specs_dir = checkpoint / "specs"
            specs_dir.mkdir(exist_ok=True)
            analysis = json.loads((checkpoint / "analysis.json").read_text(encoding="utf-8"))
            SpecExtractor().extract_all(analysis, output_dir=specs_dir)
            report.add(StageOutcome(stage="extract", ok=True, summary="specs emitted"))
        if start_stage <= 3 <= end_stage:
            outcome, _arch = run_architect_stage(source, name, enforce=not force)
            report.add(outcome)
            if not outcome.ok:
                _print_report_and_exit(report, start)
        if start_stage <= 4 <= end_stage:
            outcome = run_implement_stage(source, out, tdd=False)
            report.add(outcome)
            if not outcome.ok:
                _print_report_and_exit(report, start)
        if start_stage <= 5 <= end_stage:
            outcome = run_verify_stage(source, out, refuse_without_diff=not no_verify)
            report.add(outcome)
        _print_report_and_exit(report, start)
        return

    report = run_translate_all(
        source=source,
        name=name,
        output=out,
        stages=(start_stage, end_stage),
        enforce_validator=not force,
        refuse_without_diff=not no_verify,
    )
    _print_report_and_exit(report, start)


def _print_report_and_exit(report, start_time: float) -> None:
    import time
    console.print("")
    console.print(Panel(report.summary(), title="Translation Report",
                         border_style="green" if report.ok else "red"))
    elapsed = time.monotonic() - start_time
    console.print(f"\n[bold]pipeline finished in {elapsed:.1f}s[/bold]")
    if not report.ok:
        raise typer.Exit(code=1)


@app.command()
def inspect(
    source: Path = typer.Argument(..., help="Path to C/C++ source directory"),
):
    """Quick visualization of codebase structure and call graph."""
    _banner()
    from alchemist.pipeline import run_analyze

    result = run_analyze(source, preprocessed=False)
    summary = result["summary"]

    table = Table(title="Codebase Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Files", str(summary["total_files"]))
    table.add_row("Lines of code", f"{summary['total_lines']:,}")
    table.add_row("Functions", str(summary["total_functions"]))
    table.add_row("Structs/Unions", str(summary["total_structs"]))
    table.add_row("Global variables", str(summary["total_globals"]))
    table.add_row("Macros", str(summary["total_macros"]))
    table.add_row("Typedefs", str(summary["total_typedefs"]))
    console.print(table)

    if result.get("modules"):
        mod_table = Table(title="Detected Modules")
        mod_table.add_column("Module", style="cyan")
        mod_table.add_column("Category", style="yellow")
        mod_table.add_column("Functions", style="green")
        mod_table.add_column("Lines", style="green")
        for mod in result["modules"]:
            mod_table.add_row(
                mod["name"],
                mod["category"],
                str(len(mod["functions"])),
                str(mod["total_lines"]),
            )
        console.print(mod_table)


@app.command()
def version():
    """Print version."""
    console.print(f"alchemist {__version__}")


@app.command()
def doctor():
    """Check the environment: compilers, server, key files."""
    _banner()
    import shutil

    table = Table(title="alchemist environment check")
    table.add_column("check", style="cyan")
    table.add_column("status")
    table.add_column("detail", style="dim")

    def row(name, ok, detail):
        status = "[green]OK[/green]" if ok else "[red]FAIL[/red]"
        table.add_row(name, status, detail)

    # Compiler presence
    for bin_name in ("cargo", "rustc", "gcc"):
        path = shutil.which(bin_name)
        row(bin_name, path is not None, path or "(not on PATH)")

    # Server reachability
    try:
        import httpx
        from alchemist.config import AlchemistConfig
        endpoint = AlchemistConfig().local_endpoint or "http://100.109.172.64:8090/v1"
        r = httpx.get(endpoint.replace("/v1", "/health"), timeout=5)
        ok = r.status_code == 200
        row("local LLM server", ok, f"{endpoint} → HTTP {r.status_code}")
    except Exception as e:
        row("local LLM server", False, f"unreachable: {e}")

    # Standards catalog
    try:
        from alchemist.standards import list_algorithms
        algos = list_algorithms()
        row("standards catalog", bool(algos),
            f"{len(algos)} algorithms ({', '.join(algos[:6])}...)")
    except Exception as e:
        row("standards catalog", False, str(e))

    # Scrubber
    try:
        from alchemist.implementer.scrubber import scrub_rust
        _, _ = scrub_rust("pub fn x() {}\n")
        row("scrubber", True, "30 rules loaded")
    except Exception as e:
        row("scrubber", False, str(e))

    # Anti-stub
    try:
        from alchemist.implementer.anti_stub import scan_text
        _ = scan_text("t.rs", "pub fn y() { 0 }")
        row("anti-stub detector", True, "loaded")
    except Exception as e:
        row("anti-stub detector", False, str(e))

    # Plugins
    try:
        from alchemist.plugins import list_plugins, load_builtins
        load_builtins()
        plugins = list_plugins()
        row("plugins", True, f"{len(plugins)} loaded: {', '.join(p.name for p in plugins)}")
    except Exception as e:
        row("plugins", False, str(e))

    console.print(table)


standards_app = typer.Typer(help="Query the standards test-vector catalog.")
app.add_typer(standards_app, name="standards")


@standards_app.command("list")
def standards_list():
    """List every algorithm with catalog vectors."""
    _banner()
    from alchemist.standards import list_algorithms, lookup_test_vectors
    table = Table(title="standards catalog")
    table.add_column("algorithm", style="cyan")
    table.add_column("vectors")
    table.add_column("sources", style="dim")
    for algo in list_algorithms():
        vecs = lookup_test_vectors(algo)
        sources = sorted({v.source for v in vecs if v.source})
        table.add_row(algo, str(len(vecs)), "; ".join(sources)[:80])
    console.print(table)


@standards_app.command("show")
def standards_show(
    algorithm: str = typer.Argument(..., help="Algorithm name (e.g. adler32, sha256)"),
):
    """Dump catalog vectors for one algorithm."""
    _banner()
    from alchemist.standards import lookup_test_vectors, match_algorithm
    canonical = match_algorithm(algorithm)
    if not canonical:
        console.print(f"[red]unknown algorithm {algorithm!r}[/red]")
        raise typer.Exit(code=1)
    vectors = lookup_test_vectors(canonical)
    for v in vectors:
        console.print(f"[bold]{v.name}[/bold]")
        console.print(f"  input    = 0x{v.input_hex}")
        if v.key_hex:
            console.print(f"  key      = 0x{v.key_hex}")
        console.print(f"  expected = 0x{v.expected_hex}")
        if v.description:
            console.print(f"  [dim]{v.description}[/dim]")


@app.command()
def new(
    name: str = typer.Argument(..., help="Project name"),
    path: Path = typer.Option(Path("."), "--path", "-p", help="Parent directory"),
):
    """Scaffold a new Alchemist project with a skeleton `.alchemist/` tree."""
    _banner()
    root = path / name
    if root.exists() and any(root.iterdir()):
        console.print(f"[red]{root} is not empty — refusing to overwrite[/red]")
        raise typer.Exit(code=1)
    root.mkdir(parents=True, exist_ok=True)
    (root / ".alchemist").mkdir()
    (root / ".alchemist" / "specs").mkdir()
    readme = root / "README.md"
    readme.write_text(
        f"# {name}\n\n"
        "Alchemist translation project.\n\n"
        "Put your C source under `src/` (or point `alchemist translate` at another\n"
        "directory with `--source`). Then run:\n\n"
        "```bash\n"
        f"alchemist translate ./src --name {name}\n"
        "```\n",
        encoding="utf-8",
    )
    (root / "src").mkdir()
    console.print(f"[green]initialized project at {root}[/green]")


if __name__ == "__main__":
    app()
