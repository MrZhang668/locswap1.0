"""The advisor graph's per-thread state."""

from __future__ import annotations

from typing import TypedDict


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
