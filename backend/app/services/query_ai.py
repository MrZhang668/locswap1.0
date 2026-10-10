"""LLM call site B: query structuring / clarification (LIGHT model).

Triggered when: rule parsing fails, or post-fusion confidence is too low.
Input: user query + the last <= CHAT_HISTORY_TURNS turns of conversation.
Output: filters / semantic_text / clarify_question.
Fallback: on empty result or exception, go straight to three-leg recall
(never block retrieval).
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import settings
from .llm import CALL_SITE_QUERY, LLMResult, get_llm_client
from .llm_schemas import QueryUnderstandingOutput
from .prompts import load_prompt

logger = logging.getLogger(__name__)


def _format_history(history: list[dict[str, str]] | None) -> str:
    if not history:
        return "（没有更早的对话）"
    turns = history[-(settings.chat_history_turns * 2) :]
    lines = []
    for turn in turns:
        role = "User" if turn.get("role") == "user" else "Assistant"
        text = (turn.get("content") or "").strip()
        if text:
            lines.append(f"{role}: {text[:200]}")
    return "\n".join(lines) or "（没有更早的对话）"


def understand_query(
    query: str,
    history: list[dict[str, str]] | None = None,
    reason: str = "rule_parse_failed",
) -> tuple[QueryUnderstandingOutput | None, LLMResult]:
    client = get_llm_client()
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"最近的对话：\n{_format_history(history)}\n\n"
                f"Current user input: {query.strip()}\n"
                f"Trigger reason: {reason}\n"
                "Output JSON only."
            ),
        }
    ]
    result = client.complete_json(
        call_site=CALL_SITE_QUERY,
        model=settings.llm_model_light,
        system=load_prompt("query_understanding"),
        content=content,
        schema=QueryUnderstandingOutput,
        max_tokens=settings.llm_max_tokens_query,
        temperature=settings.llm_temperature_query,
    )
    if not result.ok or result.data is None:
        logger.info("[query_ai] degrading to plain three-leg recall: %s", result.error)
        return None, result
    return result.data, result  # type: ignore[return-value]
