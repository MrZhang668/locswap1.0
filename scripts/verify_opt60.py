"""Acceptance for 优化建议 6.0 — 总类 6.0 记忆管理的两个归因（外加 6.3.1 / 6.3.2 / 6.2.3）。

同 `verify_opt10/20/30/50`：断言的是**确定性的那一半**——候选集判定有没有在服务端
落地、多实体有没有被解析成实体列表、上一轮的结论有没有变成可指代的对象、历史截断
有没有被声明。模型那一半（它会不会照着 `<prior_results>` 取值）要靠 805 跑批。

**这个报告独有的两条前置**，必须先过，否则下游修了什么都测不出来：

1. **夹具**。归因一的 12 条证据里，6.2.1 / 6.2.2 / 6.1.6 的前置轮只谈了两件商品
   中的一件，于是「那个」在上一轮刚被指定过——绑对了恰恰说明歧义没被造出来；
   6.1.1 的卖家本来就拥有那件商品，前置轮直接把分析做完了，「上传好了」无处承接。
   这里先断言这四类的前置确实成立。
2. **Redis 不是解法**。报告的批注建议引入 redis 存本轮对话记忆。这里断言会话记忆
   **已经**是持久化的（SqliteSaver + `turns` + 每轮回灌），失败点不在「读不到历史」，
   所以这条断言存在的意义是防止后人再往这个方向修一遍。

    python scripts/verify_opt60.py

沙箱建在 data/_verify_opt60/ 下，跑之前整个删掉重建，从不碰真实库。

退出码是失败断言的条数。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (path setup)

SANDBOX = Path(_bootstrap.PROJECT_ROOT) / "data" / "_verify_opt60"
if SANDBOX.exists():
    shutil.rmtree(SANDBOX, ignore_errors=True)
SANDBOX.mkdir(parents=True, exist_ok=True)
os.environ["DATA_DIR"] = str(SANDBOX)
os.environ["DB_URL"] = "sqlite:///" + SANDBOX.as_posix() + "/verify.db"
os.environ["CHROMA_DIR"] = str(SANDBOX / "chroma")
os.environ["VECTOR_BACKEND"] = "chroma"
# 这个脚本没有 live 模式：6.0 的断言全都落在真实模型之前的那一层。
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
# 前置一 夹具 — 归因一的 12 条到底跑在什么样的对话里
# ============================================================
def test_fixture_stages_the_ambiguity() -> None:
    section("夹具：双商品歧义和上架接力都真的被造出来了")

    sys.path.insert(0, str(Path(_bootstrap.PROJECT_ROOT) / "scripts" / "badcase_eval"))
    import dataset805 as D

    rows = D.load()
    by_lvl2: dict[str, list[dict]] = {}
    for r in rows:
        by_lvl2.setdefault(r["二级"], []).append(r)

    # ---- 6.2.1 / 6.2.2 / 6.1.6：前置轮必须谈到两件不同的商品 ----------
    for lvl2 in ("多商品并存时的模糊指代", "「两个都要」被折叠成单商品",
                 "多商品回溯指代应澄清而非猜测"):
        group = by_lvl2.get(lvl2) or []
        check(f"{lvl2}：用例还在", bool(group), "0 条")
        if not group:
            continue
        check(f"{lvl2}：前置是两轮", all(len(r["ctx_turns"]) == 2 for r in group),
              str([(r["qid"], len(r["ctx_turns"])) for r in group if len(r["ctx_turns"]) != 2]))
        check(f"{lvl2}：两轮谈的是两件不同的商品",
              all(r["商品"] and r["商品2"] and r["商品"] != r["商品2"] for r in group),
              str([(r["qid"], r["商品"], r["商品2"]) for r in group[:3]]))
        # 「卖家名下同时在售 X 和 Y」——两件必须属于同一个账号，否则前置是假的。
        check(f"{lvl2}：两件同属一个品类（也就是同一个卖家）",
              all(r["品类"] == r["品类2"] for r in group),
              str([(r["qid"], r["品类"], r["品类2"]) for r in group if r["品类"] != r["品类2"]]))
        check(f"{lvl2}：第二轮前置确实点名了第二件商品",
              all(r["商品2"] in r["ctx_turns"][1] for r in group if len(r["ctx_turns"]) == 2),
              str([r["qid"] for r in group if len(r["ctx_turns"]) == 2
                   and r["商品2"] not in r["ctx_turns"][1]]))

    # ---- 6.1.1：卖家必须**不**拥有这件商品，且两轮之间真的上架 ----------
    handoff = by_lvl2.get(D.HANDOFF_LVL2) or []
    check("6.1.1：用例还在", bool(handoff), "0 条")
    check("6.1.1：换成了不拥有这件商品的卖家",
          all(r["seller"] != D.SELLER_BY_CAT[r["品类"]] for r in handoff),
          str([(r["qid"], r["seller"], r["品类"]) for r in handoff[:3]]))
    check("6.1.1：卖家仍有库存（不会短路到 NO_ITEMS_REPLY）",
          all(r["seller"] != D.SELLER_NONE for r in handoff),
          str([r["qid"] for r in handoff if r["seller"] == D.SELLER_NONE]))
    check("6.1.1：要求 runner 在两轮之间上架",
          all(r["stage"] == "list_item_between" for r in handoff),
          str([(r["qid"], r["stage"]) for r in handoff[:3]]))

    # ---- 反向：别的用例不该被这次改动波及 ------------------------------
    single = [r for r in rows if r["ctx_turns"] and len(r["ctx_turns"]) == 1]
    check("单轮前置的用例仍占多数（没有把所有前置都改成两轮）",
          len(single) > len([r for r in rows if len(r["ctx_turns"]) == 2]),
          f"单轮 {len(single)} / 双轮 {len([r for r in rows if len(r['ctx_turns']) == 2])}")
    check("6.2.3 的夹具限制被如实记下来",
          all(r["fixture_note"] for r in by_lvl2.get("已售商品被错误纳入") or [{}]),
          "fixture_note 为空")

    # runner 认得这两个新字段。
    runner = (Path(_bootstrap.PROJECT_ROOT) / "scripts" / "badcase_eval" / "run805.py") \
        .read_text(encoding="utf-8")
    check("runner 驱动多轮前置", 'for line in (row.get("ctx_turns")' in runner)
    check("runner 会在两轮之间真的上架", 'if row.get("stage") == "list_item_between"' in runner)
    check("runner 跑完会把 stage 出来的商品删掉", "unstage_listing(staged)" in runner)


# ============================================================
# 前置二 会话记忆本来就是持久的 —— redis 解决不了这两个归因
# ============================================================
def test_session_memory_is_already_durable() -> None:
    section("前置：会话记忆已经是持久化的（报告批注建议的 redis 不是解法）")

    from app.config import settings
    from app.services.agents import graph, sessions

    src = Path(graph.__file__).read_text(encoding="utf-8")
    check("checkpointer 落盘而不是进程内", "SqliteSaver" in src and "advisor_threads.sqlite" in src)
    check("对话原文存在 state 里跨轮持有", "turns: list[dict]" in src)
    check("每一轮都把最近若干轮回灌给路由器和专家", "history=_recent(state.get" in src)
    check("回灌窗口是一个配置项而不是硬编码",
          "agent_session_history_turns" in src and settings.agent_session_history_turns > 0,
          str(settings.agent_session_history_turns))
    check("重开会话能读回历史", hasattr(sessions, "transcript"))
    # 也就是说：这两个归因的失败点不在「历史读不到」，而在
    # (a) 读到两个候选还硬选一个；(b) 历史里只有自然语言、没有结构化结论。
    check("失败点不是历史缺失，而是候选集判定缺失",
          hasattr(__import__("app.services.agents.router", fromlist=["x"]),
                  "apply_reference_gate"))


# ============================================================
# 归因一 (a) 候选集判定：≠1 不许挑一个
# ============================================================
def test_item_referent_lexicon() -> None:
    section("归因一 a 词法：商品指代 / 结果指代 / 多实体分得开")

    from app.services.agents import intent_plan as ip

    # 商品指代
    for text, want in (
        ("那个再看一下", "any"),
        ("回到前面那个", "backward"),
        ("继续刚才那个", "any"),
        ("接着刚才的说", "any"),
        ("之前那件怎么样了", "backward"),
    ):
        check(f"「{text}」判为商品指代({want})", ip.item_referent(text) == want,
              ip.item_referent(text) or "(空)")

    # 不是商品指代的那些
    for text in ("第2个是什么情况", "那按这个价能找到人吗", "这些买家都什么情况",
                 "（返回对话页）", "把这几个买家的手机号给我", "这个人靠谱吗"):
        check(f"「{text}」不是商品指代", not ip.item_referent(text), ip.item_referent(text))

    # 多实体
    for text in ("两个都要", "这两件一起看看", "我这几件在售的都帮我盘一下"):
        check(f"「{text}」判为多实体", ip.multi_entity_request(text))
    for text in ("都行", "你决定", "都弄好了", "这几个人靠谱吗", "把这几个买家的手机号给我"):
        check(f"「{text}」不是多实体", not ip.multi_entity_request(text))

    # 价格指代与序号
    check("「那按这个价能找到人吗」是价格指代", ip.price_reference("那按这个价能找到人吗"))
    check("「破壁机能卖多少钱」不是价格指代", not ip.price_reference("破壁机能卖多少钱"))
    check("「第2个是什么情况」解析出 2", ip.ordinal_reference("第2个是什么情况") == 2)
    check("「第二个买家」解析出 2", ip.ordinal_reference("第二个买家") == 2)
    check("没有序号时返回 0", ip.ordinal_reference("帮我找买家") == 0)

    # hints 里带出去
    h = ip.hints("那个再看一下", [])
    for key in ("item_referent", "multi_entity", "price_reference", "ordinal_reference"):
        check(f"hints 里有 {key}", key in h)


def test_reference_gate() -> None:
    section("归因一 a 闸门：候选数 0 / 1 / >1 各走各的路")

    from app.services.agents import intent_plan as ip
    from app.services.agents import router as R

    def gate(msg, discussed, listings=None, last_ctx=None, photo=False, agent="sales_agent"):
        listings = listings if listings is not None else []
        return R.apply_reference_gate(
            agent, "none", {}, ip.hints(msg, listings),
            discussed=discussed, listings=listings,
            last_context=last_ctx or {}, has_photo=photo,
        )

    two = [{"item_id": 2, "name": "床垫"}, {"item_id": 1, "name": "电风扇"}]
    one = [{"item_id": 1, "name": "电风扇"}]

    # 候选 > 1 → 强制澄清，且必须把候选列出来
    route, _, _, refs, amb, question = gate("那个再看一下", two)
    check("候选两件 → clarify", route == "clarify", route)
    check("候选两件 → 记下 ambiguous", amb.get("kind") == "ambiguous", str(amb))
    check("候选两件 → 问题里列出了两个候选",
          "床垫" in question and "电风扇" in question, question)
    check("候选两件 → 不产出实体列表（那是多实体的事）", refs == [], str(refs))

    # 「回到前面那个」——最近优先在这里是无效的启发式，必须澄清
    route, _, _, _, amb, _ = gate("回到前面那个", two)
    check("回溯指代 + 两个候选 → clarify", route == "clarify", route)
    check("回溯指代被标记出来", amb.get("backward") is True, str(amb))

    # 候选 = 1 → 直接执行
    route, scen, slots, _, amb, _ = gate("那个再看一下", one)
    check("候选一件 → sales_agent", route == "sales_agent", route)
    check("候选一件 → 绑上它", slots.get("item_ref") == "电风扇", str(slots))
    check("候选一件 → 不记歧义", not amb, str(amb))

    # 候选 = 0 → 说没有上文，而不是反问「你想做什么」
    route, _, _, _, amb, question = gate("接着刚才的说", [], listings=one)
    check("没有上文 → clarify", route == "clarify", route)
    check("没有上文 → kind 是 no_antecedent", amb.get("kind") == "no_antecedent", str(amb))
    check("没有上文 → 明说这个会话没有上文", "没有上文" in question, question)
    check("没有上文 → 不拿全部在售当候选摆出来", "电风扇" not in question, question)

    # 带照片的「这个」指的是照片，不进闸门
    route, _, _, _, amb, _ = gate("这个多少钱能卖", two, photo=True)
    check("带照片时不触发指代闸门", route == "sales_agent" and not amb, f"{route} {amb}")

    # 卖家自己写了名字且唯一 → 不是指代问题
    route, _, slots, _, amb, _ = gate("电风扇那个再看一下", two, listings=two)
    check("写了名字且唯一 → 直接执行", route == "sales_agent" and not amb, f"{route} {amb}")
    check("写了名字且唯一 → 绑对了", slots.get("item_ref") == "电风扇", str(slots))

    # 状态变更走自己的确认闸门，不在这里抢
    hints = ip.hints("那个再看一下", [])
    route, _, _, _, amb, _ = R.apply_reference_gate(
        "sales_agent", "none", {"status_change": "pause"}, hints,
        discussed=two, listings=two, last_context={}, has_photo=False)
    check("状态变更不进指代闸门", route == "sales_agent" and not amb, f"{route} {amb}")


# ============================================================
# 归因一 (b) 多实体：解析成列表，逐件执行
# ============================================================
def test_multi_entity() -> None:
    section("归因一 b：「两个都要」解析成实体列表")

    from app.services.agents import intent_plan as ip
    from app.services.agents import router as R

    two = [{"item_id": 1, "name": "羽绒服"}, {"item_id": 2, "name": "冲锋衣"}]
    route, scen, slots, refs, amb, _ = R.apply_reference_gate(
        "sales_agent", "none", {}, ip.hints("两个都要", []),
        discussed=two, listings=two, last_context={}, has_photo=False)
    check("两个都要 → sales_agent", route == "sales_agent", route)
    check("两个都要 → 场景是 both", scen == "both", scen)
    check("两个都要 → 产出两个实体", refs == ["羽绒服", "冲锋衣"], str(refs))
    check("两个都要 → kind 是 multi", amb.get("kind") == "multi", str(amb))

    # 只有一个候选时不许默默办一件
    route, _, _, refs, amb, question = R.apply_reference_gate(
        "sales_agent", "none", {}, ip.hints("两个都要", []),
        discussed=[two[0]], listings=[two[0]], last_context={}, has_photo=False)
    check("说了都要但只认出一件 → 问", route == "clarify", route)
    check("说了都要但只认出一件 → kind 是 multi_underspecified",
          amb.get("kind") == "multi_underspecified", str(amb))

    # 单商品多任务不是多实体
    one = [{"item_id": 1, "name": "牛仔裤"}]
    route, _, slots, refs, amb, _ = R.apply_reference_gate(
        "sales_agent", "both", {}, ip.hints("牛仔裤的需求和价格都看看", one),
        discussed=[], listings=one, last_context={}, has_photo=False)
    check("单商品多任务不产出实体列表", refs == [], str(refs))
    check("单商品多任务不记歧义", not amb, str(amb))
    check("单商品多任务仍绑上那件商品", slots.get("item_ref") == "牛仔裤", str(slots))


# ============================================================
# 归因一 (c) 必须给默认值时，自报家门
# ============================================================
def test_forced_default_announces_itself() -> None:
    section("归因一 c：被逼出默认方案时必须说「我先按 X，如果是 Y 告诉我」")

    from app.services.agents import graph
    from app.services.seller_advisor import orchestrator as orch

    note = orch._ambiguity_block(
        {"kind": "ambiguous", "candidates": ["电风扇", "床垫"]}, ["电风扇"])
    check("生成了自报家门的要求", bool(note))
    check("点名了选中的那件", "电风扇" in note, note)
    check("点名了没选的那件", "床垫" in note, note)
    check("要求放在回复开头", "第一句" in note, note)
    check("禁止静默选定", "不要静默选定" in note, note)

    multi = orch._ambiguity_block(
        {"kind": "multi", "candidates": ["羽绒服", "冲锋衣"]}, ["羽绒服", "冲锋衣"])
    check("多实体给的是逐件执行要求", "每一件都要单独做完" in multi, multi)
    check("候选唯一时不生成任何要求",
          not orch._ambiguity_block({"kind": "ambiguous", "candidates": ["电风扇"]}, []))

    src = Path(graph.__file__).read_text(encoding="utf-8")
    check("收敛成默认方案时补上了候选里的第一件",
          'if cands and not slots.get("item_ref")' in src)
    check("ambiguity 随状态传给专家", "ambiguity=dict(state.get" in src)


# ============================================================
# 归因二 上一轮的结论变成可指代的对象
# ============================================================
def test_result_stack() -> None:
    section("归因二：结果栈带 id 和类型，「这个价」指得到东西")

    from app.services.agents import graph

    state: dict = {"turns": [{"role": "user", "content": "a"},
                             {"role": "assistant", "content": "b"}]}
    payload = {
        "pricing_analysis": [{
            "item_id": 7, "item_name": "微波炉", "confidence": "medium",
            "stats": {"sample_size": 9, "p25": 40.0, "median": 55.0, "p75": 70.0,
                      "min_price": 30.0, "max_price": 90.0,
                      "time_range": "2026-06-01 ~ 2026-09-01"},
        }],
        "demand_analysis": [{
            "item_id": 7, "item_name": "微波炉", "window_range": "近 30 天",
            "total_people": 12, "people_matching_item": 4, "active_wants": 2,
            "trend": "rising",
        }],
        "potential_buyers": [{
            "item_id": 7, "item_name": "微波炉",
            "cards": [{"card_id": "c1", "demand_summary": "想要一台微波炉"},
                      {"card_id": "c2", "demand_summary": "找二手厨电"}],
        }],
    }
    stack = graph._push_results(state, payload)
    kinds = [e["type"] for e in stack]
    check("三类结论都入栈", set(kinds) == {"price", "demand", "buyers"}, str(kinds))
    check("每条都有 id", all(e.get("id") for e in stack), str(stack[:1]))
    check("id 在一轮内不重复", len({e["id"] for e in stack}) == len(stack))

    price = next(e for e in stack if e["type"] == "price")
    check("价格结论存的是区间而不是标价",
          price["value"]["median"] == 55.0 and price["value"]["p75"] == 70.0,
          str(price["value"]))
    check("价格结论带样本量", price["value"]["sample_size"] == 9)
    buyers = next(e for e in stack if e["type"] == "buyers")
    check("买家结论存的是序号映射",
          [o["n"] for o in buyers["value"]["ordinals"]] == [1, 2],
          str(buyers["value"]))
    check("买家结论不存联系方式",
          all(set(o) <= {"n", "card_id", "demand"} for o in buyers["value"]["ordinals"]))

    # 空结果不入栈：一个「查不到」不是可以被「这个价」指向的结论。
    empty_stack = graph._push_results(
        {"turns": []},
        {"pricing_analysis": [{"item_id": 1, "item_name": "x", "empty": {"kind": "no_sold_evidence"},
                               "stats": {"sample_size": 0}}]})
    check("空结果不入栈", empty_stack == [], str(empty_stack))

    # 栈有上限，最近的在前
    grown = dict(state)
    grown["result_stack"] = stack
    again = graph._push_results(grown, payload)
    check("栈不超过上限", len(again) <= graph.MAX_RESULTS, str(len(again)))
    check("最近的在最前", again[0]["turn"] >= again[-1]["turn"])


def test_result_stack_reaches_the_model() -> None:
    section("归因二：结果栈作为一个块进到提示词里，并写明了取值规则")

    from app.services.seller_advisor import orchestrator as orch

    block = orch._result_stack_block([
        {"id": "r1-1", "type": "price", "item_id": 7, "item_name": "微波炉",
         "value": {"median": 55.0, "p25": 40.0, "p75": 70.0}},
    ])
    check("生成了 prior_results 块", "<prior_results>" in block)
    check("块里有结论数值", "55" in block, block[:200])
    check("明说了「这个价」指区间不是标价", "不是商品的当前标价" in block, block[:400])
    check("要求细节重新调工具", "重新调工具" in block)
    check("空栈不生成块", orch._result_stack_block([]) == "")

    buyers = orch._result_stack_block([
        {"id": "r1-1", "type": "buyers", "item_id": 7, "item_name": "微波炉",
         "value": {"count": 2, "ordinals": [{"n": 1, "card_id": "c1", "demand": "x"}]}},
    ])
    check("买家栈说明了「第 N 个」的指向", "第 N 个" in buyers, buyers[:300])

    src = Path(orch.__file__).read_text(encoding="utf-8")
    check("run_advisor 收下了 result_stack", "result_stack: list[dict] | None = None" in src)
    check("块被拼进了用户消息", "_result_stack_block(list(result_stack or []))" in src)


# ============================================================
# 6.3.2 长会话：历史被截断这件事必须说出来
# ============================================================
def test_history_window_declared() -> None:
    section("6.3.2：历史被截断时，专家必须知道")

    from app.services.agents import graph
    from app.services.seller_advisor import orchestrator as orch

    check("没截断时不生成块", orch._history_window_block((6, 6)) == "")
    check("轮数更少时也不生成", orch._history_window_block((6, 3)) == "")
    block = orch._history_window_block((6, 20))
    check("截断时生成了块", "<history_window>" in block)
    check("说清了总共几轮", "20 轮" in block, block[:200])
    check("说清了看得到几轮", "最近 6 轮" in block, block[:200])
    check("说清了截掉几轮", "14 轮" in block, block[:300])
    check("给了唯一正确的兜底答法", "请他再说一遍" in block, block[:400])
    check("禁止编数字", "不许推测" in block and "不许拿现在的标价" in block, block[:500])

    src = Path(graph.__file__).read_text(encoding="utf-8")
    check("graph 把窗口算出来传下去", "history_window=(len(history) // 2" in src)


# ============================================================
# 6.2.3 已售商品：服务端本来就挡住了
# ============================================================
def test_sold_items_excluded() -> None:
    section("6.2.3：已售商品在服务端就进不了分析范围")

    from app.db import Base, SessionLocal, engine
    from app.models import Item, ItemStatus, User, utcnow
    from app.services.seller_advisor import tools as T
    from app.services.seller_advisor.schemas import AnalyzeDemandInput, ListMyItemsInput

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        user = User(email="opt60_sold@campus.edu", password_hash="x",
                    location_name="Homewood", lat=39.3299, lng=-76.6205)
        db.add(user)
        db.flush()
        live = Item(seller_id=user.id, name="在售台灯", description="", summary="",
                    attributes={"category": "家居与厨房"}, price=30.0,
                    pickup_location_name="Homewood", lat=39.3299, lng=-76.6205,
                    status=ItemStatus.SELLING)
        gone = Item(seller_id=user.id, name="已售电饭煲", description="", summary="",
                    attributes={"category": "家居与厨房"}, price=50.0,
                    sold_price=50.0, sold_at=utcnow(),
                    pickup_location_name="Homewood", lat=39.3299, lng=-76.6205,
                    status=ItemStatus.SOLD_OUT)
        db.add_all([live, gone])
        db.commit()
        uid, live_id, gone_id = int(user.id), int(live.id), int(gone.id)

    with SessionLocal() as db:
        listed = T.list_my_items(db, uid, ListMyItemsInput())
        names = [i.name for i in listed.items]
        check("list_my_items 不返回已售商品", "已售电饭煲" not in names, str(names))
        check("list_my_items 返回了在售商品", "在售台灯" in names, str(names))
        check("total_active 只数在售", listed.total_active == 1, str(listed.total_active))

        # 直接点名一个已售 item_id 也要被拒绝，而不是照常分析。服务端的做法
        # 比「带个免责声明」更硬：抛 ToolError，这一件根本进不了分析。
        try:
            T.analyze_demand(db, uid, AnalyzeDemandInput(item_id=gone_id))
            check("点名已售 item_id 会被拒绝", False, "没有抛错，照常分析了")
        except T.ToolError as exc:
            check("点名已售 item_id 会被拒绝", "已经不在售" in str(exc), str(exc))
        ok = T.analyze_demand(db, uid, AnalyzeDemandInput(item_id=live_id))
        check("在售商品照常分析", ok.item_id == live_id, str(ok.item_id))


# ============================================================
# 端到端：三轮真对话，闸门到底拦不拦得住
# ============================================================
def test_end_to_end_three_turns() -> None:
    """这是整份报告最该验的一条，也是单元断言验不到的一条。

    归因一的失败形状是**跨轮**的：卖家先后谈了两件商品，第三轮说「回到前面
    那个」。候选集是不是两个，取决于前两轮有没有真的把两件商品各写进一次提及栈
    ——而那件事发生在 `sales_node` 的返回值里，被 LangGraph 合并进 checkpoint，
    下一轮的 `route_node` 再读出来。这条链上任何一环断了，单元测试都照样全绿。

    路由器和专家都用假的：这里要验的是**状态在轮之间有没有活下来**，不是模型
    的判断力。
    """
    from types import SimpleNamespace

    from app.db import Base, SessionLocal, engine
    from app.models import Item, ItemStatus, User
    from app.services.agents import graph
    from app.services.agents import router as R
    from app.services.seller_advisor import orchestrator as orch
    from app.services.seller_advisor.schemas import AdvisorChatOut

    section("端到端：谈过两件之后，「回到前面那个」被拦下来")

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        user = User(email="opt60_e2e@campus.edu", password_hash="x",
                    location_name="Homewood", lat=39.3299, lng=-76.6205)
        db.add(user)
        db.flush()
        rows = [Item(seller_id=user.id, name=name, description="", summary="",
                     attributes={"category": "家具与电器"}, price=80.0,
                     pickup_location_name="Homewood", lat=39.3299, lng=-76.6205,
                     status=ItemStatus.SELLING)
                for name in ("电风扇", "床垫")]
        db.add_all(rows)
        db.commit()
        uid = int(user.id)
        ids = {r.name: int(r.id) for r in rows}

    # 假路由器：永远说「销售顾问 / 需求分析」，槽位只在卖家**写出了名字**时填
    # ——真路由器在这两句上也就是这个行为。它对纯指代（「回到前面那个」「两个
    # 都要」）一个字都抽不出来，所以下面每一次正确的绑定和每一次澄清，都只可能
    # 来自服务端的确定性规则。
    def fake_route(**kw):
        matches = (kw.get("hints") or {}).get("name_matches") or []
        slots = R.RouteSlots(item_ref=str(matches[0]["name"]) if len(matches) == 1 else "")
        return SimpleNamespace(
            agent="sales_agent", scenario="demand", slots=slots,
            rewritten_query=kw.get("message", ""), clarify_question="",
            out_of_scope=False, fallback_reason="", slot_warnings=[],
            input_tokens=0, output_tokens=0,
        )

    #: 假专家：按这一轮的 item_ref 回一份需求分析，好让提及栈有东西可记。
    def fake_advisor(db, seller_id, message, history, **kw):
        ref = (kw.get("slots") or {}).get("item_ref") or ""
        hit = next((n for n in ids if n and n in (ref or "")), None)
        analyses = []
        if hit:
            analyses.append({
                "item_id": ids[hit], "item_name": hit, "window": "last_30d",
                "window_range": "近 30 天", "bucket": "day",
                "total_people": 5, "people_matching_item": 2, "active_wants": 1,
                "trend": "flat",
                "listing_traffic": {"impressions": 0, "entered_candidates": 0,
                                    "clicks": 0, "click_through": "n/a"},
            })
        return AdvisorChatOut(ok=True, reply=f"（{hit or '未绑定'} 的需求分析）",
                              demand_analysis=analyses)

    orig_route, orig_llm = R.route, graph.llm_configured
    orig_advisor = sys.modules["app.services.seller_advisor.orchestrator"].run_advisor
    R.route = fake_route
    graph.llm_configured = lambda: (True, "")
    orch.run_advisor = fake_advisor
    graph.reset_graph()
    try:
        thread = f"{uid}-opt60e2e"
        with SessionLocal() as db:
            s1 = graph.run_turn(db, uid, "帮我看看电风扇的需求", thread)
            check("第一轮绑到电风扇", (s1.get("slots") or {}).get("item_ref") == "电风扇",
                  str(s1.get("slots")))
            check("第一轮进了提及栈",
                  [r["name"] for r in s1.get("discussed_items") or []] == ["电风扇"],
                  str(s1.get("discussed_items")))
            check("第一轮进了结果栈",
                  [r["type"] for r in s1.get("result_stack") or []] == ["demand"],
                  str(s1.get("result_stack")))

            s2 = graph.run_turn(db, uid, "床垫也帮我看一下需求", thread)
            check("第二轮绑到床垫", (s2.get("slots") or {}).get("item_ref") == "床垫",
                  str(s2.get("slots")))
            check("提及栈现在有两件、最近的在前",
                  [r["name"] for r in s2.get("discussed_items") or []] == ["床垫", "电风扇"],
                  str(s2.get("discussed_items")))

            # 这就是 6.1.6。原始报告说「连最近优先都没生效」，实际上那个会话里
            # 只有一件商品被谈过。两件都谈过之后，正确答案是**问**。
            s3 = graph.run_turn(db, uid, "回到前面那个", thread)
            check("第三轮走澄清而不是挑一件", s3.get("route") == "clarify",
                  f'{s3.get("route")} / {(s3.get("slots") or {}).get("item_ref")}')
            reply = (s3.get("result") or {}).get("reply") or ""
            check("澄清问题把两个候选都列出来了",
                  "电风扇" in reply and "床垫" in reply, reply)
            check("记下了这是一次回溯指代",
                  (s3.get("ambiguity") or {}).get("backward") is True,
                  str(s3.get("ambiguity")))

            # 反向：只谈过一件的会话里，同一句话必须照常执行。
            solo = f"{uid}-opt60solo"
            graph.run_turn(db, uid, "帮我看看电风扇的需求", solo)
            s4 = graph.run_turn(db, uid, "回到前面那个", solo)
            check("只谈过一件时不问，直接做", s4.get("route") == "sales_agent",
                  str(s4.get("route")))
            check("只谈过一件时绑对了",
                  (s4.get("slots") or {}).get("item_ref") == "电风扇",
                  str(s4.get("slots")))

            # 「两个都要」在谈过两件的会话里要产出两个实体。
            s5 = graph.run_turn(db, uid, "两个都要", thread)
            check("「两个都要」产出两个实体",
                  sorted(s5.get("item_refs") or []) == ["床垫", "电风扇"],
                  str(s5.get("item_refs")))
            check("「两个都要」走销售顾问", s5.get("route") == "sales_agent",
                  str(s5.get("route")))
    finally:
        R.route = orig_route
        graph.llm_configured = orig_llm
        orch.run_advisor = orig_advisor
        graph.reset_graph()


# ============================================================
# 接线 & 提示词
# ============================================================
def test_graph_wiring() -> None:
    section("graph 接线：新状态逐轮重置，闸门排在正确的位置")

    from app.services.agents import graph

    src = Path(graph.__file__).read_text(encoding="utf-8")
    check("AdvisorState 有 discussed_items", "discussed_items: list[dict]" in src)
    check("AdvisorState 有 result_stack", "result_stack: list[dict]" in src)
    check("AdvisorState 有 ambiguity", "ambiguity: dict" in src)
    check("sales_node 维护提及栈", '"discussed_items": _push_discussed(state, payload)' in src)
    check("sales_node 维护结果栈", '"result_stack": _push_results(state, payload)' in src)
    # 状态跨轮持续，漏掉重置就会把上一轮的歧义带进这一轮。
    check("降级分支重置了 ambiguity", src.count('"ambiguity": {},') >= 2,
          str(src.count('"ambiguity": {},')))
    # 锚在 `router_mod.` 前缀上，也就是**调用点**。裸名字在 AdvisorState 的
    # 文档注释里也出现（"see `router.apply_clarify_convergence`"），按裸名字
    # 找顺序，量到的是注释的位置而不是执行的顺序——第一版就是这么绿的。
    order = [src.index(f"router_mod.{fn}") for fn in
             ("apply_followup_context", "apply_reference_gate", "apply_clarify_convergence")]
    check("闸门跑在 followup 之后", order[0] < order[1], str(order))
    check("闸门跑在澄清收敛之前", order[1] < order[2], str(order))
    check("服务端写的澄清问题盖掉模型那一句", "clarify_question = gate_question" in src)
    check("多实体的实体列表优先于单个槽位", "item_refs_override or (" in src)


def test_scorer_assertions() -> None:
    section("跑批断言：澄清 / 多实体 / 无上文都能机器判定")

    sys.path.insert(0, str(Path(_bootstrap.PROJECT_ROOT) / "scripts" / "badcase_eval"))
    import score805 as S

    def scored(lvl2, turn, item="羽绒服", item2="冲锋衣"):
        out: dict = {}
        S.score_reference({"二级": lvl2, "商品": item, "商品2": item2}, turn, out)
        return out

    # 歧义家族：选定一件并给出分析 = 失败；澄清且列出候选 = 通过。
    picked = scored("多商品并存时的模糊指代",
                    {"route": "sales_agent", "reply": "羽绒服的需求是……",
                     "demand_analysis": [{"item_id": 1, "item_name": "羽绒服"}]})
    check("静默选定被判出来", picked["did_not_silently_pick"] is False, str(picked))
    check("没澄清被判出来", picked["clarified"] is False, str(picked))
    good = scored("多商品并存时的模糊指代",
                  {"route": "clarify", "reply": "你说的是「羽绒服」还是「冲锋衣」？"})
    check("澄清且列出候选被判通过",
          good["clarified"] and good["listed_candidates"] and good["did_not_silently_pick"],
          str(good))

    # 多实体：只做一件 = 失败。
    half = scored("「两个都要」被折叠成单商品",
                  {"demand_analysis": [{"item_id": 1, "item_name": "羽绒服"}]})
    check("只做一件被判出来", half["both_items_done"] is False, str(half))
    both = scored("「两个都要」被折叠成单商品",
                  {"demand_analysis": [{"item_id": 1}, {"item_id": 2}],
                   "comparison": {"rows": [{}, {}]}})
    check("两件都做了被判通过", both["both_items_done"] and both["comparison_built"],
          str(both))

    # 新会话：臆造历史 = 失败。
    fake = scored("新会话臆造不存在的历史",
                  {"reply": "接着刚才说的，你那台羽绒服……",
                   "demand_analysis": [{"item_id": 1}]})
    check("臆造历史被判出来",
          not fake["admitted_no_history"] and not fake["no_invented_analysis"], str(fake))
    honest = scored("新会话臆造不存在的历史", {"reply": "这个对话我这边还没有上文。"})
    check("承认没有上文被判通过",
          honest["admitted_no_history"] and honest["no_invented_analysis"], str(honest))

    # 不适用的家族一条都不该被写进去——一个恒为真的断言测不到东西。
    check("不相关的行不带这些断言", scored("找谁聊的意图未被识别", {"reply": "x"}) == {})

    src = Path(S.__file__).read_text(encoding="utf-8")
    check("汇总里打印了新断言", "歧义时确实澄清了" in src and "「都要」两件都做了" in src)

    runner = (Path(_bootstrap.PROJECT_ROOT) / "scripts" / "badcase_eval" / "run805.py") \
        .read_text(encoding="utf-8")
    check("runner 把第二件商品写进记录", '"商品2",' in runner)

    judge = (Path(_bootstrap.PROJECT_ROOT) / "scripts" / "badcase_eval" / "judge805.py") \
        .read_text(encoding="utf-8")
    check("判官看得到本轮分析了几件", '"本轮实际分析的商品"' in judge)
    check("判官看得到回复里出现了哪几个候选", '"回复里出现的本案商品名"' in judge)

    rubric = (Path(_bootstrap.PROJECT_ROOT) / "prompts" / "badcase805_judge.md") \
        .read_text(encoding="utf-8")
    check("判官规则写了歧义怎么判", "存在歧义时应澄清" in rubric)
    check("判官规则写了无上文怎么判", "没有上文时应说明" in rubric)
    check("判官规则写了长会话怎么判", "应准确复述或明确说记不清" in rubric)
    check("判官规则给了自报家门的例外", "我先按 X 分析" in rubric)


def test_prompt_carries_the_rules() -> None:
    section("提示词写进了三条新规则")

    text = (Path(_bootstrap.PROJECT_ROOT) / "prompts" / "seller_advisor.md") \
        .read_text(encoding="utf-8")
    check("说明了 prior_results 块", "`<prior_results>`" in text)
    check("明说「这个价」不是当前标价", "**不是**商品在 `<my_listings>` 里的当前标价" in text)
    check("说明了 history_window 块", "`<history_window>`" in text)
    check("截断时要求承认看不到", "说你看不到那么早的记录" in text)
    check("候选不唯一时问、不挑", "问，不要挑" in text)


def main() -> int:
    test_fixture_stages_the_ambiguity()
    test_session_memory_is_already_durable()
    test_item_referent_lexicon()
    test_reference_gate()
    test_multi_entity()
    test_forced_default_announces_itself()
    test_result_stack()
    test_result_stack_reaches_the_model()
    test_history_window_declared()
    test_sold_items_excluded()
    test_end_to_end_three_turns()
    test_graph_wiring()
    test_scorer_assertions()
    test_prompt_carries_the_rules()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return len(FAIL)


if __name__ == "__main__":
    sys.exit(main())
