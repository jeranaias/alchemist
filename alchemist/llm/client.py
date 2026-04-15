"""LLM client for Alchemist — local-first via RigRun (Qwen3.5-122B).

All inference runs on the local GPU via vLLM's OpenAI-compatible API.
No cloud API calls. No data leaves the machine.

For structured output, we use JSON mode + schema-in-prompt + Pydantic validation
instead of tool_use (which is Claude-specific).
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from alchemist.config import AlchemistConfig


# vLLM server details
DEFAULT_ENDPOINT = "http://100.109.172.64:8090/v1"
DEFAULT_MODEL = "local"


@dataclass
class LLMResponse:
    """Response from an LLM call."""
    content: str
    structured: dict | None = None
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0  # always 0 for local
    duration_ms: int = 0


@dataclass
class CachedContext:
    """System prompt for reuse across calls.

    vLLM prefix caching handles the caching automatically —
    identical prompt prefixes hit the KV cache. We just need to
    keep the system prompt consistent across calls.
    """
    system_prompt: str = ""
    project_context: str = ""

    @property
    def full_system(self) -> str:
        parts = [self.system_prompt]
        if self.project_context:
            parts.append(self.project_context)
        return "\n\n".join(parts)


class AlchemistLLM:
    """Local LLM client for Alchemist — all inference on RigRun."""

    def __init__(self, config: AlchemistConfig | None = None):
        self.config = config or AlchemistConfig()
        self._endpoint = self.config.local_endpoint or DEFAULT_ENDPOINT
        self._model = DEFAULT_MODEL
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._call_count = 0
        # 180s timeout — if a single request takes longer, something's wrong
        self._client = httpx.Client(timeout=180)

    def create_cached_context(self, system_text: str, project_context: str = "") -> CachedContext:
        """Create a system prompt context for reuse.

        vLLM's prefix caching will automatically cache the KV state
        for identical prefixes, so we just need to keep the system
        prompt consistent across calls.
        """
        return CachedContext(system_prompt=system_text, project_context=project_context)

    def wait_for_server(self, max_wait: int = 300, check_interval: int = 10) -> bool:
        """Wait for the server to be healthy, up to max_wait seconds."""
        import time as t
        deadline = t.monotonic() + max_wait
        while t.monotonic() < deadline:
            try:
                resp = self._client.get(
                    f"{self._endpoint}/models",
                    timeout=5,
                )
                if resp.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            t.sleep(check_interval)
        return False

    def call(
        self,
        messages: list[dict],
        cached_context: CachedContext | None = None,
        system_prompt: str = "",
        max_tokens: int = 8192,
        temperature: float = 0.0,
        **kwargs,
    ) -> LLMResponse:
        """Make an LLM call to the local vLLM server.

        Args:
            messages: Chat messages [{"role": "user", "content": "..."}]
            cached_context: Pre-built system prompt context
            system_prompt: Direct system prompt (used if no cached_context)
            max_tokens: Max output tokens
            temperature: Sampling temperature
        """
        start = time.monotonic()

        # Cache-buster: prepend a unique nonce to the user message.
        # Server-side response cache hashes on full message content, so a
        # unique token forces a cache miss for every call. Cheap (8 chars
        # of extra input) and deterministic.
        nonce = secrets.token_hex(4)

        # Build messages with system prompt
        full_messages = []
        sys_text = ""
        if cached_context:
            sys_text = cached_context.full_system
        elif system_prompt:
            sys_text = system_prompt

        if sys_text:
            full_messages.append({"role": "system", "content": sys_text})

        # Inject nonce into the first user message
        messages_with_nonce = []
        nonce_injected = False
        for m in messages:
            m_copy = dict(m)
            if not nonce_injected and m_copy.get("role") == "user":
                m_copy["content"] = f"[req-{nonce}]\n\n{m_copy['content']}"
                nonce_injected = True
            messages_with_nonce.append(m_copy)
        full_messages.extend(messages_with_nonce)

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": full_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        try:
            resp = self._client.post(
                f"{self._endpoint}/chat/completions",
                json=payload,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            return LLMResponse(
                content=f"ERROR: {e}",
                model=self._model,
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        data = resp.json()
        elapsed = int((time.monotonic() - start) * 1000)

        msg = data["choices"][0]["message"]
        # Qwen3.5 with reasoning parser puts output in 'reasoning' when thinking,
        # and 'content' may be null. Grab whichever has data.
        content = msg.get("content") or msg.get("reasoning") or ""
        usage = data.get("usage", {})

        self._total_input_tokens += usage.get("prompt_tokens", 0)
        self._total_output_tokens += usage.get("completion_tokens", 0)
        self._call_count += 1

        return LLMResponse(
            content=content,
            model=self._model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cost_usd=0.0,
            duration_ms=elapsed,
        )

    def call_structured(
        self,
        messages: list[dict],
        tool_name: str,
        tool_schema: dict,
        cached_context: CachedContext | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
        **kwargs,
    ) -> LLMResponse:
        """Make an LLM call that returns structured JSON.

        Instead of tool_use, we:
        1. Include the JSON schema in the prompt
        2. Ask the model to return only valid JSON
        3. Parse and validate with Pydantic on our end

        vLLM supports guided decoding (json schema enforcement) but
        we use prompt-based JSON for maximum compatibility.
        """
        # Build a schema description for the prompt
        schema_str = json.dumps(tool_schema, indent=2)

        # Append JSON instruction to the last user message
        augmented_messages = []
        for msg in messages:
            augmented_messages.append(msg.copy())

        if augmented_messages and augmented_messages[-1]["role"] == "user":
            augmented_messages[-1]["content"] += (
                f"\n\n## Required Output Format\n\n"
                f"Return ONLY a valid JSON object matching this schema. "
                f"No markdown, no explanation, no ```json fences — just the raw JSON.\n\n"
                f"Schema:\n```json\n{schema_str}\n```"
            )

        # Try with guided decoding first (vLLM feature)
        start = time.monotonic()
        sys_text = ""
        if cached_context:
            sys_text = cached_context.full_system

        # Cache-buster: unique nonce forces cache miss on every call
        nonce = secrets.token_hex(4)

        full_messages = []
        if sys_text:
            full_messages.append({"role": "system", "content": sys_text})

        # Inject nonce into first user message
        nonce_injected = False
        for m in augmented_messages:
            m_copy = dict(m)
            if not nonce_injected and m_copy.get("role") == "user":
                m_copy["content"] = f"[req-{nonce}]\n\n{m_copy['content']}"
                nonce_injected = True
            full_messages.append(m_copy)

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": full_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        try:
            resp = self._client.post(
                f"{self._endpoint}/chat/completions",
                json=payload,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            # Retry without guided_json if server doesn't support it
            payload.pop("extra_body", None)
            try:
                resp = self._client.post(
                    f"{self._endpoint}/chat/completions",
                    json=payload,
                )
                resp.raise_for_status()
            except httpx.HTTPError as e2:
                return LLMResponse(
                    content=f"ERROR: {e2}",
                    model=self._model,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

        data = resp.json()
        elapsed = int((time.monotonic() - start) * 1000)

        msg = data["choices"][0]["message"]
        content = msg.get("content") or msg.get("reasoning") or ""
        usage = data.get("usage", {})

        self._total_input_tokens += usage.get("prompt_tokens", 0)
        self._total_output_tokens += usage.get("completion_tokens", 0)
        self._call_count += 1

        # Parse JSON from response
        structured = self._extract_json(content)

        return LLMResponse(
            content=content,
            structured=structured,
            model=self._model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cost_usd=0.0,
            duration_ms=elapsed,
        )

    def _extract_json(self, text: str) -> dict | None:
        """Extract JSON from model response, handling various formats.

        Handles: clean JSON, markdown-fenced JSON, thinking-tagged responses,
        and truncated JSON (missing closing braces from token limit).
        """
        text = text.strip()

        # Strip thinking tags if present (Qwen3.5 uses <think>...</think>)
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try stripping markdown fences
        if "```" in text:
            for pattern in [r"```json\s*\n(.*?)```", r"```\s*\n(.*?)```"]:
                match = re.search(pattern, text, re.DOTALL)
                if match:
                    try:
                        return json.loads(match.group(1))
                    except json.JSONDecodeError:
                        continue

        # Find the first { and try to parse from there
        first_brace = text.find("{")
        if first_brace == -1:
            return None

        json_text = text[first_brace:]

        # Try direct parse of everything from first brace
        try:
            return json.loads(json_text)
        except json.JSONDecodeError:
            pass

        # Handle truncated JSON — model hit token limit before closing all braces
        # Strategy: count open braces/brackets and add closers
        repaired = self._repair_truncated_json(json_text)
        if repaired:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

        return None

    def _repair_truncated_json(self, text: str) -> str | None:
        """Attempt to repair truncated JSON by closing unclosed braces/brackets.

        Handles the common case where the model hit its token limit
        mid-generation. Tracks the last position where truncation would
        yield valid JSON with closers appended.
        """
        in_string = False
        escape = False
        stack: list[str] = []  # track { and [

        # Track last safe truncation point and its stack state
        best_point = -1
        best_stack: list[str] = []

        for i, ch in enumerate(text):
            if escape:
                escape = False
                continue
            if ch == "\\":
                if in_string:
                    escape = True
                continue
            if ch == '"':
                in_string = not in_string
                if not in_string:
                    # Just closed a string — safe point
                    best_point = i
                    best_stack = stack.copy()
                continue
            if in_string:
                continue

            if ch == "{":
                stack.append("{")
            elif ch == "[":
                stack.append("[")
            elif ch == "}":
                if stack and stack[-1] == "{":
                    stack.pop()
                    best_point = i
                    best_stack = stack.copy()
            elif ch == "]":
                if stack and stack[-1] == "[":
                    stack.pop()
                    best_point = i
                    best_stack = stack.copy()
            # Numbers, booleans, null — safe after them
            elif ch in "0123456789":
                best_point = i
                best_stack = stack.copy()
            elif ch == "e" and text[max(0,i-3):i+1] in ("true", "alse"):
                best_point = i
                best_stack = stack.copy()
            elif ch == "l" and text[max(0,i-3):i+1] == "null":
                best_point = i
                best_stack = stack.copy()

        if best_point <= 0 or not best_stack:
            return None  # Nothing to repair

        # Truncate to last safe point, remove trailing comma
        truncated = text[:best_point + 1].rstrip().rstrip(",")

        # Remove trailing dangling key (e.g., `, "key"` without a value)
        # This happens when best_point landed on a closing quote of a key
        truncated = re.sub(r',\s*"[^"]*"\s*$', '', truncated)
        truncated = truncated.rstrip().rstrip(",")

        # Close unclosed delimiters
        closers = "".join("}" if o == "{" else "]" for o in reversed(best_stack))

        return truncated + closers

    @property
    def total_cost(self) -> float:
        return 0.0  # always free — local GPU

    @property
    def stats(self) -> dict:
        return {
            "total_cost_usd": 0.0,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "call_count": self._call_count,
        }
