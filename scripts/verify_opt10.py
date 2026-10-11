"""Acceptance for 优化建议 1.0 — the five 归因 fixes, offline.

Everything here runs without an API key. That is the point: the parts of these
fixes that *can* be asserted are the deterministic ones — entity binding, the
enum exits, the routing overrides, the confirmation gate, the paused
lifecycle — and those are also the parts that silently rot. The model-facing
half (does the router actually choose `sell_through`, does the specialist run
all three steps) needs the 805-case run, which costs quota; this script is what
tells you the plumbing under it is intact before you spend any.

    python scripts/verify_opt10.py            # pure assertions, touches nothing
    python scripts/verify_opt10.py --live-db  # also exercises pause/resume for real

`--live-db` pauses and then resumes one real listing in `locswap.db`. It puts
the status back, but the status-log rows and any want restoration it triggers
stay — which is why it is opt-in rather than part of the default run.

Exit code is the number of failed assertions.
"""

from __future__ import annotations

import sys
import uuid

import _bootstrap  # noqa: F401  (path setup)

from app.models import ItemStatus

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not ok:
        line += f"\n        {detail}"
    print(line)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ============================================================
# 归因一 — implicit multi-intent: entity binding + plan
# ============================================================
def test_intent_plan() -> None:
    from app.services.nlp import intent_rules

    section("归因一 隐式多意图：实体绑定 + 计划")

    listings = [
        {"item_id": 1, "name": "美的微波炉 20L"},
        {"item_id": 2, "name": "宜家四层书架"},
        {"item_id": 3, "name": "联想拯救者笔记本电脑"},
        {"item_id": 4, "name": "桌面麦克风支架"},
    ]

    for msg, expect_id in [
        ("帮我卖微波炉", 1),
        ("我想把书架出了", 2),
        ("笔记本电脑找谁聊？", 3),
    ]:
        hit = intent_rules.match_listings(msg, listings)
        check(
            f"「{msg}」绑定到 item {expect_id}",
            len(hit) == 1 and hit[0]["item_id"] == expect_id,
            f"got {hit}",
        )

    for msg in ("帮我卖微波炉", "我想把书架出了", "笔记本电脑找谁聊？", "这个台灯怎么卖"):
        h = intent_rules.hints(msg, listings)
        check(f"「{msg}」识别为泛卖出意图", h["broad_sell_intent"], str(h))

    # The token floor: a single character must not bind.
    check(
        "单字不绑定（「桌」不命中桌面麦克风支架）",
        intent_rules.match_listings("桌", listings) == [],
        str(intent_rules.match_listings("桌", listings)),
    )
    check(
        "价格数字不当成商品名",
        intent_rules.match_listings("260 合适吗", listings) == [],
    )
    # A message naming something not in stock binds nothing — that is what
    # sends it to the publishing agent instead of the sales agent.
    check(
        "不在库的商品不绑定（空气炸锅）",
        intent_rules.match_listings("帮我卖空气炸锅", listings) == [],
    )
    check(
        "普通定价问题不算泛卖出意图",
        not intent_rules.hints("微波炉定价合适吗", listings)["broad_sell_intent"],
    )

    from app.services.agents.router import SALES_SCENARIOS, Scenario

    check("scenario 枚举含 sell_through", "sell_through" in SALES_SCENARIOS)
    check(
        "sell_through 在 Scenario 字面量里",
        "sell_through" in Scenario.__args__,  # type: ignore[attr-defined]
    )

    from app.services.agents.router import apply_broad_sell_plan

    h = intent_rules.hints("帮我卖微波炉", listings)
    for start_agent in ("clarify", "publishing_agent", "sales_agent"):
        agent, scen, slots = apply_broad_sell_plan(start_agent, "none", {"item_ref": ""}, h)
        check(f"在库泛卖出：{start_agent} → sales_agent/sell_through",
              agent == "sales_agent" and scen == "sell_through"
              and slots["item_ref"] == "美的微波炉 20L",
              f"{agent}/{scen} {slots}")

    agent, scen, _ = apply_broad_sell_plan(
        "sales_agent", "price", {"item_ref": "微波炉"}, h)
    check("模型已经选了具体分析就不覆盖成 sell_through", scen == "price")

    h_new = intent_rules.hints("我又收了个微波炉，帮我卖", listings)
    agent, _, _ = apply_broad_sell_plan("publishing_agent", "none", {}, h_new)
    check("「又收了一个」不被当成在库", agent == "publishing_agent", str(h_new["new_item_cue"]))

    agent, _, _ = apply_broad_sell_plan("publishing_agent", "none", {}, h, has_photo=True)
    check("带照片时不改写路由（拍照的多半是要上架）", agent == "publishing_agent")

    h_none = intent_rules.hints("帮我卖空气炸锅", listings)
    agent, _, _ = apply_broad_sell_plan("publishing_agent", "none", {}, h_none)
    check("不在库的泛卖出保持上架路由", agent == "publishing_agent")

    from app.services.agents.sales.orchestrator import _routing_block

    block = _routing_block("sell_through", {"item_ref": "微波炉"})
    check("sell_through 会带上固定三步计划", "analyze_demand" in block and "analyze_sold_prices" in block
          and "find_potential_buyers" in block, block[:200])


