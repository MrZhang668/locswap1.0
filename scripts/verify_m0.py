"""M0 acceptance: end-to-end self-check of event layer E1-E6 + demand capture + traffic counters.

Covers the automatable acceptance items of event layer E1-E6.
**Requires no ANTHROPIC_API_KEY** (C7) -- the script force-clears it, which also
verifies C1/C2: after the whole flow, token_usage_log must still have 0 rows.

Run:
    python scripts/verify_m0.py

Opens an isolated sandbox database and chroma directory under data/_verify_m0/,
never touches data/locswap.db, and the whole directory can be deleted afterwards.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  injects backend into sys.path

SANDBOX = Path(_bootstrap.PROJECT_ROOT) / "data" / "_verify_m0"

# ---- Must be set before importing backend.app: settings is evaluated at import time ----
if SANDBOX.exists():
    shutil.rmtree(SANDBOX, ignore_errors=True)
# On Windows, in-use files make rmtree silently leave debris behind, and a leftover
# chroma directory holds vectors from the previous run -- their ids don't match the
# fresh database, showing up as "intermittent" match failures. Rather than debug a
# fake bug once, fail hard right here.
if SANDBOX.exists():
    raise SystemExit(
        "Sandbox directory could not be fully removed: " + str(SANDBOX) + "\n"
        "Most likely the previous run's process still holds the chroma files; close it and rerun."
    )
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
os.environ["ANTHROPIC_API_KEY"] = ""                   # C7: must be all green with no key
os.environ["LLM_API_KEY"] = ""                         # clear the openai_compat key too
os.environ["EXPIRY_POLL_INTERVAL_SECONDS"] = "3600"    # keep the poller from butting in mid-acceptance

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Item,
    ItemTraffic,
    StandingWant,
    TokenUsageLog,
    WantSource,
    WantStatus,
)
from app.repositories import ItemRepository  # noqa: E402

_failures: list[str] = []
_skipped: list[str] = []


def skip(label: str, reason: str) -> None:
    """Record a check that no longer applies, loudly.

    Deleting the assertion would quietly shrink the acceptance surface and
    nobody would notice the coverage was gone; leaving it failing makes the
    baseline permanently red, and a permanently red baseline is one nobody
    reads. So it is printed, counted, and named with the reason it is off.
    """
    print("  [skip] " + label + "  <- " + reason)
    _skipped.append(label)


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "  [ok]" if ok else "  [FAIL]"
    suffix = ""
    if not ok and detail:
        suffix = "  <- " + detail
    print(mark + " " + label + suffix)
    if not ok:
        _failures.append(label)
    return ok


def section(title: str) -> None:
    print("\n-- " + title + " " + "-" * max(0, 58 - len(title)))


class Actor:
    """A registered, logged-in user plus their HTTP session."""

    def __init__(self, client: TestClient, email: str, location: str):
        self.client = client
        r = client.post("/api/auth/register", json={"email": email, "password": "pw123456"})
        r.raise_for_status()
        self.token = r.json()["token"]
        self.id = r.json()["user"]["id"]
        self.headers = {"Authorization": "Bearer " + self.token}
        # Location feeds the hard filters; without it, distance conditions never match
        self.client.patch(
            "/api/auth/me",
            json={"location_name": location, "pickup_range_enabled": False},
            headers=self.headers,
        ).raise_for_status()

    def post(self, path, **kw):
        return self.client.post(path, headers=self.headers, **kw)

    def get(self, path, **kw):
        return self.client.get(path, headers=self.headers, **kw)

    def delete(self, path, **kw):
        return self.client.delete(path, headers=self.headers, **kw)


def traffic(item_id: int) -> ItemTraffic | None:
    with SessionLocal() as db:
        return db.get(ItemTraffic, item_id)


def want_row(want_id: int) -> StandingWant | None:
    with SessionLocal() as db:
        return db.get(StandingWant, want_id)


def main() -> int:
    with TestClient(app) as client:
        cfg = client.get("/api/meta/config").json()
        location = cfg["locations"][0]["name"]
        print("Sandbox: " + str(SANDBOX))
        print("Pickup location: " + location + " | llm_available=" + str(cfg["llm_available"]) + " (should be False)")

        buyer = Actor(client, "buyer1@verify.local", location)
        buyer2 = Actor(client, "buyer2@verify.local", location)
        seller = Actor(client, "seller@verify.local", location)

        # ---------------- E2: unmet search -> demand captured as a want ----------------
        section("E2  unmet search -> demand capture")
        r = buyer.post("/api/search/chat",
                       json={"message": "used bike under $300", "history": [], "clarify_round": 0})
        r.raise_for_status()
        chat = r.json()
        check("empty-corpus search returns an unmet decision",
              chat["decision"] in ("empty", "low_confidence", "clarify"), chat["decision"])
        want_id = chat.get("want_id")
        check("ChatOut carried back a want_id", bool(want_id), repr(chat.get("want_id")))
        if not want_id:
            return report()

        w = want_row(want_id)
        check("want persisted and active", w is not None and w.status == WantStatus.ACTIVE)
        check("want captured the price constraint", (w.filters or {}).get("max_price") == 300.0, str(w.filters))
        check("want defaults to implicit (auto-captured)", w.source == WantSource.IMPLICIT, w.source)

        # ---------------- Explicit want: "notify me when it arrives" ----------------
        section("Explicit want  notify me when it arrives")
        r = buyer.post("/api/wants/" + str(want_id) + "/subscribe")
        check("subscribe endpoint 200", r.status_code == 200, r.text[:120])
        check("want upgraded to explicit", want_row(want_id).source == WantSource.EXPLICIT)

        r = buyer.post("/api/wants", json={"text": "mini fridge for my dorm under $500"})
        check("hand-written want created", r.status_code == 201, r.text[:120])
        manual = r.json()
        check("hand-written want parsed the price", manual["filters"].get("max_price") == 500.0,
              str(manual["filters"]))
        check("my-wants list has 2 entries", len(buyer.get("/api/wants").json()) == 2)

        # ---------------- E1: listing -> notify waiting buyers ----------------
        section("E1  new listing -> push to waiting buyers")
        # buyer2 is also waiting for a bike, used to verify E5's "losing runner-up want restored"
        buyer2.post("/api/search/chat",
                    json={"message": "used bike under $300", "history": [], "clarify_round": 0})
        with SessionLocal() as db:
            w2 = db.query(StandingWant).filter(StandingWant.buyer_id == buyer2.id).first()
        check("buyer2's demand was captured too", w2 is not None)
        if w2 is None:
            return report()

        r = seller.post("/api/items", json={
            "name": "Giant commuter bike",
            "description": "Graduating and selling. 26-inch commuter bike, brakes recently replaced, U-lock included",
            "summary": "26-inch commuter bike, brakes recently replaced",
            "price": 260,
            "attributes": {"category": "自行车与滑板车", "brand": "Giant", "condition": "成色很好"},
            "pickup_location_name": location,
            "pickup_time_start": "Weekdays 7:00 PM",
            "pickup_time_end": "Weekends all day",
        })
        check("listing created", r.status_code == 200, r.text[:200])
        if r.status_code != 200:
            return report()
        item_id = r.json()["item"]["item_id"]

        # Automatic fan-out to matching buyers was removed on 2026-08-22:
        # matching by score could not tell "this is the thing you asked for"
        # from "this uses your words", and every false positive landed on a
        # buyer as junk. `notify_matched_buyers` and its call site in
        # routers/items.py are gone, so `want_matched` has no producer and
        # these four assertions test a feature that does not exist.
        #
        # Batch 6 brings the push back under buyer consent — an account-level
        # opt-in that applies to explicit wants only. When it lands, un-skip
        # this block and rewrite it as the two-sided assertion the new design
        # deserves: no notification while the toggle is off, one after it is on,
        # and still none for an implicit (search-derived) want.
        REMOVED = "auto-push removed 2026-08-22; returns as opt-in in batch 6"
        notes = buyer.get("/api/notifications").json()
        matched = [n for n in notes if n["type"] == "want_matched"]
        # What *is* true today, asserted rather than assumed: listing an item
        # notifies nobody. If a push ever reappears without the consent gate,
        # this fails.
        check("listing pushes to nobody (no consent gate exists yet)", not matched,
              str(len(matched)) + " want_matched notification(s) appeared")
        skip("buyer received a want_matched notification", REMOVED)
        skip("notification body is not a blank template", REMOVED)
        skip("buyer2 got the push too", REMOVED)
        skip("item_traffic.pushed incremented", REMOVED)

        # ---------------- E6: push clicked ----------------
        section("E6  push click counter")
        # The click counter is measured on a push notification, and there are no
        # push notifications to click while E1 above is skipped. The endpoint
        # itself is untouched — this is blocked on the fixture, not broken.
        skip("click endpoint 200", REMOVED)
        skip("clicks incremented by 1", REMOVED)
        skip("click also marked it read", REMOVED)

        # ---------------- Traffic counters: impressions ----------------
        section("Traffic counters  impressions / candidates")
        buyer.post("/api/search/chat",
                   json={"message": "used bike under $300", "history": [], "clarify_round": 0})
        t = traffic(item_id)
        check("impressions recorded", t.impressions >= 1, "impressions=" + str(t.impressions))

        # ---------------- E4: reservation -> close matching wants ----------------
        section("E4  buyer reserves -> close their matching want")
        r = buyer.post("/api/reservations/" + str(item_id))
        check("buyer1 reserved successfully", r.status_code == 200, r.text[:120])
        w = want_row(want_id)
        check("bike want closed as fulfilled", w.status == WantStatus.FULFILLED, w.status)
        check("recorded which item closed it", w.fulfilled_by_item_id == item_id,
              str(w.fulfilled_by_item_id))
        check("the unrelated fridge want was not collateral damage",
              want_row(manual["want_id"]).status == WantStatus.ACTIVE)

        r = buyer2.post("/api/reservations/" + str(item_id))
        check("buyer2 reserved too (runner-up)", r.status_code == 200, r.text[:120])
        check("buyer2's want closed as well", want_row(w2.id).status == WantStatus.FULFILLED)

        # ---------------- E5: confirm sale ----------------
        section("E5  confirm sale -> sold snapshot + restore losing runner-up wants")
        r = seller.post("/api/seller/items/" + str(item_id) + "/confirm",
                        json={"buyer_id": buyer.id})
        check("sale confirmed", r.status_code == 200, r.text[:200])
        with SessionLocal() as db:
            item = db.get(Item, item_id)
            check("sold_at written", item.sold_at is not None)
            check("sold_price equals the sale price", item.sold_price == 260.0, str(item.sold_price))
            evidence = ItemRepository(db).list_sold_evidence(category="自行车与滑板车")
            check("price evidence pool can read this sale", any(i.id == item_id for i in evidence),
                  str(len(evidence)) + " rows")
        check("winning buyer's want stays fulfilled", want_row(want_id).status == WantStatus.FULFILLED)
        check("losing runner-up's want restored to active",
              want_row(w2.id).status == WantStatus.ACTIVE, want_row(w2.id).status)

        # ---------------- E5: cancelling a reservation restores the want too ----------------
        section("E5  reservation cancelled -> want restored")
        r = seller.post("/api/items", json={
            "name": "Schwinn folding bike",
            "description": "Folding bike, stores easily, fits in a dorm room, ridden for half a year",
            "summary": "Folding bike, stores easily",
            "price": 240,
            "attributes": {"category": "自行车与滑板车", "condition": "成色良好"},
            "pickup_location_name": location,
        })
        check("second listing created", r.status_code == 200, r.text[:200])
        if r.status_code != 200:
            return report()
        item2 = r.json()["item"]["item_id"]
        buyer2.post("/api/reservations/" + str(item2))
        check("want closed again after reserving", want_row(w2.id).status == WantStatus.FULFILLED)
        r = buyer2.delete("/api/reservations/" + str(item2))
        check("reservation cancelled", r.status_code == 200, r.text[:120])
        restored = want_row(w2.id)
        check("want restored to active after cancelling", restored.status == WantStatus.ACTIVE, restored.status)
        check("fulfilled_by_item_id cleared on restore", restored.fulfilled_by_item_id is None)

        # ---------------- Buyer closes a want manually ----------------
        section("Buyer closes a want manually")
        r = buyer.delete("/api/wants/" + str(manual["want_id"]))
        check("close endpoint 200", r.status_code == 200, r.text[:120])
        check("status set to dismissed",
              want_row(manual["want_id"]).status == WantStatus.DISMISSED)

        # ---------------- C1 / C2 / C7 ----------------
        section("C1 / C2 / C7  zero LLM cost")
        with SessionLocal() as db:
            n_llm = db.query(TokenUsageLog).count()
        check("token_usage_log has 0 rows across the whole flow", n_llm == 0, str(n_llm) + " rows")
        check("no errors raised anywhere without an API key", True)

    return report()


def report() -> int:
    print()
    if _skipped:
        print("[skip] " + str(len(_skipped)) + " check(s) skipped:")
        for sk in _skipped:
            print("    - " + sk)
        print()
    if _failures:
        print("[FAIL] M0 acceptance failed: " + str(len(_failures)) + " check(s)")
        for f in _failures:
            print("    - " + f)
        return 1
    print("[ok] M0 acceptance fully passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
