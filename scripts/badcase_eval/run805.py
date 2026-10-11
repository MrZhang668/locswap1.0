"""Drive the expanded case set through the live agent graph.

    python scripts/badcase_eval/run805.py --limit 20 --out runs/smoke.jsonl
    python scripts/badcase_eval/run805.py --out runs/full.jsonl

In-process for the same two reasons the intent-eval runner is: `scenario` and
`slots` live only in graph state and never reach `AdvisorChatOut`, and the
push-candidate cache that `find_potential_buyers` fills is per-process — driving
the write-action families from a separate process would resolve every confirmed
push against an empty cache and fail for a reason unrelated to the gate.

Side effects worth knowing before starting:
* the write-action families send real pushes to the seeded buyers;
* every row appends a thread to data/advisor_threads.sqlite;
* every LLM call appends a token_usage_log row, which is where the cost figure
  at the end comes from.

Skipped rows are written to the output too, carrying their reason and no turn.
They are part of the record: a case nobody could run is not a case that passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import _bootstrap  # noqa: E402,F401

from sqlalchemy import select  # noqa: E402

import dataset805  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import TokenUsageLog, User  # noqa: E402
from app.services.agents import sessions  # noqa: E402
from app.services.agents.graph import run_turn  # noqa: E402


def seller_ids(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    with SessionLocal() as db:
        for email in sorted({r["seller"] for r in rows}):
            uid = db.scalar(select(User.id).where(User.email == email))
            if uid is None:
                raise SystemExit(f"评测卖家不存在：{email}（先跑 seed_eval_items.py --apply）")
            out[email] = int(uid)
    return out


def _capture(state: dict) -> dict:
    """The observable surface of one turn, flattened for scoring.

    Tool *outputs* are kept, not just the names: whether a price was invented
    and whether an empty result was described as an empty result can only be
    checked against what the backend actually returned.
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
        "publish_verdict": state.get("publish_verdict", ""),
        "publish_blocked": bool(result.get("publish_blocked")),
        "pending_action": pending,
        "action_result": state.get("action_result") or {},
        "pricing_analysis": result.get("pricing_analysis") or [],
        "demand_analysis": result.get("demand_analysis") or [],
        # The server-built comparison table. Kept because "did the two listings
        # get described on the same axes" is now a field, not a judgement call
        # about two paragraphs of prose.
        "comparison": result.get("comparison") or None,
        "potential_buyers": result.get("potential_buyers") or [],
        # 出售计划这一族的写操作**不走 `action_result`**（那条路是推送和上下架
        # 专用的），所以不把这两个字段留下来，判官就看不到「这一轮到底有没有
        # 真的排上计划」——只能对着回复里那句「已记下」猜，而那正是它要验的东西。
        # v3 里 6 行被判成「声称已记录但后端未发生写操作」，实际上 SalePlan 那一行
        # 写进去了，只是没有任何字段承载它。
        "sale_plans": result.get("sale_plans") or ([result["sale_plan"]] if result.get("sale_plan") else []),
        "watch_enabled": result.get("watch_enabled"),
        "tokens_in": int(state.get("router_in") or 0) + int(result.get("input_tokens") or 0),
        "tokens_out": int(state.get("router_out") or 0) + int(result.get("output_tokens") or 0),
    }


def stage_listing(db, uid: int, row: dict) -> int:
    """在前置轮和被测轮之间，真的把一件商品上架。

    交接类用例（「上传好了」）的前置是「系统让卖家先去上架」。没有这一步，
    那句「上传好了」在库里对应不到任何新商品，`handoff.resume_target` 找不到
    东西可接，于是它退回去重新路由——而一句「上传好了」里没有任何可路由的
    信息，结果就是一次反问。原始报告把这读成「模型丢失了上一轮的商品与任务」，
    实际上接力机制从来没有被触发过。

    走 `ItemIndexService.create_item`（HTTP 层用的同一个入口），向量和 BM25
    索引才有文档；直接插 Item 行会让 `find_potential_buyers` 量到一个空索引。
    """
    from app.services.item_index import get_item_index
    from app.models import ItemStatus

    name, cat = row["商品"], row["品类"]
    item = get_item_index().create_item(
        db, seller_id=uid, name=name,
        summary=f"{name}，九成新，校内自提。",
        description=f"二手{name}（{cat}）。九成新，功能正常，校内当面交易。",
        attributes={"category": cat, "condition": "九成新"},
        price=120.0, photo_path=None,
        pickup_location_name="Homewood 校区", lat=39.3299, lng=-76.6205,
        pickup_time_start="工作日晚上", pickup_time_end="周末下午",
        status=ItemStatus.SELLING,
    )
    db.commit()
    return int(item.id)


def unstage_listing(item_id: int) -> None:
    """把 stage 建出来的商品删掉。

    跨行不清理的话，一个 `bc-study` 账号会在六行之后多出六件别的品类的商品，
    而后面每一行的 `<my_listings>` 和 `list_my_items` 都会看到它们——夹具的
    副作用会顺着库存漏进无关用例的断言里。
    """
    from app.models import Item

    with SessionLocal() as db:
        row = db.get(Item, int(item_id))
        if row is not None:
            db.delete(row)
            db.commit()