# ============================================================
# 归因二 — multi-turn entity context
# ============================================================
def test_context_carry() -> None:
    from app.services.nlp import intent_rules
    from app.services.agents.graph.nodes import _carry_context
    from app.services.agents.sales.orchestrator import _last_turn_block

    section("归因二 多轮实体上下文继承")

    for msg in ("这些买家都什么情况", "第 1 个买家的预算大概多少", "这些人离我远吗", "他们都住哪"):
        check(f"「{msg}」识别为指代式追问", intent_rules.hints(msg, [])["refers_to_previous"])
    check(
        "普通提问不算指代",
        not intent_rules.hints("双人沙发有人找吗", [])["refers_to_previous"],
    )

    result = {
        "used_tools": ["find_potential_buyers"],
        "potential_buyers": [
            {
                "item_id": 7,
                "item_name": "双人沙发",
                "cards": [
                    {"card_id": "candidate-1", "demand_summary": "找二手沙发",
                     "budget_display": "≤$200", "distance_display": "1km 内"},
                ],
            }
        ],
    }
    carried = _carry_context({"scenario": "find_buyers"}, result)
    check("上一轮商品被记住", carried.get("item_id") == 7 and carried.get("item_name") == "双人沙发",
          str(carried))
    check("候选卡片序号被记住", [c["card_id"] for c in carried["buyer_cards"]] == ["candidate-1"])

    # A turn that produced nothing must not erase the subject.
    kept = _carry_context({"last_context": carried, "scenario": "none"}, {"used_tools": []})
    check("空结果的一轮不会清掉上下文", kept.get("item_id") == 7, str(kept))

    from app.services.agents.router import apply_followup_context

    ctx = {"item_id": 7, "item_name": "双人沙发", "scenario": "find_buyers"}
    hints_ref = {"refers_to_previous": True}
    agent, scen, slots = apply_followup_context("clarify", "none", {"item_ref": ""}, hints_ref, ctx)
    check("指代式追问被从 clarify 拉回 sales_agent",
          agent == "sales_agent" and scen == "find_buyers" and slots["item_ref"] == "双人沙发",
          f"{agent} {scen} {slots}")
    agent, scen, slots = apply_followup_context(
        "sales_agent", "price", {"item_ref": "台灯"}, hints_ref, ctx)
    check("模型自己填了商品就不覆盖", slots["item_ref"] == "台灯" and scen == "price")
    agent, _, _ = apply_followup_context("clarify", "none", {"item_ref": ""}, hints_ref, {})
    check("没有上一轮时不强行接管", agent == "clarify")
    agent, _, _ = apply_followup_context(
        "publishing_agent", "none", {"item_ref": ""}, hints_ref, ctx)
    check("上架路由不被指代逻辑改写", agent == "publishing_agent")

    block = _last_turn_block(carried)
    check("<last_turn> 块注入给专家", "<last_turn>" in block and "双人沙发" in block, block[:160])
    check("没有上下文时不注入空块", _last_turn_block({}) == "")


