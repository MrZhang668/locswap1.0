"""Acceptance for the sale-plan agent (call site G).

和 `verify_opt20.py` 同一条取舍，但侧重点不同：那个脚本验的是**周扫描**（判据、
口径、邮件内容），这个验的是**agent 本身**——工具契约、所有权闸门、日期由谁换算、
承诺文案由谁生成、开关的两条路会不会各说各话。

这里断言的东西有一个共同点：**它们都是模型看不见的保证**。提示词里写了「照抄
commitment」「不要自己算日期」，但提示词按不住的时候，真正拦住错误的是工具层。
所以这个脚本几乎不碰模型，碰的是工具。

    python scripts/verify_plan_agent.py            # 纯断言 + 工具层（会写库，自己清理）
    python scripts/verify_plan_agent.py --live     # 加上真实的 agent 对话（要 API key，花钱）

退出码是失败断言的条数。
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

import _bootstrap  # noqa: F401  (path setup)

from sqlalchemy import select  # noqa: E402

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
# 静态契约：这个 agent 长得像不像一个 agent
# ============================================================
def test_agent_shape() -> None:
    from app.services import plan_agent
    from app.services.plan_agent import orchestrator as orch

    section("形态：和 sales_agent 同一套骨架")

    check("有独立的工具注册表", len(orch.TOOL_REGISTRY) == 6, str(sorted(orch.TOOL_REGISTRY)))
    check("有独立的工具定义（给模型看的那份）",
          len(orch.TOOL_DEFINITIONS) == len(orch.TOOL_REGISTRY))
    check("注册表和定义一一对应",
          {d["name"] for d in orch.TOOL_DEFINITIONS} == set(orch.TOOL_REGISTRY),
          str({d["name"] for d in orch.TOOL_DEFINITIONS} ^ set(orch.TOOL_REGISTRY)))
    check("有独立的调用点 id", orch.CALL_SITE_PLAN_AGENT == "G.plan_agent")
    check("对外只暴露 run_plan_agent", hasattr(plan_agent, "run_plan_agent"))

    # 每个工具的入参都是一个 extra="forbid" 的 Pydantic 模型——模型多填一个字段
    # 会被挡在执行之前，而不是被悄悄忽略。
    for name, (schema_cls, fn) in orch.TOOL_REGISTRY.items():
        check(f"{name} 的入参 schema 关闭了额外字段",
              schema_cls.model_config.get("extra") == "forbid", name)
        check(f"{name} 的实现可调用", callable(fn), name)

    # 写工具必须被标出来：缓存和「空手而归」两条守卫都依赖这个集合。
    check("写工具集合正确",
          orch.WRITE_TOOLS == {
              "create_sale_plan", "update_sale_plan", "cancel_sale_plan", "set_plan_watch",
          }, str(orch.WRITE_TOOLS))

    # 周扫描用的 `week` 是纯 Python 关键字参数，模型不能量任意一周。
    peek_def = next(d for d in orch.TOOL_DEFINITIONS if d["name"] == "peek_plan_demand")
    check("peek_plan_demand 的 schema 不暴露 week",
          set(peek_def["input_schema"]["properties"]) == {"plan_id"},
          str(peek_def["input_schema"]["properties"]))

    src = open(orch.__file__, encoding="utf-8").read()
    check("写工具不进结果缓存",
          "if name not in WRITE_TOOLS and cache_key in tool_cache" in src)
    check("有「明确动作不许空手而归」的守卫", "forced_action_retry" in src)
    check("最终输出过 PlanAgentFinal 校验", "PlanAgentFinal.model_validate" in src)


def test_prompt_contract() -> None:
    from app.services.prompts import load_prompt

    section("提示词：和代码里的保证对得上")

    p = load_prompt("plan_agent")
    for token in ("list_sale_plans", "create_sale_plan", "update_sale_plan",
                  "cancel_sale_plan", "set_plan_watch", "peek_plan_demand"):
        check(f"提示词提到工具 {token}", token in p)
    check("提示词要求照抄 commitment", "commitment" in p and "照它说" in p)
    check("提示词禁止自己算日期", "target_phrase" in p and "不要自己算" in p or "不应该自己算" in p)
    check("提示词区分撤掉和停掉", "撤掉和停掉是两件事" in p)
    check("提示词讲了总开关关着时要提醒", "必须提一句" in p)
    check("提示词禁止自己比阈值", "would_notify" in p)
    check("提示词说明增长只发邮件", "不发站内通知" in p)
    check("提示词规定了最终 JSON 格式", '"reply"' in p)


# ============================================================
# 工具层：模型看不见的那些保证
# ============================================================
def _seller(db):
    from sqlalchemy import select

    from app.models import User

    return db.scalar(select(User).limit(1))


def test_tools_live() -> None:
    from app.db import SessionLocal
    from app.models import SalePlan, SalePlanStatus
    from app.services.plan_agent import tools as T
    from app.services.plan_agent.schemas import (
        CancelSalePlanInput,
        CreateSalePlanInput,
        ListSalePlansInput,
        PeekPlanDemandInput,
        SetPlanWatchInput,
        UpdateSalePlanInput,
    )
    from app.services.seller_advisor.tools import ToolError

    section("工具层：所有权、日期、承诺、开关")

    db = SessionLocal()
    made: list[int] = []
    seller = _seller(db)
    if seller is None:
        check("需要至少一个用户", False, "users 表是空的")
        db.close()
        return
    was_master = bool(seller.sale_watch_enabled)

    try:
        # ---- 创建：日期由后端换算 ----
        r = T.create_sale_plan(db, seller.id, CreateSalePlanInput(
            item_name="验收用转换器", target_phrase="12月放假前", note="verify",
        ))
        made.append(r.plan.plan_id)
        check("原话换算成了具体日期", r.plan.target_date.endswith("-12-15"), r.plan.target_date)
        check("换算依据记了下来", r.plan.date_basis == "explicit_month", r.plan.date_basis)
        check("观察起点是目标日期前 6 周",
              date.fromisoformat(r.plan.watch_from)
              == date.fromisoformat(r.plan.target_date) - timedelta(weeks=6))
        check("回执带服务端生成的承诺清单", len(r.commitment) >= 3, str(r.commitment))
        check("承诺里写明不会自动上架",
              any("不会替你自动上架" in c for c in r.commitment), str(r.commitment))
        check("推断出来的日期会被要求说出来",
              any("12 月 15 日" in c for c in r.caveats), str(r.caveats))

        # ---- 没有类别的计划不许承诺预订那条判据 ----
        if not r.plan.category:
            check("没有类别时不承诺预订判据",
                  not any("预订超过" in c for c in r.commitment), str(r.commitment))
            check("没有类别这件事写进了 caveats",
                  any("没能归进" in c for c in r.caveats), str(r.caveats))

        # ---- 日期拒绝：过去、太远、乱写 ----
        past = (date.today() - timedelta(days=5)).isoformat()
        far = (date.today() + timedelta(days=900)).isoformat()
        for bad, label in ((past, "过去的日期"), (far, "一年多以后"), ("学期末", "不是日期")):
            try:
                T.create_sale_plan(db, seller.id, CreateSalePlanInput(
                    item_name="验收用非法日期", target_date=bad,
                ))
                check(f"{label}被拒绝", False, f"{bad} 竟然通过了")
            except ToolError:
                check(f"{label}被拒绝", True)

        # ---- 一个时间都没给：让模型去问，不许自己定 ----
        try:
            T.create_sale_plan(db, seller.id, CreateSalePlanInput(item_name="验收用没日期"))
            check("没有日期时拒绝创建", False, "竟然创建成功了")
        except ToolError as exc:
            check("没有日期时拒绝创建，并让模型去问",
                  "问他" in str(exc) or "不要自己定" in str(exc), str(exc)[:80])

        # ---- 原话优先于模型自己算的日期 ----
        r2 = T.create_sale_plan(db, seller.id, CreateSalePlanInput(
            item_name="验收用双来源",
            target_phrase="等学期末",
            target_date=(date.today() + timedelta(days=3)).isoformat(),
        ))
        made.append(r2.plan.plan_id)
        check("两个都给时以卖家原话为准",
              r2.plan.date_basis == "semester", f"{r2.plan.date_basis} / {r2.plan.target_date}")

        # ---- 所有权闸门 ----
        try:
            T.resolve_owned_plan(db, seller.id + 999_999, r.plan.plan_id)
            check("别人的 plan_id 解析不出来", False, "竟然解析成功了")
        except ToolError as exc:
            check("别人的 plan_id 解析不出来", True)
            check("「不是你的」和「不存在」说同一句话",
                  "不是你的出售计划" in str(exc), str(exc)[:80])

        # ---- 改日期 ----
        r3 = T.update_sale_plan(db, seller.id, UpdateSalePlanInput(
            plan_id=r.plan.plan_id, target_phrase="明年",
        ))
        check("改日期同时挪了观察起点",
              date.fromisoformat(r3.plan.watch_from)
              == date.fromisoformat(r3.plan.target_date) - timedelta(weeks=6))

        # ---- 单条开关 ----
        r4 = T.set_plan_watch(db, seller.id, SetPlanWatchInput(
            enabled=False, plan_id=r.plan.plan_id,
        ))
        check("停掉单条", r4.action == "watch_changed" and r4.plan.watch_enabled is False)
        check("停掉之后 watching_now 为假", r4.plan.watching_now is False)
        again = T.set_plan_watch(db, seller.id, SetPlanWatchInput(
            enabled=False, plan_id=r.plan.plan_id,
        ))
        check("重复停掉报告成 noop 而不是假装改了", again.action == "noop", again.action)

        # 停掉 ≠ 撤掉：状态仍然是 active。
        check("停掉不改状态",
              db.get(SalePlan, r.plan.plan_id).status == SalePlanStatus.ACTIVE)

        # ---- 总开关 ----
        seller.sale_watch_enabled = True
        db.commit()
        off = T.set_plan_watch(db, seller.id, SetPlanWatchInput(enabled=False))
        check("总开关可以关", off.master_enabled is False and off.action == "watch_changed")
        check("关总开关不删任何计划",
              all(v.status == SalePlanStatus.ACTIVE for v in off.affected), str(off.affected))
        check("关掉时说明这是可逆的",
              any("恢复" in c for c in off.commitment), str(off.commitment))

        # 总开关关着时，新计划的承诺**必须自曝**它现在不会发生。
        r5 = T.create_sale_plan(db, seller.id, CreateSalePlanInput(
            item_name="验收用关闭期计划", target_phrase="下个月",
        ))
        made.append(r5.plan.plan_id)
        check("总开关关着时承诺里有警告",
              any("总开关" in c and "不会发生" in c for c in r5.commitment), str(r5.commitment))

        listed = T.list_sale_plans(db, seller.id, ListSalePlansInput())
        check("列表也报告总开关状态", listed.master_enabled is False)
        check("总开关关着时列表给出告警",
              any("总开关" in c for c in listed.caveats), str(listed.caveats))

        seller.sale_watch_enabled = True
        db.commit()

        # ---- 撤销 ----
        c1 = T.cancel_sale_plan(db, seller.id, CancelSalePlanInput(plan_id=r2.plan.plan_id))
        check("撤销把状态改成 cancelled",
              c1.action == "cancelled"
              and db.get(SalePlan, r2.plan.plan_id).status == SalePlanStatus.CANCELLED)
        c2 = T.cancel_sale_plan(db, seller.id, CancelSalePlanInput(plan_id=r2.plan.plan_id))
        check("重复撤销报告成 noop", c2.action == "noop", c2.action)

        # ---- 行情 ----
        peek = T.peek_plan_demand(db, seller.id, PeekPlanDemandInput(plan_id=r.plan.plan_id))
        check("行情返回两条判据的当前位置", len(peek.would_notify) == 2, str(peek.would_notify))
        check("阈值是后端算好的措辞",
              any("线是" in w for w in peek.would_notify), str(peek.would_notify))
        check("没有历史周读数时会说明原因", bool(peek.caveats), str(peek.caveats))
        check("对话里的 current 标明这周还没走完",
              any("还没走完" in c for c in peek.caveats), str(peek.caveats))
        check("triggers 和 would_notify 出自同一份判定",
              len(peek.triggers) == sum("已经过线" in w for w in peek.would_notify),
              f"{peek.triggers} / {peek.would_notify}")

        # 周扫描的调法：传 week → 量那一整周，不再带「没走完」的提示。
        from app.services import sale_plans as SP
        last_week = SP.week_start_of(date.today()) - timedelta(days=7)
        scan = T.peek_plan_demand(
            db, seller.id, PeekPlanDemandInput(plan_id=r.plan.plan_id), week=last_week
        )
        check("周扫描调用量的是指定的那一周",
              scan.current.week_start == last_week.isoformat(), scan.current.week_start)
        check("周扫描调用不带「没走完」提示",
              not any("还没走完" in c for c in scan.caveats), str(scan.caveats))

        # ---- 同名计划是改而不是新增 ----
        before = T.list_sale_plans(db, seller.id, ListSalePlansInput()).total_active
        dup = T.create_sale_plan(db, seller.id, CreateSalePlanInput(
            item_name="验收用转换器", target_phrase="下个月",
        ))
        after = T.list_sale_plans(db, seller.id, ListSalePlansInput()).total_active
        check("同名计划是改日期而不是新增一条",
              dup.action == "updated" and after == before, f"{dup.action} {before}->{after}")
        check("改而不是增这件事会被说出来",
              any("之前就有" in c for c in dup.caveats), str(dup.caveats))

    finally:
        seller.sale_watch_enabled = was_master
        for pid in made:
            db.query(SalePlan).filter(SalePlan.id == pid).delete()
        db.commit()
        db.close()
        print("        （已清理本次创建的计划）")


# ============================================================
# 路由：计划类消息去 defer_sale，而且不许被读成商品下架
# ============================================================
def test_plan_routing() -> None:
    from app.services.agents import intent_plan, router as router_mod

    section("路由：计划动作 vs 商品上下架")

    cases = [
        ("别提醒我了", "watch_off"),
        ("把监控关掉", "watch_off"),
        ("所有提醒都停了", "watch_off"),
        ("把监控打开", "watch_on"),
        ("恢复提醒", "watch_on"),
        ("我都排了什么计划", "list"),
        ("转换器我打算12月放假前再卖", "create"),
        ("需求涨起来了你能提醒我吗", "create"),
    ]
    for msg, expect in cases:
        got = intent_plan.plan_action_intent(msg)
        check(f"「{msg}」→ {expect}", got == expect, f"got {got!r}")

    # 反例：这几条属于商品上下架，不是计划开关。读错了，卖家只想少收几封邮件，
    # 商品却从搜索里消失了。
    for msg in ("把台灯暂停一下", "先不卖了", "有机化学课本下架吧", "帮我卖微波炉"):
        check(f"「{msg}」不是计划动作", intent_plan.plan_action_intent(msg) == "", msg)

    # 覆盖函数：计划动作必须赢过 status_change。
    msg = "别提醒我了"
    hints = intent_plan.hints(msg, [])
    for came_as in ("sales_agent", "publishing_agent", "clarify"):
        agent, scen, _ = router_mod.apply_defer_sale(came_as, "demand", {}, hints)
        check(f"「{msg}」从 {came_as} 被拉回 defer_sale",
              agent == "defer_sale" and scen == "none", f"{agent}/{scen}")

    # 「转换器那条撤了吧」同时命中 pause 和 cancel——计划动作优先。
    msg2 = "转换器那条撤了吧"
    hints2 = intent_plan.hints(msg2, [])
    agent, _, _ = router_mod.apply_defer_sale("sales_agent", "none", {}, hints2)
    check("「那条撤了吧」走计划而不是商品下架",
          agent == "defer_sale" and hints2["status_change_intent"] == "pause",
          f"{agent} / status={hints2['status_change_intent']}")

    # 但「转换器撤了吧」（没有「那条」）仍然是商品下架。
    msg3 = "转换器撤了吧"
    hints3 = intent_plan.hints(msg3, [])
    agent, _, _ = router_mod.apply_defer_sale("sales_agent", "none", {}, hints3)
    check("没有「那条」时仍然是商品下架",
          agent == "sales_agent" and hints3["plan_action_intent"] == "",
          f"{agent} / plan={hints3['plan_action_intent']!r}")


# ============================================================
# 图与降级
# ============================================================
def test_graph_and_degraded() -> None:
    from app.services.agents import graph as graph_mod

    section("图接线与降级")

    src = open(graph_mod.__file__, encoding="utf-8").read()
    check("defer_sale 节点调的是 plan_agent", "run_plan_agent" in src)
    check("没有 key 时退回确定性实现", "_deterministic_plan_node" in src)
    check("expected_action 从 hints 传进去", "plan_action_intent" in src)
    check("计划这一路不写 last_context",
          "计划这一路不产生买家卡片" in src or "不写 `last_context`" in src)

    from app.services.plan_agent.orchestrator import DEGRADED_REPLY

    check("降级回复不承诺任何提醒",
          "提醒" not in DEGRADED_REPLY.replace("出售计划助手", ""), DEGRADED_REPLY)


def test_switch_single_source() -> None:
    """界面开关和对话开关必须落在同一个字段上。"""
    from app.routers import seller as seller_router
    from app.services.plan_agent import tools as T

    section("开关：两条路一个字段")

    api_src = open(seller_router.__file__, encoding="utf-8").read()
    tool_src = open(T.__file__, encoding="utf-8").read()
    check("REST 端点改 User.sale_watch_enabled",
          "user.sale_watch_enabled = bool(payload.enabled)" in api_src)
    check("工具也改 User.sale_watch_enabled",
          "user.sale_watch_enabled = params.enabled" in tool_src)
    check("REST 端点改 SalePlan.watch_enabled",
          "plan.watch_enabled = bool(payload.watch_enabled)" in api_src)
    check("工具也改 SalePlan.watch_enabled",
          "plan.watch_enabled = params.enabled" in tool_src)
    check("列表视图复用工具层的 _view，不另算一份",
          "from ..services.plan_agent.tools import _view" in api_src)


def test_weekly_pass_respects_switch() -> None:
    from app.db import SessionLocal
    from app.models import SalePlan
    from app.services import sale_plans
    from app.services.plan_agent import tools as T
    from app.services.plan_agent.schemas import CreateSalePlanInput

    section("周扫描：两个开关都认")

    db = SessionLocal()
    seller = _seller(db)
    made: list[int] = []
    was_master = bool(seller.sale_watch_enabled)
    try:
        today = date.today()
        r = T.create_sale_plan(db, seller.id, CreateSalePlanInput(
            item_name="验收用开关计划",
            target_date=(today + timedelta(weeks=3)).isoformat(),
        ))
        made.append(r.plan.plan_id)
        plan = db.get(SalePlan, r.plan.plan_id)

        check("默认进 due 列表",
              any(p.id == plan.id for p in sale_plans.due_plans(db, today)))

        plan.watch_enabled = False
        db.commit()
        check("单条停掉之后不进 due 列表",
              not any(p.id == plan.id for p in sale_plans.due_plans(db, today)))

        plan.watch_enabled = True
        seller.sale_watch_enabled = False
        db.commit()
        check("总开关关掉之后不进 due 列表",
              not any(p.id == plan.id for p in sale_plans.due_plans(db, today)))

        seller.sale_watch_enabled = True
        db.commit()
        check("开回来之后又进 due 列表",
              any(p.id == plan.id for p in sale_plans.due_plans(db, today)))
    finally:
        seller.sale_watch_enabled = was_master
        for pid in made:
            db.query(SalePlan).filter(SalePlan.id == pid).delete()
        db.commit()
        db.close()


def test_migration_backfill() -> None:
    """新加的布尔列必须在已有行上被回填。

    这一条是实测踩出来的：`ALTER TABLE ADD COLUMN` 不带默认值，模型上写的
    `default=True` 只在插入新行时生效，于是 `sale_watch_enabled` 对所有老用户是
    NULL——读出来是假。一个「默认开着」的功能对每一个现存卖家都是关着的，而且
    没有任何地方会报错。
    """
    from sqlalchemy import text

    from app.db import engine

    section("迁移：新列在老行上有值")

    with engine.begin() as conn:
        for table, column in (
            ("users", "sale_watch_enabled"),
            ("sale_plans", "watch_enabled"),
        ):
            nulls = conn.execute(
                text(f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NULL')
            ).scalar()
            check(f"{table}.{column} 没有 NULL 残留", int(nulls or 0) == 0, f"{nulls} 行")

    import app.db as db_mod

    check("迁移助手会回填标量默认值", hasattr(db_mod, "_backfill_default"))


def test_hold_links() -> None:
    """「量大了再卖」的三条链路。

    这一组验的是**状态变更**，不是措辞。三条链路各自都有一个静默失败的版本：
    商品没被真的收起来、恢复上架之后计划还在盯、上传完的东西直接开卖了——
    三种都长得像成功，只有查状态才分得出来。
    """
    from app.db import SessionLocal
    from app.models import Item, ItemStatus, SalePlan, SalePlanStatus
    from app.services.item_index import get_item_index
    from app.services.lifecycle import LifecycleService
    from app.services.plan_agent import tools as T
    from app.services.plan_agent.schemas import CreateSalePlanInput

    section("「量大了再卖」三条链路：隐身 / 上传后隐身 / 取消隐身撤计划")

    db = SessionLocal()
    seller = _seller(db)
    item = db.scalar(
        select(Item).where(Item.seller_id == seller.id,
                           Item.status == ItemStatus.SELLING).limit(1)
    ) if seller else None
    if item is None:
        check("需要一件在售商品", False, "找不到可用的在售商品")
        db.close()
        return

    original_status = item.status
    made: list[int] = []
    try:
        # ---- 链路 1：已上传 → 自动隐身 ----
        r = T.create_sale_plan(db, seller.id, CreateSalePlanInput(
            item_name=item.name, target_phrase="等学期末",
        ))
        made.append(r.plan.plan_id)
        db.refresh(item)
        check("链路1：匹配到在售商品", r.plan.item_id == item.id, str(r.plan.item_id))
        check("链路1：商品被收进隐身", item.status == ItemStatus.PAUSED, item.status)
        check("链路1：隐身 = 不在 SEARCHABLE 里",
              item.status not in ItemStatus.SEARCHABLE, item.status)
        check("链路1：计划记住了这是「收起来等」", r.plan.hold_item is True)
        # 状态变更绝不能静默发生——这句话必须在承诺清单里，而且排在最前面。
        check("链路1：回执第一句就说了已收起",
              bool(r.commitment) and "收起来" in r.commitment[0], str(r.commitment[:1]))
        check("链路1：回执说了怎么恢复",
              any("恢复上架" in c for c in r.commitment), str(r.commitment))

        # ---- 链路 3：取消隐身 → 撤计划 ----
        cancelled = LifecycleService(db, get_item_index()).resume(item, seller)
        db.refresh(item)
        plan = db.get(SalePlan, r.plan.plan_id)
        check("链路3：resume 报告了作废条数", cancelled == 1, str(cancelled))
        check("链路3：商品回到在售", item.status == ItemStatus.SELLING, item.status)
        check("链路3：计划被作废",
              plan.status == SalePlanStatus.CANCELLED, plan.status)
        from datetime import date as _d

        from app.services import sale_plans as _sp
        check("链路3：不再进周扫描队列",
              not any(p.id == plan.id for p in _sp.due_plans(db, _d.today())))

        # 手动暂停的商品**不该**被这条规则误伤：它没有 hold_item 计划。
        LifecycleService(db, get_item_index()).pause(item, seller)
        again = LifecycleService(db, get_item_index()).resume(item, seller)
        check("链路3：普通暂停/恢复不会牵连任何计划", again == 0, str(again))

    finally:
        db.refresh(item)
        if item.status != original_status:
            try:
                LifecycleService(db, get_item_index()).resume(item, seller)
            except Exception:  # noqa: BLE001
                pass
        if made:
            db.query(SalePlan).filter(SalePlan.id.in_(made)).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_hold_link2_wiring() -> None:
    """链路 2 的接线：未上传 → 先上架，且「上传完要隐身」这个记号传得到 approve。"""
    from app.services.agents import handoff, intent_plan
    from app.services.agents import graph as graph_mod
    from app.services.publishing.approve import _hold_after_publish

    section("链路 2 接线：未上传时先去上架，记号能传到 approve")

    src = open(graph_mod.__file__, encoding="utf-8").read()
    check("图里有「库里没有就先上架」这条分叉",
          'route, scenario = "publishing_agent", "none"' in src)
    check("挂起时会写 hold_after_publish", '"hold_after_publish"] = True' in src or
          'carry["hold_after_publish"] = True' in src)
    check("AdvisorState 有 hold_after_publish",
          "hold_after_publish" in graph_mod.AdvisorState.__annotations__)

    # 挂起的任务类型必须是 defer_sale，否则接力回来会去 sales_agent 做分析，
    # 而卖家要的是把计划记下来。
    hints = intent_plan.hints("老式打字机等学期末再卖", [])
    patch = handoff.capture(
        route="publishing_agent", scenario="none", slots={"item_ref": "老式打字机"},
        hints=hints, message="老式打字机等学期末再卖", turn_index=0,
    )
    check("挂起的任务是 defer_sale",
          patch.get("pending_tasks") == [handoff.DEFER_TASK], str(patch.get("pending_tasks")))
    check("清理函数会把 hold_after_publish 一起清掉",
          handoff.cleared().get("hold_after_publish") is False, str(handoff.cleared()))

    # 读不到会话状态时**失败取假**：宁可正常上架让卖家自己去暂停，也不要一件
    # 他以为在卖的东西谁也搜不到。
    from app.db import SessionLocal
    db = SessionLocal()
    try:
        check("读不到会话状态时不隐身（失败取假）",
              _hold_after_publish(db, "no-such-thread-xyz") is False)
    finally:
        db.close()


# ============================================================
# --live：真实对话
# ============================================================
def test_live_agent() -> None:
    from app.db import SessionLocal
    from app.models import SalePlan
    from app.services.plan_agent import run_plan_agent

    section("真实对话（花配额）")

    db = SessionLocal()
    seller = _seller(db)
    made: list[int] = []
    was_master = bool(seller.sale_watch_enabled)
    try:
        out = run_plan_agent(
            db, seller.id, "验收用小冰箱我打算12月放假前再卖", [], required_action="create",
        )
        made += [p["plan_id"] for p in (out.sale_plans or [])]
        check("排计划这一轮真的调了写工具",
              "create_sale_plan" in (out.used_tools or []), str(out.used_tools))
        check("返回了计划卡片", len(out.sale_plans or []) >= 1, str(out.sale_plans))
        check("回复里说出了具体日期",
              "12" in out.reply and ("15" in out.reply or "月" in out.reply), out.reply[:160])
        print(f"        回复：{out.reply[:150]}")

        if out.sale_plans:
            out2 = run_plan_agent(
                db, seller.id, "小冰箱那条先别提醒我了", [], required_action="watch_off",
            )
            check("停掉这一轮调了 set_plan_watch",
                  "set_plan_watch" in (out2.used_tools or []), str(out2.used_tools))
            touched = {p["plan_id"]: p for p in (out2.sale_plans or [])}
            check("那条计划真的被停了",
                  any(not p["watch_enabled"] for p in touched.values()), str(out2.sale_plans))
            print(f"        回复：{out2.reply[:150]}")

        out3 = run_plan_agent(db, seller.id, "我都排了哪些计划", [], required_action="list")
        check("查询这一轮不做任何写操作",
              not ({"create_sale_plan", "cancel_sale_plan", "update_sale_plan", "set_plan_watch"}
                   & set(out3.used_tools or [])), str(out3.used_tools))
        print(f"        回复：{out3.reply[:150]}")
    finally:
        seller.sale_watch_enabled = was_master
        for pid in set(made):
            db.query(SalePlan).filter(SalePlan.id == pid).delete()
        db.commit()
        db.close()
        print("        （已清理本次创建的计划）")


def main() -> int:
    test_agent_shape()
    test_prompt_contract()
    test_plan_routing()
    test_graph_and_degraded()
    test_switch_single_source()
    test_migration_backfill()
    test_tools_live()
    test_weekly_pass_respects_switch()
    test_hold_links()
    test_hold_link2_wiring()
    if "--live" in sys.argv:
        test_live_agent()
    else:
        print()
        print("=== 真实对话：跳过（加 --live 才跑，需要 API key 且会花配额）===")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return len(FAIL)


if __name__ == "__main__":
    sys.exit(main())
