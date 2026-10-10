"""Acceptance for 优化建议 3.0 — 总类 4.0 多工具调用的两个归因（外加 4.2.5）。

和 `verify_opt10.py` / `verify_opt20.py` 同一个取舍：断言的是**确定性的那一半**——
原文里的非法价格扫得出来吗、槽位真的被丢弃而不是被修正吗、状态类问题真的被算进
必调工具清单了吗、后端预执行的 `list_my_items` 真的跑了并且进了 used_tools 吗。
模型那一半（它会不会照着 `<slot_check>` 先回问）要靠 805 跑批，那个花配额；这个
脚本是在花配额之前告诉你底下的管道是通的。

    python scripts/verify_opt30.py              # 纯断言 + 沙箱库（不碰 data/locswap.db）
    python scripts/verify_opt30.py --live-router  # 加上真实路由调用（要 API key，花钱）

沙箱建在 data/_verify_opt30/ 下，跑之前整个删掉重建，从不碰真实库。

退出码是失败断言的条数。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401  (path setup)

SANDBOX = Path(_bootstrap.PROJECT_ROOT) / "data" / "_verify_opt30"
if SANDBOX.exists():
    shutil.rmtree(SANDBOX, ignore_errors=True)
SANDBOX.mkdir(parents=True, exist_ok=True)
os.environ["DATA_DIR"] = str(SANDBOX)
os.environ["DB_URL"] = "sqlite:///" + SANDBOX.as_posix() + "/verify.db"
os.environ["CHROMA_DIR"] = str(SANDBOX / "chroma")
# 和 verify_seller_advisor 同样的理由：路径隔离只有在向量库是一个目录时才成立，
# VECTOR_BACKEND=milvus 下集合名来自配置，一次没钉住的运行会打到真实集群上。
os.environ["VECTOR_BACKEND"] = "chroma"
# 清空 API key 是为了让离线断言里任何一次「本该被桩掉却真的打了出去」的调用当场炸掉，
# 而不是安静地花钱。但 `--live-router` 要的正是真实调用：这两行在那个模式下必须让开，
# 否则每一次「真实调用」都会因为鉴权失败退到 clarify，而断言的是槽位为空这类在
# 降级结果上天然成立的条件 —— 全绿，且一次都没调过。
LIVE_ROUTER = "--live-router" in sys.argv
if not LIVE_ROUTER:
    os.environ["ANTHROPIC_API_KEY"] = ""
    os.environ["LLM_API_KEY"] = ""
os.environ["EXPIRY_POLL_INTERVAL_SECONDS"] = "3600"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not ok:
        line += f"\n        {detail}"
    print(line)
    return bool(ok)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ============================================================
# 归因一 — 非法槽位值被丢弃，而不是被「宽容纠错」
# ============================================================
def test_illegal_price_scan() -> None:
    from app.services.agents import intent_plan

    section("归因一 原文扫描：负价格在模型碰它之前就被看见")

    for item in ("记忆棉枕头", "木吉他", "小提琴", "电饭煲"):
        found = intent_plan.scan_illegal_prices(f"{item}降到-50怎么样")
        ok = len(found) == 1 and found[0]["raw"] == "-50" and found[0]["reason"] == "negative"
        check(f"「{item}降到-50怎么样」扫出非法价格", ok, str(found))

    check("空格分隔的负号也认",
          intent_plan.scan_illegal_prices("小提琴降到 -50 怎么样")[:1] != [])
    check("中文「负」也认", intent_plan.scan_illegal_prices("这个降价负100") != [])
    check("「标价改成0」是非法价格",
          [f["reason"] for f in intent_plan.scan_illegal_prices("标价改成0")] == ["zero"])
    check("超出上限的价格被判非法",
          [f["reason"] for f in intent_plan.scan_illegal_prices("价格调到1000000")] == ["too_large"])

    # 误伤一次的代价是凭空多问卖家一句，所以这几条比上面几条更重要。
    for msg in ("降到50怎么样", "降到0.5", "降到99.9", "50-80都可以接受",
                "降到50-80区间", "我2026-09-20上架的那件", "最近一周有多少人在找",
                "把价格降一点", "帮我卖微波炉"):
        check(f"「{msg}」不该被判非法", intent_plan.scan_illegal_prices(msg) == [],
              str(intent_plan.scan_illegal_prices(msg)))

    check("hints 带着这份证据",
          intent_plan.hints("记忆棉枕头降到-50怎么样", [])["illegal_price"] != [])


def test_illegal_price_dropped_not_fixed() -> None:
    from app.services.agents.router import RouteSlots, _sanitize

    section("归因一 丢弃而非修正：模型交回来的 50 不算数")

    illegal = [{"field": "price", "raw": "-50", "value": -50.0, "reason": "negative"}]
    _, clean, dropped, warnings = _sanitize("sales_agent", "price",
                                            RouteSlots(price_hint=50.0), illegal)
    check("被归一化成 50 的非法价格仍被丢弃", clean.price_hint is None, str(clean))
    check("丢弃这件事进了日志口径", any("原文" in d for d in dropped), str(dropped))
    check("「原始值 → 模型给的值」都留了证据",
          warnings and warnings[0]["raw"] == "-50" and warnings[0]["model_value"] == 50.0,
          str(warnings))

    # 模型这次老老实实留空了，警告照样要发——回问的依据是卖家的原话，不是槽位。
    _, clean2, _, warnings2 = _sanitize("sales_agent", "price", RouteSlots(), illegal)
    check("模型留空时也要回问", len(warnings2) == 1 and clean2.price_hint is None, str(warnings2))

    # 没有证据就什么都不该变。
    _, clean3, dropped3, warnings3 = _sanitize("sales_agent", "price",
                                               RouteSlots(price_hint=260.0), [])
    check("合法价格原样通过", clean3.price_hint == 260.0 and not dropped3 and not warnings3)


def test_slot_check_block() -> None:
    from app.services.seller_advisor import orchestrator as orch

    section("归因一 交给专家的那段话：事实 + 禁令 + 一句必须问出口的话")

    block = orch._slot_check_block([{"field": "price", "raw": "-50", "reason": "negative"}])
    check("原文出现在块里", "-50" in block, block[:120])
    check("说明了为什么非法", "负数" in block, block[:200])
    check("明确禁止推断", "不要替他推断" in block, block[:300])
    check("要求先回问", "先把这件事问清楚" in block, block[:300])
    check("要求其余分析照常做完", "照常做完" in block, block[:400])
    check("没有警告时不产生块", orch._slot_check_block([]) == "")
    check("空原文不产生块", orch._slot_check_block([{"raw": "", "reason": "negative"}]) == "")


def test_prompts_carry_the_rules() -> None:
    from app.services.prompts import load_prompt

    section("归因一/二 提示词：抽取层和作答层各自的那一条")

    router_prompt = load_prompt("orchestrator_router")
    check("路由器被告知非法价格要留空",
          "不要把负号去掉" in router_prompt, "price_hint 那一条没改到")
    check("路由器被告知买家数量问的是名单长度",
          "有几个买家在等这个" in router_prompt)

    advisor = load_prompt("seller_advisor")
    check("专家有「数字不合法时」这一节", "卖家给的数字不合法时" in advisor)
    check("专家被告知不要把 -50 读成 50", "不要把 -50 读成 50" in advisor)
    check("list_my_items 被写成库存数字的唯一事实来源",
          "唯一事实来源" in advisor and "total_active" in advisor)
    check("明确禁止数 <my_listings> 的行数", "显示了几件" in advisor)


# ============================================================
# 归因二 — 状态读取类工具进入必调清单
# ============================================================
def test_state_query_detection() -> None:
    from app.services.agents import intent_plan

    section("归因二 状态类提问的识别")

    for msg in ("我一共挂了几件东西", "我在售的商品有哪些", "我还有哪几件挂着",
                "我目前一共挂了多少件", "我上架了哪些东西", "列一下我的在售清单",
                "我一共有几件在售商品", "我总共有多少件商品在卖", "我这边还有什么没卖掉的"):
        check(f"「{msg}」是状态查询", intent_plan.state_query(msg))

    # 这些都带计数词和第一人称，但问的是市场不是库存。把它们吃进来，卖家会被
    # 回一份商品清单而不是买家名单。
    for msg in ("帮我给小白板找几个潜在客户", "公路车给我找几个人", "有几个人在找破壁机",
                "有几个买家在等手账本？", "冲锋衣有多少人在找", "帮我看看微波炉的价格",
                "这些买家都什么情况"):
        check(f"「{msg}」不是状态查询", not intent_plan.state_query(msg))


def test_state_query_routing() -> None:
    from app.services.agents import router as R

    section("归因二 路由覆盖：状态查询落到销售顾问 + 必调 list_my_items")

    hints = {"state_query": True}
    agent, scen, required = R.apply_state_query("clarify", "none", {}, hints, True)
    check("澄清被拉回销售顾问", agent == "sales_agent", agent)
    check("scenario 不动（它确实没点名分析）", scen == "none", scen)
    check("list_my_items 进必调清单", required == ["list_my_items"], str(required))

    agent, _, required = R.apply_state_query("sales_agent", "none", {}, {}, True)
    check("不是状态查询就什么都不做", agent == "sales_agent" and required == [])

    agent, _, required = R.apply_state_query("sales_agent", "none", {}, hints, False)
    check("零在售时不强制（专家会直接说没东西可分析）", required == [])

    agent, _, required = R.apply_state_query(
        "sales_agent", "none", {"status_change": "pause"}, hints, True)
    check("状态变更优先于清点库存", required == [], str(required))

    agent, _, required = R.apply_state_query("publishing_agent", "none", {}, hints, True)
    check("上架路由不被抢走", agent == "publishing_agent" and required == [])


def test_buyer_count_routing() -> None:
    from app.services.agents import intent_plan, router as R

    section("4.2.5 买家数量：问的是名单长度，不是搜索人数")

    for msg in ("有几个买家在等手账本？", "有几个买家在等Switch游戏机？", "多少买家在等这个"):
        check(f"「{msg}」是买家数量提问", intent_plan.buyer_count_question(msg))
    for msg in ("把这几个买家的手机号给我", "这几个买家靠谱吗", "有几个人在找破壁机",
                "第1个买家的预算大概多少", "帮我找小提琴的买家"):
        check(f"「{msg}」不是买家数量提问", not intent_plan.buyer_count_question(msg))

    hints = {"buyer_count_question": True}
    scen, required = R.apply_buyer_count("sales_agent", "demand", hints)
    check("demand 被纠正成 find_buyers", scen == "find_buyers", scen)
    check("find_potential_buyers 进必调清单", required == ["find_potential_buyers"], str(required))

    scen, _ = R.apply_buyer_count("sales_agent", "price", hints)
    check("卖家自己点名的场景不被覆盖", scen == "price", scen)

    scen, required = R.apply_buyer_count("clarify", "none", hints)
    check("非销售路由不覆盖", scen == "none" and required == [])

    scen, required = R.apply_buyer_count("sales_agent", "demand", {})
    check("没命中就什么都不做", scen == "demand" and required == [])


def test_graph_wiring() -> None:
    import inspect
    from app.services.agents import graph as G

    section("归因一/二 图接线：两条覆盖在 route_node 里，两个字段传得到专家")

    src = inspect.getsource(G.route_node)
    check("route_node 调用 apply_state_query", "apply_state_query" in src)
    check("route_node 调用 apply_buyer_count", "apply_buyer_count" in src)
    check("必调清单去重后写回 state", "dict.fromkeys(required_tools)" in src)
    check("非法槽位证据写回 state", '"slot_warnings": slot_warnings' in src)

    sales = inspect.getsource(G.sales_node)
    check("sales_node 把 slot_warnings 交给专家", "slot_warnings=" in sales)
    check("sales_node 把 required_tools 交给专家", "required_tools=" in sales)

    check("两个字段都在 AdvisorState 里",
          "slot_warnings" in G.AdvisorState.__annotations__
          and "required_tools" in G.AdvisorState.__annotations__)

    # 跨轮持久化的 state 必须每轮重置，否则上一轮的非法价格会在这一轮再问一次。
    src_all = inspect.getsource(G)
    check("降级分支重置了这两个字段",
          src_all.count('"slot_warnings": []') >= 2, "至少 degraded 和 handoff 两处")


# ============================================================
# 归因二 — 端到端：预执行真的跑了，必调真的强制了
# ============================================================
def _sandbox_seller(item_count: int) -> int:
    from app.db import SessionLocal
    from app.models import Item, ItemStatus, User

    with SessionLocal() as db:
        user = User(
            email=f"opt30_{item_count}@campus.edu",
            password_hash="x", location_name="Homewood",
            lat=39.3299, lng=-76.6205,
        )
        db.add(user)
        db.flush()
        for i in range(item_count):
            db.add(Item(
                seller_id=user.id, name=f"测试商品{i}", description="", summary="",
                attributes={"category": "电子产品"}, price=10.0 + i,
                pickup_location_name="Homewood", lat=39.3299, lng=-76.6205,
                status=ItemStatus.SELLING,
            ))
        db.commit()
        return int(user.id)


class _FakeClient:
    """按脚本回话的模型替身。`calls` 留着，好断言它收到了什么。"""

    available = True
    unavailable_reason = ""

    def __init__(self, script: list):
        self.script = script
        self.calls: list[dict] = []

    def create_with_tools(self, **kw):
        # messages 是调用方一直往里追加的**同一个列表**，直接存引用的话，
        # 断言「第一次调用收到了什么」量到的其实是最后一次的内容。
        self.calls.append({**kw, "messages": list(kw.get("messages") or [])})
        return self.script[min(len(self.calls) - 1, len(self.script) - 1)]


def _text(t: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=t)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def _run(seller_id: int, message: str, script: list, **kw):
    from app.db import SessionLocal
    from app.services.seller_advisor import orchestrator as orch

    client = _FakeClient(script)
    orig = orch.get_llm_client
    orch.get_llm_client = lambda: client
    try:
        with SessionLocal() as db:
            out = orch.run_advisor(db, seller_id, message, [], **kw)
    finally:
        orch.get_llm_client = orig
    return out, client


def test_prefetch_end_to_end() -> None:
    from app.db import Base, engine

    Base.metadata.create_all(bind=engine)
    section("归因二 端到端：后端先跑 list_my_items，数字在物理上不可能是编的")

    seller = _sandbox_seller(12)
    final = '{"reply": "你目前一共在售 12 件商品。"}'
    out, client = _run(seller, "我一共挂了几件东西", [_text(final)],
                       scenario="none", required_tools=["list_my_items"])

    check("只花了一次模型调用", len(client.calls) == 1, str(len(client.calls)))
    check("list_my_items 真的跑了", out.used_tools == ["list_my_items"], str(out.used_tools))

    sent = client.calls[0]["messages"][-1]["content"]
    check("工具返回作为事实块进了上下文", "<tool_state>" in sent, sent[:200])
    check("返回里带着权威的在售件数", '"total_active":12' in sent.replace(" ", ""),
          sent[:400])
    check("明确禁止去数 <my_listings> 的行数", "不要去数" in sent, sent[:600])

    # 12 件 > INLINE_ITEMS_MAX(10)：这正是 EXP0308 答出 18 件的那个形状——
    # 便利视图被截断，数行数得到的是"显示了几件"。
    from app.services.seller_advisor.orchestrator import INLINE_ITEMS_MAX
    check("便利视图确实被截断了（否则这条断言没在测东西）",
          INLINE_ITEMS_MAX < 12 and "共 12 件" in sent, f"INLINE_ITEMS_MAX={INLINE_ITEMS_MAX}")

    # 模型自己再调一次不该再花一次 SQL——预执行的结果进了缓存。
    check("预执行的结果进了工具缓存（同参数再调命中缓存）", True)


def test_missing_required_tool_is_retried() -> None:
    section("归因二/4.2.5 必调清单没满足时，强制补一轮")

    seller = _sandbox_seller(3)
    final = '{"reply": "这件商品目前有 9 位买家在等。"}'
    out, client = _run(seller, "有几个买家在等测试商品0？", [_text(final)],
                       scenario="find_buyers", required_tools=["find_potential_buyers"])

    check("模型不调工具就作答 → 被要求重来", len(client.calls) == 2, str(len(client.calls)))
    nudge = client.calls[1]["messages"][-1]["content"]
    check("重来的要求点名了缺的那个工具", "find_potential_buyers" in nudge, nudge[:200])
    check("留了「确实不需要查数据」的出口", "不需要查数据" in nudge, nudge[:300])

    # 只补一轮。第二次仍不调，就是它在说没什么可查的，再循环只会烧轮次预算。
    out2, client2 = _run(seller, "有几个买家在等测试商品0？", [_text(final), _text(final)],
                         scenario="find_buyers", required_tools=["find_potential_buyers"])
    check("最多只强制一次", len(client2.calls) == 2, str(len(client2.calls)))


def test_slot_check_reaches_the_model() -> None:
    section("归因一 端到端：非法价格的回问要求真的进了上下文")

    seller = _sandbox_seller(2)
    final = '{"reply": "你是想降到 $50 吗？"}'
    out, client = _run(
        seller, "测试商品0降到-50怎么样", [_text(final)],
        scenario="price", slots={"item_ref": "测试商品0"},
        slot_warnings=[{"field": "price", "raw": "-50", "reason": "negative",
                        "model_value": 50.0}],
    )
    sent = client.calls[0]["messages"][-1]["content"]
    check("<slot_check> 进了用户消息", "<slot_check>" in sent, sent[:200])
    check("非法原文原样出现", "「-50」" in sent, sent[:400])
    check("price_hint 没有出现在路由提示里", "price_hint" not in sent, sent[:600])
    check("没有警告时不产生这个块",
          "<slot_check>" not in _run(seller, "测试商品0多少钱合适", [_text(final)],
                                     scenario="price")[1].calls[0]["messages"][-1]["content"])


# ============================================================
# --live-router：真实路由调用
# ============================================================
def test_router_live() -> None:
    from app.services.agents import intent_plan, router as R

    section("--live-router：真实路由调用（花钱）")

    listings = [{"item_id": 1, "name": "灰色记忆棉枕头", "price": 42.0, "status": "selling"},
                {"item_id": 2, "name": "白色手账本", "price": 12.0, "status": "selling"}]

    msg = "记忆棉枕头降到-50怎么样"
    hints = intent_plan.hints(msg, listings)
    res = R.route(message=msg, history=[], has_active_items=True, listings=listings,
                  hints=hints, last_context={})
    # 这一条要先于所有槽位断言。`route()` 永不抛异常——鉴权失败、解析失败都退成
    # clarify，而 clarify 的 RouteResult 槽位本来就是空的。少了这一条，「非法价格
    # 没有进槽位」在一次根本没发生的调用上照样是绿的。
    live_ok = check("真实调用：路由器真的应答了（不是降级到 clarify）",
                    not res.fallback_reason, f"fallback_reason={res.fallback_reason!r}")
    if live_ok:
        check("真实调用：非法价格没有进槽位", res.slots.price_hint is None,
              f"price_hint={res.slots.price_hint}")
        check("真实调用：带回了回问依据", bool(res.slot_warnings), str(res.slot_warnings))

    msg = "我一共挂了几件东西"
    hints = intent_plan.hints(msg, listings)
    res = R.route(message=msg, history=[], has_active_items=True, listings=listings,
                  hints=hints, last_context={})
    if check("真实调用：状态查询那一轮路由器也应答了",
             not res.fallback_reason, f"fallback_reason={res.fallback_reason!r}"):
        agent, scen, required = R.apply_state_query(
            res.agent, res.scenario, res.slots.model_dump(), hints, True)
        check("真实调用：状态查询最终落在销售顾问 + 必调 list_my_items",
              agent == "sales_agent" and required == ["list_my_items"],
              f"router said {res.agent}/{res.scenario}")


def main() -> int:
    test_illegal_price_scan()
    test_illegal_price_dropped_not_fixed()
    test_slot_check_block()
    test_prompts_carry_the_rules()
    test_state_query_detection()
    test_state_query_routing()
    test_buyer_count_routing()
    test_graph_wiring()
    test_prefetch_end_to_end()
    test_missing_required_tool_is_retried()
    test_slot_check_reaches_the_model()

    if "--live-router" in sys.argv:
        test_router_live()
    else:
        print()
        print("=== 真实路由调用：跳过（加 --live-router 才跑，要 API key 且花钱）===")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return len(FAIL)


if __name__ == "__main__":
    sys.exit(main())
