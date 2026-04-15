"""Helpers for converting Pydantic models to tool_use schemas.

This module bridges Pydantic model definitions to Claude's tool_use format,
enabling structured output from LLM calls.
"""

from __future__ import annotations

from typing import Any, get_type_hints

from pydantic import BaseModel


def pydantic_to_tool_schema(model_class: type[BaseModel]) -> dict:
    """Convert a Pydantic model to a JSON Schema suitable for Claude tool_use.

    Claude's tool_use accepts JSON Schema for the input_schema field.
    Pydantic's model_json_schema() produces compatible output.
    """
    schema = model_class.model_json_schema()

    # Remove $defs reference wrapper if present — inline definitions
    # Claude handles flat schemas better than deeply nested $ref schemas
    if "$defs" in schema:
        schema = _resolve_refs(schema, schema.get("$defs", {}))
        schema.pop("$defs", None)

    return schema


def _resolve_refs(obj: Any, defs: dict) -> Any:
    """Recursively resolve $ref pointers in a JSON Schema."""
    if isinstance(obj, dict):
        if "$ref" in obj:
            ref_path = obj["$ref"]
            # Format: #/$defs/ClassName
            if ref_path.startswith("#/$defs/"):
                ref_name = ref_path.split("/")[-1]
                if ref_name in defs:
                    resolved = defs[ref_name].copy()
                    # Merge any additional properties (like description)
                    for k, v in obj.items():
                        if k != "$ref":
                            resolved[k] = v
                    return _resolve_refs(resolved, defs)
            return obj
        return {k: _resolve_refs(v, defs) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_refs(item, defs) for item in obj]
    return obj


def make_extraction_messages(
    code: str,
    instruction: str,
    context: str = "",
) -> list[dict]:
    """Build chat messages for spec extraction.

    Args:
        code: The C source code to analyze
        instruction: What to extract (e.g., "Extract the algorithm specification")
        context: Additional context (e.g., module summary, related code)
    """
    parts = []
    if context:
        parts.append(f"## Context\n\n{context}\n\n")
    parts.append(f"## Source Code\n\n```c\n{code}\n```\n\n")
    parts.append(f"## Instruction\n\n{instruction}")

    return [{"role": "user", "content": "".join(parts)}]