# ============================================================
# 归因三 — status change: paused status, gate, execution
# ============================================================
def test_status_change() -> None:
    from app.services.nlp import intent_rules
    from app.services.agents import proposals as prop

    section("归因三 状态变更意图落地")

    check("ItemStatus 有 paused", ItemStatus.PAUSED == "paused")
    check("paused 不可搜索", ItemStatus.PAUSED not in ItemStatus.SEARCHABLE)
    check("paused 不是终态", ItemStatus.PAUSED not in ItemStatus.TERMINAL)
    check("reserved 不可暂停", ItemStatus.RESERVED not in ItemStatus.PAUSABLE)

    for msg in ("把显示器下架吧", "有机化学课本先不卖了", "这个先撤了", "台灯停一下"):
        check(f"「{msg}」→ pause", intent_rules.status_change_intent(msg) == "pause",
              intent_rules.status_change_intent(msg))
    for msg in ("把台灯重新上架", "沙发恢复上架吧", "那个再挂出来"):
        check(f"「{msg}」→ resume", intent_rules.status_change_intent(msg) == "resume",
              intent_rules.status_change_intent(msg))
    check("「删掉这件」→ delete", intent_rules.status_change_intent("删掉这件") == "delete")
    check("普通销售问题不触发状态变更",
          intent_rules.status_change_intent("这个台灯有人找吗") == "")

    # The gate. Consent may restate the action; anything hedged or negated may not.
    for yes in ("好", "行", "下架吧", "好的，下架吧", "对，暂停吧", "确认", "恢复上架"):
        check(f"同意：「{yes}」", prop.is_affirmative_for_status(yes))
    for no in ("先别", "不用了", "等等", "下架之后还能恢复吗", "那另一件呢", "先看看需求"):
        check(f"不同意：「{no}」", not prop.is_affirmative_for_status(no), no)
    # The push gate stays narrow — widening consent there was never the ask.
    check("推送闸门未被放宽", not prop.is_affirmative("好的，发给他们吧"))

    from app.services.agents.router import RouteSlots, _sanitize

    slots = RouteSlots(status_change="pause")
    _, clean, dropped, _w = _sanitize("publishing_agent", "none", slots)
    check("非 sales 路由上的状态变更被丢弃", clean.status_change == "" and dropped, str(dropped))
    _, clean, _, _w = _sanitize("sales_agent", "none", RouteSlots(status_change="pause"))
    check("sales 路由上的状态变更保留", clean.status_change == "pause")

    check("delete 不在可执行目标里", "delete" not in prop.StatusTarget.__args__)  # type: ignore[attr-defined]


def test_status_change_live() -> None:
    """The lifecycle half, against a real (temporary) database."""
    section("归因三 pause/resume 生命周期（真实 DB）")

    from app.db import SessionLocal
    from app.models import Item, User
    from app.services.agents import proposals as prop
    from app.services.item_index import get_item_index
    from app.services.lifecycle import LifecycleError, LifecycleService

    db = SessionLocal()
    try:
        item = (
            db.query(Item)
            .filter(Item.status == ItemStatus.SELLING)
            .order_by(Item.id.desc())
            .first()
        )
        if item is None:
            check("库里有在售商品可测", False, "no selling item in the database")
            return
        seller = db.get(User, item.seller_id)
        service = LifecycleService(db, get_item_index())
        original = item.status

        service.pause(item, seller)
        check("pause 之后状态为 paused", item.status == ItemStatus.PAUSED, item.status)
        check("pause 之后离开索引", get_item_index().get_record(item.id) is None)

        try:
            service.pause(item, seller)
            check("重复 pause 被拒绝", False, "second pause did not raise")
        except LifecycleError:
            check("重复 pause 被拒绝", True)

        # The read-only tools must not see it while it is paused.
        from app.services.agents.sales import tools as tool_impl

        try:
            tool_impl.resolve_owned_item(db, item.seller_id, item.id)
            check("已下架商品对分析工具不可见", False, "resolve_owned_item accepted a paused item")
        except tool_impl.ToolError:
            check("已下架商品对分析工具不可见", True)

        from app.services.agents.sales.schemas import ListMyItemsInput

        out = tool_impl.list_my_items(db, item.seller_id, ListMyItemsInput())
        check("默认不列出已下架商品",
              all(r.item_id != item.id for r in out.items))
        out2 = tool_impl.list_my_items(db, item.seller_id, ListMyItemsInput(include_paused=True))
        check("include_paused=True 才列出",
              any(r.item_id == item.id for r in out2.items))
        check("total_active 不计入已下架",
              out2.total_active == sum(1 for r in out2.items if r.status != ItemStatus.PAUSED))

        # Executing a confirmed proposal goes through the same service.
        pending = prop.build_status_change(item, "resume")
        outcome = prop.execute(db, item.seller_id, pending)
        check("确认后 resume 执行成功", outcome.succeeded == 1 and outcome.new_status == ItemStatus.SELLING,
              outcome.model_dump_json())
        check("resume 之后回到索引", get_item_index().get_record(item.id) is not None)

        if item.status != original:
            item.status = original
            db.commit()
    finally:
        db.close()


