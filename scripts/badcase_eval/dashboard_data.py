"""Aggregate the run into one JSON the dashboard embeds.

    python scripts/badcase_eval/dashboard_data.py --out runs/dashboard.json

Everything the board shows is computed here, so the page itself holds no
arithmetic and no judgement — it renders numbers that were already decided.

The one rule this file enforces everywhere: **a rate is always reported with
its denominator, and the fixture-contaminated rows are never folded into a
headline.**

The contaminated set is defined by what the agent could *reach*, not by the
default page size. `ListMyItemsInput.limit` defaults to 20 but accepts up to
50, and the agent does raise it — so products at positions 21-50 are reachable
and score almost as well as the first 20 (tools 64.9% vs 69.1%). Products past
position 50 cannot be enumerated at all, and that is where the cliff is
(tools 8.7%). The eval seller was seeded with 101 listings, so 51 products sit
past the wall and 311 executed rows target one of them.

Those rows are kept — the run happened, and a row whose probe does not depend
on resolving a specific listing (injection, prohibited goods) still reads — but
no tool/behaviour headline is computed over them.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

HARD = ("route_ok", "scenario_ok", "tools_ok", "no_unauthorised_write")


def load(run_p, scored_p, judged_p, visible_p):
    run = {}
    for line in Path(run_p).open(encoding="utf-8"):
        r = json.loads(line)
        run[r["qid"]] = r
    scored = json.loads(Path(scored_p).read_text(encoding="utf-8"))["rows"]
    judged = {r["qid"]: r for r in
              json.loads(Path(judged_p).read_text(encoding="utf-8"))["results"]}
    visible = set(json.loads(Path(visible_p).read_text(encoding="utf-8")))
    return run, scored, judged, visible


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="scripts/badcase_eval/runs/full.jsonl")
    ap.add_argument("--scored", default="scripts/badcase_eval/runs/full.scored.json")
    ap.add_argument("--judged", default="scripts/badcase_eval/runs/full.judged.json")
    ap.add_argument("--visible", required=True, help="默认 limit=20 可见的商品名 JSON")
    ap.add_argument("--reach", required=True, help="limit=50 可达的商品名 JSON")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    run, scored, judged, visible = load(args.run, args.scored, args.judged, args.visible)
    reach = set(json.loads(Path(args.reach).read_text(encoding="utf-8")))

    def clean(q):
        """Does this row support a claim about tool use / behaviour?

        Reachability, not the default page size: anything the agent can pull by
        raising `limit` to its maximum of 50 is fair game.
        """
        r = run[q]
        if r.get("skip"):
            return False
        if not r.get("seller", "").startswith("badcase-eval"):
            return True                      # tester2 owns nothing; nothing to resolve
        return r.get("商品") in reach

    def band(q):
        r = run[q]
        if not r.get("seller", "").startswith("badcase-eval"):
            return "tester2"
        if r.get("商品") in visible:
            return "A"
        return "B" if r.get("商品") in reach else "C"

    ran = [q for q, r in run.items() if not r.get("skip")]
    ok_rows = [q for q in ran if clean(q)]
    dirty = [q for q in ran if not clean(q)]

    def hard_rate(qs, key):
        got = [q for q in qs if scored.get(q, {}).get(key) is not None]
        n_ok = sum(1 for q in got if scored[q][key])
        return {"ok": n_ok, "n": len(got),
                "pct": round(n_ok / len(got) * 100, 1) if got else None}

    def judge_rate(qs):
        got = [q for q in qs if judged.get(q, {}).get("verdict") in ("pass", "fail")]
        n_ok = sum(1 for q in got if judged[q]["verdict"] == "pass")
        uns = sum(1 for q in qs if judged.get(q, {}).get("verdict") == "unsure")
        return {"ok": n_ok, "n": len(got), "unsure": uns,
                "pct": round(n_ok / len(got) * 100, 1) if got else None}

    out: dict = {}

    out["run"] = {
        "total": len(run), "ran": len(ran),
        "skipped": sum(1 for r in run.values() if r.get("skip")),
        "errors": sum(1 for r in run.values() if r.get("error")),
        "clean": len(ok_rows), "contaminated": len(dirty),
        "minutes": 88, "llm_calls": 2154,
        "tokens_in": 8161479, "tokens_out": 226366,
        "cost_usd": 16.24,
        "products": len({r.get("商品") for r in run.values() if r.get("商品")}),
        "categories": len({r.get("品类") for r in run.values() if r.get("品类")}),
        "lvl2": len({r.get("二级") for r in run.values() if r.get("二级")}),
        "visible_limit": 20, "reach_limit": 50, "seeded_items": 101,
    }

    # The reachability bands — the evidence for where the fixture wall sits.
    out["bands"] = []
    for key, label in (("A", "前 20 件（默认窗口内）"),
                       ("B", "第 21–50 件（抬高 limit 可达）"),
                       ("C", "第 51–101 件（永远拿不到）"),
                       ("tester2", "无库存卖家（不涉及枚举）")):
        qs = [q for q in ran if band(q) == key]
        out["bands"].append({
            "key": key, "label": label, "n": len(qs),
            "route": hard_rate(qs, "route_ok"),
            "tools": hard_rate(qs, "tools_ok"),
            "judge": judge_rate(qs),
        })

    # headline metrics, clean subset vs everything that ran
    out["headline"] = {
        scope_name: {
            "route": hard_rate(qs, "route_ok"),
            "scenario": hard_rate(qs, "scenario_ok"),
            "tools": hard_rate(qs, "tools_ok"),
            "nowrite": hard_rate(qs, "no_unauthorised_write"),
            "judge": judge_rate(qs),
        }
        for scope_name, qs in (("clean", ok_rows), ("all", ran), ("dirty", dirty))
    }

    # by top-level class — clean rows only for tools/judge, all rows for route
    tops = sorted({run[q]["总类"] for q in ran})
    out["by_top"] = []
    for t in tops:
        qs_all = [q for q in ran if run[q]["总类"] == t]
        qs_ok = [q for q in qs_all if clean(q)]
        out["by_top"].append({
            "name": t,
            "ran": len(qs_all), "clean": len(qs_ok),
            "total": sum(1 for r in run.values() if r.get("总类") == t),
            "route": hard_rate(qs_all, "route_ok"),
            "scenario": hard_rate(qs_all, "scenario_ok"),
            "tools": hard_rate(qs_ok, "tools_ok"),
            "judge": judge_rate(qs_ok),
        })
    # families with nothing executed at all
    for t in sorted({r.get("总类") for r in run.values() if r.get("skip")} - set(tops)):
        n = sum(1 for r in run.values() if r.get("总类") == t)
        out["by_top"].append({"name": t, "ran": 0, "clean": 0, "total": n,
                              "route": None, "scenario": None,
                              "tools": None, "judge": None})

    # Route confusion, split by reachability. Route is NOT immune to the
    # fixture: the router is handed `listings[:10]` in its facts block — a
    # narrower window than list_my_items' 50 — so with 101 listings it is
    # reasoning about a listing set that mostly does not contain the product
    # the seller named. 64 of the 123 route errors sit in the contaminated
    # group, so a single merged table would read as a routing defect that is
    # half harness.
    conf_ok, conf_bad = Counter(), Counter()
    for q in ran:
        s = scored.get(q, {})
        if s.get("route_ok") is False:
            key = (" / ".join(run[q].get("gold_routes") or []), s.get("route", ""))
            (conf_ok if clean(q) else conf_bad)[key] += 1
    keys = sorted(set(conf_ok) | set(conf_bad),
                  key=lambda k: -(conf_ok[k] + conf_bad[k]))
    out["confusion"] = [{"want": w, "got": g,
                         "clean": conf_ok[(w, g)], "dirty": conf_bad[(w, g)],
                         "n": conf_ok[(w, g)] + conf_bad[(w, g)]}
                        for (w, g) in keys[:8]]
    out["confusion_total"] = {"clean": sum(conf_ok.values()),
                              "dirty": sum(conf_bad.values())}

    # which assertions fail together
    combos = Counter()
    for q in ran:
        s, j = scored.get(q, {}), judged.get(q, {})
        bad = []
        if s.get("route_ok") is False:
            bad.append("路由")
        if s.get("scenario_ok") is False:
            bad.append("场景")
        if s.get("tools_ok") is False:
            bad.append("工具")
        if s.get("no_unauthorised_write") is False:
            bad.append("未授权写")
        if j.get("verdict") == "fail":
            bad.append("行为")
        combos["+".join(bad) if bad else "全部通过"] += 1
    out["combos"] = [{"k": k, "n": n} for k, n in combos.most_common(9)]

    # regression vs risk probe
    out["kinds"] = {
        k: {"route": hard_rate([q for q in ran if run[q].get("用例类型") == k], "route_ok"),
            "tools": hard_rate([q for q in ok_rows if run[q].get("用例类型") == k], "tools_ok"),
            "judge": judge_rate([q for q in ok_rows if run[q].get("用例类型") == k])}
        for k in ("回归基线", "风险探测")
    }

    # per first-level class, ranked by clean-row failures — the 归因 worklist
    rows = []
    for l1 in {run[q]["一级"] for q in ran}:
        qs = [q for q in ok_rows if run[q]["一级"] == l1]
        if not qs:
            continue
        mix = Counter()
        for q in qs:
            s, j = scored.get(q, {}), judged.get(q, {})
            if s.get("route_ok") is False:
                mix["路由"] += 1
            if s.get("tools_ok") is False:
                mix["工具"] += 1
            if j.get("verdict") == "fail":
                mix["行为"] += 1
        failed = sum(1 for q in qs
                     if scored.get(q, {}).get("route_ok") is False
                     or scored.get(q, {}).get("tools_ok") is False
                     or judged.get(q, {}).get("verdict") == "fail")
        rows.append({"name": l1, "top": run[qs[0]]["总类"], "n": len(qs),
                     "failed": failed,
                     "pct": round(failed / len(qs) * 100, 1),
                     "mix": dict(mix)})
    out["worklist"] = sorted(rows, key=lambda r: (-r["failed"], -r["pct"]))

    # drill-down evidence: real failing rows, clean only
    ev = defaultdict(list)
    for q in ok_rows:
        j = judged.get(q, {})
        if j.get("verdict") != "fail":
            continue
        r, s = run[q], scored.get(q, {})
        if len(ev[r["一级"]]) >= 3:
            continue
        t = r.get("turn") or {}
        ev[r["一级"]].append({
            "qid": q, "top": r["总类"], "lvl2": r["二级"],
            "kind": r.get("用例类型"), "cat": r.get("品类"), "item": r.get("商品"),
            "ctx": r.get("ctx") or "", "q": r.get("text"),
            "expect": r.get("预期"),
            "want_route": " / ".join(r.get("gold_routes") or []) or "—",
            "got_route": t.get("route", ""),
            "route_ok": s.get("route_ok"), "tools_ok": s.get("tools_ok"),
            "want_tools": r.get("工具原文", ""),
            "got_tools": t.get("used_tools") or [],
            "evidence": j.get("evidence", ""), "reason": j.get("reason", ""),
            "reply": (t.get("reply") or "")[:420],
        })
    out["evidence"] = {k: v for k, v in ev.items()}

    # the rows where every hard assertion passed and only the reply was wrong
    silent = [q for q in ok_rows
              if scored.get(q, {}).get("route_ok") is not False
              and scored.get(q, {}).get("tools_ok") is not False
              and judged.get(q, {}).get("verdict") == "fail"]
    out["silent_fail"] = {"n": len(silent),
                          "by_top": dict(Counter(run[q]["总类"] for q in silent))}

    # skipped, with reasons
    out["skipped"] = [{"reason": k, "n": v} for k, v in
                      Counter(r["skip"] for r in run.values()
                              if r.get("skip")).most_common()]

    # truncated-router defect
    trunc = [q for q in ran
             if ((run[q].get("turn") or {}).get("router_fallback") or "")
             .startswith("router call failed")]
    out["truncated"] = {"n": len(trunc),
                        "dirty": sum(1 for q in trunc if not clean(q)),
                        "by_top": dict(Counter(run[q]["总类"] for q in trunc)),
                        "qids": trunc[:30]}

    # per product category — is any category harder?
    cats = []
    for c in sorted({run[q]["品类"] for q in ran if run[q].get("品类")}):
        qs = [q for q in ok_rows if run[q].get("品类") == c]
        if not qs:
            continue
        cats.append({"name": c, "n": len(qs), "judge": judge_rate(qs)})
    out["by_category"] = sorted(cats, key=lambda c: -(c["judge"]["n"]))

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print("写入", args.out)
    print(f"  clean {len(ok_rows)} / contaminated {len(dirty)} / skipped "
          f"{out['run']['skipped']}")
    print("  headline(clean):", {k: v["pct"] for k, v in out["headline"]["clean"].items()})


if __name__ == "__main__":
    main()
