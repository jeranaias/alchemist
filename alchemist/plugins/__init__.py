"""Domain plugin registry.

Plugins extend Alchemist with domain-specific knowledge — extra test vectors,
extra post-generation lints (e.g., constant-time for crypto), extra
extraction hints.

A plugin is a simple Python module that exposes a module-level object
named `PLUGIN` of type `DomainPlugin`. Built-in plugins live under
`alchemist.plugins.*`. Third-party plugins can register via the
`alchemist.plugins` entry-point group in their `pyproject.toml`.

The registry is intentionally small: no dynamic dispatch magic, just a
dict of plugins by name + a couple of helpers to call their hooks.
"""

from __future__ import annotations

import importlib
import importlib.metadata as _metadata
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol


# ---------------------------------------------------------------------------
# Plugin protocol
# ---------------------------------------------------------------------------

class LintRule(Protocol):
    """A post-generation lint. Returns list of (file, line, rule_name, message)."""
    def __call__(self, workspace_dir, specs) -> list[tuple]:  # noqa: ANN001
        ...


@dataclass
class DomainPlugin:
    """A domain-specific extension to Alchemist."""
    name: str
    description: str
    # Optional callables — all default to no-ops.
    post_extract: Callable | None = None
    """Augment extracted specs (e.g., pull in NIST CAVP vectors for crypto)."""
    post_skeleton: Callable | None = None
    """Augment generated skeleton (e.g., add constant-time asserts)."""
    lints: list[LintRule] = field(default_factory=list)
    """Post-generation lints to run before declaring success."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, DomainPlugin] = {}


def register(plugin: DomainPlugin) -> None:
    """Register a plugin by name."""
    _REGISTRY[plugin.name] = plugin


def get(name: str) -> DomainPlugin | None:
    return _REGISTRY.get(name)


def list_plugins() -> list[DomainPlugin]:
    return list(_REGISTRY.values())


def clear() -> None:
    """Empty the registry (for test isolation)."""
    _REGISTRY.clear()


# ---------------------------------------------------------------------------
# Built-in + entry-point discovery
# ---------------------------------------------------------------------------

_BUILTINS = ["alchemist.plugins.crypto"]


def load_builtins() -> None:
    """Import built-in plugins so they register themselves."""
    for mod_name in _BUILTINS:
        try:
            mod = importlib.import_module(mod_name)
            plugin = getattr(mod, "PLUGIN", None)
            if isinstance(plugin, DomainPlugin):
                register(plugin)
        except Exception:  # noqa: BLE001
            # Plugins must never crash Alchemist startup.
            continue


def load_entry_points(group: str = "alchemist.plugins") -> None:
    """Load plugins declared via the `alchemist.plugins` entry-point group."""
    try:
        eps = _metadata.entry_points(group=group)
    except TypeError:
        # Older API: returns dict
        eps = _metadata.entry_points().get(group, [])  # type: ignore[attr-defined]
    for ep in eps:
        try:
            plugin = ep.load()
            if isinstance(plugin, DomainPlugin):
                register(plugin)
        except Exception:  # noqa: BLE001
            continue


def load_all() -> None:
    """Convenience: load built-ins + entry-points."""
    load_builtins()
    load_entry_points()


# ---------------------------------------------------------------------------
# Orchestration helpers
# ---------------------------------------------------------------------------

def run_lints(workspace_dir, specs) -> list[tuple]:  # noqa: ANN001
    """Run every registered plugin's lints and aggregate the findings."""
    findings: list[tuple] = []
    for plugin in _REGISTRY.values():
        for lint in plugin.lints:
            try:
                out = lint(workspace_dir, specs) or []
                findings.extend(out)
            except Exception as e:  # noqa: BLE001
                findings.append((
                    "(plugin-error)", 0, f"{plugin.name}.lint_crash",
                    f"lint raised: {e}",
                ))
    return findings
