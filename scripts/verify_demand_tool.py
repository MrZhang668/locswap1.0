"""Acceptance for analyze_demand (batch 3). No API key, no tokens — the tool is SQL.

The checks are grouped by what would go wrong if they failed, because two of
these groups are the reason the tool is allowed to exist at all:

**Privacy (constraint C8).** This is the first tool that reports *aggregate
buyer behaviour* to sellers, which is a new exposure surface: counts are safe,
individual wording is not. The output schema has no slot for a buyer id, and
search phrasings are withheld below a floor of distinct users. Both are checked
against the real database rather than asserted about the code, because the
guarantee that matters is what actually comes out.

**Ownership.** Same boundary as every other item-scoped tool: an item_id the
seller does not own must not resolve, no matter how the model got hold of it.

**Honest emptiness.** "Nobody searched for this" and "we have no data" read the
same to a seller and mean opposite things. Silence must come back as a stated
measurement with a caveat, not as an empty object.

    python scripts/verify_demand_tool.py
"""

from __future__ import annotations

import sys
from datetime import timedelta

import _bootstrap  # noqa: F401  injects backend into sys.path

from pydantic import ValidationError  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import DemandEvent, DemandEventKind, Item, ItemStatus, utcnow  # noqa: E402
from app.services.seller_advisor import tools as T  # noqa: E402
from app.services.seller_advisor.schemas import (  # noqa: E402
    MIN_BUYERS_TO_SHOW_PHRASE,
    AnalyzeDemandInput,
    AnalyzeDemandOutput,
)

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  [ok] " if ok else "  [FAIL] ") + label + ("" if ok else "  <- " + detail))
    if not ok:
        _failures.append(label)
    return ok


def section(t: str) -> None:
    print("\n-- " + t + " " + "-" * max(0, 56 - len(t)))


