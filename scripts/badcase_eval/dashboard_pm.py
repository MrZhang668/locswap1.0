"""Aggregate the run for the product-manager board.

    python scripts/badcase_eval/dashboard_pm.py --reach runs/reach50.json --out runs/pm.json

Differs from dashboard_data.py in one decision: rows whose result cannot be
attributed to the product are *removed*, not annotated. The board answers four
questions — are the badcases real, what do the metrics mean and read, how is
attribution done, and what was excluded and why — and every number on it is
computed over the same valid set.

Excluded, with the plain reason the board prints:
* rows whose target product sat past the listing window the assistant can read
  (test-data setup error, not a product error). Fixtures v2 splits the eval
  seller by category so no holding exceeds 18 — this should now be zero;
* rows where the intent model's output was cut off mid-answer (a real defect,
  counted separately rather than scored as an intent result);
* rows never executed (image recognition held back by decision; front-end
  state not observable in-process; a few needing state injection).

Verdicts read the 95% Wilson interval, not the point estimate: a class whose
interval sits entirely above 50% is a confirmed common problem even at n=4,
and one whose interval straddles the line is "需补样本" even at n=22.

Vocabulary is business-facing throughout: route targets and tool names are
mapped to what the seller would call them.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

ROUTE_ZH = {"sales_agent": "销售分析助手", "publishing_agent": "上架助手", "clarify": "反问澄清"}
SCEN_ZH = {"price": "定价分析", "demand": "需求分析", "both": "需求+定价",
           "find_buyers": "找买家", "none": "（无）", "": "（无）"}
TOOL_ZH = {"list_my_items": "读取商品清单", "find_potential_buyers": "找买家",
           "analyze_sold_prices": "成交价分析", "analyze_demand": "需求分析",
           "push_item_to_want": "给买家发消息"}

#: Families whose expected outcome is a write — the no-write check is not
#: applied to them.
WRITE_EXPECTED = {"明确同意未被识别", "确认后正确执行", "推送执行偏差"}


def zh_route(s):
    return " / ".join(ROUTE_ZH.get(x, x) for x in (s or "").split(" / ") if x) or "—"


def zh_tools(lst):
    return [TOOL_ZH.get(t, t) for t in (lst or [])]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="scripts/badcase_eval/runs/full.jsonl")
    ap.add_argument("--scored", default="scripts/badcase_eval/runs/full.scored.json")
    ap.add_argument("--judged", default="scripts/badcase_eval/runs/full.judged.json")
    ap.add_argument("--reach", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    run = {}
    for line in Path(args.run).open(encoding="utf-8"):
        r = json.loads(line)
        run[r["qid"]] = r
    scored = json.loads(Path(args.scored).read_text(encoding="utf-8"))["rows"]
    judged = {r["qid"]: r for r in
              json.loads(Path(args.judged).read_text(encoding="utf-8"))["results"]}
    reach = set(json.loads(Path(args.reach).read_text(encoding="utf-8")))

    def reachable(q):
        r = run[q]
        if not r.get("seller", "").startswith("badcase-eval"):
            return True
        return r.get("商品") in reach

    def truncated(q):
        return ((run[q].get("turn") or {}).get("router_fallback") or "") \
            .startswith("router call failed")

    ran = [q for q, r in run.items() if not r.get("skip")]
    skipped = [q for q, r in run.items() if r.get("skip")]
    contaminated = [q for q in ran if not reachable(q)]
    trunc_valid = [q for q in ran if reachable(q) and truncated(q)]
    valid = [q for q in ran if reachable(q) and not truncated(q)]

    # ---- per-row verdict on the valid set ----
    # A push is executed through the proposal path, not as a tool call, so the
    # tool assertion never sees it. For the families whose whole point is "the
    # seller said yes, did it send", the executable fact is action_result.
    def wrote(q):
        return bool(((run[q].get("turn") or {}).get("action_result") or {}).get("succeeded"))

    for q in valid:
        if run[q].get("一级") in WRITE_EXPECTED and q in scored:
            scored[q]["tools_ok"] = wrote(q)

    # Verdict is about the OUTCOME. The first pass failed a row whenever any
    # internal checkpoint differed from the one the case author wrote down —
    # which marked a seller-has-nothing message answered correctly inside the
    # sales assistant as a routing failure, and a "should I lower the price"
    # answered with a clear price analysis as a scenario failure. Those are
    # paths, not problems. A row is a badcase when what the seller got was
    # wrong: the judge says so, a message went out unconfirmed, or a confirmed
    # push did not happen. The checkpoint layers are kept as diagnosis.
    def is_bad(q):
        """Strict: any checkpoint that did not do what the case asked for.

        The product owner's call, and it is defensible — a turn that reached an
        acceptable answer down the wrong path is working by luck, and luck is
        not a property you can ship on. It does mean the count includes ~155
        rows whose reply was fine, so a class's badcase rate here reads as
        "how often did this go off-script", not "how often did the seller get
        a bad answer". The judge column separates the two when you need it.
        """
        s, j = scored.get(q, {}), judged.get(q, {})
        if run[q].get("一级") in WRITE_EXPECTED and not wrote(q):
            return True          # 卖家点了头却没发出去
        return (s.get("route_ok") is False
                or s.get("scenario_ok") is False
                or s.get("tools_ok") is False
                or s.get("no_unauthorised_write") is False
                or j.get("verdict") == "fail")

    def layers(q):
        """Where a bad row went wrong, in pipeline order."""
        s, j = scored.get(q, {}), judged.get(q, {})
        out = []
        if s.get("route_ok") is False:
            out.append("分流")
        if s.get("scenario_ok") is False:
            out.append("分析类型")
        if s.get("tools_ok") is False:
            out.append("执行")
        if s.get("no_unauthorised_write") is False:
            out.append("未授权发消息")
        if j.get("verdict") == "fail":
            out.append("回复")
        return out or ["回复"]

    fails = {q: (layers(q) if is_bad(q) else []) for q in valid}
    n_bad = sum(1 for q in valid if fails[q])

    def rate(qs, key):
        got = [q for q in qs if scored.get(q, {}).get(key) is not None]
        ok = sum(1 for q in got if scored[q][key])
        return {"ok": ok, "n": len(got), "pct": round(ok / len(got) * 100, 1) if got else None}

    def jrate(qs):
        got = [q for q in qs if judged.get(q, {}).get("verdict") in ("pass", "fail")]
        ok = sum(1 for q in got if judged[q]["verdict"] == "pass")
        return {"ok": ok, "n": len(got), "pct": round(ok / len(got) * 100, 1) if got else None,
                "unsure": sum(1 for q in qs if judged.get(q, {}).get("verdict") == "unsure")}

    out: dict = {}
    out["scope"] = {
        "designed": len(run), "skipped": len(skipped), "ran": len(ran),
        "contaminated": len(contaminated), "truncated": len(trunc_valid),
        "valid": len(valid), "bad": n_bad,
        "bad_pct": round(n_bad / len(valid) * 100, 1),
        "skip_reasons": [{"reason": k, "n": v} for k, v in
                         Counter(run[q]["skip"] for q in skipped).most_common()],
        "window": 50, "seeded": 101,
    }

    # ---- metrics ----
    nowrite_rows = [q for q in valid if run[q].get("一级") not in WRITE_EXPECTED]
    # Distinct name: `wrote` above is the per-row predicate and is still needed
    # by nothing below, but shadowing a function with a count is how a later
    # edit silently breaks is_bad().
    n_stray_writes = sum(1 for q in nowrite_rows if scored.get(q, {}).get("wrote"))
    out["metrics"] = {
        "route": rate(valid, "route_ok"),
        "scenario": rate(valid, "scenario_ok"),
        "tools": rate(valid, "tools_ok"),
        "nowrite": {"ok": len(nowrite_rows) - n_stray_writes, "n": len(nowrite_rows),
                    "pct": round((len(nowrite_rows) - n_stray_writes)
                                 / len(nowrite_rows) * 100, 1)},
        "reply": jrate(valid),
        "overall": {"ok": len(valid) - n_bad, "n": len(valid),
                    "pct": round((len(valid) - n_bad) / len(valid) * 100, 1)},
    }
    out["kinds"] = {}
    for k in ("回归基线", "风险探测"):
        qs = [q for q in valid if run[q].get("用例类型") == k]
        b = sum(1 for q in qs if fails[q])
        out["kinds"][k] = {"n": len(qs), "bad": b, "bad_pct": round(b / len(qs) * 100, 1) if qs else None,
                           "route": rate(qs, "route_ok"), "tools": rate(qs, "tools_ok"),
                           "reply": jrate(qs)}

    # ---- badcase verification, per first-level class ----
    def wilson(k, n, z=1.96):
        """95% interval for a proportion. Small-n safe, unlike k/n ± 1.96·SE,
        which on 4/4 returns [1.0, 1.0] and on 0/10 returns [0, 0] — both of
        which would claim certainty the data does not carry."""
        if n == 0:
            return 0.0, 1.0
        p = k / n
        den = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / den
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
        return max(0.0, centre - half), min(1.0, centre + half)

    #: Sample size that would put a 95% interval inside ±15pp at the worst case
    #: (p=0.5). Used only to tell the reader how many more rows a still-unstable
    #: class needs — not as an admission threshold.
    TARGET_N = math.ceil(1.96 ** 2 * 0.25 / 0.15 ** 2)

    def verdict(kind, n, bad_pct, lo, hi):
        """Read the interval, not the point estimate.

        A fixed n-threshold gets this wrong in both directions: it buries a
        4/4 total failure as "sample too small" (the interval starts at 51%,
        the problem is certain) and it dresses up a 3/22 as a finding (the
        interval spans 5–33%, which is anything from rare to common). What
        decides is whether the interval sits entirely on one side of a line.
        """
        if kind == "回归基线":
            if hi < 0.20:
                return "功能正常"
            if lo >= 0.50:
                return "已退化"
            if lo >= 0.20:
                return "轻度退化"
            return "需补样本"
        if lo >= 0.50:
            return "属实·普遍"
        if lo >= 0.20:
            return "属实·偶发"
        if hi < 0.20:
            return "基本未复现"
        return "需补样本"

    ORIGINAL = {  # first-level classes that appeared in the 09-17 manual sheet
        "意图识别不充分", "泛意图识别差", "泛意图识别好的情况", "意图正确识别", "意图识别正确",
        "缺少多模态分析", "模型输出内容丢失", "会话内记忆丢失", "任务优先级", "agent功能缺失",
        "正确识别多个任务意图，并能调用正确的tool", "噪声图像识别正确",
    }
    groups = defaultdict(list)
    for q in valid:
        groups[(run[q]["总类"], run[q]["一级"])].append(q)
    verify = []
    for (top, l1), qs in groups.items():
        kinds = Counter(run[q].get("用例类型") for q in qs)
        kind = "回归基线" if kinds.get("回归基线", 0) > kinds.get("风险探测", 0) else "风险探测"
        b = sum(1 for q in qs if fails[q])
        pct = round(b / len(qs) * 100, 1)
        layer_mix = Counter()
        for q in qs:
            for l in fails[q]:
                layer_mix[l] += 1
        # first failing layer = where the problem starts
        first = Counter(fails[q][0] for q in qs if fails[q])
        lo, hi = wilson(b, len(qs))
        v = verdict(kind, len(qs), pct, lo, hi)
        verify.append({
            "top": top, "name": l1, "kind": kind, "origin": l1 in ORIGINAL,
            "n": len(qs), "bad": b, "pct": pct,
            "ci": [round(lo * 100, 1), round(hi * 100, 1)],
            "verdict": v,
            # How many more rows this class needs before its interval could
            # land on one side. Only meaningful while the verdict is unstable.
            "need": max(0, TARGET_N - len(qs)) if v == "需补样本" else 0,
            "mix": dict(layer_mix), "first": dict(first),
        })
    # A problem type from the manual sheet that ended up with no valid row is
    # not "absent" — it is unverified, and the board has to say so. Silently
    # dropping 意图识别不充分 (the sheet's first badcase) because every one of
    # its rows targeted a product past the listing window would let the table
    # read as if it had been checked and cleared.
    present = {v["name"] for v in verify}
    for l1 in sorted(ORIGINAL - present):
        qs = [q for q, r in run.items() if r.get("一级") == l1]
        if not qs:
            continue
        n_skip = sum(1 for q in qs if run[q].get("skip"))
        n_dirty = sum(1 for q in qs if q in contaminated)
        n_trunc = sum(1 for q in qs if q in trunc_valid)
        parts = []
        if n_skip:
            parts.append(f"{n_skip} 条未执行（缺测试数据）")
        if n_dirty:
            parts.append(f"{n_dirty} 条落在剔除的假失败数据里")
        if n_trunc:
            parts.append(f"{n_trunc} 条输出被截断")
        kinds = Counter(run[q].get("用例类型") for q in qs)
        verify.append({
            "top": run[qs[0]]["总类"], "name": l1, "origin": True,
            "kind": "回归基线" if kinds.get("回归基线", 0) > kinds.get("风险探测", 0) else "风险探测",
            "n": 0, "bad": 0, "pct": None, "verdict": "无法验证",
            "ci": None, "need": 0, "mix": {}, "first": {},
            "note": f"设计 {len(qs)} 条：" + "，".join(parts) + "。本轮无有效样本，需重跑。",
        })

    verify.sort(key=lambda v: (v["kind"] != "风险探测", v["verdict"] == "无法验证",
                               -v["bad"], -(v["pct"] or 0)))
    out["verify"] = verify
    unstable = [v for v in verify if v["verdict"] == "需补样本"]
    out["stability"] = {
        "target_n": TARGET_N,
        "stable": sum(1 for v in verify if v["verdict"] not in ("需补样本", "无法验证")),
        "unstable": len(unstable),
        "need_rows": sum(v["need"] for v in unstable),
    }

    # ---- attribution: where does the failure start ----
    first_layer = Counter(fails[q][0] for q in valid if fails[q])
    only_reply = sum(1 for q in valid if fails[q] == ["回复"])
    out["attribution"] = {
        "first_layer": [{"k": k, "n": first_layer[k]} for k in
                        ("分流", "分析类型", "执行", "未授权发消息", "回复") if first_layer[k]],
        "only_reply": only_reply,
        "bad": n_bad,
    }
    # route confusion, valid rows only, business labels
    conf = Counter()
    for q in valid:
        s = scored.get(q, {})
        if s.get("route_ok") is False:
            conf[(zh_route(" / ".join(run[q].get("gold_routes") or [])),
                  ROUTE_ZH.get(s.get("route", ""), s.get("route", "")))] += 1
    out["confusion"] = [{"want": w, "got": g, "n": n} for (w, g), n in conf.most_common(8)]

    # ---- evidence, business labels ----
    ev = defaultdict(list)
    for q in valid:
        if not fails[q] or len(ev[run[q]["一级"]]) >= 3:
            continue
        r, s, j, t = run[q], scored.get(q, {}), judged.get(q, {}), run[q].get("turn") or {}
        ev[r["一级"]].append({
            "qid": q, "kind": r.get("用例类型"), "cat": r.get("品类"), "item": r.get("商品"),
            "ctx": r.get("ctx") or "", "q": r.get("text"), "expect": r.get("预期"),
            "want_route": zh_route(" / ".join(r.get("gold_routes") or [])),
            "got_route": ROUTE_ZH.get(t.get("route", ""), t.get("route", "") or "—"),
            "route_ok": s.get("route_ok"),
            "got_tools": zh_tools(t.get("used_tools")),
            "tools_ok": s.get("tools_ok"),
            "layers": fails[q],
            "reply": (t.get("reply") or "")[:420],
            "judge_reason": j.get("reason", ""), "judge_evidence": j.get("evidence", ""),
        })
    out["evidence"] = dict(ev)

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("写入", args.out)
    print(f"  有效 {len(valid)} | badcase {n_bad} ({out['scope']['bad_pct']}%) | "
          f"剔除污染 {len(contaminated)} + 截断 {len(trunc_valid)} | 未执行 {len(skipped)}")
    print("  指标:", {k: v["pct"] for k, v in out["metrics"].items()})
    print("  失败起点:", out["attribution"]["first_layer"], "仅回复:", only_reply)


if __name__ == "__main__":
    main()
