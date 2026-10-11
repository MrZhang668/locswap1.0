"""LLM connectivity smoke test: fire one minimal request per path using the real .env config.

Verifies two paths (burns a small number of real tokens):
  1. complete_json — the "JSON output + schema validation" that call sites A/B rely on;
  2. create_with_tools — the tool-call round trip that call site C (seller advisor) relies on.

Run:
    python scripts/smoke_llm.py
"""

from __future__ import annotations

import sys

import _bootstrap  # noqa: F401

from pydantic import BaseModel, ConfigDict

from app.config import settings
from app.services.llm.client import get_llm_client


class Echo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: str
    country: str


def main() -> int:
    client = get_llm_client()
    print(f"provider={settings.llm_provider}  base_url={settings.llm_base_url or '(official)'}")
    print(f"model_main={settings.llm_model_main}  model_light={settings.llm_model_light}")
    if not client.available:
        print(f"[FAIL] LLM unavailable: {client.unavailable_reason}")
        return 1

    # ---- 1. complete_json ----
    r = client.complete_json(
        call_site="smoke.json",
        model=settings.llm_model_light,
        system="You output only a JSON object, with no explanation.",
        content=[{"type": "text", "text": 'Please output {"city": "Baltimore", "country": "USA"}'}],
        schema=Echo,
        max_tokens=100,
        temperature=0.0,
    )
    if r.ok:
        print(f"[ok] complete_json: {r.data.model_dump()}  "
              f"(in={r.input_tokens}, out={r.output_tokens}, attempts={r.attempts})")
    else:
        print(f"[FAIL] complete_json: {r.error}")
        return 1

    # ---- 2. create_with_tools (one tool round trip) ----
    tools = [{
        "name": "get_weather",
        "description": "Look up today's weather for the given city. Must be called before answering weather questions.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    }]
    messages = [{"role": "user", "content": "What's the weather like in Baltimore today?"}]
    resp = client.create_with_tools(
        call_site="smoke.tools",
        model=settings.llm_model_main,
        system="You can use tools. When you need weather data you must call get_weather.",
        messages=messages,
        tools=tools,
        max_tokens=300,
        temperature=0.0,
    )
    tool_uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
    if not tool_uses:
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        print(f"[FAIL] Model answered directly instead of calling the tool: {text[:120]}")
        return 1
    tu = tool_uses[0]
    print(f"[ok] Model requested tool: {tu.name}({tu.input})")

    messages.append({"role": "assistant", "content": resp.content})
    messages.append({"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": tu.id,
        "content": '{"city": "Baltimore", "weather": "sunny", "temp_f": 88}',
    }]})
    resp2 = client.create_with_tools(
        call_site="smoke.tools",
        model=settings.llm_model_main,
        system="You can use tools. When you need weather data you must call get_weather.",
        messages=messages,
        tools=tools,
        max_tokens=300,
        temperature=0.0,
    )
    text = "".join(b.text for b in resp2.content if getattr(b, "type", "") == "text")
    if "sunny" in text.lower() or "88" in text:
        print(f"[ok] Tool result used in final answer: {text[:100]}")
    else:
        print(f"[WARN] Final answer doesn't clearly reference the tool result: {text[:120]}")
    print("[ok] Smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
