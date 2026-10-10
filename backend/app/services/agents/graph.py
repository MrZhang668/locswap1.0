"""The agent graph: route once, then hand off to one specialist (LangGraph).

    START -> route -> sales_agent -> propose -(nothing to propose)-> END
                                             -(interrupt: seller answers)-> act -> END
                   -> status_change -(interrupt: seller confirms)-> act -> END
                   -> publishing_agent -> publish_compliance -> (block) -> END
                                                             -> publish_extract -> END
                   -> defer_sale -> END
                   -> clarify    -> END
                   -> item_not_found -> END   (named something they do not own)
                   -> listing_done   -> END   ("uploaded it" — and it is already live)
                   -> degraded   -> END

Why a graph rather than another hand-written branch:
* the conversation is checkpointed per thread, so history stops being something
  the browser posts back and starts being state the server owns;
* the routing decision, the rewritten query and the specialist's result are all
  in one inspectable state object rather than spread across local variables;
* the publishing flow is a multi-step pipeline with a real branch — a blocked
  submission must not reach extraction at all — which is exactly what this
  structure is for.

What deliberately did **not** move into the graph: the seller advisor's tool
loop. Its Pydantic validation, per-item ownership resolution, round caps,
result caching and server-side suppression of ungrounded output are the safety
properties of this feature. They stay in `seller_advisor.orchestrator`, and the
node here just calls it.

The SQLAlchemy session travels in `config["configurable"]`, never in the state:
state gets serialized into the checkpoint, and a live DB session must not.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command, interrupt
from sqlalchemy.orm import Session

from ...config import settings
from ...models import ItemStatus, User
from ...repositories import ItemRepository
from ..prompts import load_prompt
from ..seller_advisor.schemas import AdvisorChatTurn
from . import defer_intent, handoff, intent_plan
from . import proposals as prop
from . import router as router_mod
from .lc_llm import llm_configured, read_turn_meter, start_turn_meter

logger = logging.getLogger(__name__)

PUBLISH_RETRY_REPLY = (
    "刚才没能把这条检查完，麻烦你再发一次。"
)

#: Used only when the router flagged a request as out of scope but gave no
#: wording of its own. Says what this assistant does, not what it cannot do,
#: because the seller still has a real thing they were trying to get done.
OUT_OF_SCOPE_REPLY = (
    "这件事不在我这儿——我负责的是你在售商品的需求、定价和买家匹配。"
    "买东西、退款和联系买家分别在「找东西」「购物车」和消息页里。"
    "需要的话，我可以先帮你看看手上这几件现在有多少人在找。"
)

#: Attributes whose absence actually stops a listing from being useful. Colour,
#: material and size are nice to have; without these four a buyer cannot find
#: the item or judge it.
REQUIRED_DRAFT_FIELDS = ("name", "description", "category", "condition")


class AdvisorState(TypedDict, total=False):
    """Per-thread state. Everything here is checkpointed, so it must stay
    JSON-shaped — no ORM objects, no sessions."""

    seller_id: int
    message: str                 # the seller's raw message this turn
    turns: list[dict]            # persisted conversation, oldest first
    has_active_items: bool
    listings: list[dict]
    route: str
    #: Which analysis the sales agent should run; "none" for every other route.
    scenario: str
    #: The router's closed-set extraction, already validated. JSON-shaped
    #: because everything in this state is checkpointed.
    slots: dict
    rewritten_query: str
    clarify_question: str
    #: True when the router judged the request outside what a seller-side
    #: advisor does at all. Kept in state so the clarify node can state a
    #: boundary instead of asking a question.
    out_of_scope: bool
    #: The deterministic pre-router reading of this message (entity binding,
    #: broad-sell / status-change / coreference cues). Advice for the router
    #: and the resolution input for the status-change gate.
    hints: dict
    #: 这一轮被丢弃的非法槽位值，连同卖家的原话。专家看到它就必须先回问，
    #: 不得替他推断一个合法值（4.1.5）。
    slot_warnings: list[dict]
    #: 这一轮**必须**有结果的工具。服务端算出来的，与 `scenario` 无关：
    #: 「我一共挂了几件东西」的 scenario 是 none，但它的答案只能来自
    #: `list_my_items`（4.2.3）。
    required_tools: list[str]
    #: 卖家这一轮对**回复形式**提的要求（要多短、要不要表格）。和内容无关，所以
    #: 不走 scenario：「简单说，破壁机行情怎么样」问的还是行情。读自他的原话，
    #: 因为这是一条他下达的指令，不是一种可以由模型体会的风格偏好（5.3.3/5.3.4）。
    output_contract: dict
    # ---- session-level task state (see `handoff`) ----
    #: What the seller originally asked for, in their own words, when a hop
    #: through publishing got in the way of it.
    original_intent: str
    #: The analyses still owed to them. A list because a queue is a list — the
    #: single-value version of this is what dropped every item but the last.
    pending_tasks: list[str]
    #: The entities those analyses are about, as the seller named them.
    pending_items: list[dict]
    #: When the task was parked, ISO. Resuming binds to listings created after
    #: this moment, which is a fact about the database rather than a guess
    #: about which word the seller used in the listing form.
    handoff_at: str
    handoff_turn: int
    handoff_multi: bool
    #: 这次去上架，是因为卖家说了「以后再卖」。上传完成之后那件商品**默认隐身**
    #: （在库中、不曝光），而不是直接开卖——他说的就是「现在先不卖」。
    #: 服务端状态而不是前端标记：`publish/approve` 带着 thread_id 过来，能自己
    #: 查这个会话的状态，不需要相信客户端传了什么。
    hold_after_publish: bool
    #: Set on the turn a parked task is picked back up, so the reply can say
    #: what it is continuing rather than starting as if nothing preceded it.
    handoff_note: dict
    #: 这一轮卖家点名了、但他名下根本没有的商品。非空即意味着这一轮走的是
    #: `item_not_found`：没有可执行的分析，诉求已经挂账，回复由服务端写。
    missing_items: list[str]
    #: 这个会话里最近一次**真的发布成功**的商品（`{item_id, name, at}`）。由
    #: `approve` 在写库成功那一刻写进来——上架完成是一个发生在图之外的事件，
    #: 图这边只能靠它主动写一次才知道。用完即清：它回答的是「刚才挂出去的是
    #: 哪件」，而这个问题只在紧接着的那一两轮里成立。
    last_listed: dict
    # ---- clarification state machine (see `router.apply_clarify_convergence`) ----
    #: Slots the seller has already settled in this thread. Nothing here may be
    #: asked about a second time.
    filled_slots: dict
    #: The seller said "you decide". Session-level on purpose: handing the
    #: choice over once should not have to be repeated.
    user_authorized: bool
    #: Consecutive turns that ended in a question. Past the cap, a default plan
    #: runs instead of a third identical question.
    clarify_rounds: int
    #: Why a default plan was chosen instead of asking ("authorized" /
    #: "clarify_cap"). Empty when the seller's own words picked the route.
    forced_default: str
    #: Several listings this turn is about, when it is about several. `slots`
    #: holds one `item_ref` because a slot is one value; a resumed multi-item
    #: handoff is the case that needs all of them.
    item_refs: list[str]
    #: A recorded plan to sell something later — what the defer node wrote.
    sale_plan: dict
    #: What the previous turn produced: the listing it was about and a short
    #: summary of the buyer cards it returned. This is what makes "这些买家都
    #: 什么情况" resolvable — without it the turn is a fresh search whose
    #: result no longer matches what the seller is looking at.
    last_context: dict
    #: 这个会话里被真正分析过的商品，最近的在前，按 item_id 去重。
    #:
    #: 指代候选集就是它。不是卖家的全部在售商品——「那个再看一下」在一个谈过两件
    #: 东西的对话里是两个候选，在一个谈过一件东西的对话里是一个候选，而卖家名下
    #: 有多少件在售跟这句话指谁毫无关系。拿全部在售当候选集，会把一次澄清变成
    #: 一道十八选一的选择题。
    discussed_items: list[dict]
    #: 上一轮**产出**的结构化结论，最近的在前。
    #:
    #: 「那按这个价能找到人吗」里的「这个价」指的是这里的 price 类结论，不是
    #: 商品的 `price` 字段。两者都是数字、都真实存在，读错的那一版输出**完全
    #: 正常**——路由对、工具对，只有取值悄悄换了一个来源（6.1.2）。
    #:
    #: 只存结论，不存明细：候选买家的联系方式是活的工具结果，隔一轮复述一遍
    #: 就是拿一份可能过期的名单当现状。明细要用就重新调工具。
    result_stack: list[dict]
    #: 这一轮的指代判定结果：候选是空、唯一、还是不唯一。不唯一时它同时是
    #: 「必须澄清」的依据和（万一被授权/轮次上限逼出默认方案时）「必须自报
    #: 家门」的依据。
    ambiguity: dict
    router_fallback: str
    router_in: int
    router_out: int
    result: dict                 # the payload the API layer turns into AdvisorChatOut
    # ---- the outer advice loop ----
    #: Proposals made so far in this thread, against advisor_react_max_loops.
    react_loops: int
    #: The validated proposal the seller is being asked about, if any.
    pending_action: dict
    #: What the last confirmed action actually did.
    action_result: dict
    #: Set when a confirm prompt was answered with something that was not a yes;
    #: carries that message so it can still be answered on its own terms.
    declined_message: str
    # ---- publishing flow, accumulated across turns of one thread ----
    publish_text: str            # everything the seller has said about this item
    publish_photo: str           # relative path of the one uploaded photo
    publish_verdict: str
    publish_findings: list[dict]
    publish_draft: dict
    photo_path: str              # photo attached to *this* turn


def _db(config: RunnableConfig) -> Session:
    return config["configurable"]["db"]


def _seller_facts(db: Session, seller_id: int) -> tuple[bool, list[dict]]:
    """The one SQL fact the router is not allowed to guess at."""
    active = [
        i for i in ItemRepository(db).list_by_seller(int(seller_id))
        if i.status in ItemStatus.SEARCHABLE
    ]
    listings = [
        {
            "item_id": i.id,
            "name": (i.name or "")[:60],
            "price": float(i.price),
            "category": (i.attributes or {}).get("category"),
        }
        for i in active[:10]
    ]
    return bool(active), listings


def _recent(turns: list[dict]) -> list[dict[str, str]]:
    keep = settings.agent_session_history_turns * 2
    return [{"role": t["role"], "content": t["content"]} for t in turns[-keep:]]


# ---------------- Nodes ----------------
def route_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    db = _db(config)
    has_items, listings = _seller_facts(db, state["seller_id"])
    # Deterministic reading of the message, computed before the model sees it:
    # which of the seller's own listings its words overlap, and whether it
    # looks like a broad "just sell this", a status change, a follow-up to the
    # previous turn, a confirmation, an authorisation, or a "later, not now".
    # Entity binding belongs on this side — it is a token comparison against
    # rows we already hold, and asking a model to do it costs a call and buys a
    # chance to invent an item.
    hints = intent_plan.hints(state["message"], listings)
    last_context = state.get("last_context") or {}

    # With no key there is nothing to route to, and routing anyway would answer
    # a real question with a clarifying one — which reads as a broken product
    # rather than a missing configuration.
    ok, reason = llm_configured()
    if not ok:
        return {
            "has_active_items": has_items,
            "listings": listings,
            "route": "degraded",
            "scenario": "none",
            # Reset, not omitted: the state persists across turns of a thread,
            # so leaving these out would carry the *previous* turn's extraction
            # into this one.
            "slots": {},
            "hints": hints,
            "slot_warnings": [],
            "required_tools": [],
            "output_contract": {},
            "ambiguity": {},
            "out_of_scope": False,
            "rewritten_query": state["message"],
            "router_fallback": reason,
        }

    # ---- 「要卖，但不是现在」交给模型判 -------------------------------------
    # 调用点 H。这一层盖掉的是 `hints` 里那两个由正则算出来的值，盖的位置就是它们
    # 原来的位置——下游（`apply_defer_sale`、链路 2、`hold_after_publish`、计划
    # agent 的 `expected_action`）一个字都没改。换掉的是判断的来源，不是判断的
    # 后果：一个封闭词表认不出「我想在 2 个月后出售这支笔」，而那句话的两半意思
    # 都在字面上，模型读得出来。
    #
    # 跑在路由**之前**，因为路由的提示词把 `pre_router` 当事实读。让它看见一个
    # 过期的 `defer_sale_intent=false`，等于先给它一条错误证据再指望它判对。
    # 调用失败时这个函数退回原来的正则，所以这里不需要 try。
    defer_reading = defer_intent.classify(
        state["message"],
        history=_recent(state.get("turns", [])),
        has_photo=bool(state.get("photo_path")),
    )
    defer_intent.apply(hints, defer_reading)

    # ---- a parked task comes back first -----------------------------------
    # "上传好了" carries no routable information at all; handing it to a model
    # buys a coin flip. What it *does* carry is that the thing the previous hop
    # was waiting for is done — and what to do next is then computable from the
    # task queue plus one SQL read. This is the fix for 3.2.1 / 3.2.2, and it
    # deliberately runs before the router rather than after it: routing a
    # message whose answer is already known is a call spent to be overruled.
    carry: dict[str, Any] = {}
    resume = handoff.resume_target(db, state["seller_id"], state, state["message"], hints)
    if resume is not None and resume.get("expired"):
        carry.update(handoff.cleared())
    elif resume is not None and resume.get("items"):
        names = [str(i.get("name") or "") for i in resume["items"] if i.get("name")]
        logger.info("[handoff] resuming %s for %s", resume["scenario"], names)
        # 挂起的是哪种任务，就回哪个专家。「帮我卖小白板」回 sales_agent；
        # 「黑色笔记本量大了再卖」回 defer_sale——接力恢复的是**同一个**诉求，
        # 不是一个形状相近的替代品。
        resumed_route = (
            "defer_sale" if resume["scenario"] == handoff.DEFER_TASK else "sales_agent"
        )
        return {
            "has_active_items": has_items,
            "listings": listings,
            "hints": hints,
            "route": resumed_route,
            "scenario": "none" if resumed_route == "defer_sale" else resume["scenario"],
            "slots": {"item_ref": names[0] if names else ""},
            "slot_warnings": [],
            "required_tools": [],
            # 形式要求读自卖家最早那句话：现在这一句是「上传好了」，
            # 里面没有任何形式要求可读。
            "output_contract": intent_plan.output_contract(
                resume["original_intent"] or state["message"]),
            "item_refs": names,
            "ambiguity": {},
            "out_of_scope": False,
            # The seller's original words, not "上传好了" — the specialist is
            # answering the question they actually asked two turns ago.
            "rewritten_query": (resume["original_intent"] or state["message"])[:500],
            "clarify_question": "",
            "router_fallback": "",
            "router_in": 0,
            "router_out": 0,
            "clarify_rounds": 0,
            "handoff_note": {
                "original_intent": resume["original_intent"],
                "items": names,
                "unbound": resume["unbound"],
            },
            **handoff.cleared(),
        }

    decision = router_mod.route(
        message=state["message"],
        history=_recent(state.get("turns", [])),
        has_active_items=has_items,
        listings=listings,
        has_photo=bool(state.get("photo_path")),
        hints=hints,
        last_context=last_context,
    )
    slots = decision.slots.model_dump()
    route = decision.agent
    scenario = decision.scenario
    forced_default = ""
    # 服务端这一轮认定必须有结果的工具，以及被丢弃的非法槽位值。两者都不是
    # 模型的输出，所以都不经过它——它只会收到一段说明和一条硬要求。
    required_tools: list[str] = []
    #: 指代判定的产物。在 `out_of_scope` 分支里保持空值——「这事不归我管」
    #: 和「你说的是哪一件」是两个判断，不该互相救场。
    ambiguity: dict[str, Any] = {}
    item_refs_override: list[str] = []
    clarify_question = decision.clarify_question
    slot_warnings: list[dict] = list(decision.slot_warnings or [])
    if slot_warnings:
        logger.info("[router] illegal slot values dropped, will ask instead: %s", slot_warnings)

    if not decision.out_of_scope:
        # ---- broad sell intent: in stock means sell, not list -------------
        route, scenario, slots = router_mod.apply_broad_sell_plan(
            route, scenario, slots, hints, bool(state.get("photo_path"))
        )

        # ---- "later, not now" has its own exit ----------------------------
        # Runs before the follow-up and clarification overrides so that a
        # deferred sale is not first rescued into `sales_agent` and then
        # answered with an analysis the seller did not ask for.
        route, scenario, slots = router_mod.apply_defer_sale(route, scenario, slots, hints)

        # ---- 「以后再卖」的两条链路，由一个事实分开 ----------------------
        # 东西在不在库里。在库 → 直接收进隐身并排计划（`defer_sale`）；不在库 →
        # 先得有这件东西，所以走上架，而「以后再卖」这个诉求挂起来等它回来。
        #
        # 这个判断没有交给模型：`name_matches` 是后端对卖家自己那几行做的分词比对，
        # 是事实不是推断。让模型去读它，只是多一次掷硬币的机会。
        if (
            route == "defer_sale"
            and hints.get("defer_sale_intent")
            and not hints.get("name_matches")
            and not hints.get("wants_reminder")
        ):
            logger.info("[defer] nothing listed under that name; listing first")
            route, scenario = "publishing_agent", "none"

        # ---- coreference backstop -----------------------------------------
        before = (route, scenario, slots.get("item_ref"))
        route, scenario, slots = router_mod.apply_followup_context(
            route, scenario, slots, hints, last_context
        )
        if (route, scenario, slots.get("item_ref")) != before:
            logger.info(
                "[router] follow-up bound to the previous turn: %s -> %s",
                before, (route, scenario, slots.get("item_ref")),
            )

        # ---- 指代候选集：不唯一时不许挑一个 -------------------------------
        # 跑在接力上下文绑定**之后**：`apply_followup_context` 处理的是「上一轮
        # 刚产出过一个结果集，这一轮接着问它」，那种情形候选天然唯一，不该被
        # 这道闸门拦下。这道闸门管的是它管不了的那一半——候选是零个或者两个
        # 以上（6.2.1 / 6.1.6 / 6.2.2 / 6.3.1）。
        gate_before = (route, slots.get("item_ref"))
        route, scenario, slots, gate_refs, ambiguity, gate_question = (
            router_mod.apply_reference_gate(
                route, scenario, slots, hints,
                discussed=list(state.get("discussed_items") or []),
                listings=listings,
                last_context=last_context,
                has_photo=bool(state.get("photo_path")),
            )
        )
        if ambiguity:
            logger.info(
                "[reference] %s: %s (was %s)",
                ambiguity.get("kind"), ambiguity.get("candidates"), gate_before,
            )
        if gate_question:
            # 服务端写的问题盖掉模型写的那句。模型那句是在它**不知道候选集**的
            # 前提下写的，所以它问不出「是 A 还是 B」，只能问「你指哪件」——而
            # 那正是卖家答不上来的提法。
            clarify_question = gate_question
        if gate_refs:
            item_refs_override = list(gate_refs)

        # ---- clarification has to terminate -------------------------------
        # Everything the seller has already settled is written back, and a
        # question that would repeat itself is replaced by a default plan that
        # says it is one. Skipped for an out-of-scope turn on purpose: "this is
        # not something I do" is a boundary, not an unfinished question.
        route, scenario, slots, forced_default = router_mod.apply_clarify_convergence(
            route, scenario, slots, hints,
            filled_slots=state.get("filled_slots") or {},
            user_authorized=bool(state.get("user_authorized")),
            clarify_rounds=int(state.get("clarify_rounds", 0)),
            last_context=last_context,
            has_active_items=has_items,
            max_clarify_rounds=settings.advisor_max_clarify_rounds,
        )
        if forced_default:
            logger.info("[clarify] converged without asking again: %s", forced_default)
            # 收敛把一次「候选不唯一」的澄清变成了一个默认方案。槽位这时候还是
            # 空的（闸门故意没填），所以在这里补上排第一的候选——同时 `ambiguity`
            # 原样留在状态里，专家据此在回复开头自报家门（归因一 c）。没有这一步，
            # 默认方案会落在一个没有商品的分析上。
            cands = [str(c) for c in (ambiguity.get("candidates") or []) if c]
            if cands and not slots.get("item_ref"):
                slots = {**slots, "item_ref": cands[0][:120]}

        # ---- a question about the seller's own state is a tool call --------
        # 「我一共挂了几件东西」既不是分析也不是上架，而它的答案有一个唯一的
        # 事实来源。以前这类消息的 scenario 是 `none`，于是「必须调工具」那条
        # 约束一条都罩不到它，模型凭上下文报了一个数字——同一个问题在三个会话
        # 里给出三个不同的数字。
        route, scenario, required = router_mod.apply_state_query(
            route, scenario, slots, hints, has_items
        )
        required_tools.extend(required)

        # ---- "how many buyers" is a list length, not a search count -------
        scenario, required = router_mod.apply_buyer_count(route, scenario, hints)
        required_tools.extend(required)

    # ---- status change beats analysis ------------------------------------
    # 「把显示器下架吧」 is an instruction, not a question, and it used to be
    # answered with questions about brand and size. The router names the
    # change; the gate below it is what decides whether anything happens.
    if route == "sales_agent" and slots.get("status_change"):
        route = "status_change"
        scenario = "none"

    # ---- 点名了一件库里没有的东西：这不是分析，是一次上架前置 --------------
    # 跑在挂账**之前**，因为它改的正是挂账要读的那个路由结果。以前这一轮的路由
    # 是 `sales_agent`，顾问在回复里写一句「请先上架」就算办完了——话对，但没有
    # 任何东西被记下来，卖家上架完回来只能被重新问一遍想做什么。
    refs = item_refs_override or ([slots["item_ref"]] if slots.get("item_ref") else [])
    route, missing_items = router_mod.apply_missing_item_gate(
        route, slots, refs, hints, listings, bool(state.get("photo_path"))
    )

    # ---- 上架是有终点的 ----------------------------------------------------
    # 一句光秃秃的「上传好了」不是商品描述。草稿箱空着、这一轮也没带照片时，
    # 送进上架流程只会拿这四个字去跑一遍抽取，然后问一件已经挂在货架上的商品
    # 「成色如何」——卖家照做，库里就多一件一模一样的东西。
    route = router_mod.apply_listing_done_gate(
        route, hints, str(state.get("publish_text") or ""),
        bool(state.get("photo_path")), dict(state.get("last_listed") or {}),
    )

    # ---- park what this turn cannot deliver --------------------------------
    carry.update(
        handoff.capture(
            route=route,
            scenario=scenario,
            slots=slots,
            hints=hints,
            message=state["message"],
            turn_index=len(state.get("turns") or []) // 2,
            # 挂的是这一轮涉及的全部实体，不是 `slots` 里那唯一一个。
            item_refs=missing_items or refs,
        )
    )
    # 链路 2 的记号。跟着 handoff 一起挂起，也跟着一起清掉——一个没被清的
    # `hold_after_publish` 会让三轮之后一次无关的上架悄悄变成隐身。
    if route == "publishing_agent" and hints.get("defer_sale_intent"):
        carry["hold_after_publish"] = True

    return {
        "has_active_items": has_items,
        "listings": listings,
        "hints": hints,
        "route": route,
        "scenario": scenario,
        "slots": slots,
        "slot_warnings": slot_warnings,
        # 去重但保序：两条覆盖规则可能点到同一个工具。
        "required_tools": list(dict.fromkeys(required_tools)),
        "output_contract": hints.get("output_contract") or {},
        # 多实体判定给出的实体列表优先：`slots` 只放得下一个 `item_ref`，而
        # 「两个都要」的正确答案本来就是一个列表（6.2.2）。
        "item_refs": refs,
        # 每轮重置：上一轮找不到的那件东西，这一轮可能刚被传上来。
        "missing_items": missing_items,
        "ambiguity": ambiguity,
        "out_of_scope": decision.out_of_scope,
        "rewritten_query": decision.rewritten_query,
        "clarify_question": clarify_question,
        "router_fallback": decision.fallback_reason,
        "router_in": decision.input_tokens,
        "router_out": decision.output_tokens,
        "forced_default": forced_default,
        "handoff_note": {},
        # Remembered so the next turn cannot ask about them again, and reset to
        # zero whenever the turn actually got somewhere.
        "filled_slots": _remember_slots(state.get("filled_slots") or {}, slots),
        "user_authorized": bool(state.get("user_authorized")) or bool(hints.get("is_authorization")),
        "clarify_rounds": int(state.get("clarify_rounds", 0)) + 1 if route == "clarify" else 0,
        **carry,
    }


#: Slots worth carrying across turns. `status_change` is deliberately absent:
#: it is an instruction, and an instruction that survives its own turn is an
#: action waiting to fire at the wrong moment.
REMEMBERED_SLOTS = ("item_ref", "category", "time_window", "price_hint")


def _remember_slots(previous: dict, current: dict) -> dict:
    """What the seller has settled so far. A filled slot is never re-asked.

    Only non-empty values are written, and a new value overwrites the old one:
    the seller changing the subject is not the same as the seller repeating
    themselves, and the *latest* thing they named is the one they mean.
    """
    merged = dict(previous)
    for key in REMEMBERED_SLOTS:
        value = current.get(key)
        if value not in (None, "", []):
            merged[key] = value
    return merged


def sales_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    # Imported here, not at module scope: the advisor orchestrator pulls its
    # model client out of services.agents, so a top-level import would close a
    # cycle. The dependency only ever runs one way — graph calls advisor.
    from ..seller_advisor.orchestrator import run_advisor

    all_turns = state.get("turns", [])
    history = [
        AdvisorChatTurn(role=t["role"], content=t["content"][:2000])
        for t in _recent(all_turns)
    ]
    out = run_advisor(
        _db(config),
        state["seller_id"],
        state["rewritten_query"],
        history,
        scenario=state.get("scenario", "none"),
        slots=state.get("slots") or {},
        last_context=state.get("last_context") or {},
        item_refs=list(state.get("item_refs") or []),
        handoff_note=state.get("handoff_note") or {},
        forced_default=state.get("forced_default", ""),
        slot_warnings=list(state.get("slot_warnings") or []),
        required_tools=list(state.get("required_tools") or []),
        output_contract=dict(state.get("output_contract") or {}),
        result_stack=list(state.get("result_stack") or []),
        ambiguity=dict(state.get("ambiguity") or {}),
        # 这一轮看得见几轮、这个会话一共有几轮。两个数不相等时，专家必须知道
        # 它手上的历史是**被截断过的**——否则一个窗口外的目标价，它会当作
        # 「我没看到所以不存在」或者干脆编一个出来（6.3.2）。
        history_window=(len(history) // 2, len(all_turns) // 2),
    )
    payload = out.model_dump()
    return {
        "result": payload,
        "last_context": _carry_context(state, payload),
        "discussed_items": _push_discussed(state, payload),
        "result_stack": _push_results(state, payload),
        "turns": _append(state, out.reply),
        # Cleared per turn: a proposal the seller already answered must not be
        # re-offered, and a result from three turns ago must not be re-rendered.
        "pending_action": {},
        "action_result": {},
        "declined_message": "",
        # One-shot flags. Both describe *this* turn's provenance; carrying them
        # forward would make the next reply apologise for a default it did not
        # pick, or announce a handoff that finished two turns ago.
        "handoff_note": {},
        "forced_default": "",
    }


#: 提及栈和结果栈各留这么多条。栈是给指代解析用的，不是给审计用的——卖家说
#: 「前面那个」的时候，他脑子里的「前面」不会翻过八件商品。
MAX_DISCUSSED = 8
MAX_RESULTS = 6


def _analyzed(result: dict) -> list[dict]:
    """这一轮真正分析过的商品，去重保序。

    三份输出里的 `item_id` 是同一个口径（都是工具返回的那一件），所以合起来
    数就是「这一轮谈了哪几件」。用工具结果而不是槽位，是因为槽位是**打算**
    分析哪一件，工具结果是**实际**分析了哪一件——指代候选集只能由后者组成，
    否则一次失败的绑定会在栈里留下一个卖家从没看到过的候选。
    """
    rows: list[dict] = []
    for group in ("demand_analysis", "pricing_analysis", "potential_buyers"):
        for entry in result.get(group) or []:
            if entry.get("item_id"):
                rows.append({"item_id": entry["item_id"],
                             "name": str(entry.get("item_name") or "")[:60]})
    out: list[dict] = []
    seen: set[int] = set()
    for row in rows:
        if int(row["item_id"]) in seen:
            continue
        seen.add(int(row["item_id"]))
        out.append(row)
    return out


def _push_discussed(state: AdvisorState, result: dict) -> list[dict]:
    """这一轮谈过的商品进栈，最近的在前。

    重复提及**移到栈顶而不是新增一条**：一件商品被聊了三轮，它还是一个候选。
    没有这条去重，「那个再看一下」在一个只谈过一件东西的对话里也会变成三选一。
    """
    fresh = _analyzed(result)
    if not fresh:
        return list(state.get("discussed_items") or [])
    keep = {int(r["item_id"]) for r in fresh}
    previous = [
        r for r in (state.get("discussed_items") or [])
        if r.get("item_id") and int(r["item_id"]) not in keep
    ]
    return (fresh + previous)[:MAX_DISCUSSED]


def _price_conclusion(entry: dict) -> dict:
    """一条定价分析里可以被「这个价」指到的东西。

    存的是**区间和中位数**，不是建议价：卖家说「按这个价」时，指的是系统刚
    报出来的那段成交价，而三档建议价是模型给的三个数字，「这个」指不到它们
    中的哪一个。样本量一起存，因为一个 3 个样本的中位数和一个 40 个样本的
    中位数不是一回事，下一轮引用它的时候必须能说清楚。
    """
    stats = entry.get("stats") or {}
    return {
        "p25": stats.get("p25"),
        "median": stats.get("median"),
        "p75": stats.get("p75"),
        "min_price": stats.get("min_price"),
        "max_price": stats.get("max_price"),
        "sample_size": stats.get("sample_size"),
        "time_range": stats.get("time_range") or "",
        "confidence": entry.get("confidence") or "",
    }


def _push_results(state: AdvisorState, result: dict) -> list[dict]:
    """把这一轮的结论入栈，带 id 和类型，最近的在前。

    修的是归因二：上一轮的产出只以自然语言留在 `turns` 里，模型读得到那段话，
    却没法把它当成一个可以被「这个价」「第 2 个」指向的**对象**。于是「按这个
    价找人」用的是商品的当前标价——路由对、工具对，取值悄悄换了来源，而输出
    读起来完全正常（6.1.2）。

    三类结论各自可被指代的说法不同，所以类型是显式的：`price` 接「这个价 /
    那个区间」，`buyers` 接「第 N 个 / 这些人」，`demand` 接「这个热度 /
    刚才那个趋势」。

    **不存明细。** 候选买家是活的工具结果，隔一轮复述一份可能已经过期的名单，
    比不记得更糟。栈里存的是「上一轮结论是什么」，要用细节就重新调工具。
    """
    stack = list(state.get("result_stack") or [])
    turn = len(state.get("turns") or []) // 2
    fresh: list[dict] = []

    for entry in result.get("pricing_analysis") or []:
        if entry.get("empty"):
            continue
        fresh.append({
            "type": "price",
            "item_id": entry.get("item_id"),
            "item_name": str(entry.get("item_name") or "")[:60],
            "value": _price_conclusion(entry),
        })
    for entry in result.get("demand_analysis") or []:
        if entry.get("empty"):
            continue
        fresh.append({
            "type": "demand",
            "item_id": entry.get("item_id"),
            "item_name": str(entry.get("item_name") or "")[:60],
            "value": {
                "window_range": entry.get("window_range") or "",
                "total_people": entry.get("total_people"),
                "people_matching_item": entry.get("people_matching_item"),
                "active_wants": entry.get("active_wants"),
                "trend": entry.get("trend") or "",
            },
        })
    for group in result.get("potential_buyers") or []:
        cards = group.get("cards") or []
        if not cards:
            continue
        fresh.append({
            "type": "buyers",
            "item_id": group.get("item_id"),
            "item_name": str(group.get("item_name") or "")[:60],
            "value": {
                "count": len(cards),
                "ordinals": [
                    {"n": n, "card_id": c.get("card_id"),
                     "demand": (c.get("demand_summary") or "")[:60]}
                    for n, c in enumerate(cards[:8], start=1)
                ],
            },
        })

    if not fresh:
        return stack
    #: id 在栈里唯一且稳定：`r<轮次>-<序号>`。带上轮次，是为了让「刚才那个」和
    #: 「最开始那个」在需要时能被区分开。
    for n, row in enumerate(fresh, start=1):
        row["id"] = f"r{turn}-{n}"
        row["turn"] = turn
    return (fresh + stack)[:MAX_RESULTS]


def _carry_context(state: AdvisorState, result: dict) -> dict:
    """What the next turn needs to understand a pronoun.

    Only the *shape* of the last result travels — which listing it was about,
    what kind of analysis it was, and an outline of the buyer cards that were
    shown (ordinal, demand summary, budget, distance). Not the cards
    themselves: they are a live tool result, and replaying a stale copy a turn
    later is how a seller gets told about a buyer who is already gone. Anything
    that has to be accurate is looked up again; this is only here so the turn
    knows *what* to look up.

    Carried forward when the turn produced nothing new, so one intervening
    "谢谢" does not erase the subject of the conversation.
    """
    groups = result.get("potential_buyers") or []
    pricing = result.get("pricing_analysis") or []
    demand = result.get("demand_analysis") or []
    source = (groups or pricing or demand)
    if not source:
        return dict(state.get("last_context") or {})

    first = source[0]
    cards = [
        {
            "card_id": c.get("card_id"),
            "demand": (c.get("demand_summary") or "")[:60],
            "budget": c.get("budget_display"),
            "distance": c.get("distance_display"),
        }
        for g in groups
        for c in (g.get("cards") or [])
    ][:8]
    return {
        "item_id": first.get("item_id"),
        "item_name": (first.get("item_name") or "")[:60],
        "scenario": state.get("scenario", "none"),
        "buyer_cards": cards,
        "used_tools": result.get("used_tools") or [],
    }


def propose_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    """Ask whether an action follows from the analysis, then stop and wait.

    The stop is a real one. `interrupt()` suspends the graph inside this node
    and checkpoints it; nothing downstream runs until the thread is resumed with
    the seller's answer. That matters because the node after this one sends
    messages to people: a model that merely *said* it was waiting could be
    talked past in the same breath, and the only thing standing between a
    suggestion and a stranger's inbox would be the model's own restraint.
    """
    result = state.get("result") or {}
    db = _db(config)
    loops = int(state.get("react_loops", 0))

    if loops >= settings.advisor_react_max_loops:
        logger.info("[react] proposal budget spent (%d)", loops)
        return {}
    # Nothing to act on unless this turn actually looked up buyers: the only
    # action is pushing to them, and the candidate ordinals it needs exist only
    # in that tool's result.
    if "find_potential_buyers" not in (result.get("used_tools") or []):
        return {}

    raw = _ask_for_proposal(state, result)
    if raw is None:
        return {}
    pending, dropped = prop.validate_proposal(db, state["seller_id"], raw)
    if pending is None:
        if dropped:
            logger.warning("[react] proposal dropped: %s", dropped)
        return {}

    answer = interrupt(
        {
            "kind": "confirm_action",
            "action": pending.action,
            "item_name": pending.item_name,
            "recipients": pending.recipients,
            "rationale": pending.rationale,
            "question": pending.question,
        }
    )
    # Execution reached only on a clear yes. Everything else — a question, a
    # hedge, a change of subject — declines, and the message is answered as an
    # ordinary turn on the next pass.
    if not prop.is_affirmative(str(answer or "")):
        logger.info("[react] proposal declined")
        # The offer is dropped — deliberately, and it does not survive into the
        # next turn: a proposal kept alive is one the seller can trigger by
        # accident three messages later with an "ok" meant for something else.
        #
        # But a reply that is not a yes is usually still a real message ("what's
        # their budget?"), and consuming it as a bare decline answers nobody. It
        # is handed back for `run_turn` to put through a normal turn.
        return {
            "react_loops": loops + 1,
            "pending_action": {},
            "declined_message": str(answer or ""),
        }
    return {
        "react_loops": loops + 1,
        "pending_action": pending.model_dump(),
    }


def act_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    """Carry out a confirmed action. Every per-recipient condition is re-checked
    inside `push_item_to_want` against the live database — the candidate card
    the proposal was built from is a snapshot and is never the authority."""
    pending = state.get("pending_action") or {}
    if not pending:
        return {}
    result = dict(state.get("result") or {})
    try:
        outcome = prop.execute(_db(config), state["seller_id"], prop.PendingAction(**pending))
    except Exception as exc:  # noqa: BLE001
        logger.exception("[react] action failed")
        note = f"刚才没能发出去（{type(exc).__name__}）。什么都没有发送。"
        result["reply"] = (result.get("reply", "") + "\n\n" + note).strip()
        return {"result": result, "pending_action": {}, "turns": _append(state, note)}

    note = outcome.summary
    if outcome.failed:
        note += " " + "; ".join(outcome.failed[:3])
    result["reply"] = (result.get("reply", "") + "\n\n" + note).strip()
    return {
        "result": result,
        "action_result": outcome.model_dump(),
        "pending_action": {},
        "turns": _append(state, note),
    }


def _after_propose(state: AdvisorState) -> str:
    return "act" if state.get("pending_action") else "end"


def _ask_for_proposal(state: AdvisorState, result: dict) -> prop.ActionProposal | None:
    """One cheap structured call: given what was just found, is there a next
    step worth asking about? Returns None on any failure — a proposal that
    could not be produced cleanly is not worth reconstructing."""
    from .lc_llm import CALL_SITE_ROUTER, build_chat_model, record_usage, router_model_name

    cards = []
    for group in result.get("potential_buyers") or []:
        for c in group.get("cards") or []:
            cards.append({
                "item_id": group.get("item_id"),
                "card_id": c.get("card_id"),
                "demand": c.get("demand_summary"),
                "budget": c.get("budget_display"),
                "distance": c.get("distance_display"),
                "score": c.get("match_score"),
            })
    if not cards:
        return None

    try:
        chat = build_chat_model(
            model=router_model_name(),
            temperature=settings.llm_temperature_router,
            max_tokens=settings.llm_max_tokens_router,
        )
        out = chat.with_structured_output(prop.ActionProposal, include_raw=True).invoke(
            [
                ("system", load_prompt("advisor_proposal")),
                ("human",
                 f"<analysis>{result.get('reply', '')[:1200]}</analysis>\n"
                 f"<candidates>{json.dumps(cards[:10], ensure_ascii=False)}</candidates>"),
            ]
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[react] proposal call failed: %s", type(exc).__name__)
        return None
    raw = out.get("raw") if isinstance(out, dict) else None
    if raw is not None:
        record_usage(CALL_SITE_ROUTER, router_model_name(), raw)
    return out.get("parsed") if isinstance(out, dict) else None


def status_change_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    """「先不卖了」 — resolve which listing, then ask, then (only then) do it.

    Three things are deliberately not the model's to decide here:

    * **which listing** — resolved by name overlap against the seller's own
      rows, including the paused ones a resume would target, which are
      invisible to every read-only tool by design;
    * **whether it is unambiguous** — two candidates means a question, not a
      pick. Acting on the wrong one is a listing off sale that the seller
      still thinks is selling;
    * **whether it happens** — the same `interrupt()` gate the push action
      uses. The node suspends and is checkpointed; nothing runs until the
      seller answers, and an answer that is not consent leaves the listing
      exactly as it was.
    """
    db = _db(config)
    slots = state.get("slots") or {}
    target = str(slots.get("status_change") or "")
    seller_id = int(state["seller_id"])

    if target == "delete":
        # Permanent deletion is not offered here. It is the one change that
        # cannot be walked back, and a chat confirmation is a thinner guard
        # than the dialog in My Selling that says so in as many words.
        reply = (
            "永久删除我这边不做——它不可恢复。你可以在「我的橱窗」里点删除，"
            "那边会再跟你确认一次。"
            "如果你只是暂时不想卖了，我可以帮你下架：商品会保留着，随时能恢复上架。"
        )
        return {"result": {"ok": True, "reply": reply}, "turns": _append(state, reply)}

    item, note = _resolve_for_status(db, seller_id, state, target)
    if item is None:
        return {"result": {"ok": True, "reply": note}, "turns": _append(state, note)}

    pending = prop.build_status_change(item, target)
    answer = interrupt(
        {
            "kind": "confirm_action",
            "action": pending.action,
            "target": pending.target,
            "item_name": pending.item_name,
            "recipients": 0,
            "rationale": pending.rationale,
            "question": pending.question,
        }
    )
    if not prop.is_affirmative_for_status(str(answer or "")):
        # Same rule as the push gate: anything that is not consent is a
        # decline, and the message the seller actually sent is handed back to
        # be answered as an ordinary turn rather than spent as a "no".
        logger.info("[status] change declined for item %s", item.id)
        return {"pending_action": {}, "declined_message": str(answer or "")}
    # `result` is reset rather than left alone: it persists across turns, and
    # `act` appends its outcome to whatever is in there. Without this, the
    # confirmation of a delisting would come back with the previous turn's
    # pricing analysis stapled on top of it.
    return {"pending_action": pending.model_dump(), "result": {"ok": True, "reply": ""}}


def _resolve_for_status(
    db: Session, seller_id: int, state: AdvisorState, target: str
) -> tuple[Any, str]:
    """(item, reason_it_could_not_be_resolved). Exactly one of the two is set.

    Pausing looks at what is on sale; resuming looks at what is paused. Asking
    "which one" against the wrong set is how a resume request gets answered
    with "you have nothing paused" while the paused item sits right there.
    """
    from ...models import Item

    wanted = ItemStatus.RESUMABLE if target == "resume" else ItemStatus.PAUSABLE
    rows = [
        i for i in ItemRepository(db).list_by_seller(seller_id) if i.status in wanted
    ]
    if not rows:
        if target == "resume":
            return None, "你名下没有暂停中的商品，没有可以恢复上架的东西。"
        return None, "你现在没有在售的商品，没有可以下架的东西。"

    listings = [{"item_id": i.id, "name": (i.name or "")[:60]} for i in rows]
    by_id = {int(i.id): i for i in rows}

    # The seller's own words for the item first, then the raw message. The
    # rewritten query is skipped on purpose: it is the router's paraphrase, and
    # a paraphrase that substituted a listing name would bind the match to a
    # word the seller never said.
    for text in (str((state.get("slots") or {}).get("item_ref") or ""), state["message"]):
        matches = intent_plan.match_listings(text, listings) if text else []
        winner = intent_plan.best_match(matches)
        if winner is not None:
            return by_id[int(winner["item_id"])], ""
        if matches:
            names = "、".join(f"「{m['name']}」" for m in matches)
            return None, f"你是说 {names} 里的哪一件？说一下名字我就帮你处理。"

    last = state.get("last_context") or {}
    if last.get("item_id") and int(last["item_id"]) in by_id:
        return by_id[int(last["item_id"])], ""

    if len(rows) == 1:
        return by_id[int(rows[0].id)], ""

    names = "、".join(f"「{(i.name or '')[:30]}」" for i in rows[:6])
    verb = "恢复上架" if target == "resume" else "下架"
    return None, f"你想{verb}哪一件？我这边能看到：{names}。"


#: 卖家要求提醒、但既没说商品也没说时间时问的那一句。问一次，问全，而不是
#: 一轮问一个——归因三说的就是这个。
DEFER_ASK_BOTH = (
    "这个我能做：你定一个打算开卖的日子，我会从那天往前 {weeks} 周开始，"
    "每周替你看一次这一类东西的行情，买的人多了就在站内通知你并发一封邮件，"
    "需求涨得快就发邮件提醒你可以上架了。\n"
    "告诉我两件事就行——想卖的是什么，打算什么时候卖（「12 月放假前」这种说法就够）。"
)

DEFER_ASK_DATE = (
    "「{item}」我记下了。打算什么时候开卖？说个大概就行，"
    "「学期末」「12 月放假前」「下个月」我都能换算成具体日期。"
)

#: 能力边界的那一句。写死在这里而不是交给模型临场发挥，因为这句话的每一个字都是
#: 承诺：3.3.4 的教训是，一旦口头答应了一个不存在的行为，卖家会按它安排自己的
#: 时间，信任损伤远大于一次答错。所以系统只说它真的会做的事，并且把不做的事
#: 也说出来。
DEFER_LIMITS = (
    "说清楚我会做什么：从{watch_from}起，每周替你看一次{category_phrase}。"
    "一周的预订超过 {reserve} 单，我就在站内通知你并发一封邮件到 {email}；"
    "需求比上一周涨了 {growth}% 以上，我发一封邮件到 {email} 提醒你可以上架了。"
    "到{target}为止。\n"
    "我不会做的事：不会替你自动上架，也不会在这两个条件之外主动找你。"
    "想提前挂出来随时去「手动上架」；不想等了就跟我说一声，我把这条计划撤掉。"
)

#: 没有类别的计划里，「一周预订超过 N 单」那条**根本不会触发**——预订事件带的是
#: item_id 而不是措辞，没有类别就没法归因。继续把它列在「我会做什么」里，等于在
#: 同一段话里先说它用不上、再把它当成承诺。少列一条，好过自相矛盾。
DEFER_LIMITS_NO_CATEGORY = (
    "说清楚我会做什么：从{watch_from}起，每周替你看一次{category_phrase}。"
    "这个数比上一周涨了 {growth}% 以上，我就发一封邮件到 {email} 提醒你可以上架了。"
    "到{target}为止。\n"
    "我不会做的事：不会替你自动上架，也不会在这个条件之外主动找你。"
    "想提前挂出来随时去「手动上架」；不想等了就跟我说一声，我把这条计划撤掉。"
)


def defer_sale_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    """出售计划这一路的入口：交给 `plan_agent`（调用点 G）。

    这个节点自己不做业务判断，和 `sales_node` 是同一个形状——图负责把轮次路由到
    正确的专家，专家自己带工具、带提示词、带轮次上限。本轮之前它是一段写死的
    程序逻辑，现在那段逻辑退到了兜底位置：

    * **有模型** → `run_plan_agent`。卖家可以改日期、撤计划、开关监控、问行情，
      而不只是「排一条」；
    * **没有模型** → `_deterministic_plan_node`。这条路一次调用都不花，所以没有
      API key 的环境里「记一条计划」仍然能用。一个只在配好 key 时才存在的承诺，
      对着 demo 环境就是一句空话。

    确定性的东西**没有交给模型**，而是作为事实注入（`plan_agent` 里的
    `<parsed_date>` / `<my_plans>` / `<watch_switch>`）：日期换算仍然由
    `intent_plan.parse_target_date` 做，承诺文案仍然由工具层生成。模型拿到的是
    选择权，不是这些值的编辑权。
    """
    ok, _ = llm_configured()
    if not ok:
        return _deterministic_plan_node(state, config)

    from ..plan_agent import run_plan_agent

    history = [
        AdvisorChatTurn(role=t["role"], content=t["content"][:2000])
        for t in _recent(state.get("turns", []))
    ]
    out = run_plan_agent(
        _db(config),
        state["seller_id"],
        state["message"],
        history,
        required_action=str((state.get("hints") or {}).get("plan_action_intent") or ""),
        # 调用点 H 算出来的日期，只在词法规则认不出来时才会被用上（那一头自己判）。
        # 不给的话，「2 个月后」这种说法会一路走到计划 agent 那里变成一句「你打算
        # 哪天开卖」——而卖家刚刚才说过。
        fallback_date=str((state.get("hints") or {}).get("defer_target_date") or ""),
    )
    payload = out.model_dump()
    return {
        "result": payload,
        "turns": _append(state, out.reply),
        # 计划这一路不产生买家卡片和价格卡，所以不写 `last_context`——写了会让
        # 下一轮的指代绑到一件根本没被分析过的商品上。
        "pending_action": {},
        "action_result": {},
        "declined_message": "",
    }


def _deterministic_plan_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    """没有模型可用时的兜底：确定性地记一条计划。

    这个节点存在的理由是**能力缺口需要一个出口**。归因二的 10 条 badcase 里，
    这类诉求被路由强行分配给了 publishing_agent（催上传）或 sales_agent（做需求
    分析），两个回答都答非所问；而最贵的一条是系统对「需求涨起来了你能提醒我吗」
    只做澄清，既没答应也没拒绝——口头承诺一个不存在的能力，卖家会按它安排时间。

    所以这里不是一段更委婉的话术，而是真的把事情办了：记一条计划，说清楚触发
    条件，然后由 `services.sale_plans` 的周扫描去兑现。

    三件事故意留在服务端：

    * **日期换算** —— 「12 月放假前」落到哪一天，是 `intent_plan.parse_target_date`
      的确定性结果，不是模型的即兴发挥；而且换算结果会被**说出来**，因为一个猜错的
      日期和一个猜对的日期在回复里长得一模一样；
    * **承诺的措辞** —— 写死在上面的常量里。能力边界不是每轮重新组织语言的地方；
    * **不问第二遍** —— 商品和日期缺哪个就一次问齐，缺两个就问一句话，这是归因三
      的同一条规矩。
    """
    db = _db(config)
    message = state["message"]
    hints = state.get("hints") or {}
    slots = state.get("slots") or {}

    # ---- 哪件东西 ----
    item_name = str(slots.get("item_ref") or "").strip()
    item_id = None
    matches = hints.get("name_matches") or []
    winner = intent_plan.best_match(matches) if matches else None
    if winner is not None:
        # 名字对上了一件在售商品：计划挂到它身上，卖家想先下架再等也说得通。
        item_name = item_name or str(winner.get("name") or "")
        item_id = int(winner["item_id"])

    # ---- 什么时候 ----
    target, basis = intent_plan.parse_target_date(message)
    if target is None and hints.get("defer_target_date"):
        # 调用点 H 算出来的那个日期。排在词法规则之后、路由槽位之前：这三者都
        # 可能给出日期，而顺序就是可信度顺序——能算的不问模型，要问也问那个
        # **只读了这一句话**的调用，而不是那个同时在选 agent、填场景的调用。
        try:
            target = date.fromisoformat(str(hints["defer_target_date"]))
            basis = "model"
        except ValueError:
            target = None
    if target is None and slots.get("target_date"):
        # 词法规则没覆盖到的说法，用路由器抽的那个值兜底。它已经过了
        # `_plausible_date` 校验，不是随便一个字符串。
        try:
            target = date.fromisoformat(str(slots["target_date"]))
            basis = "model"
        except ValueError:
            target = None

    if target is None and not item_name:
        reply = DEFER_ASK_BOTH.format(weeks=settings.sale_plan_watch_weeks)
        return {"result": {"ok": True, "reply": reply}, "turns": _append(state, reply)}
    if target is None:
        reply = DEFER_ASK_DATE.format(item=item_name)
        return {"result": {"ok": True, "reply": reply}, "turns": _append(state, reply)}
    if not item_name:
        reply = DEFER_ASK_BOTH.format(weeks=settings.sale_plan_watch_weeks)
        return {"result": {"ok": True, "reply": reply}, "turns": _append(state, reply)}

    # ---- 记下来 ----
    from ..sale_plans import MAX_ACTIVE_PLANS_PER_SELLER, active_plan_count, create_plan

    seller_id = int(state["seller_id"])
    if active_plan_count(db, seller_id) >= MAX_ACTIVE_PLANS_PER_SELLER:
        reply = (
            f"你已经有 {MAX_ACTIVE_PLANS_PER_SELLER} 条在盯的出售计划了，这是上限。"
            "先撤掉一条再加新的吧——告诉我撤哪件就行。"
        )
        return {"result": {"ok": True, "reply": reply}, "turns": _append(state, reply)}

    plan = create_plan(
        db, seller_id,
        item_name=item_name,
        target_date=target,
        date_basis=basis,
        note=message[:300],
        item_id=item_id,
    )

    user = db.get(User, seller_id)
    reply = _defer_reply(plan, basis, bool(item_id), user.email if user else "")
    view = {
        "plan_id": plan.id,
        "item_name": plan.item_name,
        "category": plan.category,
        "target_date": plan.target_date.isoformat(),
        "watch_from": plan.watch_from.isoformat(),
        "date_basis": plan.date_basis,
    }
    return {
        "sale_plan": view,
        "result": {"ok": True, "reply": reply, "sale_plan": view},
        "turns": _append(state, reply),
    }


#: 日期是怎么来的，用卖家看得懂的话说一遍。`explicit_date` 不在表里——卖家自己
#: 说了确切日期时，把它复述一遍是废话。
_BASIS_PHRASE = {
    "explicit_month": "你说的是 {month} 月，我记的是 {date}",
    "semester": "「学期末 / 放假前」我记的是 {date}",
    "relative": "我把它换算成了 {date}",
    "fuzzy": "你没说具体时间，我先记成 {date}",
    "model": "我把它理解成了 {date}",
}


def _cn_date(value: date) -> str:
    """「2026 年 11 月 3 日」。不走 strftime 是因为 `%d` 会给出 03，而没有人这么说
    日期；`%-d` 又在 Windows 上不可用。"""
    return f"{value.year} 年 {value.month} 月 {value.day} 日"


def _cn_md(value: date) -> str:
    return f"{value.month} 月 {value.day} 日"


def _defer_reply(plan: Any, basis: str, already_listed: bool, email: str) -> str:
    """回执。三段：记下了什么、日期是怎么来的、我会做和不会做什么。

    中间那一段是刻意加的。系统推断出来的日期必须被**说出来**——它和一个猜对的
    日期在回复里长得一模一样，只有摆出来卖家才有机会改。
    """
    lines = [f"记下了：「{plan.item_name}」，{_cn_date(plan.target_date)}开卖。"]

    phrase = _BASIS_PHRASE.get(basis)
    if phrase:
        lines.append(
            phrase.format(month=plan.target_date.month, date=_cn_md(plan.target_date))
            + "，不对的话告诉我具体日期，我改。"
        )

    if not plan.category:
        # 没归进任何类别时，预订那条判据**在这条计划上是失效的**——预订事件带的是
        # item_id 不是措辞，没有类别就没法归因。说出来，是因为一个不会触发的条件
        # 被列在「我会做什么」里，就是一个不会兑现的承诺。
        lines.append(
            f"有一点要说在前面：「{plan.item_name}」我没能归进平台现有的类别，"
            "所以只能按名字去匹配买家的搜索措辞——数字会偏小，但它是真的。"
            "代价是「一周预订超过多少单」那条判断在这件东西上用不上，"
            "下面只列了实际会生效的那一条。想让它更准的话，告诉我它属于哪一类。"
        )

    if already_listed:
        lines.append(
            "这件现在还挂在架上。想先收起来的话跟我说「下架」，我会问你一句再动手；"
            "留着继续卖也行，这条计划不影响它。"
        )

    template = DEFER_LIMITS if plan.category else DEFER_LIMITS_NO_CATEGORY
    lines.append(
        template.format(
            watch_from=_cn_date(plan.watch_from),
            category_phrase=(
                f"「{plan.category}」这一类的行情" if plan.category
                else f"有多少人在搜和「{plan.item_name}」对得上的东西"
            ),
            reserve=settings.sale_plan_reserve_threshold,
            growth=int(settings.sale_plan_growth_threshold * 100),
            email=email or "你的注册邮箱",
            target=_cn_md(plan.target_date),
        )
    )
    return "\n".join(lines)


def publishing_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    """Intake: fold this turn into what the seller has already told us.

    Publishing is a conversation, not a form submit — the first message rarely
    contains everything. Accumulating here means the follow-up answer ("it's a
    Trek, ridden two years") is reviewed and extracted together with the
    original description rather than on its own, where it would make no sense.
    """
    said = state.get("publish_text", "")
    combined = (said + "\n" + state["message"]).strip() if said else state["message"]
    photo = state.get("photo_path") or state.get("publish_photo", "")
    return {"publish_text": combined[:4000], "publish_photo": photo}


def publish_compliance_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    """Call site E. Runs before extraction so a prohibited item never reaches
    the step that would write nice copy for it.

    代价是这一步手上只有**卖家打的字**，而上架流程的正常用法是「发一张照片，说
    一句上传好了」——商品描述是下一步从照片里抽出来的。所以判「写得够不够」的
    那两条规则在这里被摘掉（`SUPPRESSED_IN_PUBLISHING`）：它们判的那份文本还不
    存在，必然命中，而卖家看到的是一条他无法照办的警告——他已经写好的描述就在
    下面那张表里。它们问的事，那张表里两秒钟就能选完。
    """
    from ..publishing.pipeline import PublishingError, review_compliance
    from ..publishing.rules import publishable_findings

    try:
        verdict, findings, in_tok, out_tok = review_compliance(
            _db(config), text=state["publish_text"], photo_path=state.get("publish_photo", "")
        )
    except PublishingError as exc:
        logger.warning("[publishing] compliance step failed: %s", exc)
        return {
            "publish_verdict": "error",
            "result": {"ok": False, "reply": PUBLISH_RETRY_REPLY, "message": str(exc)[:200]},
            "turns": _append(state, PUBLISH_RETRY_REPLY),
        }

    kept = publishable_findings(findings)
    if len(kept) != len(findings):
        logger.info(
            "[publishing] not showing %s — a form field, not a chat requirement",
            [f.code for f in findings if f not in kept],
        )
    findings = kept
    payload = {
        "publish_verdict": verdict,
        "publish_findings": [f.model_dump() for f in findings],
        "router_in": state.get("router_in", 0) + in_tok,
        "router_out": state.get("router_out", 0) + out_tok,
    }
    if verdict != "block":
        return payload

    reply = (
        "这件东西不能发布。"
        + " ".join(f"{f.title}：{f.reason}" for f in findings if f.severity == "block")
    )
    payload["result"] = {
        "ok": True,
        "reply": reply,
        "publish_findings": payload["publish_findings"],
        "publish_blocked": True,
    }
    payload["turns"] = _append(state, reply)
    return payload


def publish_extract_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    """Call site F: structured content plus an honest list of what is missing."""
    from ..publishing.pipeline import PublishingError, extract_listing
    from ..publishing.schemas import field_label

    try:
        draft, in_tok, out_tok = extract_listing(
            _db(config), text=state["publish_text"], photo_path=state.get("publish_photo", "")
        )
    except PublishingError as exc:
        logger.warning("[publishing] extraction step failed: %s", exc)
        return {
            "result": {"ok": False, "reply": PUBLISH_RETRY_REPLY, "message": str(exc)[:200]},
            "turns": _append(state, PUBLISH_RETRY_REPLY),
        }

    attrs = draft.attributes
    ready = all(view_get(draft, f) for f in REQUIRED_DRAFT_FIELDS)
    view = {
        "name": draft.name,
        "summary": draft.summary,
        "description": draft.description,
        "category": attrs.category,
        "brand": attrs.brand,
        "condition": attrs.condition,
        "color": attrs.color,
        "material": attrs.material,
        "size": attrs.size,
        "photo_path": state.get("publish_photo", ""),
        "missing_fields": draft.missing_fields,
        "ready": ready,
    }
    findings = list(state.get("publish_findings", []))
    prefix = (
        "你附的照片我打不开，所以下面这些只根据你的描述整理。"
        if state.get("publish_photo_error") else ""
    )
    # 一张照片就够了。剩下的在**表里**补，不在对话里补。
    #
    # 以前这里分两条路：草稿齐了给表单，草稿不齐就把模型写的 `follow_up_question`
    # 当回复发出去（「这套积木是全新未拆封，还是已拆封或拼装过？」）。问题是那个
    # 问题**下面那张表已经在问了**——「成色 · 必填」就是一个下拉框。于是卖家被要求
    # 用打字回答一件他点一下就能选完的事，而且不回答就以为发不出去。
    #
    # 现在只有一条路：永远把表给他，缺什么就点名让他在**表里**选。
    # `draft.follow_up_question` 因此不再被读取——模型仍然会写一句，那是它的推理
    # 痕迹，不是给卖家的指令。
    missing = [field_label(f) for f in (draft.missing_fields or []) if f]
    if view["ready"] or not missing:
        reply = (
            "这是我整理出来的商品信息——你过一遍，有不对的地方直接改，"
            "然后填上价格和自提点就能发布了。"
        )
    else:
        reply = (
            "这是我整理出来的商品信息——你过一遍，有不对的地方直接改。"
            f"还差{'、'.join(missing[:4])}，在下面那张表里选一下；"
            "再填上价格和自提点就能发布了。"
        )
    reply = prefix + reply
    return {
        "publish_draft": view,
        "result": {
            "ok": True,
            "reply": reply,
            "publish_draft": view,
            "publish_findings": findings,
        },
        "turns": _append(state, reply),
        "router_in": state.get("router_in", 0) + in_tok,
        "router_out": state.get("router_out", 0) + out_tok,
    }


def view_get(draft: Any, field: str) -> Any:
    """Read a required field from either the draft or its attributes."""
    if hasattr(draft, field):
        return getattr(draft, field)
    return getattr(draft.attributes, field, None)


def _after_compliance(state: AdvisorState) -> str:
    """A blocked or failed submission stops here — extraction is only for
    things that may actually be listed."""
    return "stop" if state.get("publish_verdict") in ("block", "error") else "extract"


def degraded_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    from ..seller_advisor.orchestrator import DEGRADED_REPLY   # lazy: see sales_node

    return {
        "result": {
            "ok": True,
            "reply": DEGRADED_REPLY,
            "degraded": True,
            "message": state.get("router_fallback", ""),
        },
        "turns": _append(state, DEGRADED_REPLY),
    }


def clarify_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    """One sentence back to the seller: either a question that decides the
    route, or — when the request was outside the advisor's remit at all — a
    statement of where that line is. The router writes both; this node only
    picks a fallback when it wrote neither, and the fallback differs because
    answering an out-of-scope request with the capability menu reads as the
    system not having understood it."""
    reply = state.get("clarify_question") or (
        OUT_OF_SCOPE_REPLY if state.get("out_of_scope") else router_mod.FALLBACK_QUESTION
    )
    return {"result": {"ok": True, "reply": reply}, "turns": _append(state, reply)}


#: 挂起的任务说给卖家听的名字。回复里必须点出它——「我接着办」如果不说清接的是
#: 哪件事，在卖家眼里和没接是一样的（这正是 `HANDOFF_CONTINUATION` 的同一条理由）。
PARKED_TASK_LABEL = {
    "demand": "需求分析",
    "price": "定价分析",
    "both": "需求和定价分析",
    "find_buyers": "买家匹配",
    "sell_through": "怎么把它卖出去",
    "defer_sale": "出售计划",
}

#: 卖家点名的东西不在他库里时的回复。服务端写死，不过模型。
#:
#: 三句话，缺一不可：**没找到什么**、**现在该做什么**、**做完之后我接着替你办
#: 什么**。第三句模型写不出来——挂账是 `route_node` 在它之前几微秒写下的，那张
#: 条子不在它的上下文里；而恰恰是第三句在告诉卖家「你刚说的话没有白说」。
MISSING_ITEM_REPLY = (
    "我没找到你在售的{names}。需求、成交价和买家都是按一件**已经上架**的商品去查的，"
    "所以得先有它。\n\n"
    "你可以直接在这里发一张照片、顺手说两句它的情况（成色、用了多久），我帮你整理成"
    "商品信息。上架完回来跟我说一声，我接着把你刚才要的{task}做完——不用再说一遍。"
)


def item_not_found_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    """卖家点名了一件不在他库里的商品。

    这一轮没有任何可执行的分析——顾问的三个工具都要一个属于他的 item_id——但它
    也不是一次失败：诉求刚刚被 `route_node` 挂到了账上，这里要做的是把「为什么
    现在办不了」和「办完什么之后我接着办」一次说清楚。

    回复由服务端写，不过模型。以前这段话是 `sales_agent` 自由发挥的产物：内容
    没错，但系统这一侧认为这一轮已经正常结束了，于是诉求谁也没记——归因一。现在
    这个节点存在本身就是那条记录的凭据，两者不可能再各说各话。
    """
    names = [str(n) for n in (state.get("missing_items") or []) if n]
    tasks = list(state.get("pending_tasks") or [])
    reply = MISSING_ITEM_REPLY.format(
        names="、".join(f"「{n}」" for n in names) or "那件东西",
        task=PARKED_TASK_LABEL.get(tasks[0] if tasks else "", "那件事"),
    )
    return {
        "result": {"ok": True, "reply": reply},
        "turns": _append(state, reply),
        # 和 sales_node 一样按轮清空：一个卖家已经答过的提议不该被重新端上来。
        "pending_action": {},
        "action_result": {},
        "declined_message": "",
    }


#: 东西已经挂出去了，卖家又说了一次「上传好了」。
#:
#: 这段话要做的不是客气，是**止损**：明说这件事已经完成、明说不用再传一次，
#: 否则卖家会照着上一句「还差一些信息」再发布一遍，而系统这边完全不会察觉。
LISTING_DONE_REPLY = (
    "「{name}」已经挂出去了，买家现在就能搜到它——这件事办完了，不用再传一次。\n\n"
    "要我接着看看它现在有多少人在找、该定什么价、有没有可以直接联系的买家吗？"
)


def listing_done_node(state: AdvisorState, config: RunnableConfig) -> dict[str, Any]:
    """「上传好了」，而东西确实已经挂出去了。

    这一轮没有任何工具要调：要回答的问题（上架办完了没有）在 `approve` 写库成功
    的那一刻就有答案了，`last_listed` 就是那个答案被带过来的方式。

    注意它和 `handoff` 的分工：挂账还在的时候，`route_node` 一开头就接力走了，
    根本到不了这里。这个节点接的是**账已经销掉之后**又来一次的那句「上传好了」
    ——实测里卖家会说第二遍，而第二遍正是把重复商品造出来的那一遍。
    """
    listed = dict(state.get("last_listed") or {})
    reply = LISTING_DONE_REPLY.format(name=str(listed.get("name") or "刚才那件东西"))
    return {
        "result": {"ok": True, "reply": reply},
        "turns": _append(state, reply),
        # 一次性：它回答的是「刚才挂出去的是哪件」，说过一遍就不该再被翻出来。
        "last_listed": {},
        "pending_action": {},
        "action_result": {},
        "declined_message": "",
    }


def _append(state: AdvisorState, reply: str) -> list[dict]:
    return [
        *state.get("turns", []),
        {"role": "user", "content": state["message"]},
        {"role": "assistant", "content": reply},
    ]


def _pick(state: AdvisorState) -> str:
    return state.get("route") or "clarify"


# ---------------- Graph construction ----------------
_graph = None
_checkpointer = None


def get_checkpointer():
    """Checkpoints go to their own SQLite file under DATA_DIR.

    Persistent rather than in-process because conversations are now named
    things a seller manages in a list: a restart that leaves the list intact
    but every transcript empty is worse than not having the list. Kept in a
    separate file from the business database so a checkpoint-format change
    never touches listings.
    """
    global _checkpointer
    if _checkpointer is None:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        settings.data_path.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(settings.data_path / "advisor_threads.sqlite"), check_same_thread=False
        )
        _checkpointer = SqliteSaver(conn)
        _checkpointer.setup()
    return _checkpointer


def get_graph():
    """Compiled once per process; nodes are stateless so this is safe to share."""
    global _graph
    if _graph is None:
        from langgraph.graph import END, START, StateGraph

        builder = StateGraph(AdvisorState)
        builder.add_node("route", route_node)
        builder.add_node("sales_agent", sales_node)
        builder.add_node("status_change", status_change_node)
        builder.add_node("propose", propose_node)
        builder.add_node("act", act_node)
        builder.add_node("defer_sale", defer_sale_node)
        builder.add_node("publishing_agent", publishing_node)
        builder.add_node("publish_compliance", publish_compliance_node)
        builder.add_node("publish_extract", publish_extract_node)
        builder.add_node("clarify", clarify_node)
        builder.add_node("item_not_found", item_not_found_node)
        builder.add_node("listing_done", listing_done_node)
        builder.add_node("degraded", degraded_node)
        builder.add_edge(START, "route")
        builder.add_conditional_edges(
            "route",
            _pick,
            {
                "sales_agent": "sales_agent",
                "status_change": "status_change",
                "defer_sale": "defer_sale",
                "publishing_agent": "publishing_agent",
                "clarify": "clarify",
                "item_not_found": "item_not_found",
                "listing_done": "listing_done",
                "degraded": "degraded",
            },
        )
        builder.add_edge("publishing_agent", "publish_compliance")
        builder.add_conditional_edges(
            "publish_compliance", _after_compliance,
            {"extract": "publish_extract", "stop": END},
        )
        builder.add_edge("sales_agent", "propose")
        builder.add_conditional_edges("propose", _after_propose, {"act": "act", "end": END})
        # The status-change gate ends in the same `act` node as a push: one
        # place where a confirmed action is carried out, one place where its
        # outcome is written back into the reply.
        builder.add_conditional_edges(
            "status_change", _after_propose, {"act": "act", "end": END}
        )
        for node in (
            "act", "publish_extract", "defer_sale", "clarify",
            "item_not_found", "listing_done", "degraded",
        ):
            builder.add_edge(node, END)
        _graph = builder.compile(checkpointer=get_checkpointer())
    return _graph


def reset_graph() -> None:
    """Test hook: rebuild the graph and drop every checkpoint."""
    global _graph, _checkpointer
    _graph = None
    _checkpointer = None


def run_turn(
    db: Session, seller_id: int, message: str, thread_id: str, photo_path: str = ""
) -> AdvisorState:
    """One turn through the graph. Returns the resulting state.

    `router_in`/`router_out` are reset per turn: they are this turn's bill, and
    a checkpointed thread would otherwise keep re-adding every earlier turn's
    tokens to the total.

    What the turn actually cost is metered in `lc_llm` instead of summed out of
    the state, and attached to the returned snapshot as `usage`. The state
    cannot answer the question honestly: `propose_node` spends a model call
    whose return value `interrupt()` throws away, a declined proposal runs the
    graph twice for one turn, and a resumed thread carries the *previous*
    turn's counters in its checkpoint. The meter sees every call either way,
    and starting it here — once, around everything — is what makes the number
    this turn's rather than the thread's.
    """
    start_turn_meter()
    graph = get_graph()
    config = {"configurable": {"thread_id": thread_id, "db": db}}

    # A thread paused at a confirm prompt is resumed with this message as the
    # answer, not restarted with it as a new question. Checked against the
    # graph's own state rather than a flag we keep: the graph is the authority
    # on whether it is suspended, and a flag could disagree with it.
    snapshot = graph.get_state(config)
    if snapshot.next:
        logger.info("[react] resuming thread %s at %s", thread_id, snapshot.next)
        resumed = graph.invoke(Command(resume=message), config=config)
        declined = resumed.get("declined_message")
        if not declined:
            resumed["usage"] = read_turn_meter()
            return resumed
        # Not a yes, so the offer is gone — but the seller said something, and
        # it deserves an answer rather than being spent as a "no". Run it as an
        # ordinary turn, which is what it would have been had nothing been
        # pending.
        logger.info("[react] proposal declined; answering the message instead")
        graph.update_state(config, {"declined_message": ""})

    state = graph.invoke(
        {
            "seller_id": int(seller_id),
            "message": message,
            "photo_path": photo_path or "",
            "router_in": 0,
            "router_out": 0,
        },
        config=config,
    )
    # Read after the invoke, not accumulated into the state: `usage` never
    # reaches the checkpoint, so it cannot be mistaken for thread state and
    # re-reported on a later turn.
    state["usage"] = read_turn_meter()
    return state