def main() -> int:
    db = SessionLocal()
    try:
        live = db.query(Item).filter(Item.status == ItemStatus.SELLING).all()
        if not live:
            print("no live listings — run the seed and scripts/simulate_platform.py first")
            return 2
        item = next((i for i in live if (i.attributes or {}).get("category")), live[0])
        out = T.analyze_demand(db, item.seller_id, AnalyzeDemandInput(item_id=item.id))

        section("ownership")
        other = next((i for i in live if i.seller_id != item.seller_id), None)
        if other is None:
            check("a second seller exists to test against", False, "single-seller dataset")
        else:
            try:
                T.analyze_demand(db, item.seller_id, AnalyzeDemandInput(item_id=other.id))
                check("another seller's item does not resolve", False, "it returned data")
            except T.ToolError:
                check("another seller's item does not resolve", True)
        try:
            T.analyze_demand(db, item.seller_id, AnalyzeDemandInput(item_id=999_999))
            check("a nonexistent item_id does not resolve", False, "it returned data")
        except T.ToolError:
            check("a nonexistent item_id does not resolve", True)

        section("privacy — C8")
        dumped = out.model_dump_json()
        check("no buyer id anywhere in the payload",
              "buyer_id" not in dumped, "buyer_id appears in the output")
        check("the output schema has no buyer-id field",
              "buyer_id" not in AnalyzeDemandOutput.model_fields)
        check("every reported phrase is above the k-anonymity floor",
              all(p.people >= MIN_BUYERS_TO_SHOW_PHRASE for p in out.top_phrases),
              str([(p.phrase, p.people) for p in out.top_phrases]))

        # Prove the floor bites: count what the raw table holds vs what came out.
        raw: dict[str, set[int]] = {}
        for ev in db.query(DemandEvent).filter(
            DemandEvent.kind == DemandEventKind.SEARCH,
            DemandEvent.category == out.category,
        ):
            raw.setdefault((ev.query or "").lower(), set()).add(ev.buyer_id)
        rare = {p for p, b in raw.items() if 0 < len(b) < MIN_BUYERS_TO_SHOW_PHRASE}
        shown = {p.phrase for p in out.top_phrases}
        check(f"naturally rare phrasings are withheld ({len(rare)} below the floor)",
              not (rare & shown), str(sorted(rare & shown)[:3]))

        # The check above passes vacuously when the dataset happens to contain no
        # rare phrasing — and a privacy guarantee that is only ever exercised by
        # luck is not a tested guarantee. So provoke it: write one phrase used by
        # a single buyer, confirm it cannot come back out, and roll it away.
        secret = "想给室友玛丽挑个生日礼物"
        buyer = db.query(DemandEvent).filter(
            DemandEvent.kind == DemandEventKind.SEARCH).first()
        if buyer is not None:
            # Inside the default window on purpose. Stamped with the row's own
            # timestamp the probe proved nothing: that row sits in June, outside
            # last_30d, so the phrase was never a candidate for disclosure and
            # "it did not leak" was true for the wrong reason.
            db.add(DemandEvent(buyer_id=buyer.buyer_id, kind=DemandEventKind.SEARCH,
                               query=secret, category=out.category,
                               at=utcnow() - timedelta(days=1)))
            db.flush()
            probed = T.analyze_demand(db, item.seller_id, AnalyzeDemandInput(item_id=item.id))
            leaked = secret in probed.model_dump_json()
            check("a phrase used by one person cannot reach the seller", not leaked,
                  "the single-user phrase appeared in the output")
            check("and the seller is told a phrasing was withheld",
                  any("隐去" in c for c in probed.caveats), str(probed.caveats))
            db.rollback()
            # Re-open the read side after the rollback.
            db.expire_all()

        section("schema is closed")
        try:
            AnalyzeDemandOutput(**{**out.model_dump(), "buyer_ids": [1, 2]})
            check("extra fields are refused", False, "an extra field was accepted")
        except ValidationError:
            check("extra fields are refused", True)
        try:
            AnalyzeDemandInput(item_id=item.id, window="since_monday")
            check("free-text window refused", False, "accepted an unlisted window")
        except ValidationError:
            check("free-text window refused", True)

        section("bucketing and windows")
        wk = T.analyze_demand(db, item.seller_id, AnalyzeDemandInput(item_id=item.id, window="last_90d"))
        dy = T.analyze_demand(db, item.seller_id, AnalyzeDemandInput(item_id=item.id, window="last_7d"))
        check("90 days buckets weekly", wk.bucket == "week", wk.bucket)
        check("7 days buckets daily", dy.bucket == "day", dy.bucket)
        check("a longer window sees at least as much", wk.total_events >= dy.total_events,
              f"90d={wk.total_events} 7d={dy.total_events}")
        check("series is sorted by period", [b.period for b in wk.series] == sorted(b.period for b in wk.series))
        check("no bucket claims more people than events",
              all(b.people <= b.events for b in wk.series if b.events),
              str([(b.period, b.people, b.events) for b in wk.series if b.people > b.events][:3]))
        check("people matching this item never exceeds the category total",
              wk.people_matching_item <= wk.total_people,
              f"{wk.people_matching_item} > {wk.total_people}")

        section("trend is computed, not guessed")
        mk = lambda *ev: [  # noqa: E731
            type(wk.series[0])(period=f"p{i}", people=1, events=e, views=0)
            for i, e in enumerate(ev)
        ]
        check("rising is detected", T._trend(mk(2, 3, 10, 12))[0] == "rising")
        check("falling is detected", T._trend(mk(12, 10, 3, 2))[0] == "falling")
        check("noise is not a trend", T._trend(mk(8, 9, 8, 9))[0] == "flat")
        check("too few buckets is 'insufficient', not 'flat'",
              T._trend(mk(5, 6, 7))[0] == "insufficient_data")

        section("honest emptiness")
        no_cat = next((i for i in live if not (i.attributes or {}).get("category")), None)
        if no_cat is None:
            print("  [skip] no uncategorised listing in this dataset to test")
        else:
            o = T.analyze_demand(db, no_cat.seller_id, AnalyzeDemandInput(item_id=no_cat.id))
            check("an uncategorised listing says why, rather than returning the platform's demand",
                  o.total_events == 0 and o.caveats, str(o.caveats))
        check("a normal result still carries the market-vs-listing caveat",
              any("listing_traffic" in c for c in out.caveats), str(out.caveats))
        t = out.listing_traffic
        check("click-through is 'n/a' when never shown, not 0%",
              (t.click_through == "n/a") == (t.impressions == 0),
              f"impressions={t.impressions} ctr={t.click_through}")

        print()
        if _failures:
            print(f"[FAIL] {len(_failures)} check(s):")
            for f in _failures:
                print("    - " + f)
            return 1
        print("[ok] analyze_demand acceptance passed")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
