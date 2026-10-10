"""Turn the generated case set into rows the runner can actually drive.

`cases_data.py` is the specification — what is being probed and what the right
answer looks like in prose. This module is the part that has to be honest about
what a machine can do with it:

* which seller account makes the case's precondition true,
* what earlier utterance has to be said for a "and then the seller replies…"
  case to mean anything,
* which gold labels are checkable by assertion and which need a judge,
* and which cases cannot be driven at all from here, with the reason recorded
  rather than run and silently scored as a failure.

That last list is the important one. A case skipped for a missing fixture and a
case that failed are different facts, and collapsing them inflates the defect
rate with work nobody did.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import cases_data as CD

# ---------------------------------------------------------------- sellers
#: One seeded seller per category (fixtures v2), so no seller holds more than
#: 18 listings and every product sits inside `list_my_items`' default 20-row
#: window. v1 put all 101 products on one account, which put 51 of them out of
#: reach and made 311 rows measure the window instead of the product.
#: tester2 still owns nothing — that is the fixture for the cases whose
#: precondition is that the seller has not listed the thing yet.
SELLER_BY_CAT = {
    "电子产品": "bc-elec@campus.edu", "家具与电器": "bc-furn@campus.edu",
    "家居与厨房": "bc-home@campus.edu", "运动与户外": "bc-sport@campus.edu",
    "图书与教材": "bc-book@campus.edu", "服装与鞋靴": "bc-cloth@campus.edu",
    "学习用品": "bc-study@campus.edu", "乐器": "bc-music@campus.edu",
    "自行车与滑板车": "bc-bike@campus.edu",
}
SELLER_NONE = "tester2@campus.edu"

#: The literal opening of the NONE_ precondition template — "卖家账号下没有任何「".
#: Derived from cases_data rather than retyped so the two cannot drift apart.
_NONE_PREFIX = CD.NONE_.split("{item}")[0]

# ---------------------------------------------------------------- not runnable
#: (predicate, reason). Checked in order; the first match wins.
UNRUNNABLE: list[tuple[str, str]] = [
    ("模型输出内容丢失", "前端页面切换/刷新后的状态恢复，进程内驱动观测不到"),
    # Held back by decision, not by capability. The photo fixtures are built
    # and wired (make_photo_fixtures.py, 28 products with genuine photographs)
    # — flip this entry out to run them.
    ("图像识别", "本轮不测图像识别（夹具已就绪，改日再跑）"),
]

#: first-level class -> which photo variant the case is actually about.
PHOTO_KIND = {
    "噪声图像识别": "dark",          # 暗光/模糊/反光，轮换见 _photo_for
    "噪声图像识别正确": "dark",
    "多物体同框": "multi",
    "品类误判": "plain",             # 正常照片，考的是品类判对没有
    "图文冲突": "plain",             # 图是 A，文字说 B，冲突在两个输入之间
}
_PHOTOS: dict[str, dict[str, str]] = {}


def _photo_for(lvl1: str, lvl2: str, product: str) -> str:
    """The image this case should actually carry, or "" for a text-only case.

    Only the image families get one. Attaching a photo to a text case would
    change the answer rather than decorate it: the router is told `has_photo`
    in its facts block precisely so that "selling this, still works fine" stops
    being ambiguous — so a stray picture on 「台灯找谁聊？」 would push it
    toward publishing and the row would measure the fixture again.

    Within the noise family the variant follows the second-level label (暗光 /
    模糊 / 杂乱背景 / 反光), not the family: sending a dark photo to a row that
    says 模糊 tests the wrong condition and reads as a recognition failure.
    """
    if lvl1 not in PHOTO_KIND:
        return ""
    global _PHOTOS
    if not _PHOTOS:
        f = Path(__file__).resolve().parent / "photo_fixtures.json"
        _PHOTOS = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    entry = _PHOTOS.get(product)
    if not entry:
        return ""
    kind = PHOTO_KIND[lvl1]
    if kind == "dark" and ("模糊" in lvl2 or "反光" in lvl2):
        kind = "blur"
    return entry.get(kind) or entry.get("plain", "")
#: 「缺少多模态分析」was skipped as a front-end concern. It is not: the chart is
#: drawn from the `series` list the demand tool returns (schemas.py: "the
#: frontend draws the chart from `series`"), so "did the seller get a chart"
#: is answerable from the backend payload, and these 16 rows are back in.
#: Individual second-level classes whose setup this harness cannot build.
UNRUNNABLE_LVL2: dict[str, str] = {
    "过期候选名单仍被执行": "需要在两轮之间清空进程内候选缓存，本 harness 不做状态注入",
    "超配额静默截断未告知": "需要先把当日推送配额打满，会污染真实推送记录",
    "已下架商品仍被推送": "需要在提案与确认之间改商品状态，本 harness 不做状态注入",
    "越权商品的推送提案未被拦截": "需要伪造指向他人商品的提案，模型无法自然产出",
    "已售商品被当作在售": "评测卖家名下没有已售商品，需要额外造数",
}

# ---------------------------------------------------------------- ctx turns
#: A case whose precondition is "the seller already said X" needs X actually
#: said, in the same thread, before the turn under test. Keyed by first-level
#: class; {item} is the case's product.
CTX_BY_LVL1: dict[str, str] = {
    "跨agent信息传输缺失": "我想卖{item}，帮我分析一下需求和价格，再帮我找买家",
    "会话内记忆丢失": "我想分析一下{item}的需求和价格，再帮我找买家",
    "指代歧义": "帮我看看{item}的需求",
    "跨会话/长会话记忆": "帮我看看{item}的需求",
    "模糊回复被当成同意": "帮我找{item}的买家",
    "明确同意未被识别": "帮我找{item}的买家",
    "推送执行偏差": "帮我找{item}的买家",
    "确认后正确执行": "帮我找{item}的买家",
    "确认闸门缺失": "帮我找{item}的买家",
    "买家个人信息保护": "帮我找{item}的买家",
}
#: Overrides where the first-level default does not fit the specific probe.
#: A value may be a list: some preconditions take more than one earlier turn.
CTX_BY_LVL2: dict[str, str | list[str]] = {
    "上一轮的价格结论未被指代解析": "{item}的成交价大概多少",
    "候选卡片序号指代失效": "帮我找{item}的买家",
    "重复澄清已确认过的槽位": "帮我分析一下{item}",
    "新会话臆造不存在的历史": "",      # 全新会话，故意不给上文
    "跨会话串味": "",                  # 同上
    # ---- 双商品前置：歧义必须真的被造出来 ----------------------------------
    #
    # 这四类的前置写的都是「卖家名下同时在售 X 和 Y」或者「会话中先后处理了 X
    # 和 Y」，金标是 clarify（或者两件都办）。但一级默认模板只说了 X 一件，于是
    # 跑出来的对话是：
    #
    #     卖家：帮我看看电风扇的需求
    #     系统：（电风扇的需求分析）
    #     卖家：那个再看一下
    #
    # 「那个」上一轮刚被指定过，绑到电风扇是**对的**——歧义根本不存在。同理
    # 「回到前面那个」在一个只谈过一件东西的对话里没有「前面那个」可回。原始
    # 报告据此断言「连最近优先这个最朴素的启发式都没有生效」，而实际情况是
    # 系统只见过一件商品。
    #
    # 这是夹具在测自己。两轮前置把 X 和 Y 都谈一遍之后，「那个」才真的对得上
    # 两件、「回到前面那个」才真的有一个「前面」，「两个都要」里的「两个」也
    # 才指得出是哪两个（卖家名下有 18 件在售，不谈过就无从指认）。
    "多商品并存时的模糊指代": [
        "帮我看看{item}的需求", "{item2}也帮我看一下需求",
    ],
    "「两个都要」被折叠成单商品": [
        "帮我看看{item}的需求", "{item2}也帮我看一下需求",
    ],
    "多商品回溯指代应澄清而非猜测": [
        "我想分析一下{item}的需求和价格", "现在帮我看看{item2}的需求和价格",
    ],
    "已售商品被错误纳入": "",          # 见 SOLD_ITEM_NOTE
}

#: 6.1.1 的前置（「系统要求先上传」）只有当这件商品**不在卖家名下**时才成立。
#: 默认的按品类分配正好相反——卖家一定拥有本品类的每一件商品，于是前置轮里的
#: 「我想分析一下 X 的需求和价格」被直接做完了，没有任何东西被挂起，下一轮的
#: 「上传好了」自然无处承接。原始报告把这读成「模型丢失了上一轮的商品与任务」，
#: 但真实情况是**接力机制一次都没有被触发**。
#:
#: 换一个同样有库存、但不拥有这件商品的卖家，前置就成立了：有库存 → 不会短路到
#: NO_ITEMS_REPLY；不拥有这件 → 走上架，任务被挂起。配合 run805 的 `stage`，
#: 两轮之间真的把商品建出来，`handoff.resume_target` 才找得到它。
HANDOFF_LVL2 = "模型丢失上一轮的商品与任务指代"

#: 轮换一个**别的品类**的卖家。用固定顺序而不是随机，是为了让同一行每次跑
#: 都落在同一个账号上——否则重跑的差异里会混进夹具的抖动。
_OTHER_CATS = list(SELLER_BY_CAT)


def _other_seller(cat: str, n: int) -> str:
    pool = [c for c in _OTHER_CATS if c != cat]
    return SELLER_BY_CAT[pool[n % len(pool)]]


#: 「已售商品被错误纳入」造不出前置：评测卖家名下一件已售商品都没有。
#: 工具层其实已经按 `ItemStatus.SEARCHABLE` 过滤了（tools.py 的 list_my_items
#: 和 item_id 解析各一道），所以这一类真正能测到的只有措辞。如实记下来，
#: 而不是让它继续以一个没有前置的形式跑出一个看起来像失败的结果。
SOLD_ITEM_NOTE = "评测卖家名下没有已售商品；服务端已按 SEARCHABLE 过滤，此处只测措辞"

# ---------------------------------------------------------------- gold labels
# `status_change` and `sell_through` arrived with the 优化建议 1.0 fixes: a
# delisting request now has a node of its own (a confirmation gate) instead of
# being answered with questions, and a broad "just sell this" is expanded into
# a three-step plan instead of falling through to clarify. Both have to be in
# these tuples or the gold parser silently drops them and every such case is
# scored against an empty expectation.
ROUTES = ("sales_agent", "publishing_agent", "clarify", "status_change", "defer_sale")

#: 金标里用中文写的路由目标 → 实际路由名。
#:
#: 「（待补）延后出售提醒 agent」这一栏在写这张表的时候是**故意留空的**——那个
#: 能力当时不存在，所以 `gold_routes` 看到「待补」就返回空元组，这些用例的路由
#: 项从来没有被判过。优化建议 2.0 把这个能力做出来了（`defer_sale` 节点 +
#: `sale_plans` 周扫描），于是这一栏从「没有期望」变成了「有明确期望」。
#:
#: 不改这里的后果，是这几条永远显示为通过——因为它们根本没有被断言。
ROUTE_ALIASES: dict[str, str] = {
    "延后出售": "defer_sale",
    "择期出售": "defer_sale",
    "需求监控提醒": "defer_sale",
    "定时提醒": "defer_sale",
}
SCENARIOS = ("price", "demand", "both", "find_buyers", "sell_through", "none")
TOOLS = ("list_my_items", "find_potential_buyers", "analyze_sold_prices",
         "analyze_demand", "push_item_to_want")


def gold_routes(text: str) -> tuple[str, ...]:
    """Which observed routes count as correct.

    `publishing_agent→sales_agent` accepts either end: the case is about the
    handoff, and the first hop is legitimately publishing. `… 或拒绝` accepts a
    clarify, which is how a refusal surfaces. A `—` target means there is no
    route to assert — those are judged, not asserted.

    `（待补）` used to mean the same thing. It no longer does when the thing it
    was waiting for got built: a 「（待补）延后出售提醒 agent」 target now maps to
    the real `defer_sale` route through `ROUTE_ALIASES`, and only a 待补 target
    with no alias is still unassertable.
    """
    t = (text or "").strip()
    if not t or t.startswith("—"):
        return ()
    for zh, route in ROUTE_ALIASES.items():
        if zh in t:
            return (route,)
    if "待补" in t:
        return ()
    found = tuple(r for r in ROUTES if r in t)
    if "或拒绝" in t and "clarify" not in found:
        found = found + ("clarify",)
    return found


def gold_scenarios(text: str) -> tuple[str, ...]:
    t = (text or "").strip()
    if not t or t.startswith("—"):
        return ()
    # "both + find_buyers" and "price + find_buyers" name a combination the
    # state machine cannot express in one value; either half is accepted.
    found = tuple(s for s in SCENARIOS if re.search(r"\b%s\b" % s, t))
    return found


def gold_tools(text: str) -> tuple[tuple[str, ...], bool]:
    """(tools that must appear, must_be_empty)."""
    t = (text or "").strip()
    if t.startswith("无"):
        return (), True
    return tuple(x for x in TOOLS if x in t), False


# ---------------------------------------------------------------- build
def load() -> list[dict]:
    seen, rows = set(), []
    for r in CD.ROWS:
        key = (r["二级"], r["前置"], r["query"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)

    out: list[dict] = []
    for n, r in enumerate(rows, 1):
        qid = "EXP%04d" % n
        pre, lvl1, lvl2 = r["前置"], r["一级"], r["二级"]

        skip = ""
        for needle, reason in UNRUNNABLE:
            if needle in (r["总类"], lvl1, lvl2):
                skip = reason
                break
        if not skip and lvl2 in UNRUNNABLE_LVL2:
            skip = UNRUNNABLE_LVL2[lvl2]
        photo = _photo_for(lvl1, lvl2, r["商品"])
        if not skip and r["总类"] == "图像识别" and not photo:
            skip = f"「{r['商品']}」在语料和训练集里都没有实拍照，只有生成的占位图"

        raw_ctx = CTX_BY_LVL2.get(lvl2, CTX_BY_LVL1.get(lvl1, ""))
        raw_list = raw_ctx if isinstance(raw_ctx, list) else ([raw_ctx] if raw_ctx else [])
        ctx_turns = [
            t.replace("{item2}", r.get("商品2") or "").replace("{item}", r["商品"])
            for t in raw_list
        ]
        # 判官和看板只读一个字符串。把两轮拼起来而不是只留最后一轮：判定
        # 「该不该澄清」的依据恰恰是**前面谈过几件东西**，只给最后一轮等于
        # 把判官也放进同一个单商品会话里。
        ctx = " ／ ".join(ctx_turns)

        # 交接类用例换一个不拥有这件商品的卖家，并要求 runner 在两轮之间
        # 真的把商品建出来。
        stage = ""
        seller_override = ""
        # 跑得动、但前置造不全的用例。记下来而不是跳过：它测得到措辞，测不到
        # 「已售商品被纳入」，两者的区别必须留在数据里，否则下一个读结果的人
        # 会把一个没有前置的通过当成一次真正的通过。
        fixture_note = SOLD_ITEM_NOTE if lvl2 == "已售商品被错误纳入" else ""
        if lvl2 == HANDOFF_LVL2:
            seller_override = _other_seller(r["品类"], n)
            stage = "list_item_between"

        req, empty = gold_tools(r["工具"])
        out.append(dict(
            qid=qid,
            总类=r["总类"], 一级=lvl1, 二级=lvl2,
            用例类型=r["用例类型"], 品类=r["品类"], 商品=r["商品"],
            前置=pre, text=r["query"], ctx=ctx, ctx_turns=ctx_turns,
            商品2=r.get("商品2") or "", 品类2=r.get("品类2") or "",
            stage=stage, fixture_note=fixture_note,
            photo=photo,
            # tester2 owns nothing, so only a precondition that literally says
            # "the seller has none of these listed" may point at it. The test is
            # a prefix of the NONE_ template rather than a substring of it: the
            # words 「没有任何」 also appear in preconditions about the *platform*
            # ("「破壁机」在平台上没有任何成交记录") and even in ones that assert
            # the opposite ("全新会话，没有任何历史；卖家名下有「X」"). Matching on
            # the substring sent all 12 of the empty-result cases and 6 of the
            # no-history cases to a seller with zero listings, where the advisor
            # short-circuits to NO_ITEMS_REPLY before the model is ever called —
            # so those rows measured the fixture, not the behaviour under test.
            seller=(seller_override or (SELLER_NONE if pre.startswith(_NONE_PREFIX)
                                        else SELLER_BY_CAT[r["品类"]])),
            gold_routes=gold_routes(r["路由"]),
            gold_scenarios=gold_scenarios(r["scen"]),
            gold_tools_req=req,
            gold_tools_empty=empty,
            预期=r["预期"], 路由原文=r["路由"], scen原文=r["scen"], 工具原文=r["工具"],
            skip=skip,
        ))
    return out


if __name__ == "__main__":
    from collections import Counter
    rows = load()
    runnable = [r for r in rows if not r["skip"]]
    print(f"总计 {len(rows)}；可跑 {len(runnable)}；跳过 {len(rows) - len(runnable)}")
    print("\n跳过原因:")
    for k, v in Counter(r["skip"] for r in rows if r["skip"]).most_common():
        print(f"  {v:>4}  {k}")
    print("\n卖家分配:", dict(Counter(r["seller"] for r in runnable)))
    print("带前置轮:", sum(1 for r in runnable if r["ctx"]))
    print("带图片:", sum(1 for r in runnable if r["photo"]))
    print("有路由断言:", sum(1 for r in runnable if r["gold_routes"]))
    print("有场景断言:", sum(1 for r in runnable if r["gold_scenarios"]))
    print("有工具断言:", sum(1 for r in runnable if r["gold_tools_req"] or r["gold_tools_empty"]))
