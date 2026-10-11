"""Acceptance for 调用点 H —— 延后出售识别改成模型判定。

和 `verify_opt20.py` 同一个取舍：这里断言的是**这一层周围那些确定性的东西**——
服务端还愿意采信模型的哪一部分、调用失败时退回哪里、判断盖进 `hints` 之后下游
那几条路由覆盖有没有跟着动。模型判得准不准，只有真调用才知道，所以放在 `--live`
后面，它花配额。

    python scripts/verify_defer_intent.py          # 纯断言，不花一分钱
    python scripts/verify_defer_intent.py --live   # 真调模型，跑一张正反例表

这个脚本存在的理由：这次改动把一个**确定性判据**换成了一次模型调用，而这个判断
决定的是「商品是立刻挂上货架，还是先收起来等到那天」。两个方向都不是答偏一点。
所以底下那几道服务端闸门（引文必须是原话、日期必须落在一年内、失败退回词法）
必须每次都在——它们是这次改动敢做的前提。

退出码是失败断言的条数。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

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
# 1. 服务端只采信模型输出的哪一部分
# ============================================================
def test_sanitize() -> None:
    section("服务端校验：引文要是原话，日期要落在一年内")
    from app.services.agents import defer_intent as D

    msg = "我想在2个月后出售这支笔"
    good = D._DeferVerdict(
        is_defer=True, time_phrase="2个月后", sell_action="出售",
        target_date=(date.today() + timedelta(days=60)).isoformat(),
        reason="有卖出动作「出售」+ 未来时间「2个月后」",
    )
    r = D._sanitize(good, msg)
    check("合格的一份判断原样通过",
          r.is_defer and r.time_phrase == "2个月后" and r.target_date and not r.dropped,
          str(r.model_dump()))
    check("来源标成 model", r.source == "model", r.source)

    # 编出来的引文：会被原样送进 parse_target_date 和工具的 target_phrase。
    made_up = D._DeferVerdict(is_defer=True, time_phrase="等学期末", sell_action="出售")
    r = D._sanitize(made_up, msg)
    check("卖家没说过的时间引文被丢掉", r.time_phrase == "" and r.dropped, str(r.dropped))
    check("引文被丢掉不影响判断本身", r.is_defer is True)

    r = D._sanitize(
        D._DeferVerdict(is_defer=True, time_phrase="2个月后", target_date="2020-01-01"), msg
    )
    check("过去的日期被丢掉", r.target_date == "" and r.dropped, str(r.dropped))

    far = (date.today() + timedelta(days=D.MAX_PLAN_DAYS + 5)).isoformat()
    r = D._sanitize(D._DeferVerdict(is_defer=True, time_phrase="2个月后", target_date=far), msg)
    check("一年以外的日期被丢掉", r.target_date == "", far)

    r = D._sanitize(D._DeferVerdict(is_defer=True, time_phrase="2个月后", target_date="下个月"), msg)
    check("不是日期的字符串被丢掉", r.target_date == "")

    r = D._sanitize(D._DeferVerdict(is_defer=True, time_phrase="2 个月后"), msg)
    check("引文里多一个空格仍算原话", r.time_phrase == "2 个月后", str(r.dropped))


# ============================================================
# 2. 调用失败退回词法，不是退回「不是延后」
# ============================================================
def test_fallback() -> None:
    section("调用失败：退回词法判定")
    from app.services.agents import defer_intent as D

    original = D.build_chat_model
    try:
        def boom(**_kwargs):
            raise RuntimeError("provider down")

        D.build_chat_model = boom
        r = D.classify("等学期末再把这台显示器挂出去")
        check("模型挂了，延后意图仍然认得出来", r.is_defer is True, str(r.model_dump()))
        check("来源标成 lexical，跑批能把这一轮摘出去", r.source == "lexical", r.source)
        check("失败原因记在 dropped 里", any("call_failed" in d for d in r.dropped), str(r.dropped))

        r = D.classify("帮我看看这个显示器现在能卖多少")
        check("失败兜底不会凭空造一个延后意图", r.is_defer is False)
    finally:
        D.build_chat_model = original


def test_switch_off() -> None:
    section("开关关掉：整层退回词法，一次调用都不花")
    from app.config import settings
    from app.services.agents import defer_intent as D

    original = D.build_chat_model
    settings.defer_intent_model_enabled = False
    try:
        def boom(**_kwargs):  # 调到了就是 bug
            raise AssertionError("switch off but the model was called")

        D.build_chat_model = boom
        r = D.classify("等学期末再把这台显示器挂出去")
        check("关掉之后不再调用模型", r.source == "lexical" and r.is_defer is True)
        check("关掉的理由记在 dropped 里", r.dropped == ["disabled"], str(r.dropped))
    finally:
        D.build_chat_model = original
        settings.defer_intent_model_enabled = True


# ============================================================
# 3. 判断盖进 hints 之后，下游那几条覆盖跟着动
# ============================================================
def test_apply_overlay() -> None:
    section("盖进 hints：下游没改，但读到的是模型的判断")
    from app.services.agents import defer_intent as D
    from app.services.nlp import intent_rules
    from app.services.agents import router as router_mod

    msg = "我想在2个月后出售这支笔，成色良好"
    hints = intent_rules.hints(msg, [])
    reading = D.DeferReading(
        is_defer=True, time_phrase="2个月后", sell_action="出售",
        target_date=(date.today() + timedelta(days=60)).isoformat(),
        source="model", reason="卖出动作+未来时间",
    )
    D.apply(hints, reading)
    check("hints 里的 defer_sale_intent 变成模型的判断", hints["defer_sale_intent"] is True)
    check("计划动作跟着变成 create", hints["plan_action_intent"] == "create",
          hints["plan_action_intent"])
    check("日期挂在 hints 上给节点用", bool(hints["defer_target_date"]))
    check("来源和理由都留痕", hints["defer_source"] == "model" and hints["defer_reason"])

    agent, scen, _ = router_mod.apply_defer_sale("publishing_agent", "none", {}, hints)
    check("路由覆盖认这个判断：publishing_agent → defer_sale",
          agent == "defer_sale" and scen == "none", f"{agent}/{scen}")

    # 反方向：词法说是延后，模型说不是——以模型为准，这正是这次改造要的方向。
    hints2 = intent_rules.hints("等学期末再挂出去", [])
    check("前提：词法层本来判为延后", hints2["defer_sale_intent"] is True)
    D.apply(hints2, D.DeferReading(is_defer=False, source="model", reason="问的是行情"))
    check("模型说不是，hints 跟着变假", hints2["defer_sale_intent"] is False)
    check("计划动作也跟着撤回", hints2["plan_action_intent"] == "", hints2["plan_action_intent"])
    agent, _, _ = router_mod.apply_defer_sale("sales_agent", "demand", {}, hints2)
    check("路由不再被拉去 defer_sale", agent == "sales_agent", agent)

    # 管理类动作不归模型判：它们有自己的词表和自己的闸门。
    hints3 = intent_rules.hints("把转换器那条计划撤了", [])
    check("前提：撤计划由词法判", hints3["plan_action_intent"] == "cancel")
    D.apply(hints3, D.DeferReading(is_defer=False, source="model"))
    check("模型说不是延后，撤计划这个动作不受影响",
          hints3["plan_action_intent"] == "cancel", hints3["plan_action_intent"])


# ============================================================
# 4. 接线：跑在路由之前，日期一路传到计划 agent
# ============================================================
def test_wiring() -> None:
    section("接线")
    from pathlib import Path

    from app.config import settings
    from app.services.agents import graph as graph_mod
    from app.services.llm.langchain_client import CALL_SITE_DEFER_INTENT

    src = _bootstrap.module_source(graph_mod)
    i_classify = src.find("defer_intent.classify")
    i_route = src.find("decision = router_mod.route(")
    check("route_node 里调了调用点 H", i_classify > 0)
    check("它跑在路由调用之前", 0 < i_classify < i_route, f"{i_classify} vs {i_route}")
    check("判断被盖回 hints", "defer_intent.apply(hints" in src)
    check("模型日期传给了计划 agent", "fallback_date=str((state.get(\"hints\")" in src)
    check("确定性节点也用得上它", 'hints.get("defer_target_date")' in src)

    from app.services.agents.planning import orchestrator as plan_mod

    psrc = Path(plan_mod.__file__).read_text(encoding="utf-8")
    check("run_plan_agent 收得下这个日期", "fallback_date: str = \"\"" in psrc)
    check("它会作为事实出现在提示词里", "_fallback_date_block(fallback_date)" in psrc)

    check("调用点常量已注册", CALL_SITE_DEFER_INTENT == "H.defer_intent", CALL_SITE_DEFER_INTENT)
    check("配额开关在 settings 里", hasattr(settings, "defer_intent_model_enabled"))
    check("轻模型跑这一层", settings.llm_max_tokens_defer_intent > 0)

    prompt = settings.prompts_path / "defer_intent.md"
    check("system prompt 文件在", prompt.exists(), str(prompt))
    if prompt.exists():
        text = prompt.read_text(encoding="utf-8")
        for must in ("is_defer", "target_date", "time_phrase", "sell_action", "reason"):
            check(f"提示词写明了出参 {must}", must in text)
        check("提示词交代了带照片那种情形", "照片" in text)
        check("提示词交代了拿不准要留空", "留空" in text)


# ============================================================
# 5. 词法兜底：那两个洞补上了
# ============================================================
def test_lexical_fallback_holes() -> None:
    section("词法兜底：截图里那句话现在也认得（兜底路径）")
    from app.services.nlp import intent_rules

    today = date(2026, 9, 23)
    positives = [
        "我想在2个月后出售这支笔，这支笔的成色良好",
        "两个月后卖这支笔",
        "三周后出售这个显示器",
        "等学期末再挂",
        "这台显示器我打算12月放假前再卖",
    ]
    for msg in positives:
        check(f"「{msg[:18]}」认得出是延后", intent_rules.defer_sale_intent(msg), msg)

    negatives = [
        "上个月挂出来的那件怎么样了",
        "帮我卖微波炉",
        "这个显示器现在值多少钱",
        "台灯先下架吧",
    ]
    for msg in negatives:
        check(f"「{msg[:18]}」不是延后", not intent_rules.defer_sale_intent(msg), msg)

    parsed, basis = intent_rules.parse_target_date("我想在2个月后出售这支笔", today)
    check("「2个月后」换算得出日期", parsed == today + timedelta(days=60), f"{parsed}/{basis}")
    parsed, _ = intent_rules.parse_target_date("三周后出售", today)
    check("「三周后」换算得出日期", parsed == today + timedelta(days=21), str(parsed))
    parsed, basis = intent_rules.parse_target_date("等学期末再挂", today)
    check("学期锚点没被新规则抢走", basis == "semester", f"{parsed}/{basis}")


# ============================================================
# 6. 真调用（要 API key，花钱）
# ============================================================
#: (消息, 期望 is_defer, 期望能算出日期)
LIVE_CASES: list[tuple[str, bool, bool]] = [
    ("我想在2个月后出售这支笔，这支笔的成色良好，还能用挺长时间的，笔身是塑料的", True, True),
    ("等学期末再把这台显示器挂出去", True, True),
    ("这个转换器我还得用一阵，用完再说", True, False),
    # 「毕业前」是延后，但**日期该留空**：哪一年毕业只有卖家知道，猜一个出来
    # 会被当成他定的日期写进计划。留空之后计划节点会问他一句，那是对的出口。
    ("毕业前想把这套床品处理掉", True, False),
    ("需求涨起来了你能提醒我一声吗", False, False),
    ("帮我卖微波炉", False, False),
    ("上个月挂出来的那件笔记本怎么样了", False, False),
    ("这个台灯几月份出手价格好一点？", False, False),
    ("台灯先下架吧，不卖了", False, False),
]


def test_live() -> None:
    section("真调用：模型判得对不对（--live）")
    from app.services.agents import defer_intent as D
    from app.services.llm.langchain_client import llm_configured

    ok, why = llm_configured()
    if not ok:
        check("跳过真调用：没有可用的模型", False, why)
        return

    for msg, want_defer, want_date in LIVE_CASES:
        r = D.classify(msg)
        if r.source != "model":
            check(f"真调用：「{msg[:16]}」", False, f"没调到模型：{r.dropped}")
            continue
        got = f"is_defer={r.is_defer} date={r.target_date or '-'} phrase={r.time_phrase or '-'} 因为{r.reason}"
        check(f"「{msg[:16]}」→ is_defer={want_defer}", r.is_defer is want_defer, got)
        if want_defer:
            if want_date:
                check(f"「{msg[:16]}」给得出日期", bool(r.target_date), got)
            else:
                check(f"「{msg[:16]}」日期留空（该问卖家）", not r.target_date, got)
        # 提醒类那一条单独看：它不是延后，但要被认出来。
        if "提醒" in msg or "通知" in msg:
            check(f"「{msg[:16]}」认出是提醒诉求", r.wants_reminder is True, got)


def test_live_route() -> None:
    """端到端：截图里那一轮，现在走到哪条链路上。

    跑的是真的 `route_node`（真路由调用 + 真调用点 H + 真数据库），因为这次改动
    要证明的恰恰是**它们连起来之后**的结果：一句「2 个月后出售」配一张照片，落点
    应该是「先上架、但收起来」，而不是当场挂上货架。这个节点只读不写。
    """
    section("端到端：截图里那一轮（--live）")
    from app.db import SessionLocal
    from app.models import User
    from app.services.agents import graph as graph_mod

    db = SessionLocal()
    try:
        seller = (
            db.query(User).filter(User.email == "tester1@campus.edu").first()
            or db.query(User).first()
        )
        if seller is None:
            check("跳过端到端：库里没有用户", False, "seed 一下再跑")
            return

        state = {
            "message": "我想在2个月后出售这支笔，这支笔的成色良好，还能用挺长时间的，笔身是塑料的",
            "seller_id": seller.id,
            "turns": [],
            "photo_path": "uploads/pen.jpg",
        }
        out = graph_mod.nodes.route_node(state, {"configurable": {"db": db}})
        got = (
            f"route={out.get('route')} hold={out.get('hold_after_publish')} "
            f"defer={(out.get('hints') or {}).get('defer_sale_intent')} "
            f"date={(out.get('hints') or {}).get('defer_target_date')}"
        )
        check("这一轮被认成延后出售", bool((out.get("hints") or {}).get("defer_sale_intent")), got)
        check("链路 2：不在库里的东西先走上架", out.get("route") == "publishing_agent", got)
        check("上架之后要收起来（hold_after_publish）", out.get("hold_after_publish") is True, got)
        check("开卖日期已经算出来了", bool((out.get("hints") or {}).get("defer_target_date")), got)
        check("计划动作是 create",
              (out.get("hints") or {}).get("plan_action_intent") == "create", got)

        # 对照组：同一张照片、同一件东西，没有那句时间——就该是一次普通上架。
        state["message"] = "卖这支笔，成色良好，笔身是塑料的"
        out = graph_mod.nodes.route_node(state, {"configurable": {"db": db}})
        got = f"route={out.get('route')} hold={out.get('hold_after_publish')}"
        check("对照组：没有时间说法就是普通上架",
              out.get("route") == "publishing_agent" and not out.get("hold_after_publish"), got)
    finally:
        db.close()


def main() -> int:
    test_sanitize()
    test_fallback()
    test_switch_off()
    test_apply_overlay()
    test_wiring()
    test_lexical_fallback_holes()

    if "--live" in sys.argv:
        test_live()
        test_live_route()
    else:
        print("\n=== 真调用：跳过（加 --live 才跑，要 API key，花配额）===")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return len(FAIL)


if __name__ == "__main__":
    sys.exit(main())
