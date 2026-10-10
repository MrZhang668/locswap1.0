"""Run the 530-query set through the live agent graph and record what happened.

    python scripts/intent_eval/run_eval.py --limit 20 --out runs/smoke.jsonl
    python scripts/intent_eval/run_eval.py --out runs/full.jsonl

In-process, not over HTTP. Two reasons, both of which would otherwise cost
coverage:

* `scenario` and `slots` exist only in the graph's state — `AdvisorChatOut` does
  not carry them. Over HTTP the whole L2 axis would be unmeasurable.
* The push-candidate cache that `find_potential_buyers` fills is per-process.
  Driving family E from a separate process means every confirmed push resolves
  against an empty cache and fails for a reason that has nothing to do with the
  gate being tested.

Nothing here modifies the system under test. It imports `run_turn` and reads the
state it returns, the same way the HTTP layer does.

Side effects to know about before running:
* family E writes real pushes to buyers (`want_pushes` rows + buyer notices);
* every row appends a thread to `data/advisor_threads.sqlite`;
* every LLM call appends a row to `token_usage_log`, which is how the cost
  report is computed afterwards — so do not clear that table mid-run.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))            # the dataset parts import each other flatly
sys.path.insert(0, str(_HERE.parent))     # scripts/_bootstrap

import _bootstrap  # noqa: E402,F401  puts backend/ on sys.path

from sqlalchemy import select  # noqa: E402

import dataset  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import TokenUsageLog, User  # noqa: E402
from app.services.agents import run_turn, sessions  # noqa: E402
from taxonomy import SELLER_PROFILES  # noqa: E402


def seller_ids() -> dict[str, int]:
    """Resolve the three test sellers once; fail loudly if the DB drifted."""
    out: dict[str, int] = {}
    with SessionLocal() as db:
        for key, spec in SELLER_PROFILES.items():
            uid = db.scalar(select(User.id).where(User.email == spec["email"]))
            if uid is None:
                raise SystemExit(f"测试卖家不存在：{spec['email']}（数据集假设它在库里）")
            out[key] = int(uid)
    return out


def _capture(state: dict) -> dict:
    """The observable surface of one turn, flattened for scoring.

    Deliberately includes the tool *outputs* and not just their names: two of
    the judge's assertions (trend faithfulness, invented prices) are checked
    against what the backend actually returned, and re-deriving that later from
    the reply is exactly the mistake those assertions exist to catch.
    """
    result = state.get("result") or {}
    interrupts = state.get("__interrupt__") or []
    pending = state.get("pending_action") or {}
    if not pending and interrupts:
        value = getattr(interrupts[0], "value", None)
        if isinstance(value, dict):
            pending = value
    return {
        "route": state.get("route", ""),
        "scenario": state.get("scenario", "none"),
        "slots": state.get("slots") or {},
        "router_fallback": state.get("router_fallback", ""),
        "reply": result.get("reply", ""),
        "ok": bool(result.get("ok", False)),
        "used_tools": result.get("used_tools") or [],
        "no_active_items": bool(result.get("no_active_items")),
        # publishing pipeline
        "publish_verdict": state.get("publish_verdict", ""),
        "publish_findings": state.get("publish_findings") or [],
        "publish_draft": state.get("publish_draft") or {},
        "publish_blocked": bool(result.get("publish_blocked")),
        # write-action gate
        "pending_action": pending,
        "action_result": state.get("action_result") or {},
        # tool payloads kept as evidence for the judge
        "pricing_analysis": result.get("pricing_analysis") or [],
        "demand_analysis": result.get("demand_analysis") or [],
        "potential_buyers": result.get("potential_buyers") or [],
        "tokens_in": int(state.get("router_in") or 0) + int(result.get("input_tokens") or 0),
        "tokens_out": int(state.get("router_out") or 0) + int(result.get("output_tokens") or 0),
    }


def run_row(row: dict, uid: int) -> dict:
    """One dataset row = one fresh thread. A context turn is sent first, in the
    same thread, and recorded separately — for family E that first turn is what
    produces the proposal the row is actually testing."""
    thread_id = sessions.issue_thread_id(uid)
    rec: dict = {"qid": row["qid"], "thread_id": thread_id, "seller": row["seller"]}
    t0 = time.time()
    with SessionLocal() as db:
        try:
            if row["ctx"]:
                ctx_state = run_turn(db, uid, row["ctx"], thread_id, "")
                rec["ctx_turn"] = _capture(ctx_state)
            state = run_turn(db, uid, row["text"], thread_id, row["photo"] or "")
            rec["turn"] = _capture(state)
            db.commit()
        except Exception as exc:  # noqa: BLE001  a crashed row is a result, not a stop
            db.rollback()
            rec["error"] = f"{type(exc).__name__}: {exc}"
            rec["traceback"] = traceback.format_exc()[-1500:]
    rec["seconds"] = round(time.time() - t0, 2)
    return rec


def token_watermark() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(TokenUsageLog.id).order_by(TokenUsageLog.id.desc())) or 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 行（0 = 全部）")
    ap.add_argument("--only", default="", help="只跑这些族，逗号分隔，如 A,F")
    ap.add_argument("--qids", default="", help="只跑这些 qid，逗号分隔")
    ap.add_argument("--out", required=True, help="JSONL 输出路径")
    args = ap.parse_args()

    rows = dataset.load()
    if args.only:
        fams = tuple(f.strip() for f in args.only.split(","))
        rows = [r for r in rows if r["family"][0] in fams]
    if args.qids:
        want = {q.strip() for q in args.qids.split(",")}
        rows = [r for r in rows if r["qid"] in want]
    if args.limit:
        rows = rows[: args.limit]

    ids = seller_ids()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    start_mark = token_watermark()

    print(f"跑 {len(rows)} 行 -> {out}")
    t_start = time.time()
    with out.open("w", encoding="utf-8") as fh:
        for n, row in enumerate(rows, start=1):
            rec = run_row(row, ids[row["seller"]])
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            turn = rec.get("turn") or {}
            flag = "ERR" if rec.get("error") else turn.get("route", "?")
            print(f"  [{n}/{len(rows)}] {row['qid']} {row['leaf']:>3} "
                  f"{flag:<17} {rec['seconds']:>5.1f}s  {row['text'][:34]}")

    with SessionLocal() as db:
        logs = db.scalars(
            select(TokenUsageLog).where(TokenUsageLog.id > start_mark)
        ).all()
    tin = sum(x.input_tokens for x in logs)
    tout = sum(x.output_tokens for x in logs)
    by_site: dict[str, int] = {}
    for x in logs:
        by_site[x.call_site] = by_site.get(x.call_site, 0) + 1
    print(f"\n用时 {time.time() - t_start:.0f}s | LLM 调用 {len(logs)} 次 | "
          f"输入 {tin:,} tok | 输出 {tout:,} tok")
    for site, n in sorted(by_site.items()):
        print(f"   {site:<28} {n:>4} 次")
    if rows:
        print(f"   每行均摊：{len(logs) / len(rows):.1f} 次调用，"
              f"{(tin + tout) / len(rows):,.0f} tok")


if __name__ == "__main__":
    main()
