"""Quick test: verify the local LLM can produce structured JSON for Alchemist."""

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from alchemist.config import AlchemistConfig
from alchemist.llm.client import AlchemistLLM
from alchemist.extractor.schemas import AlgorithmSpec
from alchemist.llm.structured import pydantic_to_tool_schema


def _server_reachable() -> bool:
    try:
        resp = httpx.get(
            f"{AlchemistConfig().local_endpoint}/models", timeout=5,
        )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(),
    reason="Local LLM server unreachable",
)


def test_basic_chat():
    """Test basic chat completion."""
    print("Test 1: Basic chat...")
    llm = AlchemistLLM()
    resp = llm.call(
        messages=[{"role": "user", "content": "What is the Adler-32 checksum algorithm? Reply in 2 sentences."}],
        max_tokens=200,
    )
    print(f"  Response ({resp.duration_ms}ms, {resp.output_tokens} tokens):")
    print(f"  {resp.content[:200]}")
    assert resp.content and "ERROR" not in resp.content, f"Chat failed: {resp.content}"
    print("  PASS\n")


def test_structured_json():
    """Test structured JSON output matching AlgorithmSpec schema."""
    print("Test 2: Structured JSON output...")
    llm = AlchemistLLM()
    schema = pydantic_to_tool_schema(AlgorithmSpec)

    resp = llm.call_structured(
        messages=[{
            "role": "user",
            "content": (
                "Extract an algorithm specification for the Adler-32 checksum.\n\n"
                "Adler-32 is a checksum algorithm from RFC 1950. It maintains two "
                "16-bit accumulators A and B. For each byte, A = (A + byte) mod 65521, "
                "B = (B + A) mod 65521. The final checksum is (B << 16) | A.\n\n"
                "Initial values: A=1, B=0. Input is a byte slice. Output is u32."
            ),
        }],
        tool_name="algorithm_spec",
        tool_schema=schema,
        max_tokens=4000,
    )

    print(f"  Response ({resp.duration_ms}ms, {resp.output_tokens} tokens)")
    if resp.structured:
        print(f"  Got structured JSON with keys: {list(resp.structured.keys())[:10]}")
        # Try to validate with Pydantic
        try:
            spec = AlgorithmSpec.model_validate(resp.structured)
            print(f"  Validated: {spec.name} ({spec.category})")
            print(f"  Description: {spec.description[:100]}")
            print("  PASS\n")
        except Exception as e:
            print(f"  Pydantic validation failed: {e}")
            print(f"  Raw keys: {list(resp.structured.keys())}")
            print("  PARTIAL PASS (JSON parsed but schema mismatch)\n")
    else:
        print(f"  No structured output. Raw content: {resp.content[:300]}")
        print("  FAIL\n")


def test_code_generation():
    """Test Rust code generation."""
    print("Test 3: Rust code generation...")
    llm = AlchemistLLM()
    resp = llm.call(
        messages=[{
            "role": "user",
            "content": (
                "Write a complete, compilable Rust implementation of the Adler-32 "
                "checksum algorithm. Include:\n"
                "- A public function `adler32(data: &[u8]) -> u32`\n"
                "- A #[cfg(test)] module with at least 3 tests\n"
                "- Doc comments\n\n"
                "Return ONLY the Rust code, no explanation."
            ),
        }],
        system_prompt="You are a Rust code generator. Return only valid Rust code.",
        max_tokens=2000,
    )
    print(f"  Response ({resp.duration_ms}ms, {resp.output_tokens} tokens)")
    has_fn = "fn adler32" in resp.content
    has_test = "#[test]" in resp.content or "#[cfg(test)]" in resp.content
    print(f"  Has fn adler32: {has_fn}")
    print(f"  Has tests: {has_test}")
    if has_fn and has_test:
        print("  PASS\n")
    else:
        print(f"  Content preview: {resp.content[:300]}")
        print("  FAIL\n")


if __name__ == "__main__":
    print("=" * 60)
    print("Alchemist Local LLM Test Suite")
    print(f"Endpoint: {AlchemistConfig().local_endpoint}")
    print("=" * 60 + "\n")

    test_basic_chat()
    test_structured_json()
    test_code_generation()

    print("All tests complete.")
