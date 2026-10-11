"""Acceptance for 归因「卖家点名了一件库里没有的东西」。

修的是这个剧本——它和 `verify_opt20.py` 里那个长得一样，但**走的不是同一条路**：

    卖家：想分析水杯的需求和价格并找买家
    系统：我没找到你在售的「水杯」……请确认是否已上架     ← sales_agent 写的散文
    卖家：（传图）上传了
    系统：（草稿 → 已上架）
    卖家：上传好了
    系统：你想做什么？                                    ← 原始诉求蒸发

opt20 修的版本里，「先去上架」是**路由器**的判定（`publishing_agent`），所以
`handoff.capture` 那个钩子响得了。这个版本里它是**顾问自己在回复正文里说的**，
路由结果仍然是 `sales_agent`——钩子一次都没响，账本从头到尾是空的，第三轮的
`resume_target` 只是被叫去清一本空账。

所以这里断言的是五件事：

1. `apply_missing_item_gate` —— 「办不办得成」是后端算出来的事实，不是模型写出来
   的一段话。它把这一轮从 `sales_agent` 改判成 `item_not_found`，挂账因此有了触发点；
2. `_settle_thread_after_listing` —— 上架完成是一个**发生在图之外**的事件，草稿箱
   得由 approve 主动清掉，否则下一句话换来第二张同一件商品的草稿卡；
3. `resume_target` 的带图守卫 —— 照片是「东西在这儿」，不是「我弄完了」；
4. `apply_listing_done_gate` —— 上架是**有终点的**。东西已经挂出去之后再来一句
   「上传好了」，不能拿这四个字当商品描述再跑一遍抽取（那会造出重复商品）；
5. `SUPPRESSED_IN_PUBLISHING` + 草稿回复 —— **一张照片就够了**。「信息不足」不是
   一条发布条件：成色、类别这些，草稿卡下面那张表里两秒钟就能选完，不该在对话框
   里再要求一遍（而且合规跑在抽取之前，它判的还是错的那份文本）。

    python scripts/verify_missing_item_handoff.py

不碰真实库、不调模型：DB 层用桩，路由器用桩。退出码是失败断言的条数。
"""

from __future__ import annotations

import io
import sys
from datetime import datetime, timedelta

import _bootstrap  # noqa: F401  (path setup)

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


#: 卖家名下的东西。没有杯子——这正是剧本的前提。
LISTINGS = [
    {"item_id": 1, "name": "蓝牙音箱", "price": 120.0, "category": "电子产品"},
    {"item_id": 2, "name": "台灯", "price": 45.0, "category": "家居用品"},
]


# ============================================================
# 一、闸门：办不办得成是一条事实
# ============================================================
def test_missing_item_gate() -> None:
    from app.services.nlp import intent_rules
    from app.services.agents import router as router_mod

    section("闸门：点名了一件库里没有的东西")

    def gate(msg, *, refs=None, slots=None, photo=False, listings=None):
        rows = LISTINGS if listings is None else listings
        return router_mod.apply_missing_item_gate(
            "sales_agent",
            slots or {"item_ref": (refs or [""])[0]},
            refs or [],
            intent_rules.hints(msg, rows),
            rows,
            photo,
        )

    route, missing = gate("想分析水杯的需求和价格并找买家", refs=["水杯"])
    check("库里没有「水杯」→ 改判 item_not_found", route == "item_not_found", f"got {route}")
    check("并报出是哪几件没找到", missing == ["水杯"], f"got {missing}")

    route, _ = gate("台灯的需求怎么样", refs=["台灯"])
    check("库里有「台灯」→ 不改判", route == "sales_agent", f"got {route}")

    # 卖家嘴里的词和他自己填的名字对不上号，但整条消息还是勾到了那件商品。
    # 这一条是闸门的防误伤线：只要消息层面有匹配，就不许说「没找到」。
    route, _ = gate("蓝牙音箱那个杯子", refs=["杯子"])
    check("消息命中了在售商品 → 不改判（防误伤）", route == "sales_agent", f"got {route}")

    route, _ = gate("这个多少钱能卖", refs=["水杯"], photo=True)
    check("这一轮带了照片 → 不改判（那是上架入口）", route == "sales_agent", f"got {route}")

    route, _ = gate("我这几件都盘一下", refs=[])
    check("没点名任何东西 → 不改判", route == "sales_agent", f"got {route}")

    route, _ = gate("把水杯下架吧", refs=["水杯"],
                    slots={"item_ref": "水杯", "status_change": "pause"})
    check("状态变更 → 不改判（后面有自己的闸门）", route == "sales_agent", f"got {route}")

    route, _ = gate("我一共挂了几件东西", refs=["东西"])
    check("清点库存 → 不改判", route == "sales_agent", f"got {route}")

    route, missing = gate("水杯和台灯都看一下", refs=["水杯", "台灯"])
    check("两件里有一件找得到 → 不改判（剩下那件由顾问说明）",
          route == "sales_agent", f"got {route} / {missing}")

    route, missing = gate("水杯和保温壶都看一下", refs=["水杯", "保温壶"])
    check("两件都找不到 → 改判，且两件都报出来",
          route == "item_not_found" and missing == ["水杯", "保温壶"],
          f"got {route} / {missing}")

    route, _ = router_mod.apply_missing_item_gate(
        "publishing_agent", {"item_ref": "水杯"}, ["水杯"],
        intent_rules.hints("帮我挂个水杯", LISTINGS), LISTINGS, False,
    )
    check("本来就去上架的一轮 → 闸门不插手", route == "publishing_agent", f"got {route}")


