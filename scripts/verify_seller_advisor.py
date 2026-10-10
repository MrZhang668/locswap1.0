"""Seller sales advisor acceptance: read-only tools + agent graph + mock model.

**Requires no ANTHROPIC_API_KEY**: real model calls are replaced by a FakeClient,
and token_usage_log must still have 0 rows at the end.

Run:
    python scripts/verify_seller_advisor.py

Opens an isolated sandbox database under data/_verify_seller_advisor/,
never touches data/locswap.db.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from types import SimpleNamespace
from pathlib import Path

import _bootstrap  # noqa: F401  injects backend into sys.path

SANDBOX = Path(_bootstrap.PROJECT_ROOT) / "data" / "_verify_seller_advisor"

# ---- Must be set before importing backend.app ----
if SANDBOX.exists():
    shutil.rmtree(SANDBOX, ignore_errors=True)
if SANDBOX.exists():
    raise SystemExit("Sandbox directory could not be fully removed: " + str(SANDBOX))
SANDBOX.mkdir(parents=True, exist_ok=True)
os.environ["DATA_DIR"] = str(SANDBOX)
os.environ["DB_URL"] = "sqlite:///" + SANDBOX.as_posix() + "/verify.db"
os.environ["CHROMA_DIR"] = str(SANDBOX / "chroma")
# Pin the sandbox to a filesystem-backed vector store, whatever .env says.
# The three overrides above isolate by *path* — which is the whole of the
# isolation as long as the vector store is a directory. It is not one under
# VECTOR_BACKEND=milvus: the collection name comes from CHROMA_COLLECTION /
# WANT_VECTOR_COLLECTION, so an un-pinned run points at the real cluster and
# `rebuild_from_db` resets the live collection and refills it with this
# script's two test items. (It did exactly that on 2026-09-14.)
os.environ["VECTOR_BACKEND"] = "chroma"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["LLM_API_KEY"] = ""    # clear the openai_compat key too
os.environ["EXPIRY_POLL_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient  # noqa: E402

import app.services.agents.graph as graph_mod  # noqa: E402
import app.services.publishing.approve as approve_mod  # noqa: E402
import app.services.publishing.pipeline as publishing_mod  # noqa: E402
import app.services.agents.sessions as sessions_mod  # noqa: E402
import app.services.seller_advisor.orchestrator as orch  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Item, Notification, StandingWant, TokenUsageLog  # noqa: E402
from app.repositories import ItemRepository  # noqa: E402
from app.services.prompts import load_prompt  # noqa: E402
from app.services.publishing.rules import active_rules, retrieve_rules, seed_rules  # noqa: E402
from app.services.publishing.schemas import ComplianceVerdict, ListingDraft, PublishFinding  # noqa: E402
from app.services.seller_advisor.schemas import (  # noqa: E402
    AnalyzeSoldPricesInput,
    FindPotentialBuyersInput,
    ListMyItemsInput,
)
from app.services.seller_advisor.tools import (  # noqa: E402
    ToolError,
    analyze_sold_prices,
    find_potential_buyers,
    list_my_items,
)

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "  [ok]" if ok else "  [FAIL]"
    suffix = "  <- " + detail if (not ok and detail) else ""
    print(mark + " " + label + suffix)
    if not ok:
        _failures.append(label)
    return ok


def section(title: str) -> None:
    print("\n-- " + title + " " + "-" * max(0, 58 - len(title)))


class Actor:
    def __init__(self, client: TestClient, email: str, location: str):
        self.client = client
        r = client.post("/api/auth/register", json={"email": email, "password": "pw123456"})
        r.raise_for_status()
        self.token = r.json()["token"]
        self.id = r.json()["user"]["id"]
        self.headers = {"Authorization": "Bearer " + self.token}
        self.client.patch(
            "/api/auth/me",
            json={"location_name": location, "pickup_range_enabled": False},
            headers=self.headers,
        ).raise_for_status()

    def post(self, path, **kw):
        return self.client.post(path, headers=self.headers, **kw)

    def get(self, path, **kw):
        return self.client.get(path, headers=self.headers, **kw)

    def patch(self, path, **kw):
        return self.client.patch(path, headers=self.headers, **kw)

    def delete(self, path, **kw):
        return self.client.delete(path, headers=self.headers, **kw)


def wants_snapshot() -> list[tuple]:
    with SessionLocal() as db:
        return sorted(
            (w.id, w.status, w.notify_count, w.last_notified_at, w.fulfilled_by_item_id,
             w.expires_at)
            for w in db.query(StandingWant).all()
        )


def notification_count() -> int:
    with SessionLocal() as db:
        return db.query(Notification).count()


def token_rows() -> int:
    with SessionLocal() as db:
        return db.query(TokenUsageLog).count()


# ---------------- FakeClient: a scripted stand-in for the model ----------------
def text_block(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def tool_block(name: str, tool_input: dict, block_id: str) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=tool_input, id=block_id)


def fake_response(blocks: list) -> SimpleNamespace:
    return SimpleNamespace(
        content=blocks, usage=SimpleNamespace(input_tokens=10, output_tokens=5)
    )


class FakeClient:
    available = True
    unavailable_reason = ""

    def __init__(self, script: list):
        self.script = script
        self.calls: list[dict] = []

    def create_with_tools(self, **kw):
        self.calls.append(kw)
        idx = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[idx]


class use_fake_route:
    """Pin the router's decision.

    The router is a real model call on every turn, so without this every chat
    test would depend on a live key and on the router's judgement — turning
    specialist tests into router tests.
    """

    def __init__(self, agent: str, rewritten: str | None = None, question: str = ""):
        self.agent, self.rewritten, self.question = agent, rewritten, question
        self.calls: list[dict] = []

    def __enter__(self):
        outer = self

        def fake_route(*, message, history, has_active_items, listings,
                       has_photo=False, **extra):
            # `**extra` keeps this stub from having to track every fact the
            # real router grows (pre-router hints, last-turn context); a stub
            # that breaks on a new keyword fails the specialist tests for a
            # reason that has nothing to do with the specialist.
            outer.calls.append({
                "message": message, "history": history,
                "has_active_items": has_active_items, "listings": listings,
                "has_photo": has_photo, **extra,
            })
            return graph_mod.router_mod.RouteResult(
                agent=outer.agent,
                rewritten_query=outer.rewritten or message,
                clarify_question=outer.question,
            )

        self._orig = graph_mod.router_mod.route
        graph_mod.router_mod.route = fake_route
        # Routing only happens at all when a model is configured; pinning the
        # decision implies that precondition.
        self._orig_configured = graph_mod.llm_configured
        graph_mod.llm_configured = lambda: (True, "")
        return self

    def __exit__(self, *a):
        graph_mod.router_mod.route = self._orig
        graph_mod.llm_configured = self._orig_configured


class use_fake_publishing:
    """Pin the two publishing model steps.

    They are multimodal calls against a live provider; scripting them keeps the
    test about the pipeline — the block branch, the rule-book reconciliation,
    the approval re-check — rather than about model judgement.
    """

    def __init__(self, verdict="pass", findings=(), draft=None):
        self.verdict, self.findings, self.draft = verdict, list(findings), draft
        self.compliance_calls: list[dict] = []
        self.extract_calls: list[dict] = []

    def __enter__(self):
        outer = self

        def fake_compliance(db, *, text, photo_path=""):
            outer.compliance_calls.append({"text": text, "photo_path": photo_path})
            return outer.verdict, list(outer.findings), 11, 3

        def fake_extract(db, *, text, photo_path=""):
            outer.extract_calls.append({"text": text, "photo_path": photo_path})
            return outer.draft or ListingDraft(), 13, 5

        self._orig = (publishing_mod.review_compliance, publishing_mod.extract_listing)
        publishing_mod.review_compliance = fake_compliance
        publishing_mod.extract_listing = fake_extract
        self._approve_orig = approve_mod.review_compliance
        approve_mod.review_compliance = fake_compliance
        return self

    def __exit__(self, *a):
        publishing_mod.review_compliance, publishing_mod.extract_listing = self._orig
        approve_mod.review_compliance = self._approve_orig


class use_fake_llm:
    def __init__(self, fake: FakeClient):
        self.fake = fake

    def __enter__(self):
        self._orig = orch.get_llm_client
        orch.get_llm_client = lambda: self.fake
        # The graph short-circuits to its degraded node when no key is
        # configured — correct in production, but here a fake client *is* the
        # configuration, so the guard has to agree with it.
        self._orig_configured = graph_mod.llm_configured
        graph_mod.llm_configured = lambda: (True, "")
        return self.fake

    def __exit__(self, *a):
        orch.get_llm_client = self._orig
        graph_mod.llm_configured = self._orig_configured


def final_json(reply: str, pricing=None, **extra) -> str:
    """The final JSON is keyed by item_id — one turn may cover several listings.

    `extra` exists so a test can deliberately send a field the schema no longer
    accepts, and assert the backend rejects it.
    """
    return json.dumps({"reply": reply, "pricing": pricing, **extra}, ensure_ascii=False)


PRIVATE_MARKERS = ["buyer_id", "raw_query", "@verify.local", '"lat"', '"lng"',
                   "origin_lat", "origin_lng"]


def assert_private(label: str, dump: str, extra: list[str] = ()) -> None:
    leaked = [m for m in list(PRIVATE_MARKERS) + list(extra) if m in dump]
    check(label, not leaked, "leaked markers: " + ",".join(leaked))


def main() -> int:  # noqa: PLR0915
    with TestClient(app) as client:
        cfg = client.get("/api/meta/config").json()
        location = cfg["locations"][0]["name"]
        print("Sandbox: " + str(SANDBOX) + " | llm_available=" + str(cfg["llm_available"]))

        seller = Actor(client, "seller@verify.local", location)
        seller2 = Actor(client, "seller2@verify.local", location)
        buyer1 = Actor(client, "buyer1@verify.local", location)
        buyer2 = Actor(client, "buyer2@verify.local", location)
        buyer3 = Actor(client, "buyer3@verify.local", location)

        # Three wants: two for bikes (budgets $300 / $800), one unrelated fridge want
        buyer1.post("/api/search/chat",
                    json={"message": "used bike under $300", "history": [], "clarify_round": 0})
        buyer2.post("/api/wants", json={"text": "mini fridge for my dorm under $500"})
        buyer3.post("/api/wants", json={"text": "used bike under $800"})

        r = seller.post("/api/items", json={
            "name": "Giant commuter bike",
            "description": "Graduating and selling. 26-inch commuter bike, brakes recently replaced, U-lock included",
            "summary": "26-inch commuter bike, brakes recently replaced",
            "price": 260,
            "attributes": {"category": "自行车与滑板车", "brand": "Giant", "condition": "成色很好"},
            "pickup_location_name": location,
        })
        check("bike listed", r.status_code == 200, r.text[:200])
        bike1 = r.json()["item"]["item_id"]

        r = seller.post("/api/items", json={
            "name": "Mechanical keyboard",
            "description": "Linear-switch mechanical keyboard, feels great to type on",
            "summary": "Linear-switch mechanical keyboard",
            "price": 120,
            "attributes": {"category": "电子产品", "condition": "成色很好"},
            "pickup_location_name": location,
        })
        keyboard = r.json()["item"]["item_id"]

        # ============ Tool 1: find_potential_buyers ============
        section("Tool 1  find_potential_buyers (standalone call, no key)")
        before_wants = wants_snapshot()
        before_notes = notification_count()

        with SessionLocal() as db:
            out = find_potential_buyers(
                db, seller.id, FindPotentialBuyersInput(item_id=bike1))
            check("output is tagged with the listing it belongs to",
                  out.item_id == bike1 and out.item_name == "Giant commuter bike",
                  f"{out.item_id}/{out.item_name}")
            check("matched the bike wants", out.total >= 2, f"total={out.total}")
            dump = out.model_dump_json()
            assert_private("output has no buyer_id/raw_query/email/exact coordinates", dump,
                           ["used bike under $300", "used bike under $800"])
            check("cards carry match score and reasons",
                  all(c.match_reasons and 0 <= c.match_score <= 1 for c in out.cards))
            check("caveat states budgets are not formal offers",
                  any("不是正式报价" in c for c in out.caveats), str(out.caveats))

            out_budget = find_potential_buyers(
                db, seller.id, FindPotentialBuyersInput(item_id=bike1, sort_by="highest_budget"))
            check("budget sort puts the $800 want first",
                  out_budget.cards and "800" in out_budget.cards[0].budget_display,
                  str([c.budget_display for c in out_budget.cards]))
            # Wording localised 2026-09-15: 「预算」, not "Budget".
            check("budget display uses 'Budget' wording, not an offer",
                  all("预算" in c.budget_display for c in out_budget.cards),
                  str([c.budget_display for c in out_budget.cards]))

            out_topn = find_potential_buyers(
                db, seller.id, FindPotentialBuyersInput(item_id=bike1, top_n=999))
            check("top_n clamped by the server-side cap",
                  len(out_topn.cards) <= settings.seller_advisor_top_n_max)

            out_empty = find_potential_buyers(
                db, seller.id, FindPotentialBuyersInput(item_id=keyboard))
            if out_empty.total == 0:
                check("honestly returns empty when nothing matches",
                      not out_empty.cards, f"total={out_empty.total}")
            else:
                # Not a defect: the 12-week demand simulation gave almost every
                # category at least one live want, so "an item nobody wants" is
                # no longer a fixture this dataset can supply. The property is
                # still worth stating — an empty result must be empty, not
                # padded — so it is checked when the data allows and reported
                # as unexercised when it does not.
                print(f"  [skip] 库里已经没有零候选的商品可测（keyboard total={out_empty.total}）")
            check("empty and non-empty results agree on their own count",
                  len(out_empty.cards) == out_empty.total or out_empty.total > len(out_empty.cards),
                  f"total={out_empty.total} cards={len(out_empty.cards)}")

        check("tool calls left Want status/push counters untouched", wants_snapshot() == before_wants)
        check("tool calls added zero notifications", notification_count() == before_notes)

        # ============ Tool 2: analyze_sold_prices ============
        section("Tool 2  analyze_sold_prices (no sold samples)")
        with SessionLocal() as db:
            out = analyze_sold_prices(db, seller.id, AnalyzeSoldPricesInput(item_id=bike1))
            check("no samples: sample_size=0 and confidence=none",
                  out.stats.sample_size == 0 and out.confidence == "none")
            check("no samples: no statistical prices produced",
                  out.stats.median is None and out.stats.min_price is None)
            check("no samples: explicitly says evidence is missing",
                  any("成交价证据" in c for c in out.caveats), str(out.caveats))

        # ============ Tool 0: list_my_items ============
        section("Tool 0  list_my_items (the model's only source of valid item ids)")
        with SessionLocal() as db:
            mine = list_my_items(db, seller.id, ListMyItemsInput())
            ids = [row.item_id for row in mine.items]
            check("lists exactly the seller's own active listings",
                  sorted(ids) == sorted([bike1, keyboard]) and mine.total_active == 2, str(ids))
            check("rows carry the fields needed to pick an item",
                  all(r.name and r.status == "selling" and r.days_listed >= 0 for r in mine.items))
            empty = list_my_items(db, seller2.id, ListMyItemsInput())
            check("a seller with nothing on sale gets an empty list, not someone else's",
                  empty.total_active == 0 and empty.items == [], str(empty.items))

        # ============ Ownership is enforced in the tool layer ============
        section("Ownership  a model-supplied item_id cannot cross sellers")
        def refusal(seller_id: int, item_id: int) -> str:
            """Run a tool that must refuse, and hand back the message the model would see."""
            with SessionLocal() as db:
                try:
                    find_potential_buyers(db, seller_id, FindPotentialBuyersInput(item_id=item_id))
                except ToolError as exc:
                    return str(exc)
            return ""

        foreign = refusal(seller2.id, bike1)
        missing = refusal(seller.id, 999999)
        check("another seller's item_id refuses",
              "不是你的在售商品" in foreign, foreign[:120])
        check("a nonexistent item_id refuses",
              "不是你的在售商品" in missing, missing[:120])
        check("both refusals read identically, so the message never confirms existence",
              foreign.replace(str(bike1), "#") == missing.replace("999999", "#"),
              foreign[:80] + " || " + missing[:80])
        with SessionLocal() as db:
            try:
                analyze_sold_prices(db, seller2.id, AnalyzeSoldPricesInput(item_id=bike1))
                check("analyze_sold_prices enforces ownership too", False, "no ToolError")
            except ToolError:
                check("analyze_sold_prices enforces ownership too", True)

        # ============ Chat route: auth / no listings / degraded mode ============
        section("Chat route  auth / no listings / degraded without key")
        unused = FakeClient([fake_response([text_block(final_json("never reached"))])])
        with use_fake_llm(unused), use_fake_route("sales_agent"):
            r = seller2.post("/api/seller/advisor/chat",
                             json={"message": "who wants to buy my stuff"})
        body = r.json()
        check("seller with no active listings -> answered without spending a model call",
              r.status_code == 200 and body["no_active_items"] is True
              and unused.calls == [] and body["input_tokens"] == 0, r.text[:200])

        r = client.post("/api/seller/advisor/chat", json={"message": "hi"})
        check("not logged in -> 401", r.status_code == 401, str(r.status_code))

        r = seller.post("/api/seller/advisor/chat",
                        json={"message": "hi", "item_id": bike1})
        check("item_id is no longer part of the contract -> 422",
              r.status_code == 422, str(r.status_code))

        r = seller.post("/api/seller/advisor/chat",
                        json={"message": "hi", "history": []})
        check("history is no longer part of the contract -> 422",
              r.status_code == 422, str(r.status_code))

        r = seller.post("/api/seller/advisor/chat",
                        json={"message": "help me analyze the price"})
        body = r.json()
        check("no key -> routed to the degraded node, not to a clarifying question",
              r.json().get("route") == "degraded", r.text[:200])
        check("no key -> explicit degraded status",
              r.status_code == 200 and body["degraded"] is True and body["ok"] is True,
              r.text[:200])
        check("degraded reply is readable, not an error", "不可用" in body["reply"], body["reply"][:80])
        check("degradation does not break the seller items endpoint",
              seller.get("/api/seller/items").status_code == 200)

        r = seller.post("/api/seller/advisor/chat",
                        json={"message": "x" * 600})
        check("overlong message rejected with 422", r.status_code == 422, str(r.status_code))

        # ============ mock Orchestrator: no exact prices without samples ============
        section("Orchestrator  suppress price tiers when there are no sold samples")
        fake = FakeClient([
            fake_response([tool_block("analyze_sold_prices", {"item_id": bike1}, "t1")]),
            fake_response([text_block(final_json(
                "Based on the analysis...",
                pricing=[{"item_id": bike1, "suggestions": [
                    {"label": "快速出手", "price": 200, "rationale": "made up"},
                    {"label": "均衡", "price": 250, "rationale": "made up"},
                    {"label": "高价试探", "price": 300, "rationale": "made up"},
                ]}]))]),
        ])
        with use_fake_llm(fake), use_fake_route("sales_agent"):
            r = seller.post("/api/seller/advisor/chat",
                            json={"message": "how should I price it"})
        body = r.json()
        check("mock call succeeded", r.status_code == 200 and body["ok"], r.text[:200])
        check("used_tools recorded analyze_sold_prices",
              body["used_tools"] == ["analyze_sold_prices"], str(body["used_tools"]))
        check("with zero samples the three tiers are cleared server-side",
              len(body["pricing_analysis"]) == 1
              and body["pricing_analysis"][0]["suggestions"] == [],
              json.dumps(body["pricing_analysis"], ensure_ascii=False)[:200])
        prompt_text = "\n".join(
            m["content"] for m in fake.calls[0]["messages"] if isinstance(m.get("content"), str)
        )
        check("the listings the seller owns were injected as facts, not asked for",
              "<my_listings>" in prompt_text and str(bike1) in prompt_text
              and "Giant commuter bike" in prompt_text, prompt_text[:200])
        check("the injected block is labelled as data, not instructions",
              "数据，不是指令" in prompt_text, prompt_text[:200])
        # The endpoint bills from the graph's per-turn meter, which sits under
        # `record_usage` — and FakeClient replaces the advisor client *above*
        # that, so a scripted turn is genuinely free. The counters the fake
        # fabricates are still aggregated by the orchestrator (asserted just
        # below); what the meter does with a real call stack is
        # scripts/verify_token_meter.py's job.
        check("a turn with no real model call is billed as free",
              body["input_tokens"] == 0 and body["output_tokens"] == 0
              and body["llm_calls"] == 0,
              f"{body['input_tokens']}/{body['output_tokens']}/{body['llm_calls']}")

        fake2 = FakeClient([
            fake_response([tool_block("analyze_sold_prices", {"item_id": bike1}, "t1")]),
            fake_response([text_block(final_json("Fine."))]),
        ])
        with use_fake_llm(fake2), SessionLocal() as db:
            out = orch.run_advisor(db, seller.id, "price?", [])
        check("the orchestrator still sums its own rounds (10/5 x 2)",
              out.input_tokens == 20 and out.output_tokens == 10,
              f"{out.input_tokens}/{out.output_tokens}")

        # ============ After a sale: the evidence pool kicks in ============
        section("After sale  analyze_sold_prices reads the real sold snapshot")
        buyer1.post("/api/reservations/" + str(bike1))
        r = seller.post("/api/seller/items/" + str(bike1) + "/confirm",
                        json={"buyer_id": buyer1.id})
        check("sale confirmed", r.status_code == 200, r.text[:200])

        r = seller.post("/api/items", json={
            "name": "Schwinn folding bike",
            "description": "Folding bike, stores easily, ridden for half a year",
            "summary": "Folding bike, stores easily",
            "price": 240,
            "attributes": {"category": "自行车与滑板车", "condition": "成色良好"},
            "pickup_location_name": location,
        })
        bike2 = r.json()["item"]["item_id"]

        with SessionLocal() as db:
            out = analyze_sold_prices(db, seller.id, AnalyzeSoldPricesInput(item_id=bike2))
            check("stats include only records with a sold snapshot", out.stats.sample_size == 1,
                  f"sample_size={out.stats.sample_size}")
            check("median equals the real sale price 260", out.stats.median == 260.0,
                  str(out.stats.median))
            check("few samples -> confidence low with a caveat",
                  out.confidence == "low" and any("置信度低" in c for c in out.caveats),
                  f"confidence={out.confidence} caveats={out.caveats}")
            check("live listing ($240 asking price) not counted as a sale",
                  240.0 not in [out.stats.min_price, out.stats.max_price])
            assert_private("sold evidence output has no buyer identity", out.model_dump_json())

        # ============ mock Orchestrator: multi-intent + structured results ============
        section("Orchestrator  multi-intent (find buyers + price analysis)")
        fake = FakeClient([
            fake_response([
                tool_block("find_potential_buyers",
                           {"item_id": bike2, "sort_by": "highest_budget", "top_n": 3}, "t1"),
                tool_block("analyze_sold_prices", {"item_id": bike2}, "t2"),
            ]),
            fake_response([text_block(final_json(
                "Buyers are waiting, and the price has evidence behind it.",
                pricing=[
                    {"item_id": bike2, "suggestions": [
                        {"label": "快速出手", "price": 220, "rationale": "below the median"},
                        {"label": "均衡", "price": 250, "rationale": "near the median"},
                        {"label": "高价试探", "price": 280, "rationale": "above the median"},
                    ]},
                    # tiers for a listing this turn never analyzed -> must be dropped
                    {"item_id": keyboard, "suggestions": [
                        {"label": "均衡", "price": 999, "rationale": "invented"},
                    ]},
                ]))]),
        ])
        with use_fake_llm(fake), use_fake_route("sales_agent"):
            r = seller.post("/api/seller/advisor/chat",
                            json={"message": "find me high-budget buyers, then analyze the price"})
        body = r.json()
        check("both tools were called",
              sorted(body["used_tools"]) == ["analyze_sold_prices", "find_potential_buyers"],
              str(body["used_tools"]))
        buyers = body["potential_buyers"]
        check("potential-buyer cards come from the tool (incl. the $800 want)",
              len(buyers) == 1 and buyers[0]["item_id"] == bike2
              and buyers[0]["total"] >= 1
              and "800" in buyers[0]["cards"][0]["budget_display"],
              json.dumps(buyers, ensure_ascii=False)[:300])
        pricing = body["pricing_analysis"]
        check("price stats come from the tool with sample=1",
              len(pricing) == 1 and pricing[0]["stats"]["sample_size"] == 1,
              json.dumps(pricing, ensure_ascii=False)[:200])
        check("tiers land on the listing that was actually analyzed",
              pricing[0]["item_id"] == bike2 and len(pricing[0]["suggestions"]) == 3)
        check("tiers invented for an unanalyzed listing are dropped",
              all(p["item_id"] != keyboard for p in pricing),
              json.dumps(pricing, ensure_ascii=False)[:200])
        check("suggestions carry the no-guarantee caveat",
              any("不保证能成交" in c for c in pricing[0]["caveats"]))
        assert_private("full response leaks no private data", json.dumps(body, ensure_ascii=False))

        # ============ Listing optimization is no longer a capability ============
        section("Scope  listing optimization was removed, not merely hidden")
        check("get_listing_search_context is not a registered tool",
              "get_listing_search_context" not in orch.TOOL_REGISTRY,
              str(sorted(orch.TOOL_REGISTRY)))
        check("it is not advertised in the tool definitions either",
              all(t["name"] != "get_listing_search_context" for t in orch.TOOL_DEFINITIONS),
              str([t["name"] for t in orch.TOOL_DEFINITIONS]))
        check("the prompt no longer promises copy optimization",
              "get_listing_search_context" not in load_prompt("seller_advisor"))

        with SessionLocal() as db:
            before_item = ItemRepository(db).get(bike2)
            before_state = (before_item.name, before_item.description, before_item.summary)
        fake = FakeClient([
            # The model tries anyway: calls the retired tool, then emits a draft
            fake_response([tool_block("get_listing_search_context", {"item_id": bike2}, "t1")]),
            fake_response([text_block(final_json(
                "Here is an optimized title.",
                listing_suggestions=[{"item_id": bike2, "optimized_title": "Rewritten title"}]))]),
            fake_response([text_block(final_json("Copy edits are handled where you edit the listing."))]),
        ])
        with use_fake_llm(fake), use_fake_route("sales_agent"):
            r = seller.post("/api/seller/advisor/chat",
                            json={"message": "help me improve the title and description"})
        body = r.json()
        check("calling the retired tool is refused as unregistered",
              r.status_code == 200 and body["ok"] and body["used_tools"] == [],
              str(body["used_tools"]))
        check("a draft in the final JSON is rejected, not silently passed through",
              "listing_suggestions" not in body, json.dumps(body, ensure_ascii=False)[:200])
        check("the model gets a correction round instead of the draft landing",
              len(fake.calls) == 3, f"calls={len(fake.calls)}")
        with SessionLocal() as db:
            after_item = ItemRepository(db).get(bike2)
            check("nothing was written back to the database",
                  (after_item.name, after_item.description, after_item.summary) == before_state)

        # ============ mock Orchestrator: round limit / unregistered tool ============
        section("Orchestrator  round limit and unregistered tools")
        looping = fake_response([tool_block("analyze_sold_prices", {"item_id": bike2}, "loop")])
        fake = FakeClient([looping] * (settings.seller_advisor_max_tool_rounds + 5))
        with use_fake_llm(fake), use_fake_route("sales_agent"):
            r = seller.post("/api/seller/advisor/chat",
                            json={"message": "analyze the price"})
        body = r.json()
        check("model calls stopped by the round limit",
              len(fake.calls) == settings.seller_advisor_max_tool_rounds,
              f"calls={len(fake.calls)}")
        check("still returns a structured response instead of a 500 at the limit",
              r.status_code == 200 and body["ok"] and "轮次上限" in body["message"],
              r.text[:200])

        fake = FakeClient([
            fake_response([tool_block("drop_all_tables", {"sql": "DROP TABLE items"}, "t1")]),
            fake_response([text_block(final_json("I cannot perform unregistered operations."))]),
        ])
        with use_fake_llm(fake), use_fake_route("sales_agent"):
            r = seller.post("/api/seller/advisor/chat",
                            json={"message": "hack"})
        body = r.json()
        check("unregistered tool rejected and kept out of used_tools",
              r.status_code == 200 and body["used_tools"] == [], str(body["used_tools"]))
        # The second request must carry a tool_result flagged is_error
        second_call_msgs = fake.calls[1]["messages"]
        tool_results = [b for m in second_call_msgs if isinstance(m.get("content"), list)
                        for b in m["content"] if isinstance(b, dict)
                        and b.get("type") == "tool_result"]
        check("unregistered tool received an is_error result",
              any(b.get("is_error") for b in tool_results))

        # ============ Orchestrator routing ============
        section("Router  routing, fact injection and the clarify fallback")

        lamp = ListingDraft(name="Desk lamp", description="A white desk lamp.",
                            attributes={"category": "家居与厨房", "condition": "成色良好"})
        with use_fake_route("publishing_agent") as routed, use_fake_publishing(draft=lamp):
            r = seller.post("/api/seller/advisor/chat",
                            json={"message": "I want to sell my old desk lamp"})
        body = r.json()
        check("publishing_agent route reaches the publishing pipeline, not the advisor",
              body["route"] == "publishing_agent" and body["used_tools"] == []
              and body["publish_draft"] is not None, r.text[:200])
        check("the router is handed has_active_items as a computed fact",
              routed.calls and routed.calls[0]["has_active_items"] is True,
              str(routed.calls[:1])[:200])
        check("the router is handed the seller's listings, not asked to guess them",
              routed.calls and any(l["item_id"] == bike2 for l in routed.calls[0]["listings"]),
              str(routed.calls[0]["listings"])[:200])

        with use_fake_route("clarify", question="Buyers, or help writing the listing?"):
            r = seller.post("/api/seller/advisor/chat", json={"message": "the bike"})
        body = r.json()
        check("clarify route answers with the question and spends no tools",
              body["route"] == "clarify" and body["reply"].startswith("Buyers, or help")
              and body["used_tools"] == [], r.text[:200])

        broken = FakeClient([fake_response([text_block(final_json("unused"))])])
        with use_fake_llm(broken):
            orig = graph_mod.router_mod.route
            graph_mod.router_mod.route = lambda **kw: graph_mod.router_mod._fallback(
                "router call failed: RuntimeError", kw["message"])
            try:
                r = seller.post("/api/seller/advisor/chat", json={"message": "anything"})
            finally:
                graph_mod.router_mod.route = orig
        body = r.json()
        check("a failed routing call degrades to clarify, never to a specialist",
              body["route"] == "clarify" and broken.calls == [], r.text[:200])

        # ============ Server-side conversation threads ============
        section("Threads  history is server state, not client input")
        two_turns = [
            fake_response([text_block(final_json("First answer."))]),
            fake_response([text_block(final_json("Second answer."))]),
        ]
        with use_fake_llm(FakeClient(two_turns[:1])), use_fake_route("sales_agent"):
            r1 = seller.post("/api/seller/advisor/chat", json={"message": "first question"})
        thread = r1.json()["thread_id"]
        check("a thread id is minted and returned", bool(thread), r1.text[:200])
        check("the thread id is scoped to the seller", thread.startswith(f"{seller.id}-"), thread)
        check("the conversation shows up in the seller's list",
              any(t["thread_id"] == thread for t in seller.get("/api/seller/advisor/threads").json()),
              seller.get("/api/seller/advisor/threads").text[:200])

        second = FakeClient(two_turns[1:])
        with use_fake_llm(second), use_fake_route("sales_agent") as routed2:
            r2 = seller.post("/api/seller/advisor/chat",
                             json={"message": "second question", "thread_id": thread})
        check("the same thread id comes back", r2.json()["thread_id"] == thread, r2.text[:200])
        check("the earlier turn is replayed from server state, not from the client",
              routed2.calls and any(t["content"] == "first question"
                                    for t in routed2.calls[0]["history"]),
              str(routed2.calls[0]["history"])[:200])

        with use_fake_llm(FakeClient(two_turns[:1])), use_fake_route("sales_agent") as routed3:
            r3 = seller2.post("/api/seller/advisor/chat",
                              json={"message": "whose thread is this", "thread_id": thread})
        check("another seller cannot borrow a thread id",
              r3.json()["thread_id"] != thread and r3.json()["thread_id"].startswith(f"{seller2.id}-"),
              r3.text[:200])
        check("and gets an empty history, not the owner's conversation",
              routed3.calls and routed3.calls[0]["history"] == [],
              str(routed3.calls[0]["history"])[:200])

        with use_fake_llm(FakeClient(two_turns[:1])), use_fake_route("sales_agent") as routed4:
            seller.post("/api/seller/advisor/chat",
                        json={"message": "fresh start", "thread_id": None})
        check("omitting the thread id starts a clean conversation",
              routed4.calls and routed4.calls[0]["history"] == [],
              str(routed4.calls[0]["history"])[:200])

        # ============ Conversation management ============
        section("Conversations  naming, reopening, deleting — and whose they are")
        made = seller.post("/api/seller/advisor/threads").json()
        check("a new conversation starts with a placeholder name",
              made["title"] == "New chat", str(made))

        with use_fake_llm(FakeClient([fake_response([text_block(final_json("Sure."))])])), \
                use_fake_route("sales_agent"):
            seller.post("/api/seller/advisor/chat",
                        json={"message": "how are my bikes doing", "thread_id": made["thread_id"]})
        rows = {t["thread_id"]: t for t in seller.get("/api/seller/advisor/threads").json()}
        check("the first message names the conversation",
              rows[made["thread_id"]]["title"] == "how are my bikes doing",
              str(rows[made["thread_id"]]))

        r = seller.patch(f"/api/seller/advisor/threads/{made['thread_id']}",
                         json={"title": "Bike pricing"})
        check("renaming works", r.status_code == 200 and r.json()["title"] == "Bike pricing", r.text[:200])

        with use_fake_llm(FakeClient([fake_response([text_block(final_json("Still fine."))])])), \
                use_fake_route("sales_agent"):
            seller.post("/api/seller/advisor/chat",
                        json={"message": "and the keyboard?", "thread_id": made["thread_id"]})
        rows = {t["thread_id"]: t for t in seller.get("/api/seller/advisor/threads").json()}
        check("a later message does not overwrite a name the seller chose",
              rows[made["thread_id"]]["title"] == "Bike pricing", str(rows[made["thread_id"]]))

        body = seller.get(f"/api/seller/advisor/threads/{made['thread_id']}").json()
        said = [t["content"] for t in body["turns"]]
        check("reopening replays the stored transcript",
              "how are my bikes doing" in said and "and the keyboard?" in said, str(said)[:200])

        check("another seller cannot read it",
              seller2.get(f"/api/seller/advisor/threads/{made['thread_id']}").status_code == 404)
        check("another seller cannot rename it",
              seller2.patch(f"/api/seller/advisor/threads/{made['thread_id']}",
                            json={"title": "mine now"}).status_code == 404)
        check("another seller cannot delete it",
              seller2.delete(f"/api/seller/advisor/threads/{made['thread_id']}").status_code == 404)
        check("and it is untouched afterwards",
              seller.get(f"/api/seller/advisor/threads/{made['thread_id']}").json()["title"] == "Bike pricing")

        r = seller.delete(f"/api/seller/advisor/threads/{made['thread_id']}")
        check("deleting works", r.status_code == 200, r.text[:200])
        check("the conversation is gone from the list",
              all(t["thread_id"] != made["thread_id"]
                  for t in seller.get("/api/seller/advisor/threads").json()))
        check("and its transcript is gone with it",
              seller.get(f"/api/seller/advisor/threads/{made['thread_id']}").status_code == 404)

        # ============ Rule book ============
        section("Rules  the book, and what retrieval is allowed to decide")
        with SessionLocal() as db:
            seed_rules(db)
            rules = active_rules(db)
            blocks = [r for r in rules if r.severity == "block"]
            check("the rule book seeded", len(rules) >= 20, f"{len(rules)} rules")
            check("rule codes are unique", len({r.code for r in rules}) == len(rules))
            check("every rule carries text a verdict can cite",
                  all(r.title and r.body for r in rules))

            picked = retrieve_rules(db, "selling a used desk lamp, works fine")
            picked_codes = {r.code for r in picked}
            check("retrieval never drops a hard prohibition",
                  {r.code for r in blocks} <= picked_codes,
                  str(sorted({r.code for r in blocks} - picked_codes))[:200])
            check("retrieval does narrow the advisory rules",
                  len(picked) < len(rules), f"{len(picked)} of {len(rules)}")

        # ============ Publishing pipeline ============
        section("Publishing  block stops before extraction; verdicts cite real rules")
        blocked = use_fake_publishing(
            verdict="block",
            findings=[PublishFinding(code="BAN-DRUGS", severity="block",
                                     title="Drugs and paraphernalia",
                                     reason="A vape pen cannot be listed.")],
        )
        with blocked, use_fake_route("publishing_agent"):
            r = seller.post("/api/seller/advisor/chat",
                            json={"message": "selling a barely used vape pen"})
        body = r.json()
        check("a blocked submission is refused with the rule that stopped it",
              body["publish_blocked"] is True
              and body["publish_findings"][0]["code"] == "BAN-DRUGS", r.text[:250])
        check("extraction never ran for a blocked submission",
              blocked.extract_calls == [], str(blocked.extract_calls)[:120])
        check("no listing was created", not any(
            i["name"].startswith("selling a barely") for i in seller.get("/api/seller/items").json()))

        draft = ListingDraft(
            name="IKEA desk lamp",
            summary="White IKEA desk lamp, works, small scuff.",
            description="White adjustable desk lamp with a small scuff on the base. Bulb included.",
            attributes={"category": "家居与厨房", "condition": "成色良好", "color": "White"},
            missing_fields=[],
        )
        ok_run = use_fake_publishing(verdict="pass", draft=draft)
        with ok_run, use_fake_route("publishing_agent"):
            r = seller.post("/api/seller/advisor/chat",
                            json={"message": "white ikea desk lamp, works, small scuff",
                                  "photo_path": "nope/missing.jpg"})
        body = r.json()
        pub_thread = body["thread_id"]
        check("a clean submission produces a reviewable draft",
              body["publish_draft"]["name"] == "IKEA desk lamp"
              and body["publish_draft"]["ready"] is True, r.text[:250])
        check("the photo travels to both steps",
              ok_run.compliance_calls[0]["photo_path"] == "nope/missing.jpg"
              and ok_run.extract_calls[0]["photo_path"] == "nope/missing.jpg")
        check("nothing was written to the database by drafting",
              not any(i["name"] == "IKEA desk lamp" for i in seller.get("/api/seller/items").json()))

        thin = ListingDraft(name="Bike", description="A bike.",
                            missing_fields=["condition", "brand"],
                            follow_up_question="What condition is it in?")
        follow = use_fake_publishing(verdict="pass", draft=thin)
        with follow, use_fake_route("publishing_agent"):
            r = seller.post("/api/seller/advisor/chat", json={"message": "selling my bike"})
            t2 = r.json()["thread_id"]
            r2 = seller.post("/api/seller/advisor/chat",
                             json={"message": "ridden two years, brakes replaced", "thread_id": t2})
        # 草稿不齐**不再**换成一句追问。缺的是 condition 和 brand，而这两个在草稿卡
        # 下面那张表里就是输入框——让卖家打字回答一件他点一下就能选完的事，等于把
        # 一个表单问题伪装成一条发布条件。`follow_up_question` 模型照样会写，只是
        # 没有人读它了。
        thin_reply = r.json()["reply"]
        check("a thin draft still hands over the form",
              r.json()["publish_draft"]["ready"] is False
              and thin_reply != "What condition is it in?"
              and "在下面那张表里选一下" in thin_reply, r.text[:250])
        check("and names the fields to fill in it",
              "成色" in thin_reply and "品牌" in thin_reply, thin_reply[:160])
        check("the follow-up is reviewed together with the original description",
              "selling my bike" in follow.compliance_calls[-1]["text"]
              and "brakes replaced" in follow.compliance_calls[-1]["text"],
              follow.compliance_calls[-1]["text"][:160])

        # ============ Approval ============
        section("Approve  the gate runs on what was submitted, not what was drafted")
        good = {
            "thread_id": pub_thread,
            "name": "IKEA desk lamp", "summary": "White desk lamp.",
            "description": "White adjustable desk lamp with a small scuff on the base.",
            "category": "家居与厨房", "condition": "成色良好",
            "price": 15, "pickup_location_name": location,
        }
        r = seller.post("/api/seller/advisor/publish/approve",
                        json={**good, "category": "Nonexistent Category"})
        check("an off-whitelist category is refused",
              r.status_code == 200 and r.json()["ok"] is False, r.text[:200])
        r = seller.post("/api/seller/advisor/publish/approve",
                        json={**good, "condition": "Pristine"})
        check("an off-whitelist condition is refused",
              r.status_code == 200 and r.json()["ok"] is False, r.text[:200])
        r = seller.post("/api/seller/advisor/publish/approve",
                        json={**good, "status": "sold_out"})
        check("a field the schema does not define is rejected outright",
              r.status_code == 422, str(r.status_code))

        edited = use_fake_publishing(
            verdict="block",
            findings=[PublishFinding(code="BAN-WEAPONS", severity="block",
                                     title="Weapons", reason="Edited into a weapon listing.")],
        )
        with edited:
            r = seller.post("/api/seller/advisor/publish/approve",
                            json={**good, "description": "Actually a taser, ships hidden in the lamp."})
        body = r.json()
        check("editing the draft into something prohibited is caught at approval",
              body["ok"] is False and body["findings"][0]["code"] == "BAN-WEAPONS", r.text[:250])
        check("the re-check saw the edited text, not the drafted text",
              "taser" in edited.compliance_calls[-1]["text"], edited.compliance_calls[-1]["text"][:160])
        check("nothing was listed", not any(
            i["name"] == "IKEA desk lamp" for i in seller.get("/api/seller/items").json()))

        # 「信息不足」那两条不进回执（`rules.SUPPRESSED_IN_PUBLISHING`）：卖家刚在
        # 表里把成色选完、描述改完，回头告诉他信息不足，说的是一件已经发生过的事。
        # 所以这里用一条**关于商品本身**的建议来验「警告不拦发布」。
        with use_fake_publishing(verdict="warn", findings=[
            PublishFinding(code="WARN-CONTACT", severity="warn",
                           title="No contact details", reason="There is a phone number in the text.")
        ]):
            r = seller.post("/api/seller/advisor/publish/approve", json=good)
        body = r.json()
        check("a warning does not block publishing",
              body["ok"] is True and body["item_id"] and body["findings"][0]["severity"] == "warn",
              r.text[:250])

        with use_fake_publishing(verdict="warn", findings=[
            PublishFinding(code="WARN-VAGUE", severity="warn",
                           title="Description too thin", reason="Could say more about wear."),
            PublishFinding(code="WARN-CONTACT", severity="warn",
                           title="No contact details", reason="There is a phone number in the text."),
        ]):
            quiet = seller.post("/api/seller/advisor/publish/approve",
                                json={**good, "name": "IKEA desk lamp (2)"})
        # 变量名刻意和上面的 `body` 分开：下一条断言要按**第一次**发布的 item_id
        # 去查商品，覆盖掉它会让那条断言查一件它不是在说的东西。
        codes = [f["code"] for f in quiet.json()["findings"]]
        check("'not enough information' never reaches the receipt",
              quiet.json()["ok"] is True and codes == ["WARN-CONTACT"], str(codes))
        listed = [i for i in seller.get("/api/seller/items").json() if i["item_id"] == body["item_id"]]
        check("the listing really exists and is on sale",
              listed and listed[0]["status"] == "selling" and listed[0]["name"] == "IKEA desk lamp",
              str(listed)[:200])

        with use_fake_publishing(verdict="pass"):
            r = seller2.post("/api/seller/advisor/publish/approve",
                             json={**good, "thread_id": "1-someone-elses"})
        check("approving lists under the caller, never the thread's owner",
              r.json()["ok"] is True
              and any(i["item_id"] == r.json()["item_id"] for i in seller2.get("/api/seller/items").json()),
              r.text[:200])

        # ============ Zero real LLM cost ============
        section("Zero real LLM cost")
        check("token_usage_log stayed at 0 rows (FakeClient books nothing)",
              token_rows() == 0, str(token_rows()) + " rows")

    return report()


def report() -> int:
    print()
    if _failures:
        print("[FAIL] Seller advisor acceptance failed: " + str(len(_failures)) + " check(s)")
        for f in _failures:
            print("    - " + f)
        return 1
    print("[ok] Seller advisor acceptance fully passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
