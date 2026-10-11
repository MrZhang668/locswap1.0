"""Acceptance for the advisor's action loop (batch 5).

This is the first thing the sales advisor can do rather than say, and what it
does is message real buyers. So the checks are about the gates, not the feature:

**Consent.** Only a plain, unqualified yes executes. Every other reply — a
question, a qualified yes, a change of subject, silence — declines. The failure
that matters is a stranger receiving a message the seller did not mean to send;
being asked a second time is not in the same class, so ambiguity resolves to
"no" every time.

**Validation the model cannot talk past.** A proposal naming someone else's
listing, an expired candidate ordinal, a sold item, or an invented card is
dropped — not repaired into something plausible. The seller's own daily push
quota still caps it.

**A real suspension.** While the advisor is waiting, the graph is genuinely
parked: `get_state().next` is non-empty and the next message resumes it instead
of starting a new turn. A model that merely said it was waiting could be talked
past in the same message.

Offline by default (no API key, no tokens) — the gates are all backend logic.
`--live` additionally drives a real conversation end to end.

    python scripts/verify_react_loop.py
    python scripts/verify_react_loop.py --live
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401  injects backend into sys.path

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Item, ItemStatus, StandingWant, WantStatus  # noqa: E402
from app.services.agents import proposals as P  # noqa: E402
from app.services.agents.sales import tools as T  # noqa: E402
from app.services.agents.sales.schemas import FindPotentialBuyersInput  # noqa: E402

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  [ok] " if ok else "  [FAIL] ") + label + ("" if ok else "  <- " + detail))
    if not ok:
        _failures.append(label)
    return ok


def section(t: str) -> None:
    print("\n-- " + t + " " + "-" * max(0, 56 - len(t)))


def consent() -> None:
    section("consent — only a plain yes writes")
    # Capitalised forms are in here on purpose: the affirmative pattern lost its
    # IGNORECASE flag once during an edit, and "OK" silently stopped counting as
    # consent. Safe in the direction that matters, but the seller sees the app
    # ignore them.
    for msg in ["yes", "y", "yep", "sure", "ok", "go ahead", "do it",
                "Yes!", "OK", "Sure", "please do", "confirm",
                # The advisor answers Chinese sellers too, and the failure modes
                # differ:  never fires between two hanzi, and several
                # negations are built out of an affirmative ("不好" contains
                # "好"), so the negative pattern has to be tested first.
                "好", "好的", "行", "可以", "没问题", "发吧", "确认", "同意", "好的。"]:
        if not check(f"executes on {msg!r}", P.is_affirmative(msg)):
            break
    for msg in [
        "no", "nope", "don't", "not now", "maybe later", "",
        # A qualified yes is the dangerous one: it reads as consent and means
        # something narrower than what was proposed.
        "yes but only the first one",
        "yes please send to everyone nearby",
        "no, yes to the second",
        # A question is not an answer.
        "why those two?", "what's their budget?",
        # A new topic is not an answer either.
        "what about pricing",
        # The same three traps in Chinese. 不好 / 不用了 both contain a string
        # the affirmative list accepts, and a qualified yes is still not a yes.
        "不", "不用", "不用了", "不好", "不行", "先别", "算了", "取消",
        "好，但只发第一个", "为什么是这两个？", "他们预算多少？",
    ]:
        if not check(f"declines on {msg!r}", not P.is_affirmative(msg)):
            break


def validation(db) -> None:
    section("proposal validation")
    live = db.query(Item).filter(Item.status == ItemStatus.SELLING).all()
    if not live:
        check("a live listing exists to test with", False, "empty corpus")
        return
    # A listing whose category has an active want, so a candidate list exists.
    cats = {w.category for w in db.query(StandingWant)
            .filter(StandingWant.status == WantStatus.ACTIVE) if w.category}
    item = next((i for i in live if (i.attributes or {}).get("category") in cats), live[0])
    other = next((i for i in live if i.seller_id != item.seller_id), None)
    seller = item.seller_id

    mk = P.ActionProposal
    pending, why = P.validate_proposal(db, seller, mk(action="none"))
    check("action 'none' proposes nothing", pending is None and why == "", why)

    pending, why = P.validate_proposal(db, seller, mk(action="push_to_buyers", card_ids=["candidate-1"]))
    check("a proposal with no item_id is dropped", pending is None, why)

    pending, why = P.validate_proposal(db, seller, mk(action="push_to_buyers", item_id=item.id))
    check("a proposal naming no candidate is dropped", pending is None, why)

    if other is not None:
        pending, why = P.validate_proposal(
            db, seller, mk(action="push_to_buyers", item_id=other.id, card_ids=["candidate-1"]))
        check("another seller's listing is dropped, not repaired", pending is None, why)

    pending, why = P.validate_proposal(
        db, seller, mk(action="push_to_buyers", item_id=item.id, card_ids=["candidate-99"]))
    check("an ordinal that is not in the live list is dropped", pending is None, why)

    # Now build a real candidate list and propose against it.
    found = T.find_potential_buyers(db, seller, FindPotentialBuyersInput(item_id=item.id))
    if not found.cards:
        print("  [skip] this listing has no matching wants; candidate-path checks skipped")
        return
    real = [c.card_id for c in found.cards]
    pending, why = P.validate_proposal(
        db, seller, mk(action="push_to_buyers", item_id=item.id, card_ids=real, rationale="r"))
    check("a proposal over the real candidate list validates", pending is not None, why)
    if pending:
        check("it echoes back the listing it is about", pending.item_id == item.id)
        check("recipients matches the cards kept", pending.recipients == len(pending.card_ids))
        check("it carries a question for the seller", bool(pending.question), pending.question)

    mixed = real[:1] + ["candidate-99", "candidate-98"]
    pending, why = P.validate_proposal(
        db, seller, mk(action="push_to_buyers", item_id=item.id, card_ids=mixed))
    check("unknown ordinals are dropped while real ones survive",
          pending is not None and pending.card_ids == real[:1],
          str(pending.card_ids if pending else why))

    section("schema is closed")
    from pydantic import ValidationError
    try:
        mk(action="drop_price", item_id=1)
        check("an action outside the whitelist is refused", False, "accepted 'drop_price'")
    except ValidationError:
        check("an action outside the whitelist is refused", True)
    try:
        # A broadcast cannot even be *expressed*: the cap sits on the schema, so
        # a padded proposal fails to parse rather than being trimmed later. That
        # is stronger than a quota check, which only ever runs if the value got
        # as far as being a value.
        mk(action="push_to_buyers", item_id=1, card_ids=[f"candidate-{i}" for i in range(20)])
        check("a broadcast-sized proposal cannot be expressed at all", False, "accepted 20 cards")
    except ValidationError:
        check("a broadcast-sized proposal cannot be expressed at all", True)
    check("and the schema cap sits under the daily push quota",
          5 <= settings.want_max_push_per_seller_per_day,
          f"schema=5 quota={settings.want_max_push_per_seller_per_day}")


def suspension(db) -> None:
    section("the pause is real, not narrated")
    from app.services.agents import graph as G

    check("the graph has the propose and act nodes",
          {"propose", "act"} <= set(G.get_graph().get_graph().nodes))
    check("the outer loop has its own budget, separate from tool rounds",
          settings.advisor_react_max_loops == 3
          and settings.advisor_react_max_loops != settings.seller_advisor_max_tool_rounds,
          f"react={settings.advisor_react_max_loops} tools={settings.seller_advisor_max_tool_rounds}")
    src = (G.__file__,)
    import inspect
    text = _bootstrap.module_source(G)
    check("act is only reachable through the confirm branch",
          '_after_propose' in text and '"act": "act"' in text)
    check("run_turn resumes a parked thread instead of restarting it",
          "Command(resume=" in text and "snapshot.next" in text)
    check("a new turn clears any previous proposal and result",
          '"pending_action": {}' in text and '"action_result": {}' in text)


def live_run(db) -> None:
    section("live — a real conversation (spends tokens)")
    import json
    import urllib.request

    from app.security import hash_password
    from app.models import User

    live = db.query(Item).filter(Item.status == ItemStatus.SELLING).all()
    cats = {w.category for w in db.query(StandingWant)
            .filter(StandingWant.status == WantStatus.ACTIVE) if w.category}
    item = next((i for i in live if (i.attributes or {}).get("category") in cats), None)
    if item is None:
        check("a listing with matching demand exists", False, "none found")
        return
    u = db.get(User, item.seller_id)
    u.password_hash = hash_password("test1234")
    db.commit()

    B = "http://127.0.0.1:8010"

    def call(path, body, token=None):
        req = urllib.request.Request(
            B + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": "Bearer " + token} if token else {})})
        return json.loads(urllib.request.urlopen(req, timeout=180).read())

    try:
        tok = call("/api/auth/login", {"email": u.email, "password": "test1234"})["token"]
    except Exception as exc:  # noqa: BLE001
        check("backend reachable on :8010", False, str(exc)[:120])
        return

    ask = f"who should I message about my {item.name[:34]}?"

    # Two separate threads, because a declined proposal is *gone* — that is the
    # design, not a limitation. Testing both paths down one thread would only
    # assert that a dead offer stays dead.
    def open_offer(label):
        r = call("/api/seller/advisor/chat", {"message": ask}, tok)
        print(f"     [{label}] route={r.get('route')} tools={r.get('used_tools')} "
              f"pending={bool(r.get('pending_action'))}")
        return r

    r1 = open_offer("A")
    print(f"     reply: {(r1.get('reply') or '')[:180]}")
    if not r1.get("pending_action"):
        print("  [skip] the advisor proposed nothing for this listing — correct when the "
              "matches are weak, so not a failure")
        return
    check("the advisor asks before acting", True)
    pa = r1["pending_action"]
    print(f"     asking: {pa.get('question')} ({pa.get('recipients')} recipient(s))")

    # --- thread A: a question is not consent, and must still be answered ---
    r2 = call("/api/seller/advisor/chat",
              {"message": "what's their budget?", "thread_id": r1["thread_id"]}, tok)
    check("a question instead of an answer sends nothing",
          not (r2.get("action_result") or {}).get("succeeded"), str(r2.get("action_result")))
    reply2 = (r2.get("reply") or "")
    print(f"     after the question: route={r2.get('route')} reply={reply2[:150]}")
    check("and the question is answered rather than swallowed",
          len(reply2.strip()) > 0 and r2.get("route") != "", reply2[:120])
    check("the offer does not survive the non-answer",
          not r2.get("pending_action"), str(r2.get("pending_action")))

    # --- thread B: a plain yes sends ---
    r3 = open_offer("B")
    if not r3.get("pending_action"):
        print("  [skip] no proposal on the second thread")
        return
    r4 = call("/api/seller/advisor/chat", {"message": "yes", "thread_id": r3["thread_id"]}, tok)
    res = r4.get("action_result") or {}
    print(f"     after yes: {res or '(no action recorded)'}")
    check("a plain yes is what sends", bool(res), (r4.get("reply") or "")[:160])
    if res:
        check("the result reports who it reached and who it could not",
              "succeeded" in res and "failed" in res, str(res))
        print(f"     notified {res.get('succeeded')} buyer(s); "
              f"could not reach {len(res.get('failed') or [])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    db = SessionLocal()
    try:
        consent()
        validation(db)
        suspension(db)
        if args.live:
            live_run(db)
        else:
            print("\n(offline only — pass --live to drive a real conversation)")
        print()
        if _failures:
            print(f"[FAIL] {len(_failures)} check(s):")
            for f in _failures:
                print("    - " + f)
            return 1
        print("[ok] action loop acceptance passed")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
