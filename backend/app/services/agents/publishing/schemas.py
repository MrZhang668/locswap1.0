"""Schemas for the publishing flow.

Two things the shapes here are doing on purpose:

**The model cannot name a field it must not set.** `ListingDraft` has no
`status`, no `seller_id`, no verified/featured flag — a submission that tries
to talk its way into one has nowhere to put it. That, not rule text, is what
stops prompt injection from escalating: instructions hidden in a description
can change what the model *says*, but not what the schema *accepts*.

**A verdict has to be traceable.** Every compliance finding carries the rule
code it came from, and the codes are validated against the rule book before the
verdict is used. An unexplained rejection is not something a seller can act on.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ....config import CATEGORY_DICT, CONDITION_DICT
from ...images import check_upload_path
from ...text import scrub_pii

CATEGORIES: tuple[str, ...] = tuple(CATEGORY_DICT.keys())

#: Display names for the draft/attribute fields. `missing_fields` carries the
#: raw field names (the model is told to use them), but a seller reading
#: "还缺: condition, size" is reading an API. Both the agent's reply and the
#: frontend's draft card render through these.
FIELD_LABELS_ZH: dict[str, str] = {
    "name": "标题",
    "summary": "一句话摘要",
    "description": "描述",
    "category": "类别",
    "brand": "品牌",
    "condition": "成色",
    "color": "颜色",
    "material": "材质",
    "size": "尺寸规格",
    "price": "价格",
    "photo": "照片",
}


def field_label(name: str) -> str:
    """Field name -> Chinese label, falling back to the raw name."""
    return FIELD_LABELS_ZH.get((name or "").strip(), name)


#: Best-first, same order and spelling as browse.CONDITION_ORDER — both derive
#: from the one dictionary so they cannot drift apart.
CONDITIONS: tuple[str, ...] = tuple(CONDITION_DICT.keys())

Verdict = Literal["pass", "warn", "block"]


# ============================================================
# Step 1: compliance review
# ============================================================
class RuleFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Validated against the live rule book; an invented code is dropped
    code: str = Field(min_length=1, max_length=40)
    severity: Literal["block", "warn"]
    #: What in *this* submission triggered it, in the seller's terms
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("reason")
    @classmethod
    def _scrub(cls, v: str) -> str:
        return scrub_pii(v)


class ComplianceVerdict(BaseModel):
    """LLM call 1. The overall verdict is recomputed from the findings
    server-side, so a model that says "pass" while citing a block rule does not
    get to decide."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    findings: list[RuleFinding] = Field(default_factory=list)
    #: Free-text note shown to the seller when nothing was found
    note: str = Field(default="", max_length=300)


# ============================================================
# Step 2: extraction + completeness
# ============================================================
class DraftAttributes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str | None = None
    brand: str | None = None
    condition: str | None = None
    color: str | None = None
    material: str | None = None
    size: str | None = None

    @field_validator("category")
    @classmethod
    def _known_category(cls, v: str | None) -> str | None:
        # Out-of-whitelist values are dropped, not rejected: a bad guess should
        # cost the seller a dropdown choice, not the whole submission.
        return v if v in CATEGORIES else None

    @field_validator("condition")
    @classmethod
    def _known_condition(cls, v: str | None) -> str | None:
        return v if v in CONDITIONS else None


class ListingDraft(BaseModel):
    """What the model proposes. Display-only until the seller approves it, and
    re-validated server-side at that point — never written from here."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=40)
    summary: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=600)
    attributes: DraftAttributes = Field(default_factory=DraftAttributes)
    #: Fields the model could not fill from the photo and the seller's words.
    #: The flow asks about these instead of guessing them.
    missing_fields: list[str] = Field(default_factory=list, max_length=8)
    #: One question to ask when something material is missing
    follow_up_question: str = Field(default="", max_length=200)

    @field_validator("name", "summary", "description")
    @classmethod
    def _scrub(cls, v: str) -> str:
        return scrub_pii(v or "")


# ============================================================
# API contract
# ============================================================
class PublishFinding(BaseModel):
    """A finding as the seller sees it — rule title resolved from the code."""

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["block", "warn"]
    title: str
    reason: str


class PublishDraftView(BaseModel):
    """The reviewable draft handed to the frontend."""

    model_config = ConfigDict(extra="forbid")

    name: str = ""
    summary: str = ""
    description: str = ""
    category: str | None = None
    brand: str | None = None
    condition: str | None = None
    color: str | None = None
    material: str | None = None
    size: str | None = None
    photo_path: str = ""
    missing_fields: list[str] = Field(default_factory=list)
    #: True once nothing material is missing — the frontend only offers Approve then
    ready: bool = False


class PublishApproveIn(BaseModel):
    """What the seller approves. Deliberately narrow: the price and pickup spot
    come from the seller, the text may have been edited, and nothing else about
    the listing is client-controlled."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=40)
    summary: str = Field(default="", max_length=120)
    description: str = Field(min_length=1, max_length=600)
    category: str = Field(min_length=1, max_length=40)
    condition: str = Field(min_length=1, max_length=20)
    brand: str | None = Field(default=None, max_length=40)
    color: str | None = Field(default=None, max_length=30)
    material: str | None = Field(default=None, max_length=40)
    size: str | None = Field(default=None, max_length=40)
    price: float = Field(gt=0, le=100000)
    pickup_location_name: str = Field(min_length=1, max_length=80)
    photo_path: str = Field(default="", max_length=200)

    @field_validator("photo_path")
    @classmethod
    def _uploaded_photo(cls, v: str) -> str:
        return check_upload_path(v)


class PublishApproveOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    item_id: int | None = None
    message: str = ""
    findings: list[PublishFinding] = Field(default_factory=list)
