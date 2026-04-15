"""Escape-valve: opt-in fallback to a stronger model for holistic fixes.

~5% of functions resist the local model after exhausting the per-fn TDD
loop + multi-sample + decomposed generation. For these last-mile cases
an escape valve lets the user opt in to a single holistic fix call via a
remote/stronger model endpoint.

The remote model is NOT called by default. The user must:
  1. Set ALCHEMIST_ESCAPE_ENDPOINT to the remote endpoint URL.
  2. Optionally set ALCHEMIST_ESCAPE_API_KEY for auth.
  3. Set ALCHEMIST_ESCAPE_MODEL to the model name.

When configured, TDDGenerator escalates to this fixer after the local
holistic fixer also fails. When NOT configured, the pipeline reports the
function as unresolved — it never silently calls a cloud API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from alchemist.implementer.holistic import HolisticFixer, HolisticResult
from alchemist.llm.client import AlchemistLLM


@dataclass
class EscapeValveConfig:
    endpoint: str = ""
    api_key: str = ""
    model: str = ""

    @classmethod
    def from_env(cls) -> "EscapeValveConfig":
        return cls(
            endpoint=os.environ.get("ALCHEMIST_ESCAPE_ENDPOINT", ""),
            api_key=os.environ.get("ALCHEMIST_ESCAPE_API_KEY", ""),
            model=os.environ.get("ALCHEMIST_ESCAPE_MODEL", ""),
        )

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.model)


class EscapeValveLLM:
    """Minimal LLM client that talks to the escape-valve endpoint.

    Reuses AlchemistLLM's structured-output logic but points at a
    different server. The nonce/cache-buster still applies — remote
    endpoints may cache too.
    """

    def __init__(self, config: EscapeValveConfig):
        if not config.configured:
            raise ValueError("escape valve not configured — set ALCHEMIST_ESCAPE_ENDPOINT + ALCHEMIST_ESCAPE_MODEL")
        self._endpoint = config.endpoint.rstrip("/")
        self._model = config.model
        self._api_key = config.api_key
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._client = httpx.Client(timeout=300, headers=headers)
        self.total_cost = 0.0

    def create_cached_context(self, system_text, project_context=""):
        from alchemist.llm.client import CachedContext
        return CachedContext(system_prompt=system_text, project_context=project_context)

    def call_structured(self, messages, tool_name, tool_schema,
                        cached_context=None, max_tokens=16000,
                        temperature=0.15, **kwargs):
        """Forward to the remote endpoint using the same protocol as AlchemistLLM."""
        import json
        import secrets
        import time
        from alchemist.llm.client import LLMResponse

        nonce = secrets.token_hex(4)
        full_messages = []
        if cached_context:
            full_messages.append({"role": "system", "content": cached_context.full_system})
        schema_str = json.dumps(tool_schema, indent=2)
        augmented = list(messages)
        if augmented and augmented[-1]["role"] == "user":
            augmented[-1] = dict(augmented[-1])
            augmented[-1]["content"] += (
                f"\n\nReturn ONLY valid JSON matching:\n```json\n{schema_str}\n```"
            )
        nonce_injected = False
        for m in augmented:
            mc = dict(m)
            if not nonce_injected and mc.get("role") == "user":
                mc["content"] = f"[req-{nonce}]\n\n{mc['content']}"
                nonce_injected = True
            full_messages.append(mc)

        payload = {
            "model": self._model,
            "messages": full_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        start = time.monotonic()
        try:
            resp = self._client.post(f"{self._endpoint}/chat/completions", json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            return LLMResponse(content=f"ERROR: {e}", duration_ms=int((time.monotonic() - start) * 1000))

        data = resp.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or msg.get("reasoning") or ""
        usage = data.get("usage", {})

        structured = None
        try:
            structured = json.loads(content)
        except json.JSONDecodeError:
            import re
            content_clean = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            if content_clean.startswith("```"):
                content_clean = re.sub(r"^```(?:\w+)?\s*", "", content_clean)
                content_clean = re.sub(r"```\s*$", "", content_clean)
            try:
                structured = json.loads(content_clean)
            except json.JSONDecodeError:
                pass

        return LLMResponse(
            content=content,
            structured=structured,
            model=self._model,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            duration_ms=int((time.monotonic() - start) * 1000),
        )


def try_escape_valve(
    crate_dir,
    *,
    spec_context: str = "",
    error_context: str = "",
    config: EscapeValveConfig | None = None,
) -> HolisticResult | None:
    """Attempt one holistic fix via the escape-valve model.

    Returns None if the escape valve is not configured (never silently
    calls a remote API). Returns HolisticResult otherwise.
    """
    config = config or EscapeValveConfig.from_env()
    if not config.configured:
        return None
    llm = EscapeValveLLM(config)
    fixer = HolisticFixer(llm=llm, max_iter=1, reject_stubs=True)
    return fixer.fix_crate(
        crate_dir,
        spec_context=spec_context,
        extra_error_ctx=error_context,
    )
