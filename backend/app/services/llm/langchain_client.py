"""LangChain-backed model access for the agent stack (call sites C and D).

Why a second LLM layer alongside `llm/client.py`:
* the agent stack is built on LangGraph, and staying inside LangChain's message
  and tool abstractions is what lets a node be swapped, traced, or wrapped
  without hand-rolling provider plumbing again;
* the older call sites (A listing generation, B query understanding) are on the
  hot paths of listing and search. They keep using `llm/client.py` — there is
  no upside to churning them, and a regression there is far more expensive than
  one here.

Two things this module is careful about:

**Shape.** `LangChainAdvisorClient.create_with_tools` returns the *same*
Anthropic-shaped object `llm/client.py` returns (content blocks + usage), so
the seller-advisor orchestrator — with its Pydantic validation, ownership
checks, round caps and result caching — is untouched by the swap. The
conversion is explicit in both directions rather than delegated, because a
silent shape mismatch here would look like the model "not calling tools".

**Token accounting.** Usage is read straight off LangChain's
`AIMessage.usage_metadata` and written to `token_usage_log`, including the two
cache counters. Read directly rather than through a callback handler: there are
only two invocation sites, and a direct read is deterministic and testable,
whereas a dropped callback fails silently as "the cache is never hit".

The same read feeds a per-turn meter (`start_turn_meter` / `read_turn_meter`).
`record_usage` is the single funnel every model call in the agent stack passes
through — router, sales tool loop, proposal, publishing — so metering here is
what makes "what did this one question cost?" answerable without each node
having to hand its own numbers up through the graph state. Two of those calls
cannot do that at all: the proposal call happens inside a node whose return
value `interrupt()` discards, and the seller-advisor loop sums its own rounds
but knows nothing about the router that preceded it.
"""

from __future__ import annotations

import contextvars
import logging
from types import SimpleNamespace
from typing import Any

from ...config import settings
from .client import _log_usage  # single writer for token_usage_log

logger = logging.getLogger(__name__)

#: Call sites owned by the agent stack. A.* and B.* stay in llm/client.py.
CALL_SITE_ROUTER = "D.orchestrator_router"
CALL_SITE_DEFER_INTENT = "H.defer_intent"
CALL_SITE_ITEM_LOOKUP = "I.item_lookup"


def router_model_name() -> str:
    """The router only classifies — the light model is enough, and it runs on
    every single turn, so defaulting it to the main model would quietly double
    the per-turn cost."""
    return (settings.llm_model_router or settings.llm_model_light).strip()


# ============================================================
# Model construction
# ============================================================
def _credentials() -> tuple[str, str]:
    """(api_key, base_url) for the configured provider."""
    if settings.llm_provider.strip().lower() == "openai_compat":
        return (settings.llm_api_key or settings.anthropic_api_key), settings.llm_base_url.rstrip("/")
    return settings.anthropic_api_key, ""


def llm_configured() -> tuple[bool, str]:
    """Whether a model can be built at all. Mirrors LLMClient.available so the
    agent stack degrades with the same message the rest of the app uses."""
    key, base_url = _credentials()
    if not key:
        return False, (
            "LLM_API_KEY is not configured" if settings.llm_provider.strip().lower() == "openai_compat"
            else "ANTHROPIC_API_KEY is not configured"
        )
    if settings.llm_provider.strip().lower() == "openai_compat" and not base_url:
        return False, "LLM_BASE_URL is required when LLM_PROVIDER=openai_compat"
    return True, ""


def build_chat_model(*, model: str, temperature: float, max_tokens: int) -> Any:
    """One LangChain chat model, provider chosen by config.

    Imported lazily so a deployment that never touches the agent stack does not
    pay for (or need) the provider packages.
    """
    key, base_url = _credentials()
    if settings.llm_provider.strip().lower() == "openai_compat":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            api_key=key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,   # retries are the caller's decision, not a hidden cost multiplier
        )

    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(
        model=model,
        api_key=key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=settings.llm_timeout_seconds,
        max_retries=0,
    )


# ============================================================
# Usage accounting
# ============================================================
#: This turn's running total, or None when nothing is metering. A ContextVar
#: rather than a module global because two sellers' turns run concurrently in
#: the threadpool, and a global would bill one for the other. Each request gets
#: its own copy of the context, so a write here cannot escape into another.
_turn_meter: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "advisor_turn_meter", default=None
)


def start_turn_meter() -> None:
    """Begin counting a turn. Call once, before any model call of that turn."""
    _turn_meter.set({"input_tokens": 0, "output_tokens": 0, "calls": 0, "by_call_site": {}})


def read_turn_meter() -> dict:
    """What the current turn has spent so far. Zeroes when nothing is metering
    — a script or test calling the advisor directly is not a bug to raise."""
    meter = _turn_meter.get()
    if not meter:
        return {"input_tokens": 0, "output_tokens": 0, "calls": 0, "by_call_site": {}}
    return {
        "input_tokens": meter["input_tokens"],
        "output_tokens": meter["output_tokens"],
        "calls": meter["calls"],
        # Per-site rows copied too, not just the outer dict: a caller that
        # stashes a read and looks at it later must see what the turn had
        # spent *then*, and a shallow copy would keep growing under it.
        "by_call_site": {k: dict(v) for k, v in meter["by_call_site"].items()},
    }


