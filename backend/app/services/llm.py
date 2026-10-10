"""Unified LLM wrapper (dual backend: official Anthropic SDK / OpenAI-compatible API).

Responsibilities:
1. Model names come only from config — never hardcoded here;
2. Every output is validated against a pydantic schema, with 1 automatic retry
   on failure (the validation error is fed back to the model);
3. Every call logs its usage into token_usage_log;
4. Graceful degradation when there is no API key / the call fails — the call
   site decides the fallback behavior.

When LLM_PROVIDER=openai_compat (OpenRouter and similar relays), requests go
straight through httpx to `{LLM_BASE_URL}/chat/completions`, and the response
is converted inside this module into Anthropic-shaped content blocks
(text / tool_use), so the upstream call sites (query_ai /
seller_advisor.orchestrator) are completely agnostic to the backend.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Type, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# ---- Call-site identifiers (token_report.py aggregates by these) ----
CALL_SITE_QUERY = "B.query_understanding"
CALL_SITE_SELLER_ADVISOR = "C.seller_advisor"

#: USD price per million tokens (input, output). Edit only here when prices change.
#: Cache multipliers are Anthropic's standard rates: write 1.25x, read 0.1x base input price.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "openai/gpt-5.6-terra": (2.0, 12.0),   # OpenRouter list price 2026-08
}
_DEFAULT_PRICE = (5.0, 25.0)
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Estimate the cost of one token_usage_log row. scripts/token_report.py aggregates with it."""
    in_price, out_price = PRICE_PER_MTOK.get(model, _DEFAULT_PRICE)
    return round(
        (
            input_tokens * in_price
            + cache_write_tokens * in_price * CACHE_WRITE_MULTIPLIER
            + cache_read_tokens * in_price * CACHE_READ_MULTIPLIER
            + output_tokens * out_price
        )
        / 1_000_000,
        6,
    )


@dataclass
class LLMResult:
    ok: bool
    call_site: str
    model: str
    data: BaseModel | None = None
    raw_text: str = ""
    error: str = ""
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    degraded: bool = False  # True = the model was never actually called (no key / SDK missing)
    debug: dict[str, Any] = field(default_factory=dict)


def _log_usage(
    call_site: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> None:
    """Write the token log in its own transaction — never let it affect the main flow.

    The two cache counters default to 0 for the callers that cannot see them;
    the LangChain-backed agent stack passes the real values through.
    """
    try:
        from ..db import SessionLocal
        from ..repositories import TokenUsageRepository

        with SessionLocal() as db:
            TokenUsageRepository(db).append(
                call_site, model, input_tokens, output_tokens, cache_read, cache_write
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[llm] failed to persist token usage: %s", exc)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Dig the JSON object out of the model output. The prompt already demands JSON-only; this is a safety net."""
    if not text:
        raise ValueError("empty response")
    candidate = text.strip()
    m = _FENCE_RE.search(candidate)
    if m:
        candidate = m.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        return json.loads(candidate[start : end + 1])
    raise ValueError(f"no JSON found in response: {text[:200]}")


# ============================================================
# Two-way conversion between OpenAI-compatible format and Anthropic shape
# (used by the openai_compat backend)
# ============================================================
def _block_get(block: Any, key: str, default: Any = None) -> Any:
    """A content block may be an SDK object / SimpleNamespace / dict — read uniformly."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert internal Anthropic-style messages to OpenAI Chat Completions format."""
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for b in content:
                btype = _block_get(b, "type", "")
                if btype == "text":
                    text_parts.append(_block_get(b, "text", "") or "")
                elif btype == "tool_use":
                    tool_calls.append(
                        {
                            "id": _block_get(b, "id", "") or "",
                            "type": "function",
                            "function": {
                                "name": _block_get(b, "name", "") or "",
                                "arguments": json.dumps(
                                    _block_get(b, "input", {}) or {}, ensure_ascii=False
                                ),
                            },
                        }
                    )
            msg: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
            continue
        # user: a batch of tool_result blocks becomes role=tool; multimodal content becomes image_url/text
        tool_results = [b for b in content if _block_get(b, "type") == "tool_result"]
        if tool_results:
            for tr in tool_results:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": _block_get(tr, "tool_use_id", "") or "",
                        "content": str(_block_get(tr, "content", "") or ""),
                    }
                )
            continue
        parts: list[dict[str, Any]] = []
        for b in content:
            btype = _block_get(b, "type", "")
            if btype == "text":
                parts.append({"type": "text", "text": _block_get(b, "text", "") or ""})
            elif btype == "image":
                src = _block_get(b, "source", {}) or {}
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{src.get('media_type', 'image/jpeg')};"
                                   f"base64,{src.get('data', '')}"
                        },
                    }
                )
        out.append({"role": role, "content": parts})
    return out


def _to_openai_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object"}),
            },
        }
        for t in tools
    ]