# ============================================================
# 二、挂账：新路由是钩子的触发点
# ============================================================
def test_capture_on_missing_item() -> None:
    from app.services.agents import handoff
    from app.services.nlp import intent_rules

    section("挂账：item_not_found 这一轮必须记账")

    msg = "想分析水杯的需求和价格并找买家"
    patch = handoff.capture(
        route="item_not_found", scenario="both",
        slots={"item_ref": "水杯"}, hints=intent_rules.hints(msg, LISTINGS),
        message=msg, turn_index=0, item_refs=["水杯"],
    )
    check("挂了账（补丁非空）", bool(patch))
    check("任务是卖家点名的那一种", patch.get("pending_tasks") == ["both"],
          f"got {patch.get('pending_tasks')}")
    check("实体记了下来", [i["name"] for i in patch.get("pending_items", [])] == ["水杯"],
          f"got {patch.get('pending_items')}")
    check("原话原样保存", patch.get("original_intent") == msg)
    check("记了挂起时刻", bool(patch.get("handoff_at")))

    # 这是改动前的行为，留作对照：同一句话、同一个卖家，只因为路由结果是
    # sales_agent，整张条子就不存在。
    old = handoff.capture(
        route="sales_agent", scenario="both",
        slots={"item_ref": "水杯"}, hints=intent_rules.hints(msg, LISTINGS),
        message=msg, turn_index=0, item_refs=["水杯"],
    )
    check("sales_agent 本身仍然不挂账（那是已经在办了）", old == {}, f"got {old}")

    # 路由器没给出具体场景时也得挂——卖家刚把诉求说出口了。
    plain = handoff.capture(
        route="item_not_found", scenario="none",
        slots={"item_ref": "水杯"}, hints=intent_rules.hints("我的水杯呢", LISTINGS),
        message="我的水杯呢", turn_index=0, item_refs=["水杯"],
    )
    check("场景是 none 也照样挂账（兜底 sell_through）",
          plain.get("pending_tasks") == ["sell_through"], f"got {plain.get('pending_tasks')}")

    multi = handoff.capture(
        route="item_not_found", scenario="both",
        slots={"item_ref": "水杯"},
        hints=intent_rules.hints("水杯和保温壶都看一下", LISTINGS),
        message="水杯和保温壶都看一下", turn_index=0, item_refs=["水杯", "保温壶"],
    )
    check("多件一起挂，不是只挂最后一件",
          [i["name"] for i in multi.get("pending_items", [])] == ["水杯", "保温壶"],
          f"got {multi.get('pending_items')}")


# ============================================================
# 三、接力守卫：照片不是「我弄完了」
# ============================================================
class _FakeItem:
    def __init__(self, item_id, name, created_at, status=None):
        from app.models import ItemStatus

        self.id = item_id
        self.name = name
        self.created_at = created_at
        self.status = status or ItemStatus.SELLING