def record_usage(call_site: str, model: str, message: Any) -> tuple[int, int]:
    """Write one LangChain response's usage to token_usage_log.

    Returns (input_tokens, output_tokens) so callers can also surface a
    per-request total. Missing usage is logged loudly rather than silently
    treated as zero — "no usage" and "zero tokens" are very different bugs.
    """
    usage = getattr(message, "usage_metadata", None) or {}
    if not usage:
        logger.warning("[agents] %s returned no usage_metadata (model=%s)", call_site, model)
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    details = usage.get("input_token_details") or {}
    _log_usage(
        call_site,
        model,
        in_tok,
        out_tok,
        cache_read=int(details.get("cache_read") or 0),
        cache_write=int(details.get("cache_creation") or 0),
    )
    meter = _turn_meter.get()
    if meter is not None:
        # Counted even when the response carried no usage: a call that happened
        # is a call the seller should see in the count, and a "0 tokens" row
        # next to a non-zero call count is a legible symptom rather than a
        # silent omission.
        meter["input_tokens"] += in_tok
        meter["output_tokens"] += out_tok
        meter["calls"] += 1
        site = meter["by_call_site"].setdefault(call_site, {"input": 0, "output": 0, "calls": 0})
        site["input"] += in_tok
        site["output"] += out_tok
        site["calls"] += 1
    return in_tok, out_tok


# ============================================================
# Anthropic-shape <-> LangChain conversion
# ============================================================
def _block_get(block: Any, key: str, default: Any = None) -> Any:
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-style tool definitions -> the shape bind_tools understands.

    Written out rather than routed through a generic converter: `input_schema`
    is not a key the generic helpers look for, and a tool that silently loses
    its parameter schema still "works" — it just starts getting called with
    empty arguments.
    """
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


def to_lc_messages(system: str, messages: list[dict[str, Any]]) -> list[Any]:
    """Orchestrator message list -> LangChain messages.

    The orchestrator speaks in Anthropic shape: assistant turns carry text and
    `tool_use` blocks, and tool results come back as a batch of `tool_result`
    blocks inside a user turn. LangChain wants tool results as separate
    ToolMessages keyed by tool_call_id.
    """
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    out: list[Any] = []
    if system:
        out.append(SystemMessage(content=system))

    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append(AIMessage(content=content) if role == "assistant" else HumanMessage(content=content))
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
                            "name": _block_get(b, "name", "") or "",
                            "args": _block_get(b, "input", {}) or {},
                            "id": _block_get(b, "id", "") or "",
                            "type": "tool_call",
                        }
                    )
            out.append(AIMessage(content="".join(text_parts), tool_calls=tool_calls))
            continue

        # user turn carrying tool results
        results = [b for b in content if _block_get(b, "type") == "tool_result"]
        if results:
            for tr in results:
                out.append(
                    ToolMessage(
                        content=str(_block_get(tr, "content", "") or ""),
                        tool_call_id=_block_get(tr, "tool_use_id", "") or "",
                        status="error" if _block_get(tr, "is_error") else "success",
                    )
                )
            continue
        out.append(HumanMessage(content=str(content)))
    return out


def from_lc_message(message: Any) -> SimpleNamespace:
    """LangChain AIMessage -> the Anthropic-shaped object the orchestrator reads."""
    blocks: list[SimpleNamespace] = []
    text = ""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            _block_get(b, "text", "") or "" for b in content if _block_get(b, "type") == "text"
        )
    if text:
        blocks.append(SimpleNamespace(type="text", text=text))

    for tc in getattr(message, "tool_calls", None) or []:
        blocks.append(
            SimpleNamespace(
                type="tool_use",
                name=tc.get("name", ""),
                input=tc.get("args", {}) or {},
                id=tc.get("id", "") or "",
            )
        )

    usage = getattr(message, "usage_metadata", None) or {}
    return SimpleNamespace(
        content=blocks,
        usage=SimpleNamespace(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        ),
    )


# ============================================================
# Advisor client (drop-in for llm.client.LLMClient.create_with_tools)
# ============================================================
class LangChainAdvisorClient:
    """What the seller-advisor orchestrator talks to, now backed by LangChain.

    Deliberately exposes the same three members the old client did
    (`available`, `unavailable_reason`, `create_with_tools`) so the orchestrator
    — and the acceptance script's FakeClient — need no changes.
    """

    def __init__(self) -> None:
        self._ok, self._reason = llm_configured()

    @property
    def available(self) -> bool:
        return self._ok

    @property
    def unavailable_reason(self) -> str:
        return self._reason

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
    ) -> SimpleNamespace:
        if not self._ok:
            raise RuntimeError(self._reason or "LLM unavailable")
        chat = build_chat_model(model=model, temperature=temperature, max_tokens=max_tokens)
        if tools:
            chat = chat.bind_tools(to_openai_tools(tools))
        response = chat.invoke(to_lc_messages(system, messages))
        record_usage(call_site, model, response)
        return from_lc_message(response)


_advisor_client: LangChainAdvisorClient | None = None


def get_advisor_client() -> LangChainAdvisorClient:
    global _advisor_client
    if _advisor_client is None:
        _advisor_client = LangChainAdvisorClient()
    return _advisor_client
