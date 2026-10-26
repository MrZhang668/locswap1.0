"""Run one trial in a fresh sandbox and print its result as one JSON line.

    python -X utf8 eval/harness/run_trial.py < trial.json

Input (stdin, JSON): ``{"task_id", "fixture", "turns", "trace_dir"}``.
Each turn is one of

* ``{"say": "...", "photo": "monitor_hp.jpg"}`` — a chat message, optional photo
  from ``eval/fixtures/photos``;
* ``{"approve": {"price": 15, ...}}`` — the seller presses "批准上架" on the
  draft card; fields not given are taken from the last draft.

One process per trial, because settings, the DB engine, the checkpoint store
and the vector indexes are all process-level: the environment has to be set
before ``app`` is imported, and nothing may leak from one trial to the next.

Exit status is 0 whenever a result was printed. A result with ``env_error``
means the trial never tested the agent (missing photo, failed precondition,
crash before the first turn) and must not be counted as an agent failure.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVAL_DIR = HERE.parent
PROJECT_ROOT = EVAL_DIR.parent
PHOTOS = EVAL_DIR / "fixtures" / "photos"


def _setup_env(sandbox: Path) -> None:
    os.environ["DATA_DIR"] = str(sandbox)
    os.environ["DB_URL"] = "sqlite:///" + sandbox.as_posix() + "/eval.db"
    os.environ["CHROMA_DIR"] = str(sandbox / "chroma")
    # memory, not chroma/milvus: nothing outside the sandbox may be touched.
    os.environ["VECTOR_BACKEND"] = "memory"
    # Not `auto`: auto silently falls back to hashing vectors when the model
    # fails to load, and every similarity-based result would change with it.
    os.environ["EMBEDDING_BACKEND"] = "sentence_transformers"
    os.environ["EMAIL_ENABLED"] = "false"
    os.environ["ENVIRONMENT"] = "development"
    os.environ["EXPIRY_POLL_INTERVAL_SECONDS"] = "3600"
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    sys.path.insert(0, str(HERE))


# ------------------------------------------------------------ tool trace ---
TOOL_CALLS: list[dict] = []


def _trim(obj, limit: int = 8000):
    text = json.dumps(obj, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _install_tool_trace() -> None:
    """Wrap every tool the sales and planning agents can call. Both
    orchestrators look tools up in their registry at call time, so replacing
    the entries is enough — no graph rebuild needed."""
    from app.services.agents.planning import orchestrator as plan_orch
    from app.services.agents.sales import orchestrator as sales_orch

    for agent, registry in (("sales", sales_orch.TOOL_REGISTRY),
                            ("planning", plan_orch.TOOL_REGISTRY)):
        for name, (model, fn) in list(registry.items()):
            def wrapped(db, seller_id, params, _fn=fn, _name=name, _agent=agent):
                rec = {"agent": _agent, "tool": _name, "turn": CURRENT_TURN[0],
                       "args": params.model_dump() if hasattr(params, "model_dump") else params}
                try:
                    out = _fn(db, seller_id, params)
                    rec["output"] = _trim(out.model_dump() if hasattr(out, "model_dump") else out)
                    return out
                except Exception as exc:  # noqa: BLE001  recorded, then re-raised unchanged
                    rec["error"] = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    TOOL_CALLS.append(rec)
            registry[name] = (model, wrapped)


CURRENT_TURN = [0]


# ------------------------------------------------------------- snapshots ---
def _snapshot(db, world: dict) -> dict:
    from app.models import Item, Notification, SalePlan

    seller_id = world["seller_id"]
    items = {int(i.id): {"name": i.name, "status": i.status, "price": float(i.price)}
             for i in db.query(Item).filter(Item.seller_id == seller_id).all()}
    plans = {int(p.id): {"item_id": p.item_id, "item_name": p.item_name, "status": p.status,
                         "target_date": str(p.target_date), "hold_item": bool(p.hold_item),
                         "watch_enabled": bool(p.watch_enabled), "date_basis": p.date_basis}
             for p in db.query(SalePlan).filter(SalePlan.seller_id == seller_id).all()}
    max_note = db.query(Notification.id).order_by(Notification.id.desc()).first()
    return {"items": items, "plans": plans, "max_notification": int(max_note[0]) if max_note else 0}


def _diff(db, before: dict, world: dict) -> dict:
    from app.models import Item, Notification

    after = _snapshot(db, world)
    id_to_key = {v: k for k, v in world["items"].items()}
    user_to_buyer = {v["id"]: k for k, v in world["buyers"].items()}
    want_by_buyer = {}
    from world import WANTS

    for wkey, bkey, _ in WANTS:
        want_by_buyer.setdefault(bkey, []).append(wkey)

    status_changes = {}
    for iid, row in after["items"].items():
        old = before["items"].get(iid)
        if old and old["status"] != row["status"]:
            status_changes[id_to_key.get(iid, str(iid))] = {"from": old["status"], "to": row["status"]}
    new_items = []
    for iid, row in after["items"].items():
        if iid not in before["items"]:
            item = db.get(Item, iid)
            new_items.append({"id": iid, "name": row["name"], "status": row["status"],
                              "price": row["price"], "attributes": item.attributes if item else {}})
    pushes = []
    for n in db.query(Notification).filter(Notification.id > before["max_notification"]).all():
        if n.type != "want_matched":
            continue
        bkey = user_to_buyer.get(int(n.user_id), str(n.user_id))
        payload = n.payload or {}
        pushes.append({"buyer": bkey, "wants": want_by_buyer.get(bkey, []),
                       "item": id_to_key.get(int(payload.get("item_id") or 0),
                                             str(payload.get("item_id")))})
    plans_created, plans_changed = [], []
    for pid, row in after["plans"].items():
        old = before["plans"].get(pid)
        entry = dict(row, id=pid, item=id_to_key.get(row["item_id"]) if row["item_id"] else None)
        if old is None:
            plans_created.append(entry)
        elif old != row:
            plans_changed.append({"id": pid, "before": old, "after": entry})
    return {"status_changes": status_changes, "new_items": new_items, "pushes": pushes,
            "plans_created": plans_created, "plans_changed": plans_changed}


# ---------------------------------------------------------------- trial ---
def _cards(out: dict, id_to_key: dict) -> dict:
    key = lambda iid: id_to_key.get(int(iid or 0), str(iid))  # noqa: E731
    return {
        "buyers": [{"item": key(b["item_id"]), "total": b.get("total", 0),
                    "budgets": [c.get("budget_display") for c in b.get("cards", [])],
                    "empty": bool(b.get("empty"))} for b in out.get("potential_buyers") or []],
        "pricing": [{"item": key(p["item_id"]), "sample_size": (p.get("stats") or {}).get("sample_size"),
                     "confidence": p.get("confidence"), "empty": bool(p.get("empty"))}
                    for p in out.get("pricing_analysis") or []],
        "demand": [{"item": key(d["item_id"]), "window": d.get("window"), "trend": d.get("trend"),
                    "people": d.get("total_people"), "events": d.get("total_events")}
                   for d in out.get("demand_analysis") or []],
        "comparison": bool(out.get("comparison")),
        "draft": out.get("publish_draft"),
    }


def _state_extras(db, thread_id: str) -> dict:
    from app.services.agents.graph import THREAD_STATE

    values = THREAD_STATE.read(db, thread_id)
    keep = ("scenario", "slots", "router_fallback", "out_of_scope", "clarify_rounds",
            "publish_verdict", "publish_photo", "pending_tasks", "item_refs", "user_authorized")
    return {k: values.get(k) for k in keep if k in values}


def run(spec: dict) -> dict:
    sandbox = Path(tempfile.mkdtemp(prefix="locswap-eval-"))
    _setup_env(sandbox)
    started = time.time()
    result: dict = {"task_id": spec.get("task_id", ""), "fixture": spec.get("fixture", ""),
                    "turns": [], "env_error": ""}
    try:
        import world as harness_world

        # Photos are checked before anything heavy is loaded.
        for step in spec.get("turns", []):
            name = step.get("photo")
            if name and not (PHOTOS / name).exists():
                result["env_error"] = f"缺少照片夹具：eval/fixtures/photos/{name}"
                return result

        from fastapi.testclient import TestClient

        from app.config import settings
        from app.db import SessionLocal
        from app.main import app
        from app.models import ListingRule
        from app.services.agents.publishing.rules import SEED_RULES
        from app.services.item_index import get_item_index
        from app.services.want_index import get_want_index

        _install_tool_trace()
        settings.upload_path.mkdir(parents=True, exist_ok=True)

        with TestClient(app) as client, SessionLocal() as db:
            index, want_index = get_item_index(), get_want_index()
            world = harness_world.build(db, index, want_index, spec["fixture"])
            index.rebuild_from_db(db)
            want_index.rebuild_from_db(db)

            rules = db.query(ListingRule).filter(ListingRule.active.is_(True)).count()
            if rules != len(SEED_RULES):
                result["env_error"] = f"规则库应有 {len(SEED_RULES)} 条，实际 {rules} 条"
                return result

            login = client.post("/api/auth/login",
                                json={"email": world["seller_email"],
                                      "password": harness_world.PASSWORD})
            login.raise_for_status()
            headers = {"Authorization": "Bearer " + login.json()["token"]}
            thread_id = client.post("/api/seller/advisor/threads", headers=headers).json()["thread_id"]
            id_to_key = {v: k for k, v in world["items"].items()}
            result["world"] = {k: world[k] for k in ("items", "item_names", "wants", "plans",
                                                      "persona", "buyers")}
            before = _snapshot(db, world)

            last_draft: dict = {}
            for n, step in enumerate(spec.get("turns", []), start=1):
                CURRENT_TURN[0] = n
                t0 = time.time()
                if "approve" in step:
                    st = _state_extras(db, thread_id)
                    form = {k: v for k, v in (last_draft or {}).items()
                            if k in ("name", "summary", "description", "category", "subcategory",
                                     "head_noun", "condition", "brand", "color", "material", "size")
                            and v not in (None, "")}
                    form.update(pickup_location_name=harness_world.PICKUP, thread_id=thread_id)
                    form.update(step["approve"] or {})
                    form.setdefault("photo_path", st.get("publish_photo") or "")
                    resp = client.post("/api/seller/advisor/publish/approve", json=form,
                                       headers=headers)
                    out = resp.json() if resp.headers.get("content-type", "").startswith(
                        "application/json") else {"detail": resp.text}
                    result["turns"].append({"n": n, "kind": "approve", "form": form,
                                            "status_code": resp.status_code, "out": out,
                                            "seconds": round(time.time() - t0, 1)})
                    db.expire_all()
                    # A listing created mid-trial gets the key "new", so later
                    # turns can expect analysis of it ("上传好了").
                    from app.models import Item
                    known = set(world["items"].values())
                    fresh = [int(i.id) for i in db.query(Item).filter(
                        Item.seller_id == world["seller_id"]).all() if int(i.id) not in known]
                    if fresh:
                        world["items"]["new"] = fresh[-1]
                        id_to_key[fresh[-1]] = "new"
                    continue

                body = {"message": step["say"], "thread_id": thread_id}
                if step.get("photo"):
                    # Through the real upload endpoint: the chat endpoint only
                    # accepts the path shape that endpoint returns.
                    with (PHOTOS / step["photo"]).open("rb") as fh:
                        up = client.post("/api/items/photo", headers=headers,
                                         files={"file": (step["photo"], fh, "image/jpeg")})
                    up.raise_for_status()
                    body["photo_path"] = up.json()["photo_path"]
                resp = client.post("/api/seller/advisor/chat", json=body, headers=headers)
                resp.raise_for_status()
                out = resp.json()
                if out.get("publish_draft"):
                    last_draft = dict(out["publish_draft"])
                db.expire_all()
                result["turns"].append({
                    "n": n, "kind": "say", "say": step["say"], "photo": step.get("photo", ""),
                    "reply": out.get("reply", ""), "ok": out.get("ok", True),
                    "route": out.get("route", ""), "used_tools": out.get("used_tools", []),
                    "degraded": out.get("degraded", False),
                    "no_active_items": out.get("no_active_items", False),
                    "cards": _cards(out, id_to_key),
                    "pending_action": out.get("pending_action"),
                    "action_result": out.get("action_result"),
                    "publish_blocked": out.get("publish_blocked", False),
                    "publish_findings": out.get("publish_findings", []),
                    "sale_plan": out.get("sale_plan"), "sale_plans": out.get("sale_plans", []),
                    "state": _state_extras(db, thread_id),
                    "llm_calls": out.get("llm_calls", 0),
                    "input_tokens": out.get("input_tokens", 0),
                    "output_tokens": out.get("output_tokens", 0),
                    "seconds": round(time.time() - t0, 1),
                })
            db.expire_all()
            result["diff"] = _diff(db, before, world)
            result["tool_calls"] = TOOL_CALLS
    except Exception as exc:  # noqa: BLE001  a crash is reported, never raised
        if not result["turns"]:
            result["env_error"] = f"{type(exc).__name__}: {exc}"
        else:
            result["crash"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()[-3000:]
    finally:
        result["seconds"] = round(time.time() - started, 1)
        _write_trace(spec, result)
        shutil.rmtree(sandbox, ignore_errors=True)
    return result


def _write_trace(spec: dict, result: dict) -> None:
    trace_dir = spec.get("trace_dir")
    if not trace_dir:
        return
    path = Path(trace_dir)
    path.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", spec.get("task_id") or "trial")
    with (path / f"{safe}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"spec": spec, "result": result}, ensure_ascii=False, default=str) + "\n")


def main() -> None:
    spec = json.loads(sys.stdin.read())
    # The app logs to stdout; the result must be the last line, so it is
    # printed with a marker the provider looks for.
    result = run(spec)
    sys.stdout.write("\n@@RESULT@@" + json.dumps(result, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
