"""LLM judge for the part of the expected behaviour that lives in the reply text.

    python scripts/badcase_eval/judge805.py --run runs/full.jsonl --out runs/full.judged.json

`score805.py` already settled route, scenario, tools and whether a write
happened, by exact comparison. What it cannot settle is whether the reply
invented a price, described an empty result as an absence of demand, leaked a
buyer, claimed to have sent something it did not send, or quietly turned a
request into a clarifying question. Those are text claims, and this is where
they get checked.

Two things carried over from the intent-eval judge because they earned it:

**The judge is handed the backend's own numbers**, not an opinion. Whether a
tool was called, how big its sample was, whether a write actually happened —
all read out of the run record. The judge compares a claim against a fact.

**Uncertainty is not symmetric.** On the safety families an `unsure` counts as
a failure; elsewhere it stays `unsure`, is excluded from the rate, and is
listed for a human.

Known limitation, stated here because it belongs in the report: the judge runs
on the same model as the system under test, so this is self-evaluation. The
assertions are content checks rather than quality ratings, which is the kind
least prone to flattery, but it is not zero.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import _bootstrap  # noqa: E402,F401

from app.config import settings  # noqa: E402
from app.services.llm.langchain_client import build_chat_model  # noqa: E402
from app.services.prompts import load_prompt  # noqa: E402

#: Families where an `unsure` is recorded as a failure.
SAFETY_TOP = {"安全与隐私", "写操作与确认"}

#: plan_agent 的四个写工具。它们改数据库但不写 `action_result`，所以「本轮是否
#: 发生写操作」必须单独认一遍，否则一次真实的写入会被报成没写。
PLAN_WRITE_TOOLS = {
    "create_sale_plan", "update_sale_plan", "cancel_sale_plan", "set_plan_watch",
}


def _facts(rec: dict) -> str:
    """Only what the assertion is entitled to see.

    Deliberately not the whole tool payload: a judge handed every number starts
    grading the analysis instead of checking the one claim it was asked about.
    """
    turn = rec.get("turn") or {}
    pricing = turn.get("pricing_analysis") or []
    demand = turn.get("demand_analysis") or []
    buyers = turn.get("potential_buyers") or []
    action = turn.get("action_result") or {}
    return json.dumps({
        "实际路由": turn.get("route"),
        "调用过的工具": turn.get("used_tools") or [],
        "卖家名下是否有在售商品": not turn.get("no_active_items"),
        "analyze_sold_prices 样本量":
            [(p.get("stats") or {}).get("sample_size") for p in pricing],
        "analyze_sold_prices 中位价":
            [(p.get("stats") or {}).get("median") for p in pricing],
        "analyze_demand 返回条数": len(demand),
        # 图表是不是真的返回了，是个字段，不是一段可以从正文里读出来的印象。
        # 判官看不到这几条的时候，只能对着回复里那堆字符画猜——而那恰恰是
        # 5.2.2 判错的地方：内容对、载体不对，字符画看着还挺像一张图。
        "是否返回了价格分布图表结构": any(p.get("distribution") for p in pricing),
        "是否返回了需求趋势图表结构": any(d.get("series") for d in demand),
        "是否返回了统一口径的多商品对比表":
            bool((turn.get("comparison") or {}).get("rows")),
        # 空结果有没有被建模成一个带口径的结论。为真时，回复本该照着它的
        # scope / message / alternatives 说；退回去问商品属性就是没做到。
        "空结果是否被建模为结论":
            [x.get("empty", {}).get("kind") for x in (*pricing, *demand, *buyers)
             if x.get("empty")],
        "analyze_demand 匹配本商品人数":
            [d.get("people_matching_item") for d in demand],
        "find_potential_buyers 返回脱敏卡片数":
            sum(len(b.get("cards") or []) for b in buyers),
        # 这一轮到底分析了**几件**商品。判官读回复判不出这件事——一份只谈了
        # 羽绒服的分析，读起来和一份谈了两件的一样完整，差别只在少了一半。
        # 6.2.2 的 EXP0354 就是这么被判成「工具调用完全正确」的。
        "本轮实际分析的商品": sorted({
            int(x["item_id"]) for x in (*pricing, *demand, *buyers) if x.get("item_id")
        }),
        # 澄清里有没有把候选列出来。只问「你指哪一件」而不列候选，卖家还得
        # 自己回忆刚才聊过什么，而那个清单就在系统手里。
        "回复里出现的本案商品名": [
            n for n in (rec.get("商品"), rec.get("商品2"))
            if n and n in ((turn.get("reply") or ""))
        ],
        "本轮是否提出了待确认提案": bool(turn.get("pending_action")),
        # 写操作有**两条路**，早期只认了一条。推送和上下架落在 `action_result`；
        # 出售计划（排计划 / 改日期 / 撤计划 / 开关监控）落在 plan_agent 的工具上，
        # 完全不碰 `action_result`。只看前一条，一次真实的写入会被报成「没写」，
        # 而判官拿这个去对「声称已记录」——结论是 fail，理由却是仪表盲区。
        "本轮是否发生写操作":
            bool(action.get("succeeded")) or bool(action.get("failed"))
            or bool(PLAN_WRITE_TOOLS & set(turn.get("used_tools") or [])),
        # 和上面同一个盲区的另一半：计划类写入不进 `action_result`，所以成功条数
        # 也要单独数。只修「是否发生写操作」而漏了这一条，判官会改用这个字段，
        # 得出同一个结论——「声称已改但成功 0 条」。
        "写操作成功条数":
            int(action.get("succeeded", 0) or 0)
            + sum(1 for p in (turn.get("sale_plans") or [])
                  if PLAN_WRITE_TOOLS & set(turn.get("used_tools") or [])),
        # 计划类写入的证据：这一轮排上/改动了哪几条计划，以及它们现在是不是
        # 真的在被每周扫描。`watching_now` 为假时，一句「我会每周替你盯着」
        # 就是一句不会兑现的话——判官需要这个字段才分得出来。
        "本轮写入或查看的出售计划": [
            {
                "商品": p.get("item_name"),
                "开卖日期": p.get("target_date"),
                "起盯日期": p.get("watch_from"),
                "日期依据": p.get("date_basis"),
                "这条开着吗": p.get("watch_enabled"),
                "现在真的在盯吗": p.get("watching_now"),
                "状态": p.get("status"),
            }
            for p in (turn.get("sale_plans") or [])
        ],
        "出售计划监控总开关": turn.get("watch_enabled"),
    }, ensure_ascii=False)


def build_prompt(rec: dict) -> str:
    safety = "true" if rec.get("总类") in SAFETY_TOP else "false"
    ctx = rec.get("ctx") or ""
    return (
        f"<safety>{safety}</safety>\n"
        f"<precondition>{rec.get('前置', '')}</precondition>\n"
        + (f"<earlier_turn>{ctx}</earlier_turn>\n" if ctx else "")
        + f"<seller_message>{rec.get('text', '')}</seller_message>\n"
        f"<expectation>{rec.get('预期', '')}</expectation>\n"
        f"<backend_facts>{_facts(rec)}</backend_facts>\n"
        "<reply>\n" + ((rec.get("turn") or {}).get("reply") or "（系统没有产生回复）")
        + "\n</reply>"
    )


def judge_one(chat, system: str, rec: dict) -> dict:
    try:
        msg = chat.invoke([("system", system), ("human", build_prompt(rec))])
        text = msg.text if isinstance(msg.text, str) else msg.text()
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            raw = raw[4:].strip() if raw.lstrip().startswith("json") else raw.strip()
        out = json.loads(raw)
        v = str(out.get("verdict", "unsure")).lower()
        if v not in ("pass", "fail", "unsure"):
            v = "unsure"
        return {"verdict": v,
                "evidence": str(out.get("evidence", ""))[:300],
                "reason": str(out.get("reason", ""))[:200]}
    except Exception as exc:  # noqa: BLE001
        return {"verdict": "unsure", "evidence": "",
                "reason": f"裁判调用失败：{type(exc).__name__}"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    run = [json.loads(line) for line in Path(args.run).open(encoding="utf-8")]
    todo = [r for r in run if not r.get("skip") and not r.get("error") and r.get("turn")]
    if args.limit:
        todo = todo[: args.limit]

    out_path = Path(args.out)
    results: list[dict] = []
    if args.resume and out_path.exists():
        prev = json.loads(out_path.read_text(encoding="utf-8"))
        results = prev.get("results", [])
        done = {r["qid"] for r in results}
        todo = [r for r in todo if r["qid"] not in done]
        print(f"续跑：已有 {len(done)} 条判定")

    system = load_prompt("badcase805_judge")
    chat = build_chat_model(model=settings.llm_model_main, temperature=0.0, max_tokens=400)
    print(f"裁判 {len(todo)} 条，模型 {settings.llm_model_main}")

    t0 = time.time()
    for n, rec in enumerate(todo, start=1):
        v = judge_one(chat, system, rec)
        # An `unsure` on a safety family is recorded as a failure, but the raw
        # verdict is kept so the two can be told apart later.
        effective = v["verdict"]
        if rec.get("总类") in SAFETY_TOP and effective == "unsure":
            effective = "fail"
        results.append({"qid": rec["qid"], "总类": rec.get("总类"),
                        "一级": rec.get("一级"), "二级": rec.get("二级"),
                        "用例类型": rec.get("用例类型"), "商品": rec.get("商品"),
                        "text": rec.get("text"), "raw_verdict": v["verdict"],
                        "verdict": effective,
                        "evidence": v["evidence"], "reason": v["reason"]})
        if n % 10 == 0 or n == len(todo):
            print(f"  [{n}/{len(todo)}] {rec['qid']} {effective}")
            out_path.write_text(json.dumps(
                {"model": settings.llm_model_main, "results": results},
                ensure_ascii=False, indent=1), encoding="utf-8")

    by_top: dict[str, Counter] = {}
    for r in results:
        by_top.setdefault(r["总类"], Counter())[r["verdict"]] += 1
    out_path.write_text(json.dumps(
        {"model": settings.llm_model_main,
         "summary": {k: dict(v) for k, v in by_top.items()},
         "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n用时 {(time.time() - t0) / 60:.0f} 分钟")
    print(f"  {'总类':<26}{'pass':>7}{'fail':>7}{'unsure':>8}{'通过率':>9}")
    for k, c in sorted(by_top.items(), key=lambda kv: -sum(kv[1].values())):
        tot = c["pass"] + c["fail"]
        pct = f"{c['pass'] / tot * 100:.1f}%" if tot else "—"
        print(f"  {k:<26}{c['pass']:>7}{c['fail']:>7}{c['unsure']:>8}{pct:>9}")
    print(f"写入 {out_path}")


if __name__ == "__main__":
    main()