def _shim_from_openai(data: dict[str, Any]) -> SimpleNamespace:
    """Convert a Chat Completions response to Anthropic shape (content blocks + usage)."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks: list[SimpleNamespace] = []
    if msg.get("content"):
        blocks.append(SimpleNamespace(type="text", text=str(msg["content"])))
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        blocks.append(
            SimpleNamespace(
                type="tool_use", name=fn.get("name", ""), input=args, id=tc.get("id", "")
            )
        )
    usage = data.get("usage") or {}
    return SimpleNamespace(
        content=blocks,
        usage=SimpleNamespace(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        ),
        stop_reason=choice.get("finish_reason"),
    )


class LLMClient:
    def __init__(self):
        self._client = None
        self._init_error = ""
        self._provider = (settings.llm_provider or "anthropic").strip().lower()

        if self._provider == "openai_compat":
            self._api_key = settings.llm_api_key or settings.anthropic_api_key
            self._base_url = settings.llm_base_url.rstrip("/")
            if not self._api_key:
                self._init_error = "LLM_API_KEY is not configured"
            elif not self._base_url:
                self._init_error = "LLM_BASE_URL is required when LLM_PROVIDER=openai_compat"
            return

        if not settings.anthropic_api_key:
            self._init_error = "ANTHROPIC_API_KEY is not configured"
            return
        try:
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key, timeout=settings.llm_timeout_seconds
            )
        except Exception as exc:  # noqa: BLE001
            self._init_error = f"Anthropic SDK initialization failed: {exc}"

    @property
    def available(self) -> bool:
        if self._provider == "openai_compat":
            return not self._init_error
        return self._client is not None

    @property
    def unavailable_reason(self) -> str:
        return self._init_error

    def _request(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        """One model round-trip; the two backends fork here. Return value is always Anthropic-shaped."""
        if self._provider == "openai_compat":
            import httpx

            payload: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": _to_openai_messages(system, messages),
            }
            openai_tools = _to_openai_tools(tools)
            if openai_tools:
                payload["tools"] = openai_tools
            resp = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
                timeout=settings.llm_timeout_seconds,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
            return _shim_from_openai(resp.json())

        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = tools
        return self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=messages,
            **kwargs,
        )

    def complete_json(
        self,
        *,
        call_site: str,
        model: str,
        system: str,
        content: list[dict[str, Any]],
        schema: Type[T],
        max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        """Call the model and validate the output against the schema. Auto-retries settings.llm_json_retries times on failure."""
        if not self.available:
            return LLMResult(
                ok=False,
                call_site=call_site,
                model=model,
                error=self._init_error,
                degraded=True,
            )

        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        total_in = total_out = 0
        last_error = ""
        raw_text = ""

        for attempt in range(1, settings.llm_json_retries + 2):
            try:
                response = self._request(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    messages=messages,
                )
            except Exception as exc:  # noqa: BLE001  network/auth/rate-limit all degrade uniformly
                logger.warning("[llm] %s call failed (attempt %d): %s", call_site, attempt, exc)
                return LLMResult(
                    ok=False,
                    call_site=call_site,
                    model=model,
                    error=f"{type(exc).__name__}: {exc}",
                    attempts=attempt,
                    input_tokens=total_in,
                    output_tokens=total_out,
                )

            usage = getattr(response, "usage", None)
            in_tok = int(getattr(usage, "input_tokens", 0) or 0)
            out_tok = int(getattr(usage, "output_tokens", 0) or 0)
            total_in += in_tok
            total_out += out_tok
            _log_usage(call_site, model, in_tok, out_tok)

            raw_text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")

            try:
                payload = extract_json(raw_text)
                data = schema.model_validate(payload)
                return LLMResult(
                    ok=True,
                    call_site=call_site,
                    model=model,
                    data=data,
                    raw_text=raw_text,
                    attempts=attempt,
                    input_tokens=total_in,
                    output_tokens=total_out,
                    debug={"stop_reason": getattr(response, "stop_reason", None)},
                )
            except (ValueError, ValidationError) as exc:
                last_error = str(exc)
                logger.warning("[llm] %s schema validation failed (attempt %d): %s", call_site, attempt, last_error)
                if attempt > settings.llm_json_retries:
                    break
                # Retry once, feeding the error back to the model
                messages = messages + [
                    {"role": "assistant", "content": raw_text or "(empty)"},
                    {
                        "role": "user",
                        "content": (
                            "你上一次的输出不符合要求。错误信息：\n"
                            f"{last_error}\n"
                            "Output only a single JSON object that satisfies the schema — no explanatory text."
                        ),
                    },
                ]

        return LLMResult(
            ok=False,
            call_site=call_site,
            model=model,
            raw_text=raw_text,
            error=f"schema validation failed: {last_error}",
            attempts=settings.llm_json_retries + 1,
            input_tokens=total_in,
            output_tokens=total_out,
        )

    def create_with_tools(
        self,
        *,
        call_site: str,
        model: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
        temperature: float,
    ) -> Any:
        """One raw model call with tool definitions (the foundation of call site C).

        Division of labor with complete_json: complete_json handles
        "single-shot Q&A + JSON validation"; this method only performs one API
        round-trip and logs usage. The tool loop, parameter validation, and
        round caps are all controlled by seller_advisor.orchestrator — the
        model can only request tools registered there.
        Callers must check self.available first; API exceptions propagate
        as-is, and the caller decides how to degrade.
        """
        if not self.available:
            raise RuntimeError(self._init_error or "LLM unavailable")
        response = self._request(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=messages,
            tools=tools,
        )
        usage = getattr(response, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        _log_usage(call_site, model, in_tok, out_tok)
        return response


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
