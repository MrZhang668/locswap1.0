"""Pydantic schemas for LLM outputs — hard validation of both call sites on the code side.

The privacy red line does not live only in the prompts: it is machine-checked
again here. Any output containing an email address, phone number, or social
handle fails validation and triggers a retry.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .text import clip_sentences

_PII_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),  # email
    # US phone number: optional +1, then 333-444-5555 / (333) 444-5555 style
    # (a separator or parentheses are required so ISBNs / plain IDs don't match)
    re.compile(r"(?<!\d)(?:\+?1[\s.-])?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]?\d{4}(?!\d)"),
    # social / messaging handles ("insta: xyz", "discord #xyz", "wechat abc123")
    re.compile(
        r"\b(?:wechat|whatsapp|snapchat|instagram|insta|telegram|discord|venmo|ig)\b\s*[:#]?\s*@?[\w.-]{4,}",
        re.IGNORECASE,
    ),
]

_CORE_ATTRS = ("category", "brand", "condition", "color")


def assert_no_pii(text: str, field_name: str) -> str:
    for pattern in _PII_PATTERNS:
        if pattern.search(text or ""):
            raise ValueError(f"{field_name} 触碰了隐私红线（看起来像联系方式），必须重新生成")
    return text


# ---------------- Call site A: listing content generation ----------------
class ListingGenerationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=600)
    summary: str = Field(min_length=1, max_length=120)
    attributes: dict[str, str | None] = Field(default_factory=dict)

    @field_validator("name", "description", "summary")
    @classmethod
    def _no_pii(cls, v: str, info) -> str:
        return assert_no_pii(v.strip(), info.field_name)

    @field_validator("summary")
    @classmethod
    def _max_two_sentences(cls, v: str) -> str:
        clipped = clip_sentences(v, 2)
        if not clipped:
            raise ValueError("摘要不能为空")
        return clipped

    @field_validator("attributes")
    @classmethod
    def _clean_attributes(cls, v: dict[str, str | None]) -> dict[str, str | None]:
        cleaned: dict[str, str | None] = {}
        for key, value in (v or {}).items():
            key = str(key).strip()
            if not key or key in ("seller", "seller_id", "email", "contact", "phone", "user_id"):
                continue
            if value is None:
                cleaned[key] = None
                continue
            text = str(value).strip()
            if text in ("", "null", "None", "unknown", "Unknown", "n/a", "N/A", "not sure"):
                cleaned[key] = None
                continue
            cleaned[key] = assert_no_pii(text, f"attributes.{key}")[:60]
        for core in _CORE_ATTRS:
            cleaned.setdefault(core, None)
        return cleaned


# ---------------- Call site B: query structuring / clarification ----------------
class QueryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_price: float | None = None
    min_price: float | None = None
    category: str | None = None
    brand: str | None = None
    color: str | None = None
    condition: str | None = None
    max_distance_km: float | None = None

    @field_validator("max_price", "min_price", "max_distance_km")
    @classmethod
    def _positive(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if v <= 0:
            return None
        return float(v)


class QueryUnderstandingOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: QueryFilters = Field(default_factory=QueryFilters)
    semantic_text: str = ""
    clarify_question: str | None = None

    @field_validator("semantic_text")
    @classmethod
    def _clean_semantic(cls, v: str) -> str:
        return assert_no_pii((v or "").strip(), "semantic_text")[:200]

    @field_validator("clarify_question")
    @classmethod
    def _clean_clarify(cls, v: str | None) -> str | None:
        if v is None:
            return None
        text = assert_no_pii(v.strip(), "clarify_question")
        return text[:120] or None

    @model_validator(mode="after")
    def _price_order(self) -> "QueryUnderstandingOutput":
        f = self.filters
        if f.min_price is not None and f.max_price is not None and f.min_price > f.max_price:
            f.min_price, f.max_price = f.max_price, f.min_price
        return self
