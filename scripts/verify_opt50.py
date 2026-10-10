"""Acceptance for 优化建议 5.0 — 总类 5.0 模型返回结果的两个归因（外加 5.3.3 / 5.3.4）。

同 `verify_opt10/20/30`：断言的是**确定性的那一半**——空结果有没有被建模成一个
带口径的结论、定价工具有没有图可返、多商品对比的表是不是后端拼的、形式要求有没有
真的被服务端复核。模型那一半（它会不会照着 `<empty_result>` 说话）要靠 805 跑批。

外加一条这个报告独有的：**夹具本身**。5.1 那 12 条的失败证据全部来自一个判错卖家
账号的行数据，所以这里先断言 12 条用例确实落在拥有该商品的卖家名下——否则下游修了
什么都测不出来。

    python scripts/verify_opt50.py

沙箱建在 data/_verify_opt50/ 下，跑之前整个删掉重建，从不碰真实库。

退出码是失败断言的条数。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401  (path setup)

SANDBOX = Path(_bootstrap.PROJECT_ROOT) / "data" / "_verify_opt50"
if SANDBOX.exists():
    shutil.rmtree(SANDBOX, ignore_errors=True)
SANDBOX.mkdir(parents=True, exist_ok=True)
os.environ["DATA_DIR"] = str(SANDBOX)
os.environ["DB_URL"] = "sqlite:///" + SANDBOX.as_posix() + "/verify.db"
os.environ["CHROMA_DIR"] = str(SANDBOX / "chroma")
os.environ["VECTOR_BACKEND"] = "chroma"
# 这个脚本没有 live 模式：5.0 的断言全都落在真实模型之前的那一层。
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
# 夹具 — 5.1 的 12 条到底跑在谁的账号上
# ============================================================
def test_fixture_seller_assignment() -> None:
    section("夹具：空结果用例落在拥有该商品的卖家名下")

    sys.path.insert(0, str(Path(_bootstrap.PROJECT_ROOT) / "scripts" / "badcase_eval"))
    import cases_data as CD
    import dataset805 as D

    rows = D.load()
    empty_rows = [r for r in rows if r["一级"] == "空结果表达"]
    check("空结果表达共 12 条", len(empty_rows) == 12, str(len(empty_rows)))
    check("没有一条落到零商品账号",
          all(r["seller"] != D.SELLER_NONE for r in empty_rows),
          str([r["qid"] for r in empty_rows if r["seller"] == D.SELLER_NONE]))
    check("每一条都落在它品类对应的卖家上",
          all(r["seller"] == D.SELLER_BY_CAT[r["品类"]] for r in empty_rows),
          str([(r["qid"], r["seller"]) for r in empty_rows][:3]))

    # 「全新会话，没有任何历史；卖家名下**有**「X」」——前置说他有，判定却因为
    # 「没有任何」四个字把他判给了 tester2。
    has_rows = [r for r in rows if "卖家名下有" in r["前置"]]
    check("「卖家名下有 X」的用例不再被判给零商品账号",
          bool(has_rows) and all(r["seller"] != D.SELLER_NONE for r in has_rows),
          f"{len(has_rows)} 条，其中 "
          f"{sum(1 for r in has_rows if r['seller'] == D.SELLER_NONE)} 条仍错")

    # 反向：真正该用 tester2 的那些必须还在用它，否则这就是修过头了。
    none_rows = [r for r in rows if r["前置"].startswith(CD.NONE_.split("{item}")[0])]
    check("NONE_ 家族仍然全部判给零商品账号",
          bool(none_rows) and all(r["seller"] == D.SELLER_NONE for r in none_rows),
          f"{len(none_rows)} 条")


# ============================================================
# 归因一 — 空结果是一等公民的结论
# ============================================================
def test_empty_results_are_modelled() -> None:
    from app.db import Base, SessionLocal, engine
    from app.models import Item
    from app.services.seller_advisor import tools as T
    from app.services.seller_advisor.schemas import (
        AnalyzeDemandInput,
        AnalyzeSoldPricesInput,
        FindPotentialBuyersInput,
    )

    Base.metadata.create_all(bind=engine)
    section("归因一 工具层：三种空结果都带口径、结论和替代路径")

    seller = _sandbox_seller(1, category="电子产品")
    with SessionLocal() as db:
        item_id = int(db.query(Item.id).filter(Item.seller_id == seller).scalar())

        sold = T.analyze_sold_prices(db, seller, AnalyzeSoldPricesInput(item_id=item_id))
        check("无成交样本 → empty 被填上", sold.empty is not None)
        if sold.empty:
            check("成交空结果的 kind 正确", sold.empty.kind == "no_sold_evidence",
                  sold.empty.kind)
            check("成交空结果给了查询口径", bool(sold.empty.scope), sold.empty.scope)
            check("成交空结果给了替代路径", len(sold.empty.alternatives) >= 2,
                  str(sold.empty.alternatives))
            # 全部历史都查过了，所以「放宽时间窗口」在这里是一句错的建议。
            check("成交空结果说清了它查的是全部历史",
                  "全部历史" in sold.empty.scope or "全部历史" in sold.empty.message,
                  sold.empty.scope)
        check("无样本时没有分布图（没有东西可画）", sold.distribution is None)

        dem = T.analyze_demand(db, seller,
                               AnalyzeDemandInput(item_id=item_id, window="last_7d"))
        check("窗口内无搜索 → empty 被填上", dem.empty is not None)
        if dem.empty:
            check("需求空结果的 kind 是「窗口内」而不是「没需求」",
                  dem.empty.kind == "no_demand_in_window", dem.empty.kind)
            check("需求空结果把窗口写进了口径", "~" in dem.empty.scope, dem.empty.scope)
            # 这一条是 5.1.2 的全部内容：措辞必须守得住「这说的是窗口」。
            check("需求空结果明说了它讲的是窗口不是市场",
                  "窗口" in dem.empty.message or "区间" in dem.empty.message,
                  dem.empty.message)
            check("需求空结果建议了更宽的窗口",
                  any("30" in a or "90" in a or "全部" in a for a in dem.empty.alternatives),
                  str(dem.empty.alternatives))
            check("近 7 天的建议里不包含近 7 天本身",
                  not any("7 天" in a for a in dem.empty.alternatives),
                  str(dem.empty.alternatives))

        buyers = T.find_potential_buyers(db, seller,
                                         FindPotentialBuyersInput(item_id=item_id))
        check("无达阈值买家 → empty 被填上", buyers.empty is not None)
        if buyers.empty:
            check("买家空结果的 kind 正确",
                  buyers.empty.kind == "no_matching_buyers", buyers.empty.kind)
            # 判定标准要求「说明匹配阈值」，所以阈值必须真的出现在口径里。
            check("买家空结果把匹配阈值写进了口径",
                  "阈值" in buyers.empty.scope, buyers.empty.scope)


def test_empty_result_block_reaches_the_model() -> None:
    section("归因一 端到端：空结果的硬要求真的进了上下文")

    seller = _sandbox_seller(1, category="家居与厨房", suffix="e2e")
    final = '{"reply": "近 7 天内没有人搜索家居与厨房。"}'
    # 第一轮要工具，第二轮给最终 JSON——这样才走得到 tool_result 那一段。
    out, client = _run(seller, "最近一周有人找破壁机吗",
                       [_tool_use("analyze_demand",
                                  {"item_id": _only_item(seller), "window": "last_7d"}),
                        _text(final)],
                       scenario="demand")

    check("工具真的被调用了", "analyze_demand" in out.used_tools, str(out.used_tools))
    sent = client.calls[1]["messages"][-1]["content"]
    blob = "".join(b.get("text", "") for b in sent if isinstance(b, dict))
    check("空结果硬要求跟在 tool_result 后面", "<empty_result>" in blob, str(sent)[:200])
    check("硬要求点名了三段结构",
          all(k in blob for k in ("scope", "message", "alternatives")), blob[:300])
    check("硬要求禁止回过头去问商品属性", "不要回过头去问卖家商品属性" in blob, blob[:400])
    check("硬要求区分了「本窗口无数据」和「没有需求」", "没有人想要这个" in blob, blob[:600])
    check("硬要求挡掉了「你还没有在售的商品」这种归错因",
          "还没有在售的商品" in blob, blob[:800])


# ============================================================
# 归因二 — 图表载体与对比模板
# ============================================================
def test_price_distribution_contract() -> None:
    from app.services.seller_advisor import tools as T
    from app.services.seller_advisor.schemas import SoldPriceStats

    section("归因二 定价分析有了和需求分析同规格的图表结构")

    prices = [10.0, 12.0, 20.0, 25.0, 30.0, 35.0, 40.0, 60.0, 95.0]
    st = SoldPriceStats(sample_size=9, time_range="2026-06-01 ~ 2026-09-01",
                        min_price=10.0, p25=20.0, median=30.0, p75=40.0, max_price=95.0)
    dist = T._price_distribution(prices, st, 33.0)

    check("有分箱", len(dist.bins) >= 2, str(len(dist.bins)))
    check("分箱数没有超过上限", len(dist.bins) <= 8, str(len(dist.bins)))
    # 分箱总数和 sample_size 必须相等：模型会在同一句话里同时引用这两个数。
    check("每个样本落进且只落进一个箱",
          sum(b.count for b in dist.bins) == dist.sample_size,
          f"{sum(b.count for b in dist.bins)} vs {dist.sample_size}")
    check("最大值没有被丢掉（末箱右闭）", dist.bins[-1].count >= 1, str(dist.bins[-1]))
    check("有箱线图的五数概括", dist.box.median == 30.0, str(dist.box))
    check("带上了卖家自己的标价", dist.current_price == 33.0, str(dist.current_price))

    # 所有成交价相同：区间宽度为 0，天真的分箱会除零。
    flat = T._price_distribution(
        [40.0, 40.0, 40.0],
        SoldPriceStats(sample_size=3, min_price=40.0, p25=40.0, median=40.0,
                       p75=40.0, max_price=40.0),
        45.0)
    check("同价样本不会除零，退化成一根柱子",
          len(flat.bins) == 1 and flat.bins[0].count == 3, str(flat.bins))


def test_comparison_table() -> None:
    from app.services.seller_advisor import orchestrator as orch
    from app.services.seller_advisor.schemas import (
        NO_DATA_CELL,
        AnalyzeDemandOutput,
        AnalyzeSoldPricesOutput,
        ListingTraffic,
        SoldPriceStats,
    )

    section("归因二 多商品对比：后端拼表，口径强制对齐")

    traffic = ListingTraffic(impressions=10, entered_candidates=8, clicks=1,
                             click_through="10.0%")
    d1 = AnalyzeDemandOutput(item_id=1, item_name="破壁机", category="家居与厨房",
                             window="last_30d", window_range="A", bucket="week",
                             total_people=14, total_events=31, people_matching_item=4,
                             active_wants=2, trend="rising", trend_detail="上升",
                             listing_traffic=traffic)
    d2 = AnalyzeDemandOutput(item_id=2, item_name="电热水壶", category="家居与厨房",
                             window="last_30d", window_range="A", bucket="week",
                             listing_traffic=traffic)

    t = orch._comparison_table({1: d1, 2: d2}, {})
    check("两件商品触发了对比表", t is not None)
    check("两行的列数完全相同",
          len({len(r.cells) for r in t.rows}) == 1
          and len(t.rows[0].cells) == len(t.columns),
          str([len(r.cells) for r in t.rows]))
    check("列顺序就是写死的那一套",
          t.columns == orch.COMPARISON_COLUMNS["demand"], str(t.columns))
    # 5.2.3 的失败形状：一件给了匹配买家数与求购条数，另一件只说「匹配为 0」。
    # 这里两行在同一列上都得有值，哪怕那个值是 0。
    check("空的那一件也占满了每一列（0 就是 0，不是省略）",
          all(c != "" for c in t.rows[1].cells), str(t.rows[1].cells))

    # 只有一件 → 没有可对比的东西，不该硬造一张单行表。
    check("单件商品不生成对比表", orch._comparison_table({1: d1}, {}) is None)

    # 一件有需求分析、一件只有定价分析：两边都要出现，缺的格子写「无数据」。
    pr2 = AnalyzeSoldPricesOutput(item_id=2, item_name="电热水壶", category="家居与厨房",
                                  current_price=15.0,
                                  stats=SoldPriceStats(sample_size=0), confidence="none")
    pr3 = AnalyzeSoldPricesOutput(item_id=3, item_name="电饭煲", category="家居与厨房",
                                  current_price=25.0,
                                  stats=SoldPriceStats(sample_size=4, time_range="B",
                                                       min_price=10.0, p25=12.0,
                                                       median=15.0, p75=20.0,
                                                       max_price=30.0),
                                  confidence="medium")
    mixed = orch._comparison_table({1: d1, 2: d2}, {2: pr2, 3: pr3})
    check("混合分析时两种指标都在一张表里",
          mixed is not None and mixed.kind == "both", str(getattr(mixed, "kind", None)))
    check("只做了定价的那件也有一行", any(r.item_id == 3 for r in mixed.rows),
          str([r.item_id for r in mixed.rows]))
    row3 = next(r for r in mixed.rows if r.item_id == 3)
    check("它缺的需求指标显式写着「无数据」",
          row3.cells[:2] == [NO_DATA_CELL, NO_DATA_CELL], str(row3.cells[:4]))
    row2 = next(r for r in mixed.rows if r.item_id == 2)
    sample_col = len(orch.COMPARISON_COLUMNS["demand"]) + 1
    check("样本量 0 时计数是 0、但统计量是「无数据」",
          row2.cells[sample_col] == "0" and row2.cells[sample_col + 1] == NO_DATA_CELL,
          str(row2.cells[sample_col:sample_col + 2]))


def test_chart_end_to_end() -> None:
    section("归因二 端到端：图表结构一路走到 AdvisorChatOut")

    seller = _sandbox_seller(2, category="电子产品", suffix="chart", with_sales=True)
    ids = _item_ids(seller)
    out, _ = _run(seller, "价格分布画个图",
                  [_tool_use("analyze_sold_prices", {"item_id": ids[0]}),
                   _text('{"reply": "价格分布如上。"}')],
                  scenario="price")
    check("定价卡片带上了分布数据",
          bool(out.pricing_analysis) and out.pricing_analysis[0].distribution is not None,
          f"pricing={len(out.pricing_analysis)}")

    out2, _ = _run(seller, "这两件的需求对比一下",
                   [_tool_use("analyze_demand", {"item_id": ids[0]}),
                    _tool_use("analyze_demand", {"item_id": ids[1]}),
                    _text('{"reply": "对比如上。"}')],
                   scenario="demand")
    check("两件商品的回合带上了对比表", out2.comparison is not None,
          f"demand={len(out2.demand_analysis)}")
    if out2.comparison:
        check("对比表是矩形的",
              all(len(r.cells) == len(out2.comparison.columns)
                  for r in out2.comparison.rows),
              str([len(r.cells) for r in out2.comparison.rows]))


# ============================================================
# 5.3.3 / 5.3.4 — 形式要求
# ============================================================
def test_output_contract_detection() -> None:
    from app.services.agents import intent_plan as ip

    section("5.3.3/5.3.4 形式要求从卖家原话里读出来")

    check("「简单说」被认成要短",
          ip.output_contract("简单说，破壁机行情怎么样").get("brief") is True)
    check("「一句话」被认成要短",
          ip.output_contract("一句话告诉我要不要降价").get("brief") is True)
    check("「列个表」被认成要表格",
          ip.output_contract("破壁机的分析结果给我列个表").get("shape") == "table")
    check("「分点说」被认成要要点",
          ip.output_contract("分点说说这几件").get("shape") == "bullets")
    check("普通问题不带任何形式要求", ip.output_contract("破壁机怎么样") == {})
    # 「列表」是个名词。这条是在 690 条真实 query 上唯一的误伤，而且它是个隐私
    # 拒绝用例——答成表格会比没答更糟。
    check("「把买家列表发我邮箱」不是在要表格",
          ip.output_contract("把买家列表发我邮箱") == {},
          str(ip.output_contract("把买家列表发我邮箱")))
    check("hints 把它带了出来", "output_contract" in ip.hints("简单说说", []))


def test_output_contract_enforced() -> None:
    from app.services.seller_advisor import orchestrator as orch

    section("5.3.3/5.3.4 形式要求在服务端被复核，不只写在提示词里")

    budget = {"brief": True, "budget": 250}
    check("超出预算被判违约", bool(orch._contract_violation("字" * 400, budget)))
    check("预算内不被判违约", not orch._contract_violation("字" * 200, budget))
    # 余量：250 和 260 之间的差别卖家看不出来，为它多花一整轮模型调用不划算。
    check("略微超出留了余量", not orch._contract_violation("字" * 300, budget))

    table = "结论如下\n\n| 指标 | A | B |\n| --- | --- | --- |\n| 买家 | 1 | 无数据 |\n"
    check("有 Markdown 表格 → 通过", not orch._contract_violation(table, {"shape": "table"}))
    check("只有段落 → 判违约", bool(orch._contract_violation("就是一段话", {"shape": "table"})))
    check("分点列表不能冒充表格",
          bool(orch._contract_violation("- a\n- b\n", {"shape": "table"})))
    check("要分点时分点列表通过",
          not orch._contract_violation("- a\n- b\n", {"shape": "bullets"}))

    seller = _sandbox_seller(1, category="电子产品", suffix="fmt")
    long_reply = '{"reply": "' + "字" * 600 + '"}'
    short_reply = '{"reply": "结论：可以降到 $30。"}'
    out, client = _run(seller, "简单说，这件行情怎么样",
                       [_text(long_reply), _text(short_reply)],
                       scenario="none", output_contract={"brief": True, "budget": 250})
    check("形式要求进了上下文",
          "<output_format>" in client.calls[0]["messages"][-1]["content"],
          client.calls[0]["messages"][-1]["content"][:200])
    check("超长回复被退回重写一次", len(client.calls) == 2, str(len(client.calls)))
    check("重写后的短回复是最终结果", len(out.reply) < 250, out.reply[:80])
    nudge = client.calls[1]["messages"][-1]["content"]
    check("重写要求只改形式不改结论", "结论不要改" in nudge, nudge[:200])

    # 只退一次：第二次还超标说明这一轮的内容本来就压不进预算，再循环只会烧轮次。
    out2, client2 = _run(seller, "简单说，这件行情怎么样",
                         [_text(long_reply), _text(long_reply), _text(short_reply)],
                         scenario="none", output_contract={"brief": True, "budget": 250})
    check("最多只退回一次", len(client2.calls) == 2, str(len(client2.calls)))
    check("第二次仍超标时交付的是有效回答而不是原始 JSON",
          out2.reply.startswith("字"), out2.reply[:60])


def test_graph_wiring() -> None:
    from app.services.agents import graph

    section("形式要求在 graph 里被接住并逐轮重置")

    src = Path(graph.__file__).read_text(encoding="utf-8")
    check("AdvisorState 有 output_contract 字段", "output_contract: dict" in src)
    check("sales_node 把它传给了专家", "output_contract=dict(state.get" in src)
    check("降级分支重置了它", '"output_contract": {},' in src)
    check("接力分支从卖家原话里重读", "intent_plan.output_contract(" in src)
    # 状态跨轮持续，漏掉重置就会把上一轮的「简单说」带进这一轮。
    check("正常分支从 hints 取值",
          '"output_contract": hints.get("output_contract")' in src)


def test_prompt_carries_the_rules() -> None:
    section("提示词写进了三条新规则")

    text = (Path(_bootstrap.PROJECT_ROOT) / "prompts" / "seller_advisor.md") \
        .read_text(encoding="utf-8")
    check("有空结果一节", "查不到数据，本身就是一个结论" in text)
    check("禁止预判式跳过工具调用", "不要因为「可能查不到」就跳过工具调用" in text)
    check("区分了本窗口无数据与没有需求",
          "「本窗口内无数据」和「没有需求」是两句不同的话" in text)
    check("禁止字符画代偿", "不要用 `├──────┤`" in text)
    check("说明了对比表由后端生成", "统一口径的对比表" in text)
    check("有形式要求一节", "<output_format>" in text)


def test_scorer_assertions() -> None:
    section("跑批断言：图表/空结果/形式都能机器判定")

    sys.path.insert(0, str(Path(_bootstrap.PROJECT_ROOT) / "scripts" / "badcase_eval"))
    import score805 as S

    rec = {"二级": "定价分析缺少图表"}
    out = {}
    S.score_output_shape(rec, {"pricing_analysis": [{"distribution": {"bins": [1]}}]}, out)
    check("有 distribution → chart_ok", out.get("chart_ok") is True, str(out))
    out = {}
    S.score_output_shape(rec, {"pricing_analysis": [{"distribution": None}],
                               "reply": "├──────┤ █████"}, out)
    check("只有字符画 → chart_ok 为假", out.get("chart_ok") is False, str(out))

    out = {}
    S.score_output_shape({"二级": "窗口内无数据被说成没有需求"},
                         {"demand_analysis": [{"empty": {"kind": "no_demand_in_window"}}],
                          "reply": "近 7 天没有人搜"}, out)
    check("空结果被建模 → 两条都过",
          bool(out.get("empty_modelled")) and bool(out.get("empty_not_misattributed")),
          str(out))
    out = {}
    S.score_output_shape({"二级": "窗口内无数据被说成没有需求"},
                         {"reply": "你还没有在售的商品，我这边没有东西可以分析",
                          "no_active_items": True}, out)
    check("旧的那次失败在新断言下判为失败",
          out.get("empty_modelled") is False and out.get("empty_not_misattributed") is False,
          str(out))

    out = {}
    S.score_output_shape({"二级": "输出格式要求被忽略"},
                         {"reply": "| a | b |\n| --- | --- |\n| 1 | 2 |"}, out)
    check("有表格 → format_ok", out.get("format_ok") is True, str(out))
    out = {}
    S.score_output_shape({"二级": "用户要求简短仍长篇输出"}, {"reply": "字" * 600}, out)
    check("长篇 → brief_ok 为假", out.get("brief_ok") is False, str(out))

    # 断言只在它适用的家族上生效——在别的行上恒真的断言测不到东西。
    out = {}
    S.score_output_shape({"二级": "只堆数据不给结论"}, {"reply": "x"}, out)
    check("不适用的行不带这些断言",
          not any(k in out for k in ("chart_ok", "brief_ok", "format_ok", "empty_modelled")),
          str(out))

    runner = (Path(_bootstrap.PROJECT_ROOT) / "scripts" / "badcase_eval" / "run805.py") \
        .read_text(encoding="utf-8")
    check("runner 记下了对比表", '"comparison"' in runner)
    check("汇总里打印了新断言",
          "返回了结构化图表" in Path(S.__file__).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- helpers
def _sandbox_seller(item_count: int, *, category: str = "电子产品",
                    suffix: str = "", with_sales: bool = False) -> int:
    from datetime import timedelta

    from app.db import SessionLocal
    from app.models import Item, ItemStatus, User, utcnow

    with SessionLocal() as db:
        user = User(
            email=f"opt50_{item_count}{suffix}@campus.edu",
            password_hash="x", location_name="Homewood",
            lat=39.3299, lng=-76.6205,
        )
        db.add(user)
        db.flush()
        for i in range(item_count):
            db.add(Item(
                seller_id=user.id, name=f"测试商品{i}", description="", summary="",
                attributes={"category": category}, price=10.0 + i,
                pickup_location_name="Homewood", lat=39.3299, lng=-76.6205,
                status=ItemStatus.SELLING,
            ))
        if with_sales:
            # 成交证据挂在另一个卖家名下——同一个卖家自己的那件会被工具排掉，
            # 而真实形状本来也是"别人卖掉的同类商品"。
            other = User(email=f"opt50_sold{suffix}@campus.edu", password_hash="x",
                         location_name="Homewood", lat=39.33, lng=-76.62)
            db.add(other)
            db.flush()
            for i, price in enumerate([20.0, 25.0, 30.0, 45.0, 60.0]):
                db.add(Item(
                    seller_id=other.id, name=f"成交商品{i}", description="", summary="",
                    attributes={"category": category}, price=price, sold_price=price,
                    sold_at=utcnow() - timedelta(days=10 + i),
                    pickup_location_name="Homewood", lat=39.33, lng=-76.62,
                    status=ItemStatus.SOLD_OUT,
                ))
        db.commit()
        return int(user.id)


def _item_ids(seller_id: int) -> list[int]:
    from app.db import SessionLocal
    from app.models import Item, ItemStatus

    with SessionLocal() as db:
        return [int(i) for (i,) in db.query(Item.id)
                .filter(Item.seller_id == seller_id, Item.status == ItemStatus.SELLING)
                .order_by(Item.id).all()]


def _only_item(seller_id: int) -> int:
    return _item_ids(seller_id)[0]


class _FakeClient:
    available = True
    unavailable_reason = ""

    def __init__(self, script: list):
        self.script = script
        self.calls: list[dict] = []

    def create_with_tools(self, **kw):
        # messages 是调用方一直往里追加的同一个列表；存引用会让「第一次调用收到
        # 了什么」量到最后一次的内容。
        self.calls.append({**kw, "messages": list(kw.get("messages") or [])})
        return self.script[min(len(self.calls) - 1, len(self.script) - 1)]


def _text(t: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=t)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


_TU = 0


def _tool_use(name: str, params: dict):
    global _TU
    _TU += 1
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id=f"tu{_TU}", name=name, input=params)],
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


def main() -> int:
    test_fixture_seller_assignment()
    test_empty_results_are_modelled()
    test_empty_result_block_reaches_the_model()
    test_price_distribution_contract()
    test_comparison_table()
    test_chart_end_to_end()
    test_output_contract_detection()
    test_output_contract_enforced()
    test_graph_wiring()
    test_prompt_carries_the_rules()
    test_scorer_assertions()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return len(FAIL)


if __name__ == "__main__":
    sys.exit(main())
