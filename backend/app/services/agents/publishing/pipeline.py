"""The two model steps of the publishing flow: compliance review, then extraction.

Call site E reviews a submission against the rule book (photo included); call
site F turns it into structured listing content and says what is still missing.
They are separate calls on purpose: one small schema each, each independently
testable, and a compliance decision that cannot be talked out of by the same
generation that is trying to produce a nice-looking listing.

Three server-side guarantees this module provides, none of which depend on the
model behaving:

* **The verdict is recomputed here.** Findings citing a code that is not in the
  rule book are dropped, severities are taken from the rule book rather than
  from the model, and the overall verdict is derived from what survives.
* **Blocking is conservative.** A model failure is not a block and not a pass —
  it surfaces as an error the seller can retry, because silently approving and
  silently rejecting are both wrong.
* **The draft can only contain fields the schema allows.** Status, ownership
  and anything else that would matter for escalation simply have no slot.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from ....config import settings
from ....models import ListingRule, RuleSeverity
from ...llm.langchain_client import build_chat_model, record_usage
from ...images import to_base64_source
from ...llm.client import extract_json
from ...prompts import load_prompt
from . import rules as rules_mod
from .schemas import ComplianceVerdict, ListingDraft, PublishFinding, RuleFinding

logger = logging.getLogger(__name__)

CALL_SITE_COMPLIANCE = "E.publishing_compliance"
CALL_SITE_EXTRACT = "F.publishing_extract"


class PublishingError(Exception):
    """The step could not produce a usable answer. Surfaced to the seller as
    "try again", never as an approval or a rejection."""


def _image_block(photo_path: str) -> dict[str, Any] | None:
    """One image, in the shape the configured provider understands.

    Written per provider rather than relying on a common form: an image block
    the provider silently ignores looks exactly like a model that "didn't
    notice" what was in the photo, which is the hardest kind of bug to see.
    """
    if not photo_path:
        return None
    try:
        source = to_base64_source(photo_path)
    except FileNotFoundError as exc:
        logger.warning("[publishing] %s", exc)
        return None
    if settings.llm_provider.strip().lower() == "openai_compat":
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{source['media_type']};base64,{source['data']}"},
        }
    return {"type": "image", "source": source}


def _invoke_json(
    *,
    call_site: str,
    system: str,
    text: str,
    photo_path: str,
    schema: type[BaseModel],
    max_tokens: int,
) -> tuple[BaseModel, int, int]:
    """One multimodal call whose answer must satisfy `schema`.

    Retries once with the validation error fed back — the same contract
    services/llm/client.py uses for its JSON call sites, so a malformed answer costs
    one cheap retry rather than a failed submission.
    """
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    content: list[dict[str, Any]] = []
    image = _image_block(photo_path)
    if image:
        content.append(image)
    content.append({"type": "text", "text": text})

    chat = build_chat_model(
        model=settings.llm_model_main, temperature=0.0, max_tokens=max_tokens
    )
    messages: list[Any] = [SystemMessage(content=system), HumanMessage(content=content)]
    total_in = total_out = 0
    last_error = ""

    for attempt in range(settings.llm_json_retries + 1):
        try:
            response = chat.invoke(messages)
        except Exception as exc:  # noqa: BLE001
            raise PublishingError(f"model call failed ({type(exc).__name__})") from exc
        in_tok, out_tok = record_usage(call_site, settings.llm_model_main, response)
        total_in += in_tok
        total_out += out_tok

        raw = response.text() if hasattr(response, "text") else str(response.content)
        try:
            return schema.model_validate(extract_json(raw)), total_in, total_out
        except (ValueError, ValidationError) as exc:
            last_error = str(exc)[:400]
            logger.warning("[publishing] %s attempt %d invalid: %s", call_site, attempt + 1, last_error)
            messages.append(AIMessage(content=raw[:1000] or "(empty)"))
            messages.append(
                HumanMessage(
                    content=(
                        "刚才的输出不符合要求的 JSON schema。错误信息：\n"
                        f"{last_error}\n"
                        "Reply with one JSON object that satisfies it, and nothing else."
                    )
                )
            )

    raise PublishingError(f"model output never matched the schema: {last_error}")


# ============================================================
# Step 1: compliance
# ============================================================
def _reconcile(
    verdict: ComplianceVerdict, rule_book: list[ListingRule]
) -> tuple[str, list[PublishFinding]]:
    """Turn the model's findings into the decision the backend acts on.

    The rule book is the authority for whether a code exists and how severe it
    is; the model only gets to say *which* rules it thinks were hit and why.
    """
    by_code = {r.code: r for r in rule_book}
    findings: list[PublishFinding] = []
    seen: set[str] = set()
    for f in verdict.findings:
        rule = by_code.get(f.code.strip().upper())
        if rule is None:
            logger.warning("[publishing] discarding finding for unknown rule %r", f.code)
            continue
        if rule.code in seen:
            continue
        seen.add(rule.code)
        findings.append(
            PublishFinding(
                code=rule.code,
                severity=rule.severity,      # from the rule book, not the model
                title=rule.title,
                reason=f.reason,
            )
        )
    if any(f.severity == RuleSeverity.BLOCK for f in findings):
        return "block", findings
    return ("warn" if findings else "pass"), findings


def review_compliance(
    db: Session, *, text: str, photo_path: str = ""
) -> tuple[str, list[PublishFinding], int, int]:
    """Call site E. Returns (verdict, findings, input_tokens, output_tokens)."""
    rule_book = rules_mod.retrieve_rules(db, text)
    body = (
        f"<rules>\n{rules_mod.render_rules(rule_book)}\n</rules>\n"
        f"<submission>\n{text.strip() or '（卖家没有填写文字）'}\n</submission>"
    )
    parsed, in_tok, out_tok = _invoke_json(
        call_site=CALL_SITE_COMPLIANCE,
        system=load_prompt("publishing_compliance"),
        text=body,
        photo_path=photo_path,
        schema=ComplianceVerdict,
        max_tokens=settings.publishing_max_tokens_compliance,
    )
    assert isinstance(parsed, ComplianceVerdict)
    verdict, findings = _reconcile(parsed, rule_book)
    return verdict, findings, in_tok, out_tok


# ============================================================
# Step 2: extraction + completeness
# ============================================================
def extract_listing(
    db: Session, *, text: str, photo_path: str = ""
) -> tuple[ListingDraft, int, int]:
    """Call site F. Extraction and the completeness check are one call: the
    pass that decides it cannot determine the brand is the same pass that would
    have filled it in."""
    body = f"<submission>\n{text.strip() or '（卖家没有填写文字）'}\n</submission>"
    parsed, in_tok, out_tok = _invoke_json(
        call_site=CALL_SITE_EXTRACT,
        system=load_prompt("publishing_extract"),
        text=body,
        photo_path=photo_path,
        schema=ListingDraft,
        max_tokens=settings.publishing_max_tokens_extract,
    )
    assert isinstance(parsed, ListingDraft)
    return parsed, in_tok, out_tok


__all__ = [
    "CALL_SITE_COMPLIANCE",
    "CALL_SITE_EXTRACT",
    "PublishingError",
    "RuleFinding",
    "extract_listing",
    "review_compliance",
]
