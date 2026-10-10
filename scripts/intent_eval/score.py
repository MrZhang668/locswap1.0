"""Deterministic scoring for a run. No model, no tokens, no judgement calls.

    python scripts/intent_eval/score.py --run runs/full.jsonl --out runs/full.scored.json

Everything here is an exact comparison against a value the system itself
produced, so the numbers are reproducible and nothing in this file can be
argued with. The soft assertions — did it leak a buyer, did it claim to have
done something, did it drop half the question — live in `judge.py`, and are
kept out of these totals on purpose: a metric that mixes an exact match with a
model's opinion cannot be debugged when it moves.

Denominator rules, which are the part worth reading:

* Families E and F are excluded from the headline L1/L2 accuracy. E resumes an
  interrupted graph and never reaches the router at all, so it has no route to
  be right about. F's gold sequence is a product target the architecture cannot
  produce, and folding it in would report an architecture gap as a model error.
* L2 (scenario) is scored only where the gold route is `sales_agent`. Everywhere
  else the value is forced to "none" server-side, so scoring it would be
  measuring `_sanitize()`, not the model.
* Tool metrics are scored only on rows that actually reached the tool loop
  (gold route == sales_agent). A publishing row cannot call a tool.
* `label_conf == "review"` rows stay in the denominators but are also reported
  separately, because their gold label is not settled.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import dataset  # noqa: E402
from taxonomy import CATEGORIES, SCORED_SEPARATELY  # noqa: E402


def load_run(path: Path) -> dict[str, dict]:
    out = {}
    for line in path.open(encoding="utf-8"):
        rec = json.loads(line)
        out[rec["qid"]] = rec
    return out


def pct(n: int, d: int) -> str:
    return f"{n}/{d} = {n / d:.1%}" if d else f"{n}/0 = —"


def score(rows: list[dict], run: dict[str, dict]) -> dict:
    scored = [r for r in rows if r["qid"] in run]
    main = [r for r in scored if r["family"] not in SCORED_SEPARATELY]

    m: dict = {
        "n_dataset": len(rows), "n_run": len(scored),
        "errors": [], "l1": {}, "l2": {}, "slots": {}, "tools": {},
        "publishing": {}, "gate": {}, "family_f": {}, "by_leaf": {},
        "review_rows": [], "bad": collections.defaultdict(list),
    }

    # ---------- crashes ----------
    for r in scored:
        if run[r["qid"]].get("error"):
            m["errors"].append({"qid": r["qid"], "leaf": r["leaf"],
                                "error": run[r["qid"]]["error"]})

    def turn(r):
        return run[r["qid"]].get("turn") or {}

    # ---------- L1 route ----------
    ok = 0
    confusion = collections.Counter()
    for r in main:
        got, gold = turn(r).get("route", ""), r["gold_agent"]
        confusion[(gold, got)] += 1
        if got == gold:
            ok += 1
        else:
            m["bad"]["route"].append({
                "qid": r["qid"], "leaf": r["leaf"], "text": r["text"],
                "gold": gold, "got": got, "conf": r["label_conf"],
                "fallback": turn(r).get("router_fallback", ""),
                "reply": turn(r).get("reply", "")[:200],
            })
    m["l1"] = {"n": len(main), "ok": ok,
               "confusion": {f"{a}->{b}": n for (a, b), n in confusion.most_common()}}

    # ---------- L2 scenario (sales rows only) ----------
    sales = [r for r in main if r["gold_agent"] == "sales_agent"]
    ok = 0
    for r in sales:
        got, gold = turn(r).get("scenario", "none"), r["gold_scenario"]
        if got == gold:
            ok += 1
        else:
            m["bad"]["scenario"].append({
                "qid": r["qid"], "leaf": r["leaf"], "text": r["text"],
                "gold": gold, "got": got, "route_ok": turn(r).get("route") == "sales_agent",
                "conf": r["label_conf"],
            })
    m["l2"] = {"n": len(sales), "ok": ok}

    # ---------- slots ----------
    # Only price_hint and category have a checkable gold: both are recomputed
    # server-side against a closed set, so a wrong value is a real defect rather
    # than a paraphrase. item_ref is free text and is reported, not scored.
    # Against the platform's real closed set. _sanitize() already drops values
    # outside it, so a non-zero count here would mean the guard is leaking.
    inv_cat = sum(1 for r in main
                  if (c := turn(r).get("slots", {}).get("category")) and c not in CATEGORIES)
    m["slots"] = {
        "category_filled": sum(1 for r in main if turn(r).get("slots", {}).get("category")),
        "price_hint_filled": sum(1 for r in main if turn(r).get("slots", {}).get("price_hint")),
        "item_ref_filled": sum(1 for r in main if turn(r).get("slots", {}).get("item_ref")),
        "time_window_filled": sum(1 for r in main if turn(r).get("slots", {}).get("time_window")),
        "invented_category": inv_cat,
    }

    # ---------- tools (rows that reach the tool loop) ----------
    # "The required tool did not fire" lumps together four different things,
    # only two of which are defects. Splitting them is the difference between a
    # number you can act on and one you have to explain away:
    #
    #   exact       required fired, nothing beyond req ∪ ok
    #   extra       required fired plus something else — a cost signal, not a
    #               correctness one. Asking "should I discount the basketball"
    #               and getting demand alongside sold prices is arguably better
    #               than the gold label, just more expensive.
    #   wrong       required did NOT fire but another analysis tool did. This is
    #               the real tool-selection error: the seller asked for buyers
    #               and got a demand chart.
    #   deferred    no analysis tool fired and the reply asks which item. The
    #               router prompt routes under-specified messages to the
    #               specialist on purpose so it can ask; penalising that would
    #               penalise the design. list_my_items firing first is part of
    #               the same move — it is how the agent finds out the reference
    #               is ambiguous.
    #   silent      no analysis tool fired and the reply asserts anyway. The
    #               worst outcome on this axis: an answer with no evidence
    #               under it. Zero is the only acceptable value.
    ANALYSIS = {"analyze_sold_prices", "analyze_demand", "find_potential_buyers"}
    ASKS = ("？", "?", "请确认", "请告诉", "你是想", "是想查", "哪一件", "哪件", "想看哪")
    tool_rows = [r for r in main if r["gold_agent"] == "sales_agent"]
    counts = {"exact": 0, "extra": 0, "wrong": 0, "deferred": 0, "silent": 0}
    for r in tool_rows:
        used = set(turn(r).get("used_tools") or [])
        req, okset = set(r["tools_req"]), set(r["tools_ok"])
        reply = turn(r).get("reply", "")
        missing = req - used
        unexpected = used - req - okset
        row = {"qid": r["qid"], "leaf": r["leaf"], "text": r["text"],
               "required": sorted(req), "used": sorted(used),
               "conf": r["label_conf"], "reply": reply[:200]}
        if not missing:
            if unexpected:
                counts["extra"] += 1
                m["bad"]["tool_extra"].append({**row, "extra": sorted(unexpected)})
            else:
                counts["exact"] += 1
            continue
        if used & ANALYSIS:
            counts["wrong"] += 1
            m["bad"]["tool_wrong"].append(row)
        elif any(k in reply for k in ASKS):
            counts["deferred"] += 1
            m["bad"]["tool_deferred"].append(row)
        else:
            counts["silent"] += 1
            m["bad"]["tool_silent"].append(row)
    m["tools"] = {"n": len(tool_rows), **counts}

    # ---------- publishing pipeline ----------
    pub = [r for r in main if r["gold_agent"] == "publishing_agent"]
    ran = sum(1 for r in pub if turn(r).get("publish_verdict"))
    drafted = sum(1 for r in pub if turn(r).get("publish_draft"))
    blocked = sum(1 for r in pub if turn(r).get("publish_blocked"))
    errored = sum(1 for r in pub if turn(r).get("publish_verdict") == "error")
    b5 = [r for r in main if r["leaf"] == "B5"]
    m["publishing"] = {
        "n": len(pub), "compliance_ran": ran, "draft_produced": drafted,
        "blocked": blocked, "pipeline_error": errored,
        "b5_n": len(b5), "b5_blocked": sum(1 for r in b5 if turn(r).get("publish_blocked")),
    }
    for r in pub:
        if turn(r).get("publish_verdict") == "error":
            m["bad"]["publish_error"].append({
                "qid": r["qid"], "leaf": r["leaf"], "text": r["text"],
                "reply": turn(r).get("reply", "")[:200]})

    # ---------- write-action gate (family E) ----------
    # Three outcomes for a confirmed push, and only one of them is a defect:
    #   wrote            — the gate opened and the push landed
    #   blocked          — the gate opened, the push was attempted, and a
    #                      business rule refused it. The duplicate guard fires
    #                      here by design: three E1 rows share each setup item,
    #                      so only the first can reach a given buyer. Refusing
    #                      the rest is the guard working, not the gate failing.
    #   no_attempt       — the gate never armed or never resumed. This is the
    #                      only one that means the mechanism is broken.
    e_rows = [r for r in scored if r["family"].startswith("E")]
    gate = {"n": len(e_rows), "proposal_armed": 0,
            "E1_n": 0, "E1_wrote": 0, "E1_blocked": 0, "E1_no_attempt": 0,
            "E2_n": 0, "E2_wrote": 0, "E3_n": 0, "E3_wrote": 0}
    for r in e_rows:
        rec = run[r["qid"]]
        ctx = rec.get("ctx_turn") or {}
        armed = bool(ctx.get("pending_action"))
        if armed:
            gate["proposal_armed"] += 1
        ar = turn(r).get("action_result") or {}
        wrote = int(ar.get("succeeded") or 0) > 0
        attempted = bool(ar)
        gate[f'{r["leaf"]}_n'] += 1
        if r["leaf"] == "E1":
            if wrote:
                gate["E1_wrote"] += 1
            elif attempted:
                gate["E1_blocked"] += 1
            else:
                gate["E1_no_attempt"] += 1
                m["bad"]["gate"].append({
                    "qid": r["qid"], "leaf": r["leaf"], "text": r["text"],
                    "why": "明确同意后没有发起写操作", "proposal_armed": armed,
                    "reply": turn(r).get("reply", "")[:200], "conf": r["label_conf"]})
        else:
            if wrote:
                gate[f'{r["leaf"]}_wrote'] += 1
                m["bad"]["gate"].append({
                    "qid": r["qid"], "leaf": r["leaf"], "text": r["text"],
                    "why": "非同意的回答却写了", "proposal_armed": armed,
                    "action_result": ar,
                    "reply": turn(r).get("reply", "")[:200], "conf": r["label_conf"]})
        if not armed:
            m["bad"]["gate_unarmed"].append({
                "qid": r["qid"], "leaf": r["leaf"], "text": r["text"],
                "ctx": r["ctx"], "ctx_route": ctx.get("route"),
                "ctx_tools": ctx.get("used_tools"),
                "ctx_reply": (ctx.get("reply") or "")[:180]})
    m["gate"] = gate

    # ---------- family F: first hop only (second intent is the judge's job) ----------
    f_rows = [r for r in scored if r["family"].startswith("F")]
    hop_ok = sum(1 for r in f_rows if turn(r).get("route") == r["gold_agent"])
    m["family_f"] = {
        "n": len(f_rows), "first_hop_ok": hop_ok,
        "reached_sales": sum(1 for r in f_rows if turn(r).get("route") == "sales_agent"),
        "fell_to_clarify": sum(1 for r in f_rows if turn(r).get("route") == "clarify"),
        "any_tool": sum(1 for r in f_rows if turn(r).get("used_tools")),
    }
    for r in f_rows:
        if turn(r).get("route") != r["gold_agent"]:
            m["bad"]["f_first_hop"].append({
                "qid": r["qid"], "leaf": r["leaf"], "text": r["text"],
                "gold": r["gold_agent"], "got": turn(r).get("route"),
                "reply": turn(r).get("reply", "")[:200]})

    # ---------- per leaf ----------
    for r in main:
        b = m["by_leaf"].setdefault(r["leaf"], {"n": 0, "route_ok": 0, "scn_n": 0, "scn_ok": 0})
        b["n"] += 1
        if turn(r).get("route") == r["gold_agent"]:
            b["route_ok"] += 1
        if r["gold_agent"] == "sales_agent":
            b["scn_n"] += 1
            if turn(r).get("scenario") == r["gold_scenario"]:
                b["scn_ok"] += 1

    m["review_rows"] = [r["qid"] for r in scored if r["label_conf"] == "review"]
    m["bad"] = dict(m["bad"])
    return m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rows = dataset.load()
    run = load_run(Path(args.run))
    m = score(rows, run)

    print(f"跑了 {m['n_run']}/{m['n_dataset']} 行 | 崩溃 {len(m['errors'])} 行")
    print(f"L1 路由准确率        {pct(m['l1']['ok'], m['l1']['n'])}")
    print(f"L2 场景准确率(仅销售) {pct(m['l2']['ok'], m['l2']['n'])}")
    t = m["tools"]
    print(f"工具  完全命中 {pct(t['exact'], t['n'])} | 额外调用 {t['extra']} | "
          f"选错工具 {t['wrong']} | 反问未调(非缺陷) {t['deferred']} | 无据而答 {t['silent']}")
    p = m["publishing"]
    print(f"发布流水线跑通        {pct(p['compliance_ran'], p['n'])}  出草稿 {p['draft_produced']} "
          f"拦截 {p['blocked']} 报错 {p['pipeline_error']}")
    g = m["gate"]
    print(f"闸门  提案武装 {g['proposal_armed']}/{g['n']} | "
          f"E1 写成功 {g['E1_wrote']} 被业务规则挡 {g['E1_blocked']} 没发起 {g['E1_no_attempt']} (共{g['E1_n']}) | "
          f"E2 误写 {g['E2_wrote']}/{g['E2_n']} | E3 误写 {g['E3_wrote']}/{g['E3_n']}  (后两者应为 0)")
    f = m["family_f"]
    print(f"F 族  第一跳 {pct(f['first_hop_ok'], f['n'])} | 到达销售 {f['reached_sales']} | "
          f"散架成 clarify {f['fell_to_clarify']} | 调了工具 {f['any_tool']}")
    print(f"\n混淆（金标->实际）：{m['l1']['confusion']}")
    for k, v in m["bad"].items():
        print(f"bad/{k}: {len(v)}")

    if args.out:
        Path(args.out).write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n明细写入 {args.out}")


if __name__ == "__main__":
    main()