def test_status_gate_end_to_end() -> None:
    """The gate, driven through the real graph.

    No model call is involved: the router is pinned (that decision is asserted
    separately, live) and the status-change node itself asks no model
    anything — which is the point of resolving the item server-side. What this
    exercises is the part that only exists once the graph is running: that the
    turn actually suspends, that a hedge leaves the listing alone, and that a
    yes reaches the database.
    """
    section("归因三 确认闸门端到端（真实图 + 真实 DB）")

    from app.db import SessionLocal
    from app.models import Item
    from app.services.agents import graph as g
    from app.services.agents.router import RouteResult, RouteSlots

    db = SessionLocal()
    original_route = g.nodes.router_mod.route
    original_configured = g.nodes.llm_configured
    try:
        item = (
            db.query(Item)
            .filter(Item.status == ItemStatus.SELLING)
            .order_by(Item.id.desc())
            .first()
        )
        if item is None:
            check("库里有在售商品可测", False, "no selling item")
            return
        item_id, seller_id, name = item.id, item.seller_id, item.name

        def pinned(*, message, history, has_active_items, listings, has_photo=False, **extra):
            return RouteResult(
                agent="sales_agent",
                rewritten_query=message,
                slots=RouteSlots(item_ref=name, status_change="pause"),
            )

        g.nodes.router_mod.route = pinned
        g.nodes.llm_configured = lambda: (True, "")

        # Unique per run: checkpoints are persistent, so reusing an id means
        # the first turn resumes whatever the previous run left parked and the
        # opening message is read as an answer rather than a request.
        thread = f"opt10-{item_id}-{uuid.uuid4().hex[:8]}"
        state = g.run_turn(db, seller_id, f"把「{name}」下架吧", thread)
        interrupts = state.get("__interrupt__") or []
        check("状态变更会挂起等确认", bool(interrupts), str(state.get("route")))
        payload = getattr(interrupts[0], "value", {}) if interrupts else {}
        check("确认提示说清了动作与商品",
              payload.get("action") == "update_item_status" and name[:10] in payload.get("question", ""),
              str(payload)[:200])
        db.refresh(item)
        check("挂起期间商品状态未变", item.status == ItemStatus.SELLING, item.status)

        # A hedge is not consent. The question still gets answered, though:
        # the declined message runs as an ordinary turn, which — because it is
        # still a delisting message — arrives at the gate again rather than
        # being swallowed as a "no".
        state = g.run_turn(db, seller_id, "下架之后还能恢复吗", thread)
        db.refresh(item)
        check("含糊的回答不执行下架", item.status == ItemStatus.SELLING, item.status)
        check("被拒绝后这条消息仍然得到处理，而不是被当成一句「不」吃掉",
              bool(state.get("__interrupt__") or []) or bool((state.get("result") or {}).get("reply")),
              str(state.get("result"))[:120])

        # ...and a plain yes is.
        state = g.run_turn(db, seller_id, "好的，下架吧", thread)
        db.refresh(item)
        check("明确同意后真的下架了", item.status == ItemStatus.PAUSED, item.status)
        check("回复里说明了已下架",
              "下架" in (state.get("result") or {}).get("reply", ""),
              str((state.get("result") or {}).get("reply"))[:120])

        if item.status != ItemStatus.SELLING:
            from app.services.item_index import get_item_index
            from app.models import User
            from app.services.lifecycle import LifecycleService

            LifecycleService(db, get_item_index()).resume(item, db.get(User, seller_id))
            db.refresh(item)
            check("测试后已恢复原状", item.status == ItemStatus.SELLING, item.status)
    finally:
        g.nodes.router_mod.route = original_route
        g.nodes.llm_configured = original_configured
        g.reset_graph()
        db.close()