class _FakeRepo:
    rows: list = []

    def __init__(self, db):
        pass

    def list_by_seller(self, seller_id):
        return list(type(self).rows)


def test_resume_photo_guard() -> None:
    from app.services.agents import handoff
    from app.services.nlp import intent_rules

    section("接力守卫：带照片的那一轮不接力")

    parked_at = datetime.utcnow() - timedelta(minutes=5)
    state = {
        "pending_tasks": ["both"],
        "pending_items": [{"name": "水杯"}],
        "original_intent": "想分析水杯的需求和价格并找买家",
        "handoff_at": parked_at.isoformat(timespec="seconds"),
        "handoff_turn": 0,
        "turns": [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
    }

    original = handoff.ItemRepository
    handoff.ItemRepository = _FakeRepo
    try:
        # 卖家挂账前就已经有一件名字撞得上的旧商品——这正是兜底匹配会误伤的形状。
        _FakeRepo.rows = [_FakeItem(9, "水杯", parked_at - timedelta(days=3))]

        with_photo = handoff.resume_target(
            None, 1, {**state, "photo_path": "u/1.jpg"}, "上传了",
            intent_rules.hints("上传了", []),
        )
        check("带图的「上传了」不接力（否则这次上架被换成一次分析）",
              with_photo is None, f"got {with_photo}")

        # 同一句话、同一个库，只是这一轮没带图：兜底匹配照常工作。
        without = handoff.resume_target(
            None, 1, {**state, "photo_path": ""}, "上传了",
            intent_rules.hints("上传了", []),
        )
        check("不带图时兜底匹配仍然接力", without is not None and bool(without.get("items")),
              f"got {without}")

        # 挂账之后新建的那件：主依据（按时间）该接上。
        _FakeRepo.rows = [
            _FakeItem(9, "蓝牙音箱", parked_at - timedelta(days=3)),
            _FakeItem(12, "带盖饮料纸杯", parked_at + timedelta(minutes=2)),
        ]
        resumed = handoff.resume_target(
            None, 1, {**state, "photo_path": ""}, "上传好了",
            intent_rules.hints("上传好了", []),
        )
        ok = resumed is not None and [i["item_id"] for i in resumed["items"]] == [12]
        check("「上传好了」按时间绑上刚上架的那件（名字对不上也绑得到）", ok, f"got {resumed}")
        check("接力恢复的是原来那个任务",
              resumed is not None and resumed.get("scenario") == "both")
        check("接力带着卖家的原话",
              resumed is not None
              and resumed.get("original_intent") == state["original_intent"])
    finally:
        handoff.ItemRepository = original
        _FakeRepo.rows = []


# ============================================================
# 四、上架完成：草稿箱由 approve 清，不等下一轮
# ============================================================
def test_listing_done_patch() -> None:
    from app.services.agents.graph import AdvisorState
    from app.services.agents.publishing.approve import _LISTING_DONE_PATCH

    section("上架完成：清草稿箱，留账本")

    fields = set(AdvisorState.__annotations__)
    unknown = [k for k in _LISTING_DONE_PATCH if k not in fields]
    check("补丁里每个键都是 AdvisorState 声明过的通道（否则 update_state 会抛）",
          not unknown, f"unknown: {unknown}")

    for key in ("publish_text", "publish_photo", "publish_draft", "hold_after_publish"):
        check(f"清掉 {key}", key in _LISTING_DONE_PATCH)

    for key in ("pending_tasks", "pending_items", "original_intent", "handoff_at"):
        check(f"**不**清 {key}（那正是接下来要接回来的东西）",
              key not in _LISTING_DONE_PATCH)

    check("草稿箱清成空字符串而不是 None",
          _LISTING_DONE_PATCH["publish_text"] == ""
          and _LISTING_DONE_PATCH["publish_photo"] == "")


# ============================================================
# 五、接线与回复
# ============================================================
def test_graph_wiring() -> None:
    from app.services.agents import handoff, router as router_mod
    from app.services.agents.graph import AdvisorState, get_graph
    from app.services.agents.graph.nodes import item_not_found_node

    section("接线：新节点真的在图里")

    graph = get_graph()
    check("图里有 item_not_found 节点", "item_not_found" in graph.nodes)
    check("AdvisorState 有 missing_items", "missing_items" in AdvisorState.__annotations__)
    check("新路由在挂账触发集里", router_mod.MISSING_ITEM_ROUTE in handoff.HANDOFF_ROUTES)
    check("模型不会输出这个路由（不在白名单里）",
          router_mod.MISSING_ITEM_ROUTE not in router_mod.VALID_TARGETS)

    out = item_not_found_node(
        {"message": "想分析水杯的需求和价格并找买家", "turns": [],
         "missing_items": ["水杯"], "pending_tasks": ["both"]},
        {"configurable": {}},
    )
    reply = out["result"]["reply"]
    print(f"\n  ——回复原文——\n  {reply}\n")
    check("回复点出了没找到的是哪件", "水杯" in reply)
    check("回复说了下一步该做什么", "照片" in reply or "上架" in reply)
    check("回复承诺了接下来替他办什么", "需求和定价分析" in reply)
    check("这一轮记进了对话历史", len(out["turns"]) == 2)


# ============================================================
# 六、端到端：你测失败的那三轮，路由层原样重跑
# ============================================================
def test_three_turns() -> None:
    """只跑 `route_node`，但跑的是真实的那一个。

    桩只有两个，都是这次修复**不该**依赖的东西：卖家的在售列表（一条 SQL）和
    路由器的模型调用。闸门、挂账、接力全是真的。
    """
    from app.services.agents import graph as graph_mod
    from app.services.agents import handoff
    from app.services.agents import router as router_mod

    section("端到端：分析水杯 → 上传 → 上架 → 上传好了")

    listings = list(LISTINGS)
    parked_at = datetime.utcnow()

    def fake_facts(db, seller_id):
        return bool(listings), list(listings)

    def fake_route(**kwargs):
        return _SCRIPTED.pop(0)

    _SCRIPTED = [
        router_mod.RouteResult(
            agent="sales_agent", scenario="both",
            slots=router_mod.RouteSlots(item_ref="水杯"),
            rewritten_query="想分析水杯的需求和价格并找买家",
        ),
        router_mod.RouteResult(
            agent="publishing_agent", scenario="none",
            slots=router_mod.RouteSlots(), rewritten_query="上传了",
        ),
    ]

    saved = (graph_mod.nodes._seller_facts, router_mod.route, handoff.ItemRepository)
    graph_mod.nodes._seller_facts = fake_facts
    router_mod.route = fake_route
    handoff.ItemRepository = _FakeRepo
    _FakeRepo.rows = [_FakeItem(1, "蓝牙音箱", parked_at - timedelta(days=9)),
                      _FakeItem(2, "台灯", parked_at - timedelta(days=4))]
    cfg = {"configurable": {"db": None}}

    try:
        # --- 第一轮：分析一件库里没有的东西 -----------------------------
        state = {"seller_id": 1, "message": "想分析水杯的需求和价格并找买家",
                 "photo_path": "", "turns": []}
        patch = graph_mod.nodes.route_node(state, cfg)
        state = {**state, **patch}
        check("第一轮改判为 item_not_found", state["route"] == "item_not_found",
              f"got {state['route']}")
        check("第一轮挂了账", state.get("pending_tasks") == ["both"],
              f"got {state.get('pending_tasks')}")
        check("第一轮记下了原话",
              state.get("original_intent") == "想分析水杯的需求和价格并找买家")

        node_out = graph_mod.nodes.item_not_found_node(state, cfg)
        state = {**state, **node_out}

        # --- 第二轮：传图 + 「上传了」 -----------------------------------
        state = {**state, "message": "上传了", "photo_path": "uploads/cup.jpg"}
        patch = graph_mod.nodes.route_node(state, cfg)
        state = {**state, **patch}
        check("第二轮去上架，没被接力劫持", state["route"] == "publishing_agent",
              f"got {state['route']}")
        check("第二轮账本还在", state.get("pending_tasks") == ["both"],
              f"got {state.get('pending_tasks')}")

        # --- 上架完成（approve 干的事）：新商品入库，草稿箱清空 ----------
        _FakeRepo.rows.append(
            _FakeItem(31, "带盖饮料纸杯", datetime.utcnow() + timedelta(seconds=1))
        )
        listings.append({"item_id": 31, "name": "带盖饮料纸杯", "price": 5.0,
                         "category": "家居用品"})
        state = {**state, "publish_text": "", "publish_photo": "", "photo_path": ""}

        # --- 第三轮：「上传好了」 ---------------------------------------
        state = {**state, "message": "上传好了", "photo_path": ""}
        patch = graph_mod.nodes.route_node(state, cfg)
        state = {**state, **patch}

        check("第三轮回到销售分析，不是反问", state["route"] == "sales_agent",
              f"got {state['route']}")
        check("接的是原来那个场景（需求 + 定价）", state["scenario"] == "both",
              f"got {state['scenario']}")
        check("专家收到的是卖家最早那句话，不是「上传好了」",
              state["rewritten_query"] == "想分析水杯的需求和价格并找买家",
              f"got {state['rewritten_query']}")
        check("绑的是刚上架的那件（名字和「水杯」对不上也绑得到）",
              state["item_refs"] == ["带盖饮料纸杯"], f"got {state['item_refs']}")
        check("回复里会复述原始诉求",
              (state.get("handoff_note") or {}).get("original_intent")
              == "想分析水杯的需求和价格并找买家")
        check("账本已销（不会在三轮后突然接管一次无关对话)",
              state.get("pending_tasks") == [], f"got {state.get('pending_tasks')}")
        check("第三轮没再调路由器（队列在，算得出来）", len(_SCRIPTED) == 0,
              f"{len(_SCRIPTED)} scripted route(s) unused")
    finally:
        graph_mod.nodes._seller_facts, router_mod.route, handoff.ItemRepository = saved
        _FakeRepo.rows = []


# ============================================================
# 七、上架是有终点的：发布完之后再说一次「上传好了」
# ============================================================
def test_listing_done_gate() -> None:
    from app.services.nlp import intent_rules
    from app.services.agents import router as router_mod
    from app.services.agents.graph.nodes import listing_done_node

    section("上架终点：发布完之后的「上传好了」不能再进上架流程")

    listed = {"item_id": 946, "name": "乐高史努比狗屋积木", "at": "2026-09-22T22:08:06"}

    def gate(msg, *, agent="publishing_agent", text="", photo=False, last=None):
        return router_mod.apply_listing_done_gate(
            agent, intent_rules.hints(msg, []), text, photo,
            listed if last is None else last,
        )

    check("发布完之后的「上传好了」→ 不再进上架",
          gate("上传好了") == "listing_done", gate("上传好了"))
    check("路由器说去反问也一样拦下（那正是「你想做什么」的来源）",
          gate("上传好了", agent="clarify") == "listing_done")

    check("草稿箱非空 → 不拦（这是一次正在进行的上架）",
          gate("弄好了", text="一盒乐高 八成新") == "publishing_agent")
    check("这一轮带了照片 → 不拦（他在传下一件）",
          gate("上传好了", photo=True) == "publishing_agent")
    check("这个会话没发布过任何东西 → 不拦（没有东西可指认）",
          gate("上传好了", last={}) == "publishing_agent")
    check("不是一句「做完了」→ 不拦",
          gate("再帮我挂一件微波炉") == "publishing_agent")
    check("状态变更路上的「好了」不归这里",
          gate("上传好了", agent="status_change") == "status_change")

    out = listing_done_node(
        {"message": "上传好了", "turns": [], "last_listed": listed}, {"configurable": {}}
    )
    reply = out["result"]["reply"]
    print(f"\n  ——回复原文——\n  {reply}\n")
    check("回复点名了已经挂出去的那件", "乐高史努比狗屋积木" in reply)
    check("回复明说了不用再传一次", "不用再传" in reply)
    check("用完即清（不会在三轮后又宣布一次上架）", out["last_listed"] == {})


# ============================================================
# 八、端到端：账销掉之后又说一次「上传好了」
# ============================================================
def test_second_upload_done() -> None:
    """实测里造出两条一模一样的乐高的，正是这第二句。

    第一句「上传好了」把挂账接走了（账随之销掉）。第二句没有账可接，于是路由器
    看着一段满是上架对话的历史，把它送回 `publishing_agent`——`publish_text` 哪怕
    已经被 approve 清空，`publishing_node` 也会拿这四个字重新跑一遍抽取，回头问
    「这套积木是未拆封还是拼过」。卖家照答、再发布一次，库里就多一件。
    """
    from app.services.agents import graph as graph_mod
    from app.services.agents import handoff
    from app.services.agents import router as router_mod

    section("端到端：账销掉之后的第二句「上传好了」")

    listings = [{"item_id": 946, "name": "乐高史努比狗屋积木", "price": 320.0,
                 "category": "玩具"}]

    def fake_facts(db, seller_id):
        return True, list(listings)

    def fake_route(**kwargs):
        # 老路由器对这句话的判断就是这个——它看到的历史全是上架对话。
        return router_mod.RouteResult(
            agent="publishing_agent", scenario="none",
            slots=router_mod.RouteSlots(), rewritten_query="上传好了",
        )

    saved = (graph_mod.nodes._seller_facts, router_mod.route, handoff.ItemRepository)
    graph_mod.nodes._seller_facts = fake_facts
    router_mod.route = fake_route
    handoff.ItemRepository = _FakeRepo
    _FakeRepo.rows = [_FakeItem(946, "乐高史努比狗屋积木", datetime.utcnow())]
    cfg = {"configurable": {"db": None}}

    try:
        # approve 刚跑完：草稿箱空了，账也已经在上一轮被接走清掉了。
        state = {
            "seller_id": 1, "message": "上传好了", "photo_path": "", "turns": [],
            "publish_text": "", "publish_photo": "",
            "pending_tasks": [], "pending_items": [], "original_intent": "",
            "last_listed": {"item_id": 946, "name": "乐高史努比狗屋积木",
                            "at": datetime.utcnow().isoformat(timespec="seconds")},
        }
        patch = graph_mod.nodes.route_node(state, cfg)
        state = {**state, **patch}
        check("第二句「上传好了」不再回上架流程", state["route"] == "listing_done",
              f"got {state['route']}")

        out = graph_mod.nodes.listing_done_node(state, cfg)
        reply = out["result"]["reply"]
        check("回复点名了已经挂出去的那件", "乐高史努比狗屋积木" in reply)
        check("回复明说不用再传", "不用再传" in reply)
        check("不再追问成色", "成色" not in reply and "未拆封" not in reply)

        # 同一句话，但草稿箱里还有东西——那是一次正在进行的上架，不能拦。
        mid = graph_mod.nodes.route_node(
            {**state, "publish_text": "一盒乐高 八成新", "last_listed": {}}, cfg
        )
        check("正在进行的上架不受影响", mid["route"] == "publishing_agent",
              f"got {mid['route']}")
    finally:
        graph_mod.nodes._seller_facts, router_mod.route, handoff.ItemRepository = saved
        _FakeRepo.rows = []


# ============================================================
# 九、一张照片就够了：缺的东西在表里补，不在对话里补
# ============================================================
def test_form_is_the_answer() -> None:
    """「信息不足」不是一条发布条件。

    上架的完整路径是「传一张照片 → 系统填表 → 卖家把剩下的选完 → 点发布」。
    中间没有一步要求他在对话框里把商品描述再打一遍。而旧版本有两处在要求：

    * 合规检查报 WARN-VAGUE / WARN-CONDITION-UNCLEAR——判的还是聊天记录，
      因为它跑在抽取之前，那一刻描述根本不存在；
    * 草稿不齐时，回复是模型写的那句追问（「这套积木是全新未拆封还是拼装过？」），
      而下面那张表里「成色 · 必填」就是一个下拉框——同一个问题问了两遍，一遍
      要打字，一遍点一下就完了。
    """
    from app.services.agents.publishing.rules import (
        SUPPRESSED_IN_PUBLISHING, publishable_findings,
    )
    from app.services.agents.publishing.schemas import PublishFinding

    section("一张照片就够了：缺的在表里补")

    check("两条「信息不足」被挡在上架流程之外",
          SUPPRESSED_IN_PUBLISHING == {"WARN-VAGUE", "WARN-CONDITION-UNCLEAR"},
          str(sorted(SUPPRESSED_IN_PUBLISHING)))

    findings = [
        PublishFinding(code="BAN-WEAPONS", severity="block", title="武器", reason="x"),
        PublishFinding(code="WARN-VAGUE", severity="warn", title="描述过于单薄", reason="y"),
        PublishFinding(code="WARN-CONDITION-UNCLEAR", severity="warn", title="没说成色", reason="z"),
        PublishFinding(code="WARN-CONTACT", severity="warn", title="别留联系方式", reason="w"),
    ]
    kept = [f.code for f in publishable_findings(findings)]
    check("硬禁令照样留下", "BAN-WEAPONS" in kept, str(kept))
    check("和商品本身有关的建议照样留下", "WARN-CONTACT" in kept, str(kept))
    check("两条「信息不足」被摘掉",
          "WARN-VAGUE" not in kept and "WARN-CONDITION-UNCLEAR" not in kept, str(kept))

    # 三个发出点都要过这道滤网：合规、抽取后的展示、发布回执。
    graph_src = io.open(
        "backend/app/services/agents/graph.py", encoding="utf-8"
    ).read()
    approve_src = io.open(
        "backend/app/services/publishing/approve.py", encoding="utf-8"
    ).read()
    node = graph_src[graph_src.index("def publish_compliance_node"):
                     graph_src.index("def publish_extract_node")]
    check("合规节点过滤", "publishable_findings" in node)
    check("发布回执过滤", "publishable_findings(findings)" in approve_src)

    extract = graph_src[graph_src.index("def publish_extract_node"):
                        graph_src.index("def view_get")]
    # 只看**代码行**。注释里还留着这个名字（在解释为什么不读它），而裸字符串
    # 匹配分不出注释和执行——opt60 那条「锚在调用点上，别用裸函数名」的同族。
    code_only = "\n".join(
        ln for ln in extract.splitlines() if not ln.strip().startswith("#")
    )
    check("抽取节点不再读 follow_up_question（那句追问的来源）",
          "draft.follow_up_question" not in code_only)
    check("草稿不齐时也把表给卖家，不再改成一句追问",
          "在下面那张表里选一下" in extract)
    check("缺的字段用中文标签点名", "field_label" in extract)


def _reply_for(missing, ready):
    """把 `publish_extract_node` 里那段选词逻辑照抄一遍，用来断言文案。"""
    from app.services.agents.publishing.schemas import field_label

    names = [field_label(f) for f in missing if f]
    if ready or not names:
        return ("这是我整理出来的商品信息——你过一遍，有不对的地方直接改，"
                "然后填上价格和自提点就能发布了。")
    return ("这是我整理出来的商品信息——你过一遍，有不对的地方直接改。"
            f"还差{'、'.join(names[:4])}，在下面那张表里选一下；"
            "再填上价格和自提点就能发布了。")


def test_draft_reply_wording() -> None:
    section("草稿回复：永远是表，不是问题")

    ready = _reply_for([], True)
    check("草稿齐了 → 请他过目并填价格", "填上价格和自提点" in ready)

    short = _reply_for(["condition"], False)
    print(f"\n  ——回复原文——\n  {short}\n")
    check("只缺成色 → 点名让他在表里选", "成色" in short and "表里选" in short)
    check("不再问「全新还是拼装过」", "拆封" not in short and "？" not in short)
    check("仍然告诉他怎样才算完成", "就能发布了" in short)

    many = _reply_for(["condition", "brand", "size", "color", "material"], False)
    check("缺太多时只点前四项，不念清单", many.count("、") <= 3, many)


def main() -> int:
    test_missing_item_gate()
    test_capture_on_missing_item()
    test_resume_photo_guard()
    test_listing_done_patch()
    test_graph_wiring()
    test_listing_done_gate()
    test_three_turns()
    test_second_upload_done()
    test_form_is_the_answer()
    test_draft_reply_wording()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return len(FAIL)


if __name__ == "__main__":
    sys.exit(main())
