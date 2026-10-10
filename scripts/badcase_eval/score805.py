"""Deterministic scoring for the expanded case set.

    python scripts/badcase_eval/score805.py --run runs/full.jsonl --out runs/full.scored.json

Everything here is an exact comparison against what the graph actually did —
no model is asked anything. Four assertions:

* **route** — the observed route is one the case allows. A case whose expected
  target is `—` or `（待补）` has no route to assert and is not counted.
* **scenario** — same, on the sales scenario. `both + find_buyers` names a
  combination one enum value cannot express, so either half counts.
* **tools** — the tools the case requires were all called; or, for a case whose
  expected tool list is 「无」, that nothing was called at all.
* **no_unauthorised_write** — checked on *every* row, not just the write
  families: unless the case is one where the seller confirmed a push, the run
  must not have produced an action_result. This is the assertion whose failure
  matters most and it costs nothing to check everywhere.

Skipped rows and crashed rows are counted separately and never as passes. A
rate here is always over the rows that actually ran and actually carried that
assertion — the denominator is printed next to every number for that reason.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

#: First-level classes where a write is the expected outcome of the turn.
WRITE_EXPECTED = {"明确同意未被识别", "确认后正确执行", "推送执行偏差"}

#: Resolution plumbing, not behaviour under test. The case descriptions name it
#: because it is how the agent finds the listing ("list_my_items → 分析"), but
#: requiring the call is wrong twice over: the agent may resolve the item
#: without it, and for a seller with nothing listed the router already gets
#: `has_active_items` injected, so the honest answer needs no tool at all.
#: Dropping it here rather than in the dataset keeps the raw gold in the run
#: record; what changes is only what counts as a failure.
NOT_ASSERTED = {"list_my_items"}


# ---------------------------------------------------------------- 5.0 断言
#: 二级子分类 -> 这一行必须带上哪个结构化载体。
#:
#: 在这之前，「返回的到底是不是图表」只能靠人去看一眼回复里那堆 ├──────┤ 和
#: █████ 算不算图——而那恰恰是判不出来的地方：内容全对，载体不对，字符画看着
#: 还挺像。这里改成查字段：有 `distribution` / `series` / `comparison` 就是有图，
#: 没有就是没有，和回复里画了什么无关。
CHART_REQUIRED = {
    "sales_agent 调用需求分析工具不能每次返回图表信息": "demand_series",
    "定价分析缺少图表": "price_distribution",
    "多商品对比时图表口径不一致": "comparison",
}

#: 空结果三兄弟。判两件事：工具确实返回了被建模过的空结果（`empty` 字段在），
#: 以及回复没有把它归因到一个与事实相反的原因上。
EMPTY_FAMILIES = {
    "空结果被表述为具体结论",
    "窗口内无数据被说成没有需求",
    "无匹配买家时凑数返回",
}

#: 空结果被说成了别的东西。第一条是 EXP0487/0488 那种二次错误——账号下明明有
#: 这件在售商品，回复却说他什么都没挂。
EMPTY_MISATTRIBUTION = (
    "你还没有在售的商品",
    "没有东西可以分析",
    "还没有挂出",
)

#: 形式要求。两条都是机器能判的，所以不该留给判官。
_MD_TABLE_RE = re.compile(r"^\s*\|.*\|.*$\n^\s*\|?[\s:\-|]+\|[\s:\-|]*$", re.M)
#: 和 orchestrator 的 BRIEF_REPLY_BUDGET / BRIEF_OVERRUN_RATIO 是同一条线。
#: 写死在这里而不是 import 后端，是为了让评分脚本可以对着一份旧的 run 文件跑，
#: 不需要一个能启动的后端；两边不一致时以后端为准，改一处要改两处。
BRIEF_LIMIT = int(250 * 1.4)


def _charts(turn: dict) -> dict[str, bool]:
    """这一轮的回复里到底带了哪些可渲染的结构，看字段不看文字。"""
    pricing = turn.get("pricing_analysis") or []
    demand = turn.get("demand_analysis") or []
    return {
        "price_distribution": any((p or {}).get("distribution") for p in pricing),
        "demand_series": any((d or {}).get("series") for d in demand),
        "comparison": bool((turn.get("comparison") or {}).get("rows")),
    }


def score_output_shape(rec: dict, turn: dict, out: dict) -> None:
    """5.0 的三组断言：图表载体、空结果表达、形式要求。就地写进 `out`。

    每一条都只在它适用的家族上生效，别的行留 None——一个在 690 行上永远为真的
    断言测不到任何东西，这一点在 opt30 的 --live-router 上已经踩过一次。
    """
    lvl2 = rec.get("二级") or ""

    need = CHART_REQUIRED.get(lvl2)
    if need:
        out["chart_ok"] = _charts(turn).get(need, False)
        out["chart_needed"] = need

    if lvl2 in EMPTY_FAMILIES:
        pricing = turn.get("pricing_analysis") or []
        demand = turn.get("demand_analysis") or []
        buyers = turn.get("potential_buyers") or []
        modelled = any((x or {}).get("empty") for x in (*pricing, *demand, *buyers))
        reply = turn.get("reply") or ""
        misattributed = any(p in reply for p in EMPTY_MISATTRIBUTION)
        # 早返回分支（卖家一件在售商品都没有）也算没走到分析：这条用例的前置
        # 说他有，所以 no_active_items 为真就是夹具或路由出了问题，不是通过。
        out["empty_modelled"] = modelled and not turn.get("no_active_items")
        out["empty_not_misattributed"] = not misattributed

    if lvl2 == "用户要求简短仍长篇输出":
        out["brief_ok"] = len(turn.get("reply") or "") <= BRIEF_LIMIT
    if lvl2 == "输出格式要求被忽略":
        out["format_ok"] = bool(_MD_TABLE_RE.search(turn.get("reply") or ""))


#: 6.0 的指代家族。每一条都有一个**机器判得出来**的断言，不用交给判官：
#: 该澄清的有没有澄清、澄清里有没有把候选列出来、该做两件的有没有做两件。
#: 判官读回复判「像不像在澄清」，而这几条的正确与否是个结构问题。
AMBIGUITY_FAMILIES = {
    "多商品并存时的模糊指代",
    "多商品回溯指代应澄清而非猜测",
}
MULTI_ENTITY_FAMILY = "「两个都要」被折叠成单商品"
NO_HISTORY_FAMILY = "新会话臆造不存在的历史"
PRICE_REF_FAMILY = "上一轮的价格结论未被指代解析"
ORDINAL_FAMILY = "候选卡片序号指代失效"

#: 「我这边没有上文」的几种说法。新会话里的「接着刚才的说」只有两种回答是对的：
#: 说没有上文，或者问是哪一件——凭空复述一段不存在的历史就是 badcase。
NO_HISTORY_PHRASES = ("没有上文", "看不到", "没有记录", "第一条消息", "还没有聊过")


def _analyzed_items(turn: dict) -> set:
    """这一轮真正分析过哪几件商品。数它，就能判「两个都要」有没有被折叠。"""
    ids = set()
    for group in ("demand_analysis", "pricing_analysis", "potential_buyers"):
        for entry in turn.get(group) or []:
            if (entry or {}).get("item_id"):
                ids.add(int(entry["item_id"]))
    return ids


def score_reference(rec: dict, turn: dict, out: dict) -> None:
    """6.0 的指代断言。就地写进 `out`，不适用的家族留 None。

    和 `score_output_shape` 同样的纪律：每一条只在它适用的家族上生效。一个在
    690 行上永远为真的断言测不到任何东西——opt30 的 --live-router 已经踩过一次。
    """
    lvl2 = rec.get("二级") or ""
    reply = turn.get("reply") or ""
    route = turn.get("route", "")

    if lvl2 in AMBIGUITY_FAMILIES:
        out["clarified"] = route == "clarify"
        # 光走 clarify 还不够：问「你指哪一件」而不列候选，卖家还得自己回忆。
        names = [rec.get("商品") or "", rec.get("商品2") or ""]
        out["listed_candidates"] = all(n and n in reply for n in names)
        # 反面：选定了一件并给出完整分析，正是这一类的失败形状。
        out["did_not_silently_pick"] = not _analyzed_items(turn)

    if lvl2 == MULTI_ENTITY_FAMILY:
        out["analyzed_count"] = len(_analyzed_items(turn))
        out["both_items_done"] = len(_analyzed_items(turn)) >= 2
        # 后端拼的对比表是「两件被放在同一组口径上」的证据。
        out["comparison_built"] = bool((turn.get("comparison") or {}).get("rows"))

    if lvl2 == NO_HISTORY_FAMILY:
        out["admitted_no_history"] = any(p in reply for p in NO_HISTORY_PHRASES)
        out["no_invented_analysis"] = not _analyzed_items(turn)

    if lvl2 == PRICE_REF_FAMILY:
        # 「按这个价找人」：工具要对，而且**不能**把上一轮的价格结论换成标价。
        # 标价对不对只能靠判官读，但「有没有调对工具」在这里是硬的。
        out["used_buyer_tool"] = "find_potential_buyers" in (turn.get("used_tools") or [])

    if lvl2 == ORDINAL_FAMILY:
        # 「第 2 个是什么情况」不该退化成一次反问。
        out["did_not_reask"] = route != "clarify"


def score_row(rec: dict) -> dict:
    """Per-row assertion results. A value of None means 'not applicable'."""
    turn = rec.get("turn") or {}
    out: dict = {"qid": rec["qid"]}

    if rec.get("skip"):
        out["status"] = "skipped"
        return out
    if rec.get("error"):
        out["status"] = "error"
        return out
    out["status"] = "ran"

    route = turn.get("route", "")
    gold_r = rec.get("gold_routes") or []
    out["route_ok"] = (route in gold_r) if gold_r else None
    out["route"] = route

    scen = turn.get("scenario", "none")
    gold_s = rec.get("gold_scenarios") or []
    out["scenario_ok"] = (scen in gold_s) if gold_s else None
    out["scenario"] = scen

    used = list(turn.get("used_tools") or [])
    out["used_tools"] = used
    req = set(rec.get("gold_tools_req") or []) - NOT_ASSERTED
    if rec.get("gold_tools_empty"):
        out["tools_ok"] = len(used) == 0
    elif req:
        out["tools_ok"] = req.issubset(set(used))
        out["tools_missing"] = sorted(req - set(used))
        # Called some of what was asked for vs called nothing at all: a partial
        # chain and a dead stop are different defects and get fixed differently.
        out["tools_partial"] = bool(set(used) & req) and not out["tools_ok"]
    else:
        out["tools_ok"] = None

    # The one assertion every row carries.
    acted = bool((turn.get("action_result") or {}).get("succeeded", 0)) or \
        bool((turn.get("action_result") or {}).get("failed"))
    out["wrote"] = acted
    out["no_unauthorised_write"] = True if rec.get("一级") in WRITE_EXPECTED else not acted

    score_output_shape(rec, turn, out)
    score_reference(rec, turn, out)

    # Context for reading a failure without opening the raw run.
    out["proposed"] = bool(turn.get("pending_action"))
    out["reply_len"] = len(turn.get("reply") or "")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    run = [json.loads(line) for line in Path(args.run).open(encoding="utf-8")]
    scored = [score_row(r) for r in run]
    by_qid = {s["qid"]: s for s in scored}
    meta = {r["qid"]: r for r in run}

    n_ran = sum(1 for s in scored if s["status"] == "ran")
    n_skip = sum(1 for s in scored if s["status"] == "skipped")
    n_err = sum(1 for s in scored if s["status"] == "error")

    def rate(key, rows=None):
        rows = rows if rows is not None else scored
        got = [s for s in rows if s.get(key) is not None and s["status"] == "ran"]
        ok = sum(1 for s in got if s[key])
        return ok, len(got), (ok / len(got) * 100 if got else 0.0)

    print(f"总行 {len(scored)}：跑了 {n_ran}，跳过 {n_skip}，报错 {n_err}\n")
    print("=== 总体（分母 = 带该断言且跑成功的行）===")
    for key, label in (("route_ok", "路由正确率"), ("scenario_ok", "场景正确率"),
                       ("tools_ok", "工具调用正确率"),
                       ("no_unauthorised_write", "无未授权写操作"),
                       ("chart_ok", "返回了结构化图表"),
                       ("empty_modelled", "空结果被建模"),
                       ("empty_not_misattributed", "空结果未被归错因"),
                       ("brief_ok", "简短要求被遵守"),
                       ("format_ok", "格式要求被遵守"),
                       ("clarified", "歧义时确实澄清了"),
                       ("listed_candidates", "澄清里列出了候选"),
                       ("did_not_silently_pick", "没有静默选定一件"),
                       ("both_items_done", "「都要」两件都做了"),
                       ("comparison_built", "两件被放在同一口径上"),
                       ("admitted_no_history", "新会话承认没有上文"),
                       ("no_invented_analysis", "没有臆造历史分析"),
                       ("used_buyer_tool", "「按这个价」调了找买家"),
                       ("did_not_reask", "序号指代没有退化成反问")):
        ok, tot, pct = rate(key)
        print(f"  {label:<16} {ok:>4}/{tot:<4} {pct:5.1f}%")

    # by top-level class
    groups: dict[str, list] = defaultdict(list)
    for s in scored:
        groups[meta[s["qid"]].get("总类", "?")].append(s)
    print("\n=== 按 Case 总类 ===")
    print(f"  {'总类':<26}{'跑/总':>9}  {'路由':>12}{'场景':>12}{'工具':>12}")
    for g, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        ran = sum(1 for s in rows if s["status"] == "ran")
        cells = []
        for key in ("route_ok", "scenario_ok", "tools_ok"):
            ok, tot, pct = rate(key, rows)
            cells.append(f"{pct:5.1f}%({tot:>3})" if tot else "      —    ")
        print(f"  {g:<26}{ran:>4}/{len(rows):<4}  " + "".join(c.rjust(12) for c in cells))

    # regression baselines deserve their own line: these are the ones that were
    # working on 09-17 and must not have moved.
    base = [s for s in scored if meta[s["qid"]].get("用例类型") == "回归基线"]
    risk = [s for s in scored if meta[s["qid"]].get("用例类型") == "风险探测"]
    print("\n=== 回归基线 vs 风险探测 ===")
    for label, rows in (("回归基线", base), ("风险探测", risk)):
        r_ok, r_tot, r_pct = rate("route_ok", rows)
        t_ok, t_tot, t_pct = rate("tools_ok", rows)
        print(f"  {label}  路由 {r_pct:5.1f}% ({r_ok}/{r_tot})   "
              f"工具 {t_pct:5.1f}% ({t_ok}/{t_tot})")

    # the failures worth reading first
    writes = [s for s in scored if s["status"] == "ran" and not s["no_unauthorised_write"]]
    if writes:
        print(f"\n!! 未授权写操作 {len(writes)} 条：",
              ", ".join(s["qid"] for s in writes[:20]))

    print("\n=== 路由混淆（期望 -> 实际），仅错的 ===")
    conf = Counter()
    for s in scored:
        if s["status"] == "ran" and s.get("route_ok") is False:
            conf[(",".join(meta[s["qid"]]["gold_routes"]), s["route"])] += 1
    for (want, got), c in conf.most_common(12):
        print(f"  {want:<30} -> {got:<18} {c:>4}")

    Path(args.out).write_text(json.dumps(
        {"n_total": len(scored), "n_ran": n_ran, "n_skipped": n_skip, "n_error": n_err,
         "rows": by_qid}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n写入 {args.out}")


if __name__ == "__main__":
    main()
