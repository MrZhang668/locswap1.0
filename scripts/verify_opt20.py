"""Acceptance for 优化建议 2.0 — 总类 3.0 多 agent 调用的三个归因。

和 `verify_opt10.py` 同一个取舍：这里断言的是**确定性的那一半**——词法判定、日期
换算、路由覆盖、任务状态的挂起与恢复、周扫描的触发判据、邮件正文的内容。这一半
既是修复的承重墙，也是最容易在后续改动里悄悄烂掉的部分。模型那一半（路由器真的
会选 `defer_sale` 吗、专家真的会逐件做完吗）要靠 805 跑批，那个花配额；这个脚本
是在花配额之前告诉你底下的管道是通的。

    python scripts/verify_opt20.py               # 纯断言，不碰任何真实数据
    python scripts/verify_opt20.py --live-db     # 加上真实库：建计划、跑周扫描、写邮件
    python scripts/verify_opt20.py --live-router  # 加上真实路由调用（要 API key，花钱）

`--live-db` 会在 `locswap.db` 里建一条出售计划和若干快照、通知、outbox 行，跑完
**自己清理干净**（按创建出来的 id 删）。它默认不跑，是因为它会写真实库。

退出码是失败断言的条数。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

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


# ============================================================
# 归因一 — agent 交接时的会话级任务状态
# ============================================================
def test_handoff_cues() -> None:
    from app.services.nlp import intent_rules

    section("归因一 交接信号：认出「做完了」和「不止一件」")

    for msg in ("上传好了", "传好了", "已上传", "发布好了", "都弄好了", "搞定了", "挂上去了"):
        check(f"「{msg}」识别为交接完成", intent_rules.handoff_done(msg))

    for msg in ("帮我卖微波炉", "这个多少钱合适", "怎么才能卖得快一点"):
        check(f"「{msg}」不是交接完成", not intent_rules.handoff_done(msg))

    check("「都弄好了」识别为多商品", intent_rules.multi_item_cue("都弄好了"))
    check("「这两件都传好了」识别为多商品", intent_rules.multi_item_cue("这两件都传好了"))
    check("「上传好了」不是多商品", not intent_rules.multi_item_cue("上传好了"))


def test_handoff_capture() -> None:
    from app.services.agents import handoff
    from app.services.nlp import intent_rules

    section("归因一 任务挂起：交给 publishing 时把原始诉求记下来")

    hints = intent_rules.hints("帮我卖小白板", [])
    patch = handoff.capture(
        route="publishing_agent", scenario="none", slots={"item_ref": "小白板"},
        hints=hints, message="帮我卖小白板", turn_index=0,
    )
    check("泛卖出 + 去上架 → 记下任务", patch.get("pending_tasks") == ["sell_through"], str(patch))
    check("原始诉求原样保留", patch.get("original_intent") == "帮我卖小白板", str(patch))
    check("待处理实体记下来了",
          [i["name"] for i in patch.get("pending_items", [])] == ["小白板"], str(patch))
    check("挂起时刻可解析", bool(patch.get("handoff_at")), str(patch))

    # 点名了分析的场景，队列里存的就是那个场景，不是笼统的 sell_through。
    patch2 = handoff.capture(
        route="publishing_agent", scenario="price", slots={"item_ref": "显示器"},
        hints=intent_rules.hints("这个显示器能卖多少钱", []),
        message="这个显示器能卖多少钱", turn_index=0,
    )
    check("点名了定价 → 队列里存 price", patch2.get("pending_tasks") == ["price"], str(patch2))

    # 纯上架请求后面没有藏着任何分析，不该凭空排队。
    patch3 = handoff.capture(
        route="publishing_agent", scenario="none", slots={},
        hints=intent_rules.hints("帮我把这个挂出去", []),
        message="帮我把这个挂出去", turn_index=0,
    )
    check("纯上架请求不排队", patch3 == {}, str(patch3))

    # 已经在办了就没有什么被打断，不该留条子。
    patch4 = handoff.capture(
        route="sales_agent", scenario="sell_through", slots={"item_ref": "微波炉"},
        hints=intent_rules.hints("帮我卖微波炉", []),
        message="帮我卖微波炉", turn_index=0,
    )
    check("路由到 sales 时不记交接", patch4 == {}, str(patch4))

    # clarify 也会悬空原始诉求。「这两件都帮我卖了」在库里对不上东西时，路由器问
    # 「是哪两件」——问得没错，但卖家转头去上架，回来那一跳一样什么都没留下。
    msg5 = "这两件都帮我卖了"
    hints5 = intent_rules.hints(msg5, [])
    patch5 = handoff.capture(
        route="clarify", scenario="none", slots={}, hints=hints5,
        message=msg5, turn_index=0,
    )
    check("澄清也会挂起（意图明确、只是实体不明）",
          patch5.get("pending_tasks") == ["sell_through"], str(patch5))
    check("挂起时就记下了「不止一件」", patch5.get("handoff_multi") is True, str(patch5))

    # 但不是凡澄清必挂起：卖家还没说要卖什么的时候，后台不该排一个队列。
    patch6 = handoff.capture(
        route="clarify", scenario="none", slots={},
        hints=intent_rules.hints("你能做什么", []),
        message="你能做什么", turn_index=0,
    )
    check("普通澄清不挂起", patch6 == {}, str(patch6))

    cleared = handoff.cleared()
    check("清理函数把队列和原始诉求都清空",
          cleared["pending_tasks"] == [] and cleared["original_intent"] == "" and
          cleared["pending_items"] == [], str(cleared))


def test_handoff_resume_gates() -> None:
    """接力的门槛：没有挂起任务时，「上传好了」什么都不该触发。"""
    from app.services.agents import handoff
    from app.services.nlp import intent_rules

    section("归因一 接力门槛：没有任务就不接力")

    hints = intent_rules.hints("上传好了", [])
    out = handoff.resume_target(None, 1, {"pending_tasks": []}, "上传好了", hints)
    check("没有挂起任务 → 不接力（也不碰数据库）", out is None, str(out))

    state = {
        "pending_tasks": ["sell_through"],
        "pending_items": [{"name": "小白板"}],
        "handoff_at": datetime.utcnow().isoformat(timespec="seconds"),
        "handoff_turn": 0,
        "turns": [{"role": "user", "content": "x"}] * 40,   # 20 轮，远超上限
    }
    out = handoff.resume_target(None, 1, state, "上传好了", hints)
    check("挂太久的任务作废而不是接上",
          out is not None and out.get("expired") is True, str(out))

    # 不是一句「做完了」，就不该触发接力。
    state2 = dict(state, turns=[])
    out2 = handoff.resume_target(
        None, 1, state2, "顺便问一下你们平台抽成吗",
        intent_rules.hints("顺便问一下你们平台抽成吗", []),
    )
    check("无关消息不触发接力", out2 is None, str(out2))


# ============================================================
# 归因二 — 能力缺口的出口
# ============================================================
def test_defer_detection() -> None:
    from app.services.nlp import intent_rules

    section("归因二 延后出售：认出「要卖，但不是现在」")

    for msg in (
        "转换器我打算12月放假前再卖",
        "分线器还得用一阵，等学期末再挂",
        "这个下个月再挂出去吧",
        "等考完试再卖",
    ):
        check(f"「{msg}」识别为延后出售", intent_rules.defer_sale_intent(msg))

    # 反例：这几条各自属于别的路由，误判成延后出售就是新的答非所问。
    for msg in ("帮我卖微波炉", "先不卖了", "这个多少钱合适", "上个月挂出来的那件怎么样"):
        check(f"「{msg}」不是延后出售", not intent_rules.defer_sale_intent(msg), msg)

    check("「需求涨起来了你能提醒我吗」识别为提醒请求",
          intent_rules.wants_reminder("需求涨起来了你能提醒我吗"))
    check("「什么时候挂出去最划算」识别为时机问题",
          intent_rules.timing_question("什么时候挂出去最划算"))


def test_date_parsing() -> None:
    from app.services.nlp import intent_rules

    section("归因二 日期换算：确定性的，而且说得出依据")

    today = date(2026, 9, 20)
    cases = [
        ("转换器我打算12月放假前再卖", date(2026, 12, 15), "explicit_month"),
        ("等学期末再挂", date(2026, 12, 15), "semester"),
        ("11月3号再说", date(2026, 11, 3), "explicit_date"),
        ("下个月再挂", date(2026, 10, 20), "relative"),
        ("明年再卖", date(2027, 1, 15), "relative"),
    ]
    for msg, expect, basis in cases:
        got, got_basis = intent_rules.parse_target_date(msg, today)
        check(f"「{msg}」→ {expect}", got == expect and got_basis == basis,
              f"got {got} / {got_basis}")

    got, _ = intent_rules.parse_target_date("帮我看看价格", today)
    check("没有时间词 → 不编一个日期", got is None, str(got))

    # 解析出来的日期必须落在未来，否则周扫描会立刻把它关掉。
    for msg in ("等学期末再挂", "12月放假前再卖", "过一阵子再挂"):
        got, _ = intent_rules.parse_target_date(msg, today)
        check(f"「{msg}」落在未来", got is not None and got > today, str(got))


def test_defer_routing() -> None:
    from app.services.nlp import intent_rules
    from app.services.agents import router as router_mod

    section("归因二 路由覆盖：不再被 publishing / sales 吸收")

    msg = "转换器我打算12月放假前再卖"
    hints = intent_rules.hints(msg, [])
    for came_as in ("publishing_agent", "sales_agent", "clarify"):
        agent, scen, _ = router_mod.apply_defer_sale(came_as, "demand", {}, hints)
        check(f"模型答 {came_as} → 被拉回 defer_sale",
              agent == "defer_sale" and scen == "none", f"{agent}/{scen}")

    msg2 = "需求涨起来了你能提醒我吗"
    agent, _, _ = router_mod.apply_defer_sale(
        "clarify", "none", {}, intent_rules.hints(msg2, [])
    )
    check("提醒请求也走 defer_sale", agent == "defer_sale", agent)

    # 状态变更优先：能立刻办的先办，计划可以之后再说。
    msg3 = "这个先下架，等学期末再挂"
    hints3 = intent_rules.hints(msg3, [])
    agent, _, _ = router_mod.apply_defer_sale("sales_agent", "none", {}, hints3)
    check("「先下架，等学期末再挂」→ 让位给状态变更",
          agent == "sales_agent" and hints3["status_change_intent"] == "pause",
          f"{agent} / {hints3['status_change_intent']}")

    # 无关消息不该被这条覆盖碰到。
    msg4 = "帮我卖微波炉"
    agent, scen, _ = router_mod.apply_defer_sale(
        "sales_agent", "sell_through", {}, intent_rules.hints(msg4, [])
    )
    check("普通卖出意图不受影响",
          agent == "sales_agent" and scen == "sell_through", f"{agent}/{scen}")


def test_defer_target_validation() -> None:
    from app.services.agents.router import _plausible_date

    section("归因二 target_date 校验：过去的和太远的都丢掉")

    today = date.today()
    check("明天是合法日期", _plausible_date((today + timedelta(days=1)).isoformat()))
    check("昨天被丢弃", not _plausible_date((today - timedelta(days=1)).isoformat()))
    check("两年后被丢弃", not _plausible_date((today + timedelta(days=800)).isoformat()))
    check("非日期字符串被丢弃", not _plausible_date("学期末"))


def test_trigger_rules() -> None:
    """两条触发判据，用假的快照对象跑——不碰数据库。"""
    from types import SimpleNamespace

    from app.config import settings
    from app.models import NotificationType
    from app.services.sale_plans import evaluate

    section("归因二 触发判据：热度 + 增长，各自的边界")

    def snap(reserve=0, events=0, people=0, matching=0):
        return SimpleNamespace(
            reserve_count=reserve, events=events, people=people, matching_people=matching
        )

    hits = evaluate(snap(reserve=settings.sale_plan_reserve_threshold + 1), None)
    check("预订数过线 → 触发热度通知",
          [k for k, _ in hits] == [NotificationType.SALE_PLAN_HOT], str(hits))

    hits = evaluate(snap(reserve=settings.sale_plan_reserve_threshold), None)
    check("正好等于阈值 → 不触发（判据是「超过」）", hits == [], str(hits))

    prior = snap(events=10)
    hits = evaluate(snap(events=14), prior)
    check("10 → 14（+40%）触发增长通知",
          [k for k, _ in hits] == [NotificationType.SALE_PLAN_SURGE], str(hits))

    hits = evaluate(snap(events=12), prior)
    check("10 → 12（+20%）不触发", hits == [], str(hits))

    # 基数下限：这条是防止「1 涨到 2 就是 100%」的那道闸。
    hits = evaluate(snap(events=2), snap(events=1))
    check("1 → 2 不触发（上周基数低于下限）", hits == [], str(hits))

    hits = evaluate(snap(events=20), None)
    check("没有上一周就没有增长率", hits == [], str(hits))


def test_plan_arithmetic() -> None:
    from app.config import settings
    from app.services.sale_plans import week_start_of

    section("归因二 观察窗口与周对齐")

    check("周对齐取周一", week_start_of(date(2026, 9, 24)) == date(2026, 9, 21))
    check("周一对齐到自己", week_start_of(date(2026, 9, 21)) == date(2026, 9, 21))
    check("观察窗口是 6 周", settings.sale_plan_watch_weeks == 6)


def test_defer_reply_promises() -> None:
    """回执里的每一句都是承诺，所以它只能承诺真的会发生的事。"""
    from types import SimpleNamespace

    from app.services.agents.graph.nodes import _defer_reply

    section("归因二 回执：承诺与实际判据必须一致")

    def plan(category):
        return SimpleNamespace(
            item_name="转换器", category=category,
            target_date=date(2026, 12, 15), watch_from=date(2026, 11, 3),
        )

    with_cat = _defer_reply(plan("电子产品"), "explicit_month", False, "s@campus.edu")
    check("有类别时把推断出的日期说出来", "12 月 15 日" in with_cat, with_cat[:120])
    check("有类别时两条判据都列出来",
          "预订超过" in with_cat and "涨了" in with_cat, with_cat[:200])
    check("回执写明不会自动上架", "不会替你自动上架" in with_cat, with_cat[:200])
    check("回执给出撤销的办法", "撤掉" in with_cat, with_cat[-160:])

    no_cat = _defer_reply(plan(None), "explicit_month", False, "s@campus.edu")
    # 这一条是本次修复的重点：没有类别时预订判据根本不会触发，把它继续列进
    # 「我会做什么」，就是在同一段话里先说它用不上、再把它当成承诺。
    # 判定针对的是**承诺句**，不是「预订」这两个字：解释段里会说这条判据用不上，
    # 那句话必须留着。承诺句的形状是带阈值数字的那一句。
    from app.config import settings as _s

    promise = f"一周的预订超过 {_s.sale_plan_reserve_threshold} 单"
    check("没有类别时不承诺预订那条判据", promise not in no_cat, no_cat[-400:])
    check("有类别时才承诺预订那条判据", promise in with_cat, with_cat[-400:])
    check("没有类别时说明了原因",
          "没能归进平台现有的类别" in no_cat, no_cat[:300])
    check("没有类别时仍然承诺增长那条判据", "涨了" in no_cat, no_cat[-300:])

    listed = _defer_reply(plan("电子产品"), "semester", True, "s@campus.edu")
    check("商品还挂着时会说明这一点", "还挂在架上" in listed, listed[:400])


# ============================================================
# 归因三 — 澄清收敛
# ============================================================
def test_confirm_authorize_detection() -> None:
    from app.services.nlp import intent_rules

    section("归因三 确认与授权：两类输入都携带信息")

    for msg in ("对", "对，就那个", "是的", "没错", "嗯", "就是它", "确认"):
        check(f"「{msg}」识别为确认", intent_rules.is_confirmation(msg))

    for msg in ("都行", "都行，你决定", "随便", "你看着办", "无所谓", "听你的"):
        check(f"「{msg}」识别为授权", intent_rules.is_authorization(msg))

    # 授权优先于确认：两边都像的时候读成确认，会退化成再问一次。
    check("「都行，你决定」不被读成确认", not intent_rules.is_confirmation("都行，你决定"))

    # 误伤反例：「对了」是话题转折词，不是确认。
    check("「对了，帮我看看价格」不是确认",
          not intent_rules.is_confirmation("对了，帮我看看价格"))
    check("「帮我卖微波炉」不是授权", not intent_rules.is_authorization("帮我卖微波炉"))


def test_clarify_convergence() -> None:
    from app.services.nlp import intent_rules
    from app.services.agents import router as router_mod

    section("归因三 澄清收敛：四条终止条件")

    last_ctx = {"item_id": 2, "item_name": "宜家四层书架", "scenario": "find_buyers"}

    def converge(msg, *, filled=None, authorized=False, rounds=0, agent="clarify",
                 scenario="none", slots=None, ctx=None):
        return router_mod.apply_clarify_convergence(
            agent, scenario, dict(slots or {}), intent_rules.hints(msg, []),
            filled_slots=filled or {}, user_authorized=authorized,
            clarify_rounds=rounds, last_context=ctx if ctx is not None else last_ctx,
            has_active_items=True, max_clarify_rounds=2,
        )

    # 1. 已填槽位写回
    agent, scen, slots, why = converge(
        "这学期需求怎么样", filled={"item_ref": "书架"}, agent="sales_agent", scenario="demand"
    )
    check("已填槽位补进空位", slots.get("item_ref") == "书架", str(slots))

    agent, scen, slots, why = converge(
        "台灯呢", filled={"item_ref": "书架"}, agent="sales_agent",
        scenario="demand", slots={"item_ref": "台灯"},
    )
    check("本轮抽到的值优先于记忆", slots.get("item_ref") == "台灯", str(slots))

    # 2. 授权 → 默认方案
    agent, scen, slots, why = converge("都行，你决定")
    check("授权 → 不再澄清，执行默认方案",
          agent == "sales_agent" and scen == "both" and why == "authorized",
          f"{agent}/{scen}/{why}")

    agent, scen, slots, why = converge("那需求怎么样", authorized=True)
    check("授权是会话级的，后续澄清也不再问",
          agent == "sales_agent" and why == "authorized", f"{agent}/{why}")

    # 3. 确认 → 绑定上一轮的对象
    agent, scen, slots, why = converge("对，就那个")
    check("确认 → 绑定上一轮商品并沿用场景",
          agent == "sales_agent" and slots.get("item_ref") == "宜家四层书架"
          and scen == "find_buyers" and why == "confirmed",
          f"{agent}/{scen}/{slots}/{why}")

    agent, scen, slots, why = converge("对，就那个", ctx={})
    check("没有上一轮可绑时，确认仍然是一次真正的澄清",
          agent == "clarify" and why == "", f"{agent}/{why}")

    # 4. 轮次上限
    agent, scen, slots, why = converge("我也不太确定", rounds=2)
    check("澄清到上限 → 按默认方案执行",
          agent == "sales_agent" and scen == "both" and why == "clarify_cap",
          f"{agent}/{scen}/{why}")

    agent, scen, slots, why = converge("我也不太确定", rounds=1)
    check("没到上限时正常澄清", agent == "clarify" and why == "", f"{agent}/{why}")

    # 非 clarify 路由不该被这个函数改动路由，只补槽位。
    agent, scen, slots, why = converge(
        "都行", agent="publishing_agent", scenario="none"
    )
    check("publishing 路由不被澄清收敛改写",
          agent == "publishing_agent" and why == "", f"{agent}/{why}")


def test_forced_default_is_announced() -> None:
    """默认方案必须自报家门——不声不响的默认方案只会被当成答错。"""
    from app.services.agents.sales.orchestrator import _continuation_block

    section("归因三 默认方案要说出来")

    for reason in ("authorized", "clarify_cap"):
        block = _continuation_block({}, [], reason)
        check(f"{reason} 会生成 continuation 段", "<continuation>" in block, block[:80])
        check(f"{reason} 明确要求不要再提问",
              "不要再" in block or "不要提问" in block, block[:200])

    check("没有任何来历时不生成多余的段", _continuation_block({}, [], "") == "")


def test_continuation_block() -> None:
    from app.services.agents.sales.orchestrator import _continuation_block

    section("归因一 接力段：复述原始诉求、逐件处理、缺件如实说")

    block = _continuation_block(
        {"original_intent": "帮我卖小白板", "items": ["白色磁性写字板"], "unbound": []},
        ["白色磁性写字板"], "",
    )
    check("接力段复述了原始诉求", "帮我卖小白板" in block, block[:160])
    check("接力段禁止重新澄清", "不要再问他想做什么" in block, block[:300])

    multi = _continuation_block(
        {"original_intent": "这两件都帮我卖了", "items": ["A", "B"], "unbound": []},
        ["白色写字板", "宜家书架"], "",
    )
    check("多商品时要求逐件做完", "多件商品" in multi and "不要只挑一件" in multi, multi[:300])

    partial = _continuation_block(
        {"original_intent": "这两件都帮我卖了", "items": ["A"], "unbound": ["转换器"]},
        ["白色写字板"], "",
    )
    check("有商品没绑上时要求如实说明",
          "转换器" in partial and "如实说明" in partial, partial[:300])


# ============================================================
# 结构性检查：图和状态
# ============================================================
def test_graph_wiring() -> None:
    from app.services.agents import graph as graph_mod

    section("图结构：新节点接上了，状态字段齐了")

    src = _bootstrap.module_source(graph_mod)
    for node in ("defer_sale",):
        check(f"节点 {node} 已注册", f'add_node("{node}"' in src)
        check(f"节点 {node} 在路由分支表里", f'"{node}": "{node}"' in src)

    keys = set(graph_mod.AdvisorState.__annotations__)
    for key in (
        "pending_tasks", "pending_items", "original_intent", "handoff_at",
        "filled_slots", "user_authorized", "clarify_rounds", "forced_default",
        "item_refs", "sale_plan",
    ):
        check(f"AdvisorState 有 {key}", key in keys)

    check("defer_sale 是合法路由目标",
          "defer_sale" in __import__(
              "app.services.agents.router", fromlist=["x"]
          ).VALID_TARGETS)


def test_router_prompt_mentions_new_exits() -> None:
    """提示词和代码必须同步——路由器答得出 `defer_sale` 的前提是它知道有这条路。"""
    from app.services.prompts import load_prompt

    section("提示词同步")

    router_prompt = load_prompt("orchestrator_router")
    for token in ("defer_sale", "target_date", "is_confirmation", "is_authorization",
                  "handoff_done", "multi_item_cue"):
        check(f"路由提示词提到 {token}", token in router_prompt)

    advisor_prompt = load_prompt("seller_advisor")
    check("顾问提示词有能力护栏（不得承诺提醒）",
          "主动提醒" in advisor_prompt and "不要答应" in advisor_prompt or
          "一个字都不要答应" in advisor_prompt, advisor_prompt[:0])
    check("顾问提示词要求时机问题走 analyze_demand",
          "什么时候挂出去最划算" in advisor_prompt)
    check("顾问提示词认识 <continuation>", "<continuation>" in advisor_prompt)


def test_email_outbox_offline() -> None:
    from app.services import email_outbox

    section("邮件 outbox：没配 SMTP 时的行为")

    check("默认没有配置 SMTP", not email_outbox.smtp_configured())


# ============================================================
# --live-db：真实库里跑一遍计划 → 快照 → 触发 → 通知 + 邮件
# ============================================================
def test_sale_plan_live() -> None:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import (
        DemandEvent,
        DemandEventKind,
        DemandSnapshot,
        EmailOutbox,
        EmailStatus,
        Notification,
        NotificationType,
        SalePlan,
        SalePlanStatus,
        User,
    )
    from app.services import sale_plans

    section("归因二 端到端（真实库）：计划 → 快照 → 触发 → 通知 + 邮件")

    db = SessionLocal()
    made_events: list[int] = []
    plan = None
    try:
        seller = db.scalar(select(User).limit(1))
        if seller is None:
            check("需要至少一个用户才能跑这一段", False, "users 表是空的")
            return

        today = date.today()
        target = today + timedelta(weeks=3)   # 已经在 6 周观察窗口内
        plan = sale_plans.create_plan(
            db, seller.id,
            item_name="验收用转换器", target_date=target, date_basis="explicit_month",
            note="verify_opt20",
        )
        check("计划建好了", plan.id is not None and plan.status == SalePlanStatus.ACTIVE)
        check("观察起点是目标日期前 6 周",
              plan.watch_from == target - timedelta(weeks=6), str(plan.watch_from))
        check("计划出现在 due 列表里",
              any(p.id == plan.id for p in sale_plans.due_plans(db, today)))

        # 注入需求：上上周 10 次搜索，上周 20 次搜索 + 12 单预订。
        # 两条判据都该命中——热度（12 > 10）和增长（10 → 20 是 +100%）。
        #
        # 类别用一个真实数据里不存在的合成值。真实库那一周本来就有上百次搜索和
        # 十几单预订，落在真类别上就等于在别人的数字里加了十条——断言量的不是这
        # 次注入，而是背景噪声，测不出判据对不对。
        category = "__verify_opt20__"
        plan.category = category
        last_week = sale_plans.week_start_of(today) - timedelta(days=7)
        prev_week = last_week - timedelta(days=7)

        def add(kind, when, n, query=""):
            for i in range(n):
                ev = DemandEvent(
                    buyer_id=seller.id, kind=kind, query=query, category=category,
                    at=datetime.combine(when, datetime.min.time()) + timedelta(hours=i % 20),
                )
                db.add(ev)
                db.flush()
                made_events.append(ev.id)

        add(DemandEventKind.SEARCH, prev_week, 10, "转换器")
        add(DemandEventKind.SEARCH, last_week, 20, "转换器")
        add(DemandEventKind.RESERVE, last_week, 12)
        db.commit()

        # 上上周的快照要先存在，增长率才有对照。
        sale_plans.record_snapshot(db, plan, prev_week)
        db.commit()

        before_notes = db.scalar(
            select(SalePlan.notified_count).where(SalePlan.id == plan.id)
        )
        outcome = sale_plans.run_weekly_pass(db, today=today, force=True)
        check("周扫描看了这条计划", outcome["checked"] >= 1, str(outcome))
        check("两条判据都触发了", outcome["notified"] >= 2, str(outcome))

        snap = db.scalar(
            select(DemandSnapshot).where(
                DemandSnapshot.plan_id == plan.id, DemandSnapshot.week_start == last_week
            )
        )
        check("上周快照写下来了", snap is not None)
        if snap:
            check("预订数按类别统计正确", snap.reserve_count == 12, str(snap.reserve_count))
            check("搜索事件数正确", snap.events == 20, str(snap.events))
            check("关键词口径不为零（措辞对得上）",
                  snap.matching_people >= 1, str(snap.matching_people))

        notes = list(db.scalars(
            select(Notification).where(
                Notification.user_id == seller.id,
                Notification.type.in_(
                    [NotificationType.SALE_PLAN_HOT, NotificationType.SALE_PLAN_SURGE]
                ),
            ).order_by(Notification.id.desc()).limit(4)
        ))
        kinds = {n.type for n in notes if n.payload.get("plan_id") == plan.id}
        check("站内收到热度通知", NotificationType.SALE_PLAN_HOT in kinds, str(kinds))
        check("增长只发邮件，不发站内通知",
              NotificationType.SALE_PLAN_SURGE not in kinds, str(kinds))

        mails = list(db.scalars(
            select(EmailOutbox).where(
                EmailOutbox.user_id == seller.id,
                EmailOutbox.kind.in_(
                    [NotificationType.SALE_PLAN_HOT, NotificationType.SALE_PLAN_SURGE]
                ),
            ).order_by(EmailOutbox.id.desc()).limit(4)
        ))
        mine = [m for m in mails if "验收用转换器" in m.body]
        check("邮件写进了 outbox", len(mine) >= 2, f"{len(mine)} 封")
        surge_mail = [m for m in mine if m.kind == NotificationType.SALE_PLAN_SURGE]
        check("增长邮件提醒可以上架了",
              bool(surge_mail) and "可以上架了" in surge_mail[0].subject,
              surge_mail[0].subject if surge_mail else "没有增长邮件")
        if mine:
            body = mine[0].body
            check("邮件正文写了商品名", "验收用转换器" in body)
            check("邮件正文写了目标日期", f"{target:%m 月}" in body or f"{target.month}" in str(body))
            check("邮件正文把两个口径分开说了",
                  "搜" in body and ("对得上" in body or "措辞" in body), body[:160])
            check("邮件停在 pending（没配 SMTP 就不发）",
                  all(m.status == EmailStatus.PENDING for m in mine),
                  str([m.status for m in mine]))

        check("通知计数累加了",
              db.scalar(select(SalePlan.notified_count).where(SalePlan.id == plan.id))
              > (before_notes or 0))

        # 幂等：同一周再跑一次不该再发一轮。
        again = sale_plans.run_weekly_pass(db, today=today, force=False)
        check("非 force 且刚跑过 → 不重复发", again["notified"] == 0, str(again))
        # 同一自然周里换一天（哪怕不是周一）也不该再跑：去重看的是这周的快照。
        same_week_other_day = sale_plans.week_start_of(today) + timedelta(
            days=(today.weekday() + 3) % 7
        )
        again2 = sale_plans.run_weekly_pass(db, today=same_week_other_day, force=False)
        check("同一周换一天也不重复发", again2["notified"] == 0, str(again2))

        # 过期收摊。
        plan.target_date = today - timedelta(days=1)
        db.commit()
        closed = sale_plans.close_expired(db, today)
        check("过了目标日期的计划会收摊", closed >= 1, str(closed))
        db.refresh(plan)
        check("收摊后状态是 done", plan.status == SalePlanStatus.DONE, plan.status)

    finally:
        # 按 id 清理，只删这次造出来的行。
        try:
            if plan is not None:
                db.query(DemandSnapshot).filter(DemandSnapshot.plan_id == plan.id).delete()
                db.query(EmailOutbox).filter(
                    EmailOutbox.body.like("%验收用转换器%")
                ).delete(synchronize_session=False)
                db.query(Notification).filter(
                    Notification.type.in_(
                        [NotificationType.SALE_PLAN_HOT, NotificationType.SALE_PLAN_SURGE]
                    )
                ).delete(synchronize_session=False)
                db.query(SalePlan).filter(SalePlan.id == plan.id).delete()
            if made_events:
                db.query(DemandEvent).filter(DemandEvent.id.in_(made_events)).delete(
                    synchronize_session=False
                )
            db.commit()
            print("        （已清理本次写入的计划 / 快照 / 通知 / 邮件 / 需求事件）")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"        清理失败，需要手工检查：{exc}")
        db.close()


# ============================================================
# --live-router：真实路由调用
# ============================================================
def test_router_live() -> None:
    from app.services.nlp import intent_rules
    from app.services.agents import router as router_mod

    section("真实路由调用（花配额）")

    listings = [
        {"item_id": 1, "name": "美的微波炉 20L", "price": 180.0, "category": "家居厨房"},
        {"item_id": 2, "name": "宜家四层书架", "price": 120.0, "category": "家具"},
    ]

    def call(msg, last_ctx=None):
        return router_mod.route(
            message=msg, history=[], has_active_items=True, listings=listings,
            hints=intent_rules.hints(msg, listings), last_context=last_ctx or {},
        )

    r = call("转换器我打算12月放假前再卖")
    check("真实调用：延后出售被路由（或被覆盖）到 defer_sale",
          router_mod.apply_defer_sale(
              r.agent, r.scenario, r.slots.model_dump(),
              intent_rules.hints("转换器我打算12月放假前再卖", listings),
          )[0] == "defer_sale", f"router said {r.agent}")

    r = call("都行，你决定")
    agent, scen, _, why = router_mod.apply_clarify_convergence(
        r.agent, r.scenario, r.slots.model_dump(),
        intent_rules.hints("都行，你决定", listings),
        filled_slots={"item_ref": "书架"}, user_authorized=False, clarify_rounds=0,
        last_context={"item_id": 2, "item_name": "宜家四层书架", "scenario": "demand"},
        has_active_items=True, max_clarify_rounds=2,
    )
    check("真实调用：授权类输入最终不落在 clarify",
          agent == "sales_agent", f"router={r.agent} -> {agent}/{scen}/{why}")


def main() -> int:
    test_handoff_cues()
    test_handoff_capture()
    test_handoff_resume_gates()
    test_defer_detection()
    test_date_parsing()
    test_defer_routing()
    test_defer_target_validation()
    test_trigger_rules()
    test_plan_arithmetic()
    test_defer_reply_promises()
    test_confirm_authorize_detection()
    test_clarify_convergence()
    test_forced_default_is_announced()
    test_continuation_block()
    test_graph_wiring()
    test_router_prompt_mentions_new_exits()
    test_email_outbox_offline()

    if "--live-db" in sys.argv:
        test_sale_plan_live()
    else:
        print()
        print("=== 归因二 端到端：跳过（加 --live-db 才跑，会写真实库并自行清理）===")
    if "--live-router" in sys.argv:
        test_router_live()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return len(FAIL)


if __name__ == "__main__":
    sys.exit(main())
