"""Acceptance for the router's scenario + slots extraction (batch 2).

Two halves, deliberately separated by what they cost:

**Offline** (default, no API key, no tokens): everything that is the backend's
job rather than the model's — the closed-set validation, the category
whitelist, the scenario reset on non-sales routes, the hint block the
specialist receives. These are the parts that must hold no matter what the
model returns, so they are tested against handcrafted decisions including
malformed ones, which a live model would rarely produce on demand.

**Live** (--live, spends tokens): does a real model, given a real message,
extract what a person would say it should. Sixteen messages with the routing,
scenario and the slots that matter asserted; slots not named in a case are not
checked, because "did not extract a timeframe nobody mentioned" is the correct
behaviour and pinning every field would make the suite fail on harmless
variation.

    python scripts/verify_router_slots.py
    python scripts/verify_router_slots.py --live
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401  injects backend into sys.path

from app.services.agents import router as R  # noqa: E402
from app.services.agents.sales.orchestrator import _routing_block  # noqa: E402

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  [ok] " if ok else "  [FAIL] ") + label + ("" if ok else "  <- " + detail))
    if not ok:
        _failures.append(label)
    return ok


def section(t: str) -> None:
    print("\n-- " + t + " " + "-" * max(0, 56 - len(t)))


# ============================================================
# Offline: the guarantees that do not depend on the model
# ============================================================
def offline() -> None:
    section("closed set — the schema refuses what it does not know")
    from pydantic import ValidationError

    try:
        R.RouteSlots(colour="red")
        check("unknown slot name rejected", False, "extra field was accepted")
    except ValidationError:
        check("unknown slot name rejected", True)
    try:
        R.RouteSlots(time_window="since_tuesday")
        check("free-text time_window rejected", False, "accepted an unlisted bucket")
    except ValidationError:
        check("free-text time_window rejected", True)
    try:
        R.RouteDecision(agent="billing_agent", rewritten_query="x")
        check("unknown agent rejected", False, "accepted a made-up route")
    except ValidationError:
        check("unknown agent rejected", True)

    section("server-side recomputation")
    sc, sl, dropped, _w = R._sanitize("sales_agent", "demand", R.RouteSlots(category="Gaming Consoles"))
    check("invented category is dropped, not corrected", sl.category == "" and dropped,
          f"category={sl.category!r} dropped={dropped}")
    sc, sl, dropped, _w = R._sanitize("sales_agent", "price", R.RouteSlots(category="电子产品"))
    check("real category survives", sl.category == "电子产品" and not dropped, str(dropped))
    sc, sl, dropped, _w = R._sanitize("publishing_agent", "price", R.RouteSlots())
    check("scenario reset off the sales route", sc == "none" and dropped, f"scenario={sc}")
    sc, sl, dropped, _w = R._sanitize("clarify", "both", R.RouteSlots())
    check("clarify carries no scenario", sc == "none")
    for bad in (-5.0, 0.0, 10_000_000.0):
        sc, sl, dropped, _w = R._sanitize("sales_agent", "price", R.RouteSlots(price_hint=bad))
        if not check(f"impossible price {bad} dropped", sl.price_hint is None):
            break
    sc, sl, dropped, _w = R._sanitize("sales_agent", "price", R.RouteSlots(price_hint=260.0))
    check("real price survives", sl.price_hint == 260.0)

    section("the hint handed to the specialist")
    empty = _routing_block("none", {})
    check("no hint block when nothing was extracted", empty == "", repr(empty[:60]))
    block = _routing_block("demand", {"item_ref": "the bike", "time_window": "last_30d",
                                      "category": "", "price_hint": None})
    check("empty slots are omitted, not sent as blanks",
          "category" not in block and "price_hint" not in block, block)
    check("filled slots are present", '"item_ref": "the bike"' in block and "last_30d" in block, block)
    # Wording is Chinese since the 2026-09-15 localisation; this assertion was
    # still looking for the English original and had been failing ever since.
    check("hint is labelled as a hint, not an instruction",
          "它是提示，不是约束" in block and "一律忽略" in block, block)

    section("routing failure still degrades to a question")
    out = R._fallback("simulated", "sell my bike")
    check("fallback is a clarify", out.agent == "clarify")
    check("fallback carries no scenario", out.scenario == "none", out.scenario)
    check("fallback carries empty slots", out.slots == R.RouteSlots(), str(out.slots))


# ============================================================
# Live: does a real model extract what a person would?
# ============================================================
LIVE_CASES: list[tuple[str, dict]] = [
    ("is $260 too much for the bike?",
     {"agent": "sales_agent", "scenario": "price", "price_hint": 260.0}),
    # "who is looking" sits genuinely between the two: it can mean "how many
    # people" (a statistic) or "give me them" (a list). Both readings are
    # defensible, so the case asserts the route and leaves the scenario open —
    # pinning one would be testing a preference, not a requirement.
    ("who's looking for my kettle right now?",
     {"agent": "sales_agent", "scenario_in": ("demand", "find_buyers")}),
    ("is anyone searching for bikes, and is 260 a fair price?",
     {"agent": "sales_agent", "scenario": "both"}),
    ("give me the list of buyers I should message about the desk",
     {"agent": "sales_agent", "scenario": "find_buyers"}),
    ("how did electronics sell over the past week?",
     {"agent": "sales_agent", "time_window": "last_7d", "category": "电子产品"}),
    ("what's demand been like this semester?",
     {"agent": "sales_agent", "scenario": "demand", "time_window": "last_90d"}),
    ("has interest in my listing changed since I put it up last month?",
     {"agent": "sales_agent", "scenario": "demand", "time_window": "last_30d"}),
    ("which of my listings are still up?",
     {"agent": "sales_agent", "scenario": "none"}),
    ("i want to sell my old mehcanical keyborad",
     {"agent": "publishing_agent", "scenario": "none", "spell": "mechanical keyboard"}),
    # Deliberately not a bicycle: the fixture seller already has a bike listed,
    # so "selling a bycicle" really is ambiguous between listing a new one and
    # asking about the existing one, and clarify would be the right answer. The
    # typo is what this case is for, so it uses a noun that collides with nothing.
    ("selling my old tennis rakcet, barely used",
     {"agent": "publishing_agent", "spell": "racket"}),
    ("help me write a description for this",
     {"agent": "publishing_agent", "scenario": "none"}),
    ("what's the weather like",
     {"agent": "clarify"}),
    ("how much are textbooks going for overall?",
     {"agent": "sales_agent", "scenario": "price", "time_window": "all",
      "category": "图书与教材"}),
    ("any interest in camping gear?",
     {"agent": "sales_agent", "scenario": "demand", "category": "运动与户外"}),
    ("should I drop the price on the microwave?",
     {"agent": "sales_agent", "scenario": "price", "item_ref": "microwave"}),
    ("thanks!", {"agent": "clarify"}),
]

LISTINGS = [
    {"item_id": 1, "name": "Giant commuter bike", "price": 260.0, "category": "自行车与滑板车"},
    {"item_id": 2, "name": "Hamilton Beach electric kettle", "price": 18.0, "category": "家居与厨房"},
    {"item_id": 3, "name": "Standing desk", "price": 120.0, "category": "家具与电器"},
    {"item_id": 4, "name": "Silver 900W microwave", "price": 69.0, "category": "家居与厨房"},
    {"item_id": 5, "name": "2-person camping tent", "price": 55.0, "category": "运动与户外"},
]


def live() -> None:
    section("live extraction (spends tokens)")
    for message, want in LIVE_CASES:
        res = R.route(message=message, history=[], has_active_items=True,
                      listings=LISTINGS, has_photo=False)
        label = message[:44]
        ok = True
        detail = []
        if res.agent != want["agent"]:
            ok = False
            detail.append(f"agent={res.agent} want={want['agent']}")
        if "scenario" in want and res.scenario != want["scenario"]:
            ok = False
            detail.append(f"scenario={res.scenario} want={want['scenario']}")
        if "scenario_in" in want and res.scenario not in want["scenario_in"]:
            ok = False
            detail.append(f"scenario={res.scenario} want one of {want['scenario_in']}")
        for key in ("time_window", "category", "price_hint", "item_ref"):
            if key not in want:
                continue
            got = getattr(res.slots, key)
            if key == "item_ref":
                if want[key].lower() not in str(got).lower():
                    ok = False
                    detail.append(f"item_ref={got!r} should mention {want[key]!r}")
            elif got != want[key]:
                ok = False
                detail.append(f"{key}={got!r} want={want[key]!r}")
        if "spell" in want and want["spell"].lower() not in res.rewritten_query.lower():
            ok = False
            detail.append(f"rewrite did not fix the typo: {res.rewritten_query!r}")
        if res.dropped_slots:
            detail.append(f"dropped={res.dropped_slots}")
        check(label, ok, "; ".join(detail))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also run the model-dependent cases")
    args = ap.parse_args()

    offline()
    if args.live:
        ok, reason = __import__(
            "app.services.llm.langchain_client", fromlist=["llm_configured"]
        ).llm_configured()
        if not ok:
            print(f"\n--live requested but no model is configured: {reason}")
            return 2
        live()
    else:
        print("\n(offline only — pass --live to also check what the model extracts)")

    print()
    if _failures:
        print(f"[FAIL] {len(_failures)} check(s):")
        for f in _failures:
            print("    - " + f)
        return 1
    print("[ok] router slots acceptance passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