def run_row(row: dict, uid: int) -> dict:
    thread_id = sessions.issue_thread_id(uid)
    rec: dict = {k: row[k] for k in
                 ("qid", "总类", "一级", "二级", "用例类型", "品类", "商品",
                  # 第二件商品要写进记录：判官和评分器都要用它判「澄清里有没有
                  # 把候选列出来」「两件是不是都做了」。不写的话这两条断言会在
                  # 一个永远取不到值的字段上恒为假。
                  "商品2",
                  "前置", "text", "ctx", "photo", "seller", "预期",
                  "路由原文", "scen原文", "工具原文")}
    rec.update(thread_id=thread_id,
               ctx_turns=list(row.get("ctx_turns") or []),
               stage=row.get("stage") or "",
               fixture_note=row.get("fixture_note") or "",
               gold_routes=list(row["gold_routes"]),
               gold_scenarios=list(row["gold_scenarios"]),
               gold_tools_req=list(row["gold_tools_req"]),
               gold_tools_empty=row["gold_tools_empty"])
    t0 = time.time()
    staged: int | None = None
    with SessionLocal() as db:
        try:
            # 前置可以是**多轮**。「卖家名下同时在售 X 和 Y」这个前置只有在
            # 对话里两件都被谈过时才成立——只谈一件的话，下一句「那个再看一下」
            # 里的「那个」刚被指定过，绑对了反而说明夹具没造出歧义来。
            ctx_out = []
            for line in (row.get("ctx_turns") or []):
                ctx_out.append(_capture(run_turn(db, uid, line, thread_id, "")))
            if ctx_out:
                rec["ctx_turns_out"] = ctx_out
                # 旧字段保留：既有的看板和判官都读它，指的是**最后**一轮前置。
                rec["ctx_turn"] = ctx_out[-1]
            if row.get("stage") == "list_item_between":
                staged = stage_listing(db, uid, row)
                rec["staged_item_id"] = staged
            # The photo rides on the turn under test, not the context turn:
            # every image case is "the seller sends this picture and says X".
            rec["turn"] = _capture(
                run_turn(db, uid, row["text"], thread_id, row.get("photo") or ""))
            db.commit()
        except Exception as exc:  # noqa: BLE001  a crashed row is a result, not a stop
            db.rollback()
            rec["error"] = f"{type(exc).__name__}: {exc}"
            rec["traceback"] = traceback.format_exc()[-1500:]
    if staged is not None:
        try:
            unstage_listing(staged)
        except Exception as exc:  # noqa: BLE001  清理失败是记录，不是中断
            rec["stage_cleanup_error"] = f"{type(exc).__name__}: {exc}"
    rec["seconds"] = round(time.time() - t0, 2)
    return rec


def token_watermark() -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(TokenUsageLog.id).order_by(TokenUsageLog.id.desc())) or 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="", help="只跑这些总类，逗号分隔")
    ap.add_argument("--qids", default="")
    ap.add_argument("--resume", action="store_true", help="跳过 --out 里已有的 qid")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows = dataset805.load()
    if args.only:
        keep = tuple(x.strip() for x in args.only.split(","))
        rows = [r for r in rows if any(k in r["总类"] for k in keep)]
    if args.qids:
        want = {q.strip() for q in args.qids.split(",")}
        rows = [r for r in rows if r["qid"] in want]
    if args.limit:
        rows = rows[: args.limit]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if args.resume and out.exists():
        with out.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    done.add(json.loads(line)["qid"])
                except Exception:  # noqa: BLE001  a half-written last line
                    pass
        print(f"续跑：已有 {len(done)} 行")

    ids = seller_ids([r for r in rows if not r["skip"]])
    start_mark = token_watermark()
    todo = [r for r in rows if r["qid"] not in done]
    live = sum(1 for r in todo if not r["skip"])
    print(f"跑 {len(todo)} 行（其中可执行 {live}，跳过 {len(todo) - live}）-> {out}")

    #: How many consecutive router failures before giving up on the run.
    DEAD_LIMIT = 5
    dead = 0

    t_start = time.time()
    mode = "a" if (args.resume and out.exists()) else "w"
    with out.open(mode, encoding="utf-8") as fh:
        for n, row in enumerate(todo, start=1):
            if row["skip"]:
                rec = {k: row[k] for k in ("qid", "总类", "一级", "二级", "用例类型",
                                           "品类", "商品", "前置", "text", "预期")}
                rec["skip"] = row["skip"]
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                continue
            rec = run_row(row, ids[row["seller"]])
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            turn = rec.get("turn") or {}
            flag = "ERR" if rec.get("error") else turn.get("route", "?")
            print(f"  [{n}/{len(todo)}] {rec['qid']} {flag:<16} "
                  f"{rec['seconds']:>5.1f}s  {row['text'][:30]}")

            # A router that cannot reach the model falls back to `clarify` —
            # the same value a genuinely ambiguous message produces. An API
            # outage or an exhausted key therefore does not look like an error,
            # it looks like a system that suddenly clarifies everything, and it
            # will happily fill the rest of the file with rows that measure
            # nothing. The first run of this set lost 560 rows that way. Stop
            # instead, and say why.
            if (turn.get("router_fallback") or "").startswith("router call failed"):
                dead += 1
                if dead >= DEAD_LIMIT:
                    print(f"\n!! 连续 {dead} 行 router 调用失败："
                          f"{turn.get('router_fallback')}\n"
                          f"   已停在第 {n} 行；修好之后用 --resume 从这里续跑。")
                    break
            else:
                dead = 0

    with SessionLocal() as db:
        logs = db.scalars(select(TokenUsageLog)
                          .where(TokenUsageLog.id > start_mark)).all()
    tin = sum(x.input_tokens for x in logs)
    tout = sum(x.output_tokens for x in logs)
    print(f"\n用时 {(time.time() - t_start) / 60:.0f} 分钟 | LLM 调用 {len(logs)} 次 | "
          f"输入 {tin:,} tok | 输出 {tout:,} tok")


if __name__ == "__main__":
    main()