# ============================================================
# 归因四 — unmappable time window
# ============================================================
def test_time_window() -> None:
    from app.services.agents.router import RouteSlots, TimeWindow
    from app.services.agents.sales.orchestrator import _routing_block

    section("归因四 槽位映射的不可映射出口")

    check("TimeWindow 有 unsupported 出口", "unsupported" in TimeWindow.__args__)  # type: ignore[attr-defined]
    check("空槽位仍然合法", "" in TimeWindow.__args__)  # type: ignore[attr-defined]
    RouteSlots(time_window="unsupported")  # must validate

    block = _routing_block("demand", {"item_ref": "自行车", "time_window": "unsupported"})
    check("不可映射时不把 unsupported 当成窗口值传下去",
          '"time_window"' not in block, block[:200])
    check("不可映射时明确要求说明口径", "all" in block and "静默" not in block and "明确" in block,
          block[:400])
    normal = _routing_block("demand", {"time_window": "last_90d"})
    check("正常窗口照常传递", '"time_window": "last_90d"' in normal, normal[:200])


# ============================================================
# 归因五 — scattered: no tool no answer, boundary before entity
# ============================================================
def test_guards() -> None:
    from app.services.nlp import intent_rules
    from app.services.agents.graph.nodes import OUT_OF_SCOPE_REPLY, clarify_node
    from app.services.agents.sales.orchestrator import DATA_SCENARIOS

    section("归因五 零散风险点")

    for s in ("price", "demand", "both", "find_buyers", "sell_through"):
        check(f"{s} 属于必须有工具结果的场景", s in DATA_SCENARIOS)
    check("none 不强制工具", "none" not in DATA_SCENARIOS)

    for msg in ("帮我联系一下买羊毛围巾的那个人的客服", "把订单退了", "帮我买个显示器架",
                "帮我把台灯挂到别的平台"):
        check(f"「{msg}」命中越界提示", intent_rules.hints(msg, [])["maybe_out_of_scope"], msg)
    check("正常卖家问题不命中越界提示",
          not intent_rules.hints("羊毛围巾有人找吗", [])["maybe_out_of_scope"])

    out = clarify_node({"message": "x", "out_of_scope": True, "turns": []}, None)
    check("越界时回的是边界说明而不是能力菜单",
          out["result"]["reply"] == OUT_OF_SCOPE_REPLY, out["result"]["reply"][:80])
    out2 = clarify_node({"message": "x", "clarify_question": "你是想…？", "turns": []}, None)
    check("路由器写了澄清问题时用它", out2["result"]["reply"] == "你是想…？")


def test_graph_wiring() -> None:
    section("图结构")

    from app.services.agents import graph as g

    compiled = g.get_graph()
    nodes = set(compiled.get_graph().nodes)
    check("status_change 节点已接入", "status_change" in nodes, str(sorted(nodes)))
    check("act 节点仍然是唯一的执行点", "act" in nodes)
    g.reset_graph()


