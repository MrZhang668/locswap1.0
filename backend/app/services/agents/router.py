"""Orchestrator intent router — LLM call site D.

One cheap classification call in front of the specialists. It answers exactly
one question — *who should handle this message* — and hands the specialist a
de-referenced version of it.

Three properties this router is built around, each of which cost a real bug in
the design review:

**Facts come from the backend, not the model.** Whether the seller has anything
on sale is one SQL count. Asking a model to "check" a fact it cannot see is how
you get confident nonsense, so `has_active_items` and the listing summary are
injected as data.

**The uncertain default is `clarify`, not a specialist.** Defaulting to a
specialist means an ambiguous message silently lands in the wrong workflow;
defaulting to a question costs one sentence.

**A malformed decision is not a route.** The target is validated against a
whitelist and anything unexpected — bad JSON, an unknown agent name, a model
error, a missing key — collapses to `clarify` with a generic question rather
than being passed along.

**Slots are a closed set, re-checked here.** The router also extracts what the
specialist will need as parameters — which listing, what timeframe, which
category, what price. Every one of them is a constrained type, and `category` is
additionally checked against the platform's own list: a model-invented category
would travel all the way to a SQL filter and come back as "no data", which reads
like a fact about the market rather than a bad extraction. Same rule as the
publishing verdict — values that drive a query are recomputed server-side, never
trusted as returned.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...config import CATEGORY_DICT, settings
from ..prompts import load_prompt
from ..nlp import intent_rules
from . import item_lookup
from ..llm.langchain_client import CALL_SITE_ROUTER, build_chat_model, record_usage, router_model_name

logger = logging.getLogger(__name__)

RouteTarget = Literal["sales_agent", "publishing_agent", "defer_sale", "clarify"]

#: What the seller is asking the sales agent for. Drives which tools the
#: specialist reaches for; `none` is the honest answer for publishing and
#: clarify, and for a sales message that names no analysis at all.
#:
#: `sell_through` is the one that is a *plan* rather than a question. "帮我卖
#: 微波炉" names no analysis at all, but what the seller wants is the whole
#: workup — how many people are looking, what it should cost, who to contact.
#: Before it existed, such a message either went to clarify or stopped after
#: the first hop; making it an explicit scenario is what turns an implicit
#: multi-intent into a fixed, testable three-step plan.
Scenario = Literal["price", "demand", "both", "find_buyers", "sell_through", "none"]

#: Normalised timeframes. Deliberately an enum rather than free text: sellers say
#: "this semester" and "since I listed it", and the mapping from those words to a
#: date range is exactly what a model is good at — while everything downstream of
#: it wants a value it can put in a WHERE clause without parsing English.
#: `unsupported` is the exit the enum used to lack. "从我上架到现在" is a
#: relative anchor that maps to none of these buckets, and without a way to say
#: so the model picked the nearest one — usually 30 days — producing an answer
#: whose stated basis was not the one the seller asked about. A wrong window
#: reads exactly like a right one, which makes it worse than an empty slot.
TimeWindow = Literal["last_7d", "last_30d", "last_90d", "all", "unsupported", ""]

#: What state change a message asks for, when it asks for one. `delete` is
#: deliberately separate from `pause`: 「先不卖了」 means take it down for now and
#: 「删掉」 means it is gone, and collapsing the two would let one word's worth of
#: ambiguity destroy a listing.
StatusChange = Literal["pause", "resume", "delete", ""]

#: The whitelist the model's answer is checked against. Anything else is a bug
#: in the model's output, not a new capability.
VALID_TARGETS: tuple[str, ...] = (
    "sales_agent", "publishing_agent", "defer_sale", "clarify",
)

#: Scenario only means something for the sales agent. Publishing and clarify get
#: `none` no matter what the model said.
SALES_SCENARIOS: tuple[str, ...] = (
    "price", "demand", "both", "find_buyers", "sell_through",
)

FALLBACK_QUESTION = (
    "能再说得具体一点吗？我可以帮你看看已经上架的东西有多少人在找、价格合不合适，"
    "也可以帮你把一件新东西挂出来卖。"
)


class RouteSlots(BaseModel):
    """The parameters the specialist will need, pulled out of the message once.

    Closed by construction: four fields, `extra="forbid"`, every one of them a
    constrained type. Open-ended slot filling was the alternative and it is worse
    here — a slot nothing downstream reads is tokens spent for nothing, and a
    slot the model invents a name for cannot be validated at all. Each field
    below corresponds to an argument of a tool that already exists.
    """

    model_config = ConfigDict(extra="forbid")

    #: The seller's own words for which listing they mean ("the bike", "my
    #: kettle"). Not an id — resolving it is `list_my_items`' job, against the
    #: listings the seller actually owns.
    item_ref: str = Field(default="", max_length=120)
    time_window: TimeWindow = ""
    #: Checked against CATEGORY_DICT before it leaves this module.
    category: str = Field(default="", max_length=64)
    #: A number the seller named — an asking price, a budget, a target.
    price_hint: float | None = None
    #: The one slot that does not feed an analysis: which state change the
    #: seller asked for. It maps to `update_item_status`' `target` argument, so
    #: it obeys the same rule as the others — a value here must correspond to
    #: something downstream can actually do. Nothing is ever executed off this
    #: field alone; it only routes the turn to the confirmation gate.
    status_change: StatusChange = ""
    #: When the seller wants to sell *later*: the date they named, as
    #: YYYY-MM-DD. The deterministic parser in `intent_rules` runs first and
    #: wins; this is the fallback for wordings it has no rule for. Validated
    #: below like every other query-driving value — a date in the past or two
    #: years out is a parse failure wearing a date's clothes.
    target_date: str = Field(default="", max_length=10)


class RouteDecision(BaseModel):
    """What the router must return. `rewritten_query` is what the specialist
    actually receives, so it is required even for a clarify."""

    model_config = ConfigDict(extra="forbid")

    agent: RouteTarget
    scenario: Scenario = "none"
    slots: RouteSlots = Field(default_factory=RouteSlots)
    rewritten_query: str = Field(min_length=1, max_length=500)
    clarify_question: str = Field(default="", max_length=300)
    #: The message asks for something a seller-side advisor does not do at all
    #: (buying, refunds, contacting support, posting to another platform).
    #: Judged *before* the entity is read, because the failure being fixed here
    #: is a boundary decision that lost to a product name: "帮我联系一下买羊毛围
    #: 巾的那个人的客服" was pulled into the sales agent by the words 羊毛围巾.
    out_of_scope: bool = False


class RouteResult(BaseModel):
    """The decision plus what it cost and why — surfaced to the caller so a
    surprising route can be explained without re-running anything."""

    model_config = ConfigDict(extra="forbid")

    agent: RouteTarget
    scenario: Scenario = "none"
    slots: RouteSlots = Field(default_factory=RouteSlots)
    rewritten_query: str
    clarify_question: str = ""
    #: Carried through so the clarify node can state a boundary rather than ask
    #: a question — those are different replies to different problems.
    out_of_scope: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    #: Set when the model's answer could not be used and the fallback kicked in
    fallback_reason: str = ""
    #: Slot values that were dropped for failing validation, for the log. An
    #: empty list is the normal case; a non-empty one is worth reading.
    dropped_slots: list[str] = Field(default_factory=list)
    #: 丢弃掉的**非法**槽位值，连同卖家写下的原文。和 `dropped_slots` 分开，因为
    #: 这一份不是给日志看的：一个被丢掉的非法价格必须变成对卖家的一句回问，
    #: 而不只是一条少填的槽位（4.1.5）。
    slot_warnings: list[dict] = Field(default_factory=list)


def _facts_block(
    has_active_items: bool,
    listings: list[dict],
    has_photo: bool,
    hints: dict | None = None,
    last_context: dict | None = None,
) -> str:
    facts = {
        "has_active_items": has_active_items,
        "active_item_count": len(listings),
        # Whether the seller attached a photo to *this* message. Injected rather
        # than inferred: "selling this, still works fine" reads as ambiguous on
        # its own, and stops being ambiguous once you know a photo came with it —
        # but only the backend can see the attachment.
        "has_photo": has_photo,
        # 只给路由器看原来那四个字段、前 10 件。子类目和中心词是查找用的，放进
        # 来只会改变一份被评测压住了的提示词的输入，而路由器用不上它们。
        "listings": [
            {k: row.get(k) for k in ("item_id", "name", "price", "category")}
            for row in listings[:10]
        ],
    }
    if hints:
        # Computed in `intent_rules` before this call: which listing names the
        # message actually overlaps, plus a few deterministic readings of what
        # kind of message it is. `name_matches` is a fact (a token comparison
        # against the seller's own rows); the rest are advice, and the prompt
        # says which is which.
        facts["pre_router"] = hints
    if last_context:
        # What the previous turn produced. Present so a pronoun ("这些买家",
        # "第 1 个") resolves to the result set it refers to instead of
        # starting a fresh search — the multi-turn failure that looked like
        # the router drifting between scenarios.
        facts["last_turn"] = last_context
    return json.dumps(facts, ensure_ascii=False)


def _turns_block(history: list[dict[str, str]]) -> str:
    if not history:
        return "（没有更早的对话）"
    return "\n".join(f"{t['role']}: {t['content'][:300]}" for t in history)


def _sanitize(
    agent: str, scenario: str, slots: RouteSlots, illegal: list[dict] | None = None
) -> tuple[str, RouteSlots, list[str], list[dict]]:
    """Recompute the parts of the decision that drive a query.

    Pydantic already guarantees the *shapes*. What it cannot know is which
    values mean anything in this system: that a category has to be one the
    platform actually has, that a negative price is not a price, and that
    `scenario` is meaningless outside the sales agent. Each of those is checked
    here and dropped — never corrected into a guess — with the drop recorded so
    a model that keeps inventing categories shows up in the logs instead of
    silently producing empty analyses.

    `illegal` is the same check run **on the seller's own words**, before this
    call. It exists because the value-domain check below could never fire: the
    model normalises 「降到-50」 into `price_hint=50` and hands over a value that
    is legal in every way except that the seller never said it. A check that can
    only see the normalised value cannot tell that apart from a real $50 — only
    the raw text can, so `intent_rules.scan_illegal_prices` reads it there and the
    verdict arrives here as evidence.
    """
    dropped: list[str] = []
    warnings: list[dict] = []

    if agent not in SALES_SCENARIOS_ALLOWED_FOR:
        # A publishing or clarify turn has no analysis to run. Keeping a
        # scenario here would have the specialist answer a question the seller
        # did not ask.
        if scenario != "none":
            dropped.append(f"scenario={scenario!r} (not a sales route)")
        scenario = "none"

    clean = slots.model_copy()
    if clean.status_change and agent != "sales_agent":
        # The confirmation gate lives on the sales path. A status change asked
        # for anywhere else is dropped rather than carried, so it cannot arrive
        # at a node that has no gate in front of it.
        dropped.append(f"status_change={clean.status_change!r} (not a sales route)")
        clean.status_change = ""
    if clean.category and clean.category not in CATEGORY_DICT:
        # A hallucinated category reaches a SQL filter and comes back empty,
        # which reads as "no demand for this" rather than "bad extraction".
        dropped.append(f"category={clean.category!r}")
        clean.category = ""
    if clean.price_hint is not None and not (0 < clean.price_hint < 1_000_000):
        dropped.append(f"price_hint={clean.price_hint!r}")
        clean.price_hint = None
    for bad in (illegal or []):
        # 宽容归一化在多数字段上提升体验，在价格这种强约束字段上直接变成风险：
        # 「-50 → 50」之后，路由、场景、工具调用全部正确，只有卖家的意思被改了。
        # 丢弃，绝不修正——并把模型这一轮给出的值一起记下来，好让这种静默改写
        # 在跑批里可被自动检出。
        warnings.append({
            "field": str(bad.get("field") or "price"),
            "raw": str(bad.get("raw") or ""),
            "reason": str(bad.get("reason") or ""),
            "model_value": clean.price_hint,
        })
        dropped.append(
            "price_hint={!r} (原文 {!r} 非法：{})".format(
                clean.price_hint, bad.get("raw"), bad.get("reason")
            )
        )
        clean.price_hint = None
    if clean.item_ref:
        clean.item_ref = clean.item_ref.strip()[:120]
    if clean.target_date and not _plausible_date(clean.target_date):
        # A date the seller never said, or one the model mangled. Dropping it
        # is safe: the defer-sale node asks for a date it does not have, and
        # asking once is cheaper than silently scheduling a reminder for a day
        # nobody chose.
        dropped.append(f"target_date={clean.target_date!r}")
        clean.target_date = ""
    return scenario, clean, dropped, warnings


#: How far ahead a sale plan may be scheduled. Past this it is a parse failure,
#: not a plan — almost always a year that was read off the wrong side of a
#: month boundary.
MAX_PLAN_DAYS = 400


def _plausible_date(raw: str) -> bool:
    """A future date, within the horizon a semester-scale plan can live in."""
    try:
        parsed = date.fromisoformat(raw.strip())
    except (TypeError, ValueError):
        return False
    today = date.today()
    return today < parsed <= today + timedelta(days=MAX_PLAN_DAYS)


#: Routes for which `scenario` carries meaning at all.
SALES_SCENARIOS_ALLOWED_FOR: tuple[str, ...] = ("sales_agent",)


def apply_broad_sell_plan(
    agent: str, scenario: str, slots: dict, hints: dict, has_photo: bool = False
) -> tuple[str, str, dict]:
    """"帮我卖微波炉" — turn a broad sell request about an in-stock item into the
    three-step plan, deterministically.

    This used to be left to the prompt, and the prompt could not hold it: at
    temperature 0 the same message came back `sales_agent`, `publishing_agent`
    and `clarify` on three consecutive calls. That instability is not the
    model being unreasonable — the question it is being asked ("is this thing
    already listed?") is one the backend answered before the call, in
    `name_matches`. Deciding it here is the same rule the rest of this module
    follows: values that a fact determines are computed, not requested.

    Three things switch the override off, each of which makes the fact stop
    being decisive:

    * **a photo** — people photograph what they are about to list;
    * **a new-copy cue** ("我又收了一个微波炉") — the name matches a listing
      that is not the one being talked about;
    * **an ambiguous match** — two listings share the words, so *which* one is
      a real question; the specialist is handed the turn with an empty
      `item_ref` and asks it.
    """
    if agent == "clarify" and slots.get("status_change"):
        return agent, scenario, slots
    if not hints.get("broad_sell_intent") or has_photo or hints.get("new_item_cue"):
        return agent, scenario, slots
    matches = hints.get("name_matches") or []
    if not matches:
        # Nothing of the sort on sale: this is a listing job, and that call is
        # left to the model, which can read "我想卖我的旧显示器" as naming an
        # item and "怎么才能卖得快一点" as naming none.
        return agent, scenario, slots

    updated = dict(slots)
    winner = intent_rules.best_match(matches)
    if not updated.get("item_ref") and winner is not None:
        updated["item_ref"] = str(winner.get("name") or "")[:120]
    if scenario in ("", "none"):
        scenario = "sell_through"
    return "sales_agent", scenario, updated


def apply_followup_context(
    agent: str, scenario: str, slots: dict, hints: dict, last_context: dict
) -> tuple[str, str, dict]:
    """Bind a pronoun-only turn to what the previous one produced.

    The model does the resolving when it can; this is the backstop for when it
    does not, and it is deterministic on purpose — the input is "the message
    is all pronouns, the previous turn had a subject", and there is exactly
    one sensible reading of that.

    It also rescues such a turn from `clarify`. A follow-up to the result on
    the seller's screen is short and pronoun-heavy, which is the shape the
    router over-reads as ambiguous; but it is not ambiguous about the question
    `clarify` exists to settle — which specialist — because the same one
    answered a moment ago. Asking "which item?" about the only item on screen
    is the defect, not the guard.

    Returns the possibly-updated (agent, scenario, slots).
    """
    if agent not in ("sales_agent", "clarify"):
        return agent, scenario, slots
    if not hints.get("refers_to_previous") or slots.get("item_ref"):
        return agent, scenario, slots
    if not last_context.get("item_name"):
        # Nothing to bind to: this really is a message that needs a question.
        return agent, scenario, slots

    updated = dict(slots)
    updated["item_ref"] = str(last_context["item_name"])[:120]
    if scenario in ("", "none") and last_context.get("scenario") in SALES_SCENARIOS:
        # A follow-up about a result set is still about that kind of result.
        # Only fills a gap — a scenario the model actually chose is left alone.
        scenario = str(last_context["scenario"])
    return "sales_agent", scenario, updated


#: 卖家把选择权交出来（或者澄清已经问到上限）时执行的默认方案。
#: 选 `both` 而不是 `sell_through`：3.4.1 里系统问的就是「需求分析还是定价分析」，
#: 卖家答「都行」，那么正确的默认就是这两样都做——顺手把找买家也做了，是替他
#: 多做了一件他没被问到的事。
DEFAULT_AUTHORIZED_SCENARIO = "both"


def apply_clarify_convergence(
    agent: str,
    scenario: str,
    slots: dict,
    hints: dict,
    *,
    filled_slots: dict,
    user_authorized: bool,
    clarify_rounds: int,
    last_context: dict,
    has_active_items: bool,
    max_clarify_rounds: int,
) -> tuple[str, str, dict, str]:
    """让澄清收敛。返回 (agent, scenario, slots, 收敛原因)。

    澄清本身不是毛病——3.4.3 和 3.4.4 各 4 条全过，说明「问得太频繁」这个问题
    已经解决了。缺的是**终止条件**：已经填过的槽位没有写回，已经给出的授权没有
    记下来，于是同一个问题可以被无限次重问（3.4.1、3.4.2 各 4/4 全挂）。

    三条终止条件，按从强到弱的顺序：

    1. **已填的槽位不再问。** 跨轮累积的 `filled_slots` 补进本轮的空槽。卖家上
       一轮说过商品是「书架」，这一轮就不该再被问一次是哪件。
    2. **授权即执行。** 「都行，你决定」携带的信息是「别问了」。把它读成空输入，
       然后把同一组选项再摆一遍，是这 8 条里最刺眼的一种。
    3. **确认即绑定。** 「对，就那个」指向上一轮的待定对象——`last_context` 里
       那件商品——而不是一个新话题。
    4. **轮次上限。** 前三条都没命中、澄清还在继续时，按默认方案执行并在回复里
       说明这是默认方案。一个说明清楚的默认方案，比第三次同样的提问有用。

    有一件事**故意没做**：出场边界（`out_of_scope`）不在这里被覆盖。那是另一个
    判断——「这事不归我管」和「这事该怎么做」不该互相救场。调用方在边界判定之后
    才调这个函数。
    """
    updated = dict(slots)
    reason = ""

    # 1) 已填槽位写回。只补空位，模型这一轮真的抽到的值优先——卖家换了商品时，
    #    记忆不该盖住新说法。
    for key in ("item_ref", "category", "time_window", "price_hint"):
        if not updated.get(key) and (filled_slots or {}).get(key):
            updated[key] = filled_slots[key]

    if agent != "clarify":
        return agent, scenario, updated, reason

    # 2) 授权。`user_authorized` 是会话级的：卖家说过一次「你定」之后，再把同一
    #    组选项摆给他就是没听见。
    if (hints.get("is_authorization") or user_authorized) and has_active_items:
        agent = "sales_agent"
        scenario = scenario if scenario in SALES_SCENARIOS else DEFAULT_AUTHORIZED_SCENARIO
        reason = "authorized"
        return agent, scenario, updated, reason

    # 3) 确认。绑到上一轮谈的那件东西；`last_context` 为空时它确实无处可绑，
    #    那才是一次真正需要提问的澄清。
    if hints.get("is_confirmation"):
        ref = updated.get("item_ref") or (last_context or {}).get("item_name")
        if ref:
            updated["item_ref"] = str(ref)[:120]
            prev = (last_context or {}).get("scenario")
            agent = "sales_agent"
            if scenario not in SALES_SCENARIOS:
                scenario = prev if prev in SALES_SCENARIOS else DEFAULT_AUTHORIZED_SCENARIO
            reason = "confirmed"
            return agent, scenario, updated, reason

    # 4) 轮次上限。
    if clarify_rounds >= max_clarify_rounds and has_active_items:
        agent = "sales_agent"
        scenario = scenario if scenario in SALES_SCENARIOS else DEFAULT_AUTHORIZED_SCENARIO
        reason = "clarify_cap"

    return agent, scenario, updated, reason


def apply_defer_sale(agent: str, scenario: str, slots: dict, hints: dict) -> tuple[str, str, dict]:
    """「等学期末再挂」——这类消息有它自己的去处，不该被最近的专家吸收掉。

    归因二的 10 条 badcase 里，8 条是这个形状：卖家说了「以后再卖」，系统把它读成
    「现在要卖」，于是要么催他上传（publishing_agent），要么给他做需求分析
    （sales_agent）。两个回答都文不对题，而且都建立在同一个错误上——路由层没有
    「这件事要等」这个出口，只能把消息塞给已有的某一个 agent。

    和这个模块里另外两个覆盖函数一样，它是确定性的：判断依据是「未来时间锚点 +
    卖出动作」两个词法事实，不是模型的直觉。提示词也被告知了这条路由，但提示词
    按不住这种分叉（见 `apply_broad_sell_plan` 的注释），所以事实在服务端定。

    **状态变更优先。** 「这个先下架，等学期末再挂」里有一件**已经在售**的商品和
    一个立刻可执行的动作，那条路上有确认闸门；延后计划可以等他下架之后再说。
    """
    # 计划类动作先判。它的词表**要求点名「提醒 / 监控 / 盯 / 计划」**，所以
    # 「把台灯暂停一下」仍然走上下架，只有「把台灯的提醒暂停一下」会落到这里。
    # 顺序反过来的话，`_PAUSE_RE` 里的「暂停」会把一次关提醒读成一次商品下架——
    # 卖家只是想少收几封邮件，商品却从搜索里消失了。
    if hints.get("plan_action_intent") in ("watch_off", "watch_on", "cancel", "list"):
        return "defer_sale", "none", slots
    if hints.get("status_change_intent"):
        return agent, scenario, slots
    if not (hints.get("defer_sale_intent") or hints.get("wants_reminder")):
        return agent, scenario, slots
    if agent == "defer_sale":
        return agent, "none", slots
    return "defer_sale", "none", slots


def defer_item_in_stock(slots: dict, hints: dict, listings: list[dict], message: str) -> bool:
    """「以后再卖」的那件东西现在在不在库里——这决定走排计划还是先上架。

    路由器点了名（`item_ref`）时用 `item_lookup` 判，否则退回整条消息的分词比对
    （`name_matches`）。前者是 10-10 换上的：分词会让「自行车反光灯期末再卖」命中
    那辆自行车，这一轮于是被当成「在库」，一路走到计划 agent 那里去收东西。查找
    判不出来（模型调不通）时同样退回分词——那是这个判断原来的样子。
    """
    ref = str(slots.get("item_ref") or "").strip()
    if ref:
        found = item_lookup.resolve_ref(ref, listings, message=message)
        if found.decided:
            return bool(found.matches)
    return bool(hints.get("name_matches"))


# ---------------------------------------------------------------- 指代候选集
#: 一次指代最多列这么多个候选给卖家选。超过这个数，把清单摆出来本身就变成了
#: 一道题——那时候该说的是「你说的是哪一件」，而不是把十八件商品念一遍。
MAX_CANDIDATES_LISTED = 5

#: 候选集为空时的回复。**不是一个问题，是一句事实陈述**：卖家说「接着刚才的说」
#: 而这个会话一句话都还没说过，正确的反应是告诉他这里没有上文，不是问他想聊什么
#: ——后者听起来像是系统有上文但没听懂（6.3.1）。
NO_ANTECEDENT_REPLY = (
    "这个对话我这边还没有上文——你刚才说的内容不在这个会话里，我看不到。"
    "你想看哪一件商品？告诉我名字就行，我这就去查它的需求、价格和潜在买家。"
)

#: 候选不唯一时的提问模板。必须把候选**列出来**：只问一句「你说的是哪一件」，
#: 卖家还得自己回忆刚才聊过什么，而那个清单就在系统手里。
AMBIGUOUS_ITEM_QUESTION = "你说的是哪一件？{names}。告诉我是哪个，我马上去看。"

#: 多实体诉求但凑不出两件的时候。「两个都要」而候选只有一件，说明系统并不知道
#: 第二件是什么——这时候补上一句问，比默默只办一件强。
MULTI_ENTITY_QUESTION = "你说的「都要」是哪几件？{names}——把要看的那几件报给我就行。"


def _name_list(candidates: list[dict]) -> str:
    return "、".join(f"「{c.get('name')}」" for c in candidates[:MAX_CANDIDATES_LISTED])


def _dedup(candidates: list[dict]) -> list[dict]:
    """按 item_id 去重且保序。同一件商品在提及栈和名称匹配里各出现一次，
    读成两个候选就会凭空造出一次澄清。"""
    out: list[dict] = []
    seen: set[Any] = set()
    for c in candidates:
        key = c.get("item_id") if c.get("item_id") is not None else c.get("name")
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def reference_candidates(
    hints: dict, discussed: list[dict], listings: list[dict]
) -> tuple[list[dict], str]:
    """这一轮的指代候选集，以及它是从哪儿来的。

    三个来源，按可靠性排序——越靠前的越接近「卖家刚刚亲口说的」：

    1. **本轮消息里的名称匹配**。卖家自己写出了商品名，这是最强的证据。平票
       （两件商品的重合词数一样多）就是真歧义，不平票时 `best_match` 已经能定。
    2. **这个会话里谈过的商品**（`discussed`，最近的在前）。「那个再看一下」
       「回到前面那个」的候选集就是它——不是卖家的全部在售商品，而是**这段对话
       里出现过的**那几件。拿全部在售去当候选集，会让一个两件商品的对话变成
       十八个选项的选择题。
    3. **全部在售商品**。只给「我这几件在售的都帮我盘一下」这种把范围说满了的
       诉求用。指代类的问题永远不落到这一层。

    返回 `(候选列表, 来源)`。空列表 + `"none"` 表示确实无处可指。
    """
    matches = list(hints.get("name_matches") or [])
    if matches:
        top = int(matches[0].get("hits", 0))
        tied = [m for m in matches if int(m.get("hits", 0)) == top]
        return (_dedup(tied), "name")
    if discussed:
        return (_dedup(list(discussed)), "discussed")
    if listings:
        return (_dedup(list(listings)), "listings")
    return ([], "none")


def apply_reference_gate(
    agent: str,
    scenario: str,
    slots: dict,
    hints: dict,
    *,
    discussed: list[dict],
    listings: list[dict],
    last_context: dict,
    has_photo: bool,
) -> tuple[str, str, dict, list[str], dict, str]:
    """指代候选集判定：候选数 ≠ 1 的时候，不许挑一个往下做。

    返回 `(agent, scenario, slots, item_refs, ambiguity, question)`。

    这是本总类最大那块失败（归因一，12 条）唯一的硬约束。判定标准写得很清楚
    ——「存在歧义时应澄清，不得随机选一个」——而系统的反应是选定一件商品给出
    一份数据翔实、结构完整的分析。**失败的隐蔽性就是它的危害**：卖家没有理由
    怀疑那份分析说的是另一件东西，直到他自己发现。

    模型按不住这个分叉。它的默认倾向是「给出一个完整答案」而不是「反问」，而
    提示词里的「存在歧义时应澄清」是一句建议，敌不过那个倾向。所以规则在服务端：

    * 候选数 = 0 → 说明无法定位，直接说没有上文（不是反问「你想做什么」）；
    * 候选数 = 1 → 直接执行，绑上它；
    * 候选数 > 1 → 强制 `clarify`，把候选列出来让卖家选。

    并列的一条是**多实体**：「两个都要」不是歧义，是一个明确的多实体信号，
    正确的反应是生成实体列表逐个执行（`item_refs`），而不是挑一件。

    三种情形**故意不进这道闸门**：

    * 这一轮带了照片。「这个多少钱能卖」+ 一张图，「这个」指的是那张图，不是
      会话里谈过的某件商品。805 条 query 里有 6 条长这样。
    * 卖家写出了名字且唯一。「牛仔裤的需求和价格都看看」在字面上是多实体
      （「都看看」），但它有唯一的名称匹配——多的是任务不是商品。
    * 状态变更。「那几件下架吧」后面有自己的确认闸门，那条路上的候选集判定
      由闸门做，不在这里抢。
    """
    empty: tuple[list[str], dict, str] = ([], {}, "")
    if agent in ("publishing_agent", "defer_sale") or slots.get("status_change"):
        return (agent, scenario, slots, *empty)
    if has_photo:
        return (agent, scenario, slots, *empty)

    referent = str(hints.get("item_referent") or "")
    multi = bool(hints.get("multi_entity"))
    if not referent and not multi:
        return (agent, scenario, slots, *empty)

    candidates, source = reference_candidates(hints, discussed, listings)

    # 卖家自己写出了名字，而且只对上一件——那就不是指代问题。
    if source == "name" and len(candidates) == 1:
        updated = dict(slots)
        if not updated.get("item_ref"):
            updated["item_ref"] = str(candidates[0].get("name") or "")[:120]
        return (agent, scenario, updated, *empty)

    # ---- 多实体：生成实体列表，逐件执行 --------------------------------
    if multi:
        if len(candidates) >= 2:
            names = [str(c.get("name") or "") for c in candidates[:MAX_CANDIDATES_LISTED]]
            updated = dict(slots)
            updated["item_ref"] = names[0]
            return (
                "sales_agent",
                scenario if scenario in SALES_SCENARIOS else "both",
                updated,
                names,
                {"kind": "multi", "candidates": names, "source": source},
                "",
            )
        if not referent:
            # 说了「都要」，但系统只认得出一件。问，别默默办一件。
            return (
                "clarify", "none", dict(slots), [],
                {"kind": "multi_underspecified", "candidates":
                    [str(c.get("name") or "") for c in candidates]},
                MULTI_ENTITY_QUESTION.format(names=_name_list(candidates) or "我这边没找到候选"),
            )

    # ---- 指代：候选数决定去哪 -------------------------------------------
    if not candidates or (source == "listings" and referent):
        # 指代类的问题不拿「全部在售」当候选集：卖家说「接着刚才的说」而这个会话
        # 没有上文时，正确答案是说没有上文，不是把十八件商品摆出来让他挑。
        if last_context.get("item_name"):
            # 上一轮确实产出过东西，只是它没进提及栈（比如状态变更）。交给既有的
            # `apply_followup_context` 去绑，不在这里拦。
            return (agent, scenario, slots, *empty)
        return (
            "clarify", "none", dict(slots), [],
            {"kind": "no_antecedent"},
            NO_ANTECEDENT_REPLY,
        )

    if len(candidates) == 1:
        updated = dict(slots)
        updated["item_ref"] = str(candidates[0].get("name") or "")[:120]
        prev = (last_context or {}).get("scenario")
        if scenario not in SALES_SCENARIOS:
            scenario = prev if prev in SALES_SCENARIOS else scenario
        return ("sales_agent", scenario, updated, *empty)

    names = [str(c.get("name") or "") for c in candidates[:MAX_CANDIDATES_LISTED]]
    return (
        "clarify", "none", dict(slots), [],
        {"kind": "ambiguous", "candidates": names, "source": source,
         "backward": referent == "backward"},
        AMBIGUOUS_ITEM_QUESTION.format(names=_name_list(candidates)),
    )


#: 必须以工具结果为唯一事实来源的问题，各自对应的必调工具。
#: 分析类工具（analyze_demand / analyze_sold_prices）的这条约束一直是生效的——
#: 4.2.1 / 4.2.2 / 4.2.4 共 14 条全过。唯独「读取卖家自身状态」的 list_my_items
#: 没有被纳入同一套约束，大概率是因为它被当成了轻量的上下文查询而不是事实来源：
#: 提示词里甚至写着「`<my_listings>` 里已经是同样的数据」。于是计数问题凭上下文
#: 直接作答，同一个问题在三个会话里给出 9 / 9 / 18 三个答案（4.2.3）。
STATE_TOOLS: tuple[str, ...] = ("list_my_items",)


def apply_state_query(
    agent: str, scenario: str, slots: dict, hints: dict, has_active_items: bool
) -> tuple[str, str, list[str]]:
    """「我一共挂了几件东西」——答案只能来自工具，不能来自会话记忆。

    两件事一起做，因为它们是同一个判断的两面：这类消息**一定**归销售顾问答
    （问的是卖家自己的库存，既不是上架也不是延后），而它的答案**一定**要有
    `list_my_items` 的返回作依据。

    `scenario` 不动——这类消息确实没有点名任何一种分析，`none` 就是对的。必调
    工具是独立于场景的一条约束，这正是 4.2.3 漏掉的那一层：以前「必须调工具」
    只挂在分析类场景上，一个 `none` 场景的状态查询从来没有被任何规则罩住。
    """
    if not hints.get("state_query") or not has_active_items:
        return agent, scenario, []
    if slots.get("status_change"):
        # 「那几件下架吧」不是清点库存，它前面有确认闸门。
        return agent, scenario, []
    if agent in ("publishing_agent", "defer_sale"):
        return agent, scenario, []
    return "sales_agent", scenario, list(STATE_TOOLS)


def apply_buyer_count(agent: str, scenario: str, hints: dict) -> tuple[str, list[str]]:
    """「有几个买家在等手账本？」——问的是名单的长度，不是搜索的人数。

    这两个数字都真实存在，也都由工具返回，但口径不同：`find_potential_buyers`
    给的是眼下可以联系的公开求购，`analyze_demand` 给的是一段时间里搜过类似
    东西的去重买家数。把后者当成「在等的买家」报出去，数字有出处，结论却不是
    卖家问的那件事（4.2.5）。

    只在场景**没有被卖家自己点名**时覆盖：他要是同时问了价格，那是另一个问题。
    """
    if agent != "sales_agent" or not hints.get("buyer_count_question"):
        return scenario, []
    if scenario in ("demand", "none", ""):
        scenario = "find_buyers"
    return scenario, ["find_potential_buyers"]


#: 东西已经挂出去了，而卖家又说了一句「上传好了」时走的那条路。
#:
#: 它存在的理由是一条很朴素的事实：**上架是有终点的**。商品写进库那一刻这件事
#: 就办完了，可系统这边从来没有一个地方承认过这一点——`publish_text` 只累加不
#: 清空，路由器看着一段满是上架对话的历史，把下一句话继续送进上架流程，于是
#: 「上传好了」被当成商品描述拿去跑合规、抽取，再问一遍成色。卖家照做，再发布
#: 一次，库里就多了一件一模一样的东西。实测里那两条重复的乐高就是这么来的。
LISTING_DONE_ROUTE = "listing_done"


def apply_listing_done_gate(
    agent: str,
    hints: dict,
    publish_text: str,
    has_photo: bool,
    last_listed: dict,
) -> str:
    """一句光秃秃的「上传好了」，不是商品描述。

    上架流程要的是**内容**——一张照片，或者卖家对这件东西说过的话。这一轮两样
    都没有（照片没带，草稿箱是空的，因为上一件已经发布并由 `approve` 清掉了），
    那就没有任何东西可以抽取。送进去只会拿这四个字去跑一遍合规和抽取，然后回来
    问「这套积木是未拆封还是拼过」——问的是一件已经挂在货架上的商品。

    反过来，草稿箱**不空**的时候这道闸门不能拦：系统问「成色如何」、卖家答
    「弄好了」，那是一次正在进行的上架，「好了」是它的一部分。

    `last_listed` 是 `approve` 在写库成功那一刻留下的，一次性的：有它才说得出
    「哪件东西已经挂出去了」，而说得出，是这条回复和一句敷衍的区别。
    """
    if has_photo or (publish_text or "").strip():
        return agent
    if not hints.get("handoff_done"):
        return agent
    if not last_listed:
        # 没有「刚挂出去的东西」可以指认。这可能是卖家在别的什么事上说「好了」，
        # 交给原路由——这里宁可少管，也不要凭空宣布一次没发生过的上架。
        return agent
    if agent in ("status_change", "defer_sale"):
        # 这两条路上的「好了」有自己的含义（确认下架 / 确认计划），不归这里。
        return agent
    logger.info("[listing-done] already listed %s; not re-entering publishing",
                last_listed.get("name"))
    return LISTING_DONE_ROUTE


#: 卖家点名了一件东西、而他的在售列表里没有它时走的那条路。
#:
#: 它既不是一次澄清（没有什么要问的——他说得很清楚），也不是一次分析（没有数据
#: 可分析），所以它需要自己的名字。模型永远不会输出这个值：它不在 `RouteTarget`
#: 里，只由 `apply_missing_item_gate` 在后端事实的基础上产生。
MISSING_ITEM_ROUTE = "item_not_found"


def needs_item_resolution(
    agent: str, slots: dict, item_refs: list[str], hints: dict, has_photo: bool
) -> list[str]:
    """这一轮要不要去查「卖家点名的东西是他的哪一件」；要的话返回要查的说法。

    条件和 `apply_missing_item_gate` 的前几道放行完全一样——查的结果只有那道
    闸门用，闸门不看的轮次查了就是白花一次模型调用。
    """
    if agent != "sales_agent" or has_photo:
        return []
    if slots.get("status_change") or hints.get("state_query"):
        return []
    refs = [str(r).strip() for r in (item_refs or []) if str(r).strip()]
    if not refs:
        ref = str(slots.get("item_ref") or "").strip()
        refs = [ref] if ref else []
    return refs


def apply_missing_item_gate(
    agent: str,
    slots: dict,
    item_refs: list[str],
    hints: dict,
    listings: list[dict],
    has_photo: bool,
    resolution: dict | None = None,
) -> tuple[str, list[str]]:
    """卖家要分析的那件东西不在他的在售列表里。

    销售顾问的三个工具**都**以一件属于卖家的在售商品为入参——`tools.py` 里
    「item_id=… 不是你的在售商品」是一条硬拒绝——所以这一轮没有任何可执行的
    东西。以前它照样被送进 `sales_agent`，由模型自己写一段「我没找到你在售的
    水杯，请先确认是否已上架」：话是对的，但它只是一段文字。系统这一侧认为这
    一轮已经正常办完了，于是卖家最初那个诉求（需求 + 定价 + 找买家）没有被任何
    人记下来，等他上架完回来说「上传好了」，只能被重新问一遍「你想做什么」。

    判定是**两条事实的合取**，两条都来自后端对卖家自己那几行的分词比对，没有
    一条经过模型：

    * 路由器给出的实体（`item_refs`，退化时是 `slots.item_ref`）一个都对不上；
    * 整条消息也对不上任何在售商品（`hints.name_matches` 为空）。

    第二条是用来防第一条误伤的：卖家嘴里的「杯子」和他自己填进表单的「带盖饮料
    纸杯」分词之后不一定重叠，而整条消息的匹配面更宽。两条都空，才说明库里确实
    没有这件东西；只要有一条命中，这一轮就还有活可干，交给顾问去分辨。

    带照片的一轮不进这道闸门：照片本身就是「这件东西现在给你」，那是上架流程的
    入口，不是一次找不到。

    **给了 `resolution`（`item_lookup.resolve_refs` 的结果）时，判据换成它。** 分词
    比对在两个方向上都错——「台灯」对不上「LED护眼书桌灯」，「自行车反光灯」却对得上
    「Giant白色通勤自行车」——而后一种恰好会让第二条防误伤线把一个问反光灯的卖家
    放进一份自行车分析。所以：

    * 每个说法按判定结果算找没找到，不再用分词；
    * 整条消息的分词命中只在**没有被判定解释掉**时才放行（见
      `item_lookup.explained_away`）；
    * 任何一个说法没判成（模型调用失败），整轮退回原来的判据。
    """
    if agent != "sales_agent" or has_photo:
        return agent, []
    if slots.get("status_change") or hints.get("state_query"):
        # 前者后面有自己的确认闸门，后者问的是「我一共挂了几件」——两者都不需要
        # 一件具体的商品存在。
        return agent, []
    refs = [str(r).strip() for r in (item_refs or []) if str(r).strip()]
    if not refs:
        ref = str(slots.get("item_ref") or "").strip()
        refs = [ref] if ref else []
    if refs and resolution and all(
        resolution.get(r) is not None and resolution[r].decided for r in refs
    ):
        if item_lookup.explained_away(hints.get("name_matches") or [], refs, resolution):
            return agent, []
        missing = [r for r in refs if not resolution[r].matches]
        if len(missing) != len(refs):
            return agent, []
        logger.info("[missing-item] %s not listed (resolved); this is a listing job", missing)
        return MISSING_ITEM_ROUTE, missing
    if hints.get("name_matches"):
        return agent, []
    if not refs:
        # 他没点名任何东西（「我这几件都盘一下」）。那是另一类问题，顾问答得了。
        return agent, []
    missing = [
        r for r in refs
        if not intent_rules.best_match(intent_rules.match_listings(r, listings))
    ]
    if len(missing) != len(refs):
        # 至少有一件是找得到的：这一轮有真活可干，剩下那几件由顾问在回复里说明。
        return agent, []
    logger.info("[missing-item] %s not listed; this is a listing job, not an analysis", missing)
    return MISSING_ITEM_ROUTE, missing


def _fallback(reason: str, message: str, question: str = "") -> RouteResult:
    logger.warning("[router] falling back to clarify: %s", reason)
    return RouteResult(
        agent="clarify",
        rewritten_query=message[:500],
        clarify_question=question or FALLBACK_QUESTION,
        fallback_reason=reason,
    )


def route(
    *,
    message: str,
    history: list[dict[str, str]],
    has_active_items: bool,
    listings: list[dict],
    has_photo: bool = False,
    hints: dict | None = None,
    last_context: dict | None = None,
) -> RouteResult:
    """Classify one seller message. Never raises — routing failure degrades to a question."""
    model = router_model_name()
    try:
        chat = build_chat_model(
            model=model,
            temperature=settings.llm_temperature_router,
            max_tokens=settings.llm_max_tokens_router,
        )
        # include_raw keeps the underlying AIMessage reachable; without it the
        # structured-output wrapper returns only the parsed object and the
        # token usage for this call would be invisible.
        structured = chat.with_structured_output(RouteDecision, include_raw=True)
        result = structured.invoke(
            [
                ("system", load_prompt("orchestrator_router")),
                (
                    "human",
                    f"<categories>{', '.join(CATEGORY_DICT)}</categories>\n"
                    f"<seller_facts>"
                    f"{_facts_block(has_active_items, listings, has_photo, hints, last_context)}"
                    f"</seller_facts>\n"
                    f"<recent_turns>\n{_turns_block(history)}\n</recent_turns>\n"
                    f"<current_message>{message}</current_message>",
                ),
            ]
        )
    except Exception as exc:  # noqa: BLE001  network / auth / provider quirks
        return _fallback(f"router call failed: {type(exc).__name__}", message)

    raw = result.get("raw") if isinstance(result, dict) else None
    in_tok = out_tok = 0
    if raw is not None:
        in_tok, out_tok = record_usage(CALL_SITE_ROUTER, model, raw)

    decision = result.get("parsed") if isinstance(result, dict) else None
    if decision is None:
        parse_error = result.get("parsing_error") if isinstance(result, dict) else None
        out = _fallback(f"unparseable decision: {str(parse_error)[:120]}", message)
        out.input_tokens, out.output_tokens = in_tok, out_tok
        return out

    if decision.agent not in VALID_TARGETS:
        out = _fallback(f"unknown target {decision.agent!r}", message)
        out.input_tokens, out.output_tokens = in_tok, out_tok
        return out

    agent = decision.agent
    dropped_scope: list[str] = []
    if decision.out_of_scope and agent != "clarify":
        # Boundary beats entity. The model already said this request is outside
        # what the advisor does; letting it also pick a specialist is how a
        # product name in an out-of-scope sentence ("买羊毛围巾的那个人的客服")
        # ends up calling find_potential_buyers.
        dropped_scope.append(f"agent={agent!r} (out of scope)")
        agent = "clarify"

    question = decision.clarify_question.strip()
    if agent == "clarify" and not question:
        question = FALLBACK_QUESTION

    raw_slots = decision.slots.model_dump()
    scenario, slots, dropped, warnings = _sanitize(
        agent, decision.scenario, decision.slots, (hints or {}).get("illegal_price")
    )
    dropped = dropped_scope + dropped
    if dropped:
        logger.warning("[router] dropped unusable slot values: %s", "; ".join(dropped))
    final_slots = slots.model_dump()
    if final_slots != raw_slots:
        # 「原始槽位值 → 归一化后值」落盘。静默改写的特征是链路每一步都正常，
        # 所以能发现它的唯一地方就是这两个值的差；跑批可以直接扫这一行。
        logger.info(
            "[router] slot normalisation: message=%r raw=%s final=%s",
            message[:80], raw_slots, final_slots,
        )

    return RouteResult(
        agent=agent,
        scenario=scenario,
        slots=slots,
        rewritten_query=(decision.rewritten_query.strip() or message)[:500],
        clarify_question=question if agent == "clarify" else "",
        out_of_scope=bool(decision.out_of_scope),
        input_tokens=in_tok,
        output_tokens=out_tok,
        dropped_slots=dropped,
        slot_warnings=warnings,
    )
