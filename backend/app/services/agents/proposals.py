"""The advisor's next-step proposals — the only place it can cause a side effect.

Everything else the sales advisor does is read-only: it looks up demand, sold
prices and matching wants, and says things. This module is where it can ask to
*do* something, and the whole design is about keeping that narrow.

**One action.** `push_to_buyers` and nothing else. Not because the model could
not phrase others, but because every action needs its own server-side
re-validation and its own quota, and a proposal the backend cannot fully
re-check is a proposal the model effectively executes itself. Price changes are
deliberately out: they mutate a listing, and the seller has a form for that.

**The proposal is a request, never an instruction.** Validated here against
ownership, the live candidate list and the daily quota; then the seller has to
say yes; then `push_item_to_want` re-checks every condition *again* against the
business DB, because the candidate card is a snapshot that may be minutes stale.
Three gates, and the model controls none of them.

**Ambiguity never writes.** Consent is recognised from a short list of plain
affirmatives. Anything else — a question, a qualifier, silence, a new topic —
is a decline, and the message goes on to be answered as an ordinary turn. The
failure mode that matters is messaging a stranger the seller did not mean to
message; being asked twice is not in the same class.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ...config import settings
from ...models import Item, ItemStatus
from .sales import tools as tool_impl

logger = logging.getLogger(__name__)

#: The complete set. Adding to it means adding a validator and a quota below.
#:
#: `update_item_status` is the second member, added for a failure that was the
#: opposite of the first one's: 「把显示器下架吧」 was recognised perfectly well and
#: then had nowhere to land, so the system asked about brand and size instead.
#: It goes through the same gate as a push rather than becoming a tool the
#: model may call, because the argument for the gate does not weaken when the
#: side effect stops leaving the account: taking a listing off sale on a
#: misread sentence is the seller's inventory changing without the seller.
ProposalAction = Literal["push_to_buyers", "update_item_status", "none"]

#: Which state change a confirmed `update_item_status` performs. `delete` is
#: absent on purpose — permanent deletion stays behind the explicit button in
#: My Selling, where the dialog spells out that it cannot be undone. A chat
#: gate is the right shape for a reversible change and the wrong shape for an
#: irreversible one.
StatusTarget = Literal["pause", "resume", ""]

#: Plain affirmatives, in both languages. Kept short and unambiguous on purpose
#: — a generous list is how "no, don't" or "yes but what about…" turns into a
#: message someone did not want sent. Matched against the whole reply, so a bare
#: "好的" counts while "好吗？" as a question does not read as consent.
#:
#: The Chinese half cannot use \b (it never fires between two hanzi), so the
#: alternatives are anchored by the ^…$ around the whole pattern instead, and
#: the trailing-punctuation class carries the full-width marks. 好/行/可以/发吧
#: are the ways a seller actually says yes here; anything longer than a bare
#: agreement is deliberately NOT consent — it goes back through a normal turn.
_AFFIRMATIVE = re.compile(
    r"^\s*(?:"
    r"y|ya|yes|yep|yeah|yup|sure|ok|okay|okey|alright|fine|"
    r"do it|go ahead|go for it|please do|sounds good|let'?s do it|send it|"
    r"push (?:it|them)?|confirm(?:ed)?|approve[d]?|proceed"
    r")\b[\s.!,]*$"
    r"|"
    r"^\s*(?:"
    r"好|好的|好吧|好呀|好啊|行|行吧|可以|可以的|没问题|没毛病|同意|确认|"
    r"发吧|发送|发出去|推送|推吧|去吧|就这样|就这么办|听你的|麻烦你了|辛苦了|"
    r"ok的|okk?"
    r")[\s.!,。！，、]*$",
    # Case-insensitive for the English half — a seller typing "OK" or "Yes!"
    # means the same as "ok". Without this the capitalised forms fall through to
    # "not a yes", which is safe but reads as the app ignoring them. The Chinese
    # half is unaffected: hanzi have no case.
    re.IGNORECASE,
)

#: A reply that opens with a negation is a decline even if an affirmative word
#: appears later ("no, yes to the second one" is not something to guess at).
#: 不用/先不/别 must be tested before the affirmative pattern, because 不用了
#: and 不好 both contain a string the affirmative list would otherwise accept.
_NEGATIVE = re.compile(
    r"^\s*(?:n|no|nope|nah|don'?t|do not|stop|cancel|skip|not now|later)\b"
    r"|"
    r"^\s*(?:不|别|先别|先不|暂时不|算了|再说|等等|等一下|停|取消|不要|不用|不行|不好)",
    re.IGNORECASE,
)


class ActionProposal(BaseModel):
    """What the model asks permission to do. Shapes only — meaning is checked
    by `validate_proposal`, which is the one that can say no."""

    model_config = ConfigDict(extra="forbid")

    action: ProposalAction = "none"
    item_id: int | None = None
    #: Ordinals from *this turn's* find_potential_buyers result. They are only
    #: meaningful inside that result set, which is why the mapping is resolved
    #: server-side and never sent to the model as ids.
    card_ids: list[str] = Field(default_factory=list, max_length=5)
    #: Why the seller should want this. Shown to them verbatim.
    rationale: str = Field(default="", max_length=400)
    #: The yes/no question itself.
    question: str = Field(default="", max_length=200)


class PendingAction(BaseModel):
    """A validated proposal, waiting on the seller. This is what gets
    checkpointed and what the frontend renders as a confirm prompt."""

    model_config = ConfigDict(extra="forbid")

    action: ProposalAction
    item_id: int
    item_name: str
    card_ids: list[str] = Field(default_factory=list)
    recipients: int = 0
    rationale: str = ""
    question: str = ""
    #: Only meaningful for `update_item_status`.
    target: StatusTarget = ""


class ActionResult(BaseModel):
    """What actually happened. `failed` is not an error state — a push can be
    refused per-buyer for perfectly ordinary reasons (the want closed, the
    buyer hit their daily cap), and the seller should see which."""

    model_config = ConfigDict(extra="forbid")

    action: ProposalAction
    item_id: int
    item_name: str
    succeeded: int = 0
    failed: list[str] = Field(default_factory=list)
    summary: str = ""
    #: For a status change: what the listing's status is now, read back from
    #: the row after the write rather than assumed from what was requested.
    new_status: str = ""


def is_affirmative(message: str) -> bool:
    """Whether this reply is clear consent. Defaults to False."""
    text = (message or "").strip()
    if not text or _NEGATIVE.match(text):
        return False
    return bool(_AFFIRMATIVE.match(text))


#: Consent that names the action it is consenting to. Only used for a status
#: change, and only as an *addition* to the bare affirmatives above.
#:
#: The push gate is deliberately not widened by this: the cost of a missed yes
#: there is being asked twice, and the cost of a wrongly-read yes is a message
#: to a stranger. A status change is different on both sides — it is reversible
#: in one click, and the seller who just said 「行，下架吧」 and got asked again
#: has been told the system did not understand plain Chinese. So "an
#: affirmative that restates the action" counts here: it is *more* explicit
#: than a bare 「好」, not less.
_STATUS_CONSENT = re.compile(
    r"^\s*(?:好|好的|行|可以|嗯|对|是的|没问题|同意|确认|麻烦|帮我|那就|就)?"
    r"[，,。\s、]*"
    r"(?:把[^，,。\s]{0,12})?"
    r"(?:下架|下掉|暂停|停售|先停|撤下|撤了|收起来|"
    r"重新上架|恢复上架|恢复|再挂出来|挂回来|上架)"
    r"(?:吧|了|它|他|这个|这件)?[\s.!,。！，、]*$"
)


def is_affirmative_for_status(message: str) -> bool:
    """Consent to a status change. Everything `is_affirmative` accepts, plus a
    restatement of the action itself. A leading negation still refuses."""
    text = (message or "").strip()
    if not text or _NEGATIVE.match(text):
        return False
    return is_affirmative(text) or bool(_STATUS_CONSENT.match(text))


def build_status_change(item: Item, target: str) -> PendingAction:
    """The confirm prompt for a state change. Built from resolved facts, not
    from a model proposal — by the time this is called the item has already
    been resolved against the seller's own rows and the target is one of two
    verbs, so there is nothing left for a model to contribute except a chance
    to name a different item."""
    name = (item.name or "")[:60]
    if target == "pause":
        question = f"要把「{name}」下架吗？下架之后买家搜不到它，你随时可以再恢复上架。"
        rationale = "下架是可恢复的：商品和它的信息都还在，只是不再对买家可见。"
    else:
        question = f"要把「{name}」重新上架吗？恢复之后买家就能重新搜到它。"
        rationale = "恢复上架会把这件商品重新放回搜索结果。"
    return PendingAction(
        action="update_item_status",
        item_id=int(item.id),
        item_name=name,
        target=target,  # type: ignore[arg-type]
        question=question,
        rationale=rationale,
    )


def validate_proposal(
    db: Session, seller_id: int, raw: ActionProposal
) -> tuple[PendingAction | None, str]:
    """Turn a model proposal into something executable, or refuse it.

    Returns (pending, reason_it_was_dropped). A dropped proposal is not an
    error — the common case is that there is simply nothing worth proposing,
    and silence is the right answer then.
    """
    if raw.action != "push_to_buyers":
        return None, ""
    if not raw.item_id:
        return None, "no item_id"
    if not raw.card_ids:
        return None, "no candidates named"

    try:
        item: Item = tool_impl.resolve_owned_item(db, seller_id, int(raw.item_id))
    except tool_impl.ToolError as exc:
        # The model naming an item the seller does not own is not a proposal to
        # fix up — it is a proposal to drop.
        return None, f"item not owned: {exc}"

    if item.status not in ItemStatus.SEARCHABLE:
        return None, f"listing is {item.status}, not pushable"

    # Every card must resolve against the live candidate list. An ordinal that
    # has expired or was never in the list is dropped rather than guessed at:
    # "candidate-2" from a stale list is a different person.
    resolved = [
        cid for cid in dict.fromkeys(raw.card_ids)
        if tool_impl.resolve_candidate(seller_id, item.id, cid) is not None
    ]
    if not resolved:
        return None, "no card_id resolved against the current candidate list"

    cap = settings.want_max_push_per_seller_per_day
    if len(resolved) > cap:
        resolved = resolved[:cap]

    return (
        PendingAction(
            action="push_to_buyers",
            item_id=item.id,
            item_name=(item.name or "")[:60],
            card_ids=resolved,
            recipients=len(resolved),
            rationale=raw.rationale.strip()[:400],
            question=(raw.question.strip() or
                      f"要把「{(item.name or '')[:40]}」的通知发给这 {len(resolved)} 位"
                      f"匹配的买家吗？")[:200],
        ),
        "",
    )


def execute(db: Session, seller_id: int, pending: PendingAction) -> ActionResult:
    """Perform a confirmed action, dispatching on which one it is."""
    if pending.action == "update_item_status":
        return _execute_status_change(db, seller_id, pending)
    return _execute_push(db, seller_id, pending)


def _execute_status_change(
    db: Session, seller_id: int, pending: PendingAction
) -> ActionResult:
    """Pause or resume, through the same service the My Selling button uses.

    Ownership is re-checked here against the live row rather than trusted from
    the checkpointed proposal: the confirmation may arrive minutes later, and
    the proposal is a snapshot like every other one in this module.
    """
    from ...models import Item
    from ...repositories import UserRepository
    from ..item_index import get_item_index
    from ..lifecycle import LifecycleError, LifecycleService

    item = db.get(Item, int(pending.item_id))
    seller = UserRepository(db).get(int(seller_id))
    if item is None or seller is None or int(item.seller_id) != int(seller_id):
        return ActionResult(
            action=pending.action,
            item_id=int(pending.item_id),
            item_name=pending.item_name,
            failed=["这件商品已经不在了，或者它不是你的商品"],
            summary="什么都没有改。",
        )
    service = LifecycleService(db, get_item_index())
    cancelled_plans = 0
    try:
        if pending.target == "pause":
            service.pause(item, seller)
        elif pending.target == "resume":
            # 恢复上架会顺手作废挂在这件商品上的「收起来等」计划。那是对的——
            # 「我现在就要卖」和「等需求起来再卖」不能同时成立——但**必须说出来**，
            # 否则卖家几周后才会发现他以为还排着的提醒早就没了。
            cancelled_plans = service.resume(item, seller)
        else:
            raise LifecycleError(f"未知的状态变更：{pending.target!r}")
    except LifecycleError as exc:
        return ActionResult(
            action=pending.action,
            item_id=int(item.id),
            item_name=(item.name or "")[:60],
            failed=[str(exc)],
            summary="什么都没有改。",
            new_status=item.status,
        )
    done = "已下架" if pending.target == "pause" else "已重新上架"
    plan_note = (
        f"（顺带把它那条出售计划撤掉了，共 {cancelled_plans} 条——它已经在卖了，"
        "不需要再等需求起来。）"
        if cancelled_plans else ""
    )
    tail = (
        "买家现在搜不到它了；想卖的时候跟我说一声就能恢复。"
        if pending.target == "pause"
        else "它已经回到搜索结果里了。"
    )
    return ActionResult(
        action=pending.action,
        item_id=int(item.id),
        item_name=(item.name or "")[:60],
        succeeded=1,
        summary=f"「{(item.name or '')[:40]}」{done}。{tail}{plan_note}",
        new_status=item.status,
    )


def _execute_push(db: Session, seller_id: int, pending: PendingAction) -> ActionResult:
    """Send to the confirmed buyers. Every per-buyer condition is re-checked
    inside `push_item_to_want`; this only decides who to attempt and reports
    back honestly about who it could not reach."""
    from ...models import StandingWant
    from ..wants import PushError, push_item_to_want

    item = tool_impl.resolve_owned_item(db, seller_id, pending.item_id)
    succeeded = 0
    failed: list[str] = []
    for cid in pending.card_ids:
        want_id = tool_impl.resolve_candidate(seller_id, item.id, cid)
        if want_id is None:
            failed.append(f"{cid}：候选名单已过期，请重新获取一次")
            continue
        want = db.get(StandingWant, want_id)
        if want is None:
            failed.append(f"{cid}：这条买家求购已经不存在了")
            continue
        try:
            push_item_to_want(db, item, want)
            succeeded += 1
        except PushError as exc:
            failed.append(f"{cid}: {exc}")
        except Exception:  # noqa: BLE001
            logger.exception("[proposals] push failed for card %s", cid)
            failed.append(f"{cid}：刚才没能发出去")
    db.commit()

    if succeeded and not failed:
        summary = f"已发送给 {succeeded} 位买家。"
    elif succeeded:
        summary = f"已发送给 {succeeded} 位买家；有 {len(failed)} 位没能送达。"
    else:
        summary = "什么都没有发送。"
    return ActionResult(
        action=pending.action,
        item_id=item.id,
        item_name=(item.name or "")[:60],
        succeeded=succeeded,
        failed=failed,
        summary=summary,
    )