# ============================================================
# Live: does the router actually decide this way?
# ============================================================
def test_router_live() -> None:
    """Eight real routing calls. The assertions above prove the plumbing
    exists; these prove the prompt uses it. Opt-in because it costs quota —
    and quota running out looks like a router that suddenly clarifies
    everything, so a failure here should be checked against the account before
    it is read as a regression."""
    section("路由器实测（--live-router，会消耗额度）")

    from app.services.nlp import intent_rules
    from app.services.agents import router as router_mod

    listings = [
        {"item_id": 11, "name": "美的微波炉 20L", "price": 45.0, "category": "Home & Kitchen"},
        {"item_id": 12, "name": "宜家四层书架", "price": 30.0, "category": "Furniture"},
        {"item_id": 13, "name": "小米台灯", "price": 15.0, "category": "Home & Kitchen"},
        {"item_id": 14, "name": "有机化学课本第九版", "price": 40.0, "category": "Textbooks"},
        {"item_id": 15, "name": "羊毛围巾", "price": 12.0, "category": "Clothing"},
    ]
    last_ctx = {
        "item_id": 12,
        "item_name": "宜家四层书架",
        "scenario": "find_buyers",
        "buyer_cards": [{"card_id": "candidate-1", "demand": "找二手书架"}],
    }

    def call(msg: str, ctx: dict | None = None):
        return router_mod.route(
            message=msg,
            history=[],
            has_active_items=True,
            listings=listings,
            hints=intent_rules.hints(msg, listings),
            last_context=ctx or {},
        )

    msg0 = "帮我卖微波炉"
    r = call(msg0)
    a0, s0, sl0 = router_mod.apply_broad_sell_plan(
        r.agent, r.scenario, r.slots.model_dump(), intent_rules.hints(msg0, listings))
    check("泛卖出（在库）→ sales_agent + sell_through",
          a0 == "sales_agent" and s0 == "sell_through",
          f"router={r.agent}/{r.scenario} -> {a0}/{s0} fallback={r.fallback_reason}")

    r = call("帮我卖空气炸锅")
    check("泛卖出（不在库）→ publishing_agent",
          r.agent == "publishing_agent", f"agent={r.agent} scenario={r.scenario}")

    r = call("帮我联系一下买羊毛围巾的那个人的客服")
    check("越界诉求带商品名 → clarify 且标为越界",
          r.agent == "clarify" and r.out_of_scope,
          f"agent={r.agent} out_of_scope={r.out_of_scope} q={r.clarify_question[:60]}")

    r = call("把台灯下架吧")
    check("「下架」→ status_change=pause",
          r.slots.status_change == "pause", f"slots={r.slots.model_dump()}")

    r = call("有机化学课本先不卖了")
    check("「先不卖了」→ status_change=pause",
          r.slots.status_change == "pause", f"slots={r.slots.model_dump()}")

    r = call("从我上架到现在，台灯有人问过吗")
    check("「从上架到现在」→ time_window=unsupported",
          r.slots.time_window == "unsupported", f"slots={r.slots.model_dump()}")

    r = call("这学期台灯的需求怎么样", None)
    check("回归：「这学期」仍映射 last_90d",
          r.slots.time_window == "last_90d", f"slots={r.slots.model_dump()}")

    msg = "这些买家都什么情况"
    r = call(msg, last_ctx)
    # Read through the same backstop the graph applies, because the decision
    # the product acts on is the one that comes out of it.
    agent, scen, slots = router_mod.apply_followup_context(
        r.agent, r.scenario, r.slots.model_dump(),
        intent_rules.hints(msg, listings), last_ctx,
    )
    check("指代式追问落在 sales_agent 且绑定上一轮的商品与场景",
          agent == "sales_agent" and scen == "find_buyers" and "书架" in slots["item_ref"],
          f"router={r.agent}/{r.scenario} -> {agent}/{scen} slots={slots} q={r.rewritten_query}")


def main() -> int:
    test_intent_plan()
    test_context_carry()
    test_status_change()
    test_time_window()
    test_guards()
    test_graph_wiring()
    if "--live-db" in sys.argv:
        test_status_change_live()
        test_status_gate_end_to_end()
    else:
        print()
        print("=== 归因三 pause/resume 生命周期：跳过（加 --live-db 才跑，会写真实库）===")
    if "--live-router" in sys.argv:
        test_router_live()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return len(FAIL)


if __name__ == "__main__":
    sys.exit(main())
