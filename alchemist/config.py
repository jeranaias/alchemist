"""Central configuration for Alchemist."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


class AlchemistConfig(BaseModel):
    """Runtime configuration — loaded from env vars, CLI flags, or .alchemist/config.toml."""

    # Local LLM endpoint (RigRun vLLM server)
    local_endpoint: str = Field(
        default_factory=lambda: os.environ.get(
            "ALCHEMIST_ENDPOINT", "http://100.109.172.64:8090/v1"
        )
    )
    model_name: str = "local"

    # Pipeline
    max_compile_iterations: int = 5
    parallel_modules: int = 4
    checkpoint_dir: str = ".alchemist"

    # Analyzer
    gcc_path: str = "gcc"
    preprocessor_flags: list[str] = Field(default_factory=list)

    # Paths
    source_dir: Path | None = None
    output_dir: Path | None = None

    def checkpoint_path(self, source: Path) -> Path:
        return source / self.checkpoint_dir
