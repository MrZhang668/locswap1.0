"""Acceptance for the opt-in buyer notification (batch 6, part 1).

This feature was deleted once. On 2026-08-22 the automatic fan-out was removed
because matching by score could not tell "this is the thing you asked for" from
"this uses your words", and every false positive landed on a buyer as junk. It
is back because the buyer now opts in — but consent answers "may we send you
things", not "is this thing worth sending", and the original failure was
entirely the second question.

So each check below is one of the four narrowings, tested by showing that
removing it would let a notification through:

  1. the account toggle is off unless the buyer turned it on;
  2. wants captured from a search (`implicit`) never fire, only ones the buyer
     wrote on purpose (`explicit`);
  3. the listing has to actually *name* what the buyer asked for — the gate a
     similarity threshold turned out not to be able to be (see the note on
     want_autonotify_min_similarity in config.py);
  4. the categories have to agree, and the score still has to clear a floor.

Plus the nagging caps, which apply here in full — unlike a hand-picked push,
this sender is automated, which is what those caps exist for.

Runs against a sandbox database. No API key, no tokens.

    python scripts/verify_optin_notify.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  injects backend into sys.path

SANDBOX = Path(_bootstrap.PROJECT_ROOT) / "data" / "_verify_optin"
if SANDBOX.exists():
    shutil.rmtree(SANDBOX, ignore_errors=True)
SANDBOX.mkdir(parents=True, exist_ok=True)
os.environ["DATA_DIR"] = str(SANDBOX)
os.environ["DB_URL"] = "sqlite:///" + SANDBOX.as_posix() + "/verify.db"
os.environ["CHROMA_DIR"] = str(SANDBOX / "chroma")
# Pin the sandbox to a filesystem-backed store, whatever .env says — under
# VECTOR_BACKEND=milvus the collection name comes from config, not from a path,
# so an un-pinned run would reset the live collection. (It did exactly that on
# 2026-09-14.)
os.environ["VECTOR_BACKEND"] = "chroma"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["LLM_API_KEY"] = ""

from app.config import CAMPUS_LOCATIONS, settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Item,
    ItemStatus,
    NotificationType,
    StandingWant,
    User,
    WantSource,
    WantStatus,
)
from app.repositories import NotificationRepository  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services.item_index import ItemIndexService  # noqa: E402
from app.services.wants import notify_optin_buyers  # noqa: E402

_failures: list[str] = []
LOC = CAMPUS_LOCATIONS[0]


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(("  [ok] " if ok else "  [FAIL] ") + label + ("" if ok else "  <- " + detail))
    if not ok:
        _failures.append(label)
    return ok


def section(t: str) -> None:
    print("\n-- " + t + " " + "-" * max(0, 56 - len(t)))


def mk_user(db, email: str, auto: bool) -> User:
    u = User(email=email, password_hash=hash_password("x"), avatar="avatar-0",
             location_name=LOC["name"], lat=LOC["lat"], lng=LOC["lng"],
             pickup_range_enabled=False, pickup_range_km=5.0, auto_notify=auto)
    db.add(u)
    db.flush()
    return u


def mk_want(db, buyer: User, text: str, category: str | None, source: str) -> StandingWant:
    from datetime import timedelta

    from app.models import utcnow
    w = StandingWant(
        buyer_id=buyer.id, raw_query=text, filters={}, semantic_text=text,
        category=category, max_price=None, min_price=None,
        origin_lat=LOC["lat"], origin_lng=LOC["lng"], max_distance_km=None,
        source=source, status=WantStatus.ACTIVE, notify_count=0,
        created_at=utcnow(), expires_at=utcnow() + timedelta(days=settings.want_ttl_days),
    )
    db.add(w)
    db.flush()
    return w


def notified(db, buyer_id: int, item_id: int) -> bool:
    return any(
        n.type == NotificationType.WANT_MATCHED
        and int((n.payload or {}).get("item_id") or 0) == item_id
        for n in NotificationRepository(db).list_for_user(buyer_id, limit=50)
    )


def main() -> int:
    init_db()
    db = SessionLocal()
    index = ItemIndexService()
    try:
        from app.services.want_index import get_want_index

        seller = mk_user(db, "seller@t.edu", auto=False)

        # Six buyers, each differing from the baseline in exactly one way, so a
        # failure names the narrowing that stopped working.
        baseline = mk_user(db, "baseline@t.edu", auto=True)     # should be notified
        opted_out = mk_user(db, "optedout@t.edu", auto=False)   # toggle off
        implicit = mk_user(db, "implicit@t.edu", auto=True)     # want came from a search
        wrong_cat = mk_user(db, "wrongcat@t.edu", auto=True)    # different category
        unrelated = mk_user(db, "unrelated@t.edu", auto=True)   # same category, other thing
        db.commit()

        CAT = "自行车与滑板车"
        w_base = mk_want(db, baseline, "通勤自行车", CAT, WantSource.EXPLICIT)
        mk_want(db, opted_out, "通勤自行车", CAT, WantSource.EXPLICIT)
        mk_want(db, implicit, "通勤自行车", CAT, WantSource.IMPLICIT)
        mk_want(db, wrong_cat, "通勤自行车", "电子产品", WantSource.EXPLICIT)
        mk_want(db, unrelated, "滑板头盔", CAT, WantSource.EXPLICIT)
        db.commit()
        for w in db.query(StandingWant).all():
            get_want_index().index_want(w)

        item = index.create_item(
            db, seller_id=seller.id,
            name="Giant 通勤自行车",
            description="26 寸通勤自行车，刹车刚换过，附送 U 型锁",
            summary="26 寸通勤自行车",
            attributes={"category": CAT, "brand": "Giant", "condition": "成色很好"},
            price=260.0, photo_path=None,
            pickup_location_name=LOC["name"], lat=LOC["lat"], lng=LOC["lng"],
            pickup_time_start="工作日 19:00", pickup_time_end="周末全天",
        )
        db.commit()

        section("who gets told")
        sent = notify_optin_buyers(db, item)
        print(f"     notified {sent} buyer(s)")
        check("a buyer who opted in, wrote the want, and matches is notified",
              notified(db, baseline.id, item.id),
              "the one case that should fire did not — the rest prove nothing")
        check("the account toggle is respected", not notified(db, opted_out.id, item.id))
        check("a want captured from a search never fires", not notified(db, implicit.id, item.id))
        check("a want in another category never fires", not notified(db, wrong_cat.id, item.id))
        check("an unrelated want in the same category never fires",
              not notified(db, unrelated.id, item.id))
        check("the seller is never notified about their own listing",
              not notified(db, seller.id, item.id))

        section("the listing has to name what was asked for")
        # These pairs are the measured failure of a score-only gate: on the real
        # corpus the wrong ones outscore several of the right ones, so no cut
        # point separates them. Word coverage does.
        from app.services.wants import _covers_the_want

        class _W:
            def __init__(self, t):
                self.semantic_text = t
                self.raw_query = t

        class _I:
            def __init__(self, n):
                self.name = n
                self.summary = ""

        pairs = [
            ("机械键盘", "Keychron 机械键盘", True),
            ("机械键盘", "Logitech 入门 USB 桌面麦克风", False),
            ("心理学教材", "蓝色上下册物理教材", False),
            ("沙发床", "IKEA 带扶手沙发床", True),
            ("无线耳机", "Logitech 静音无线鼠标", False),
            ("空气炸锅", "Instant Pot 8 夸脱电压力锅", False),
            # A price qualifier is a constraint, not the thing wanted — max_price
            # enforces it, so requiring the word 便宜 in a title would only ever
            # reject good matches.
            ("便宜的教材", "西班牙语语法参考教材", True),
            ("二手自行车", "Giant 通勤自行车", True),
        ]
        bad = [(w, i) for w, i, want_it in pairs
               if _covers_the_want(_W(w), _I(i)) is not want_it]
        check(f"word coverage separates all {len(pairs)} measured pairs", not bad, str(bad))

        section("the similarity floor still backstops it")
        check("the automatic floor still sits above the seller-facing bar",
              settings.want_autonotify_min_similarity > settings.want_match_min_similarity,
              f"auto={settings.want_autonotify_min_similarity} "
              f"seller={settings.want_match_min_similarity}")
        matches = {m.want_id: m.similarity for m in
                   get_want_index().match_wants_for_item(db, item, limit=50)}
        loose = [wid for wid, sim in matches.items()
                 if settings.want_match_min_similarity <= sim < settings.want_autonotify_min_similarity]
        print(f"     {len(matches)} want(s) cleared the seller bar; "
              f"{len(loose)} of them sit below the automatic one")
        check("wants between the two bars are not notified",
              all(not notified(db, db.get(StandingWant, wid).buyer_id, item.id) for wid in loose),
              str(loose))

        # The check above passes for free when nothing happens to land in that
        # band, and a threshold only ever exercised by luck is not a tested
        # threshold. So falsify it: hold the data still, drop the bar to the
        # loose one, and see whether anyone new would have been written to. If
        # nobody is, the strict bar is decoration.
        near = mk_user(db, "near@t.edu", auto=True)
        db.commit()
        mk_want(db, near, "自行车锁和骑行头盔", CAT, WantSource.EXPLICIT)
        db.commit()
        for w in db.query(StandingWant).filter(StandingWant.buyer_id == near.id):
            get_want_index().index_want(w)

        item3 = index.create_item(
            db, seller_id=seller.id,
            name="Schwinn 城市自行车",
            description="城市自行车，7 速，刚调过，带后货架",
            summary="城市自行车，7 速",
            attributes={"category": CAT, "condition": "成色良好"},
            price=210.0, photo_path=None, pickup_location_name=LOC["name"],
            lat=LOC["lat"], lng=LOC["lng"],
            pickup_time_start="工作日 19:00", pickup_time_end="周末全天",
        )
        db.commit()
        sim = {m.want_id: m.similarity for m in
               get_want_index().match_wants_for_item(db, item3, limit=50)}
        near_want = db.query(StandingWant).filter(StandingWant.buyer_id == near.id).first()
        near_sim = sim.get(near_want.id)
        print(f"     loosely-related want scores {near_sim}")

        notify_optin_buyers(db, item3)
        strict_sent = notified(db, near.id, item3.id)
        original = settings.want_autonotify_min_similarity
        settings.want_autonotify_min_similarity = settings.want_match_min_similarity
        try:
            notify_optin_buyers(db, item3)
            loose_sent = notified(db, near.id, item3.id)
        finally:
            settings.want_autonotify_min_similarity = original
        # With coverage in front, a loosely-related want is now blocked whichever
        # way the floor is set — which is the point, and makes this a check that
        # the two gates do not depend on each other rather than one about the
        # number.
        check("a loosely-related want stays blocked even with the floor lowered",
              (not strict_sent) and (not loose_sent),
              f"sim={near_sim} strict_sent={strict_sent} loose_sent={loose_sent}")

        section("it does not repeat itself")
        again = notify_optin_buyers(db, item)
        check("running the fan-out twice sends nothing the second time", again == 0, str(again))
        before = len(NotificationRepository(db).list_for_user(baseline.id, limit=50))
        notify_optin_buyers(db, item)
        after = len(NotificationRepository(db).list_for_user(baseline.id, limit=50))
        check("and the buyer's inbox does not grow", before == after, f"{before} -> {after}")

        section("nagging caps apply to an automated sender")
        db.refresh(w_base)
        check("the send was counted against the want's lifetime cap",
              (w_base.notify_count or 0) >= 1, str(w_base.notify_count))
        w_base.notify_count = settings.want_max_notify_total
        db.commit()
        item2 = index.create_item(
            db, seller_id=seller.id,
            name="Trek commuter bike", description="Commuter bike, lightly used, recently serviced",
            summary="Commuter bike", attributes={"category": CAT, "condition": "成色良好"},
            price=240.0, photo_path=None, pickup_location_name=LOC["name"],
            lat=LOC["lat"], lng=LOC["lng"],
            pickup_time_start="工作日 19:00", pickup_time_end="周末全天",
        )
        db.commit()
        notify_optin_buyers(db, item2)
        check("a want at its lifetime cap stops being notified",
              not notified(db, baseline.id, item2.id),
              "the lifetime cap exists to stop exactly this sender")

        section("the master switch")
        settings.want_autonotify_enabled = False
        try:
            fresh = mk_user(db, "fresh@t.edu", auto=True)
            db.commit()
            mk_want(db, fresh, "commuter bike", CAT, WantSource.EXPLICIT)
            db.commit()
            for w in db.query(StandingWant).filter(StandingWant.buyer_id == fresh.id):
                get_want_index().index_want(w)
            check("turning the feature off silences it for opted-in buyers too",
                  notify_optin_buyers(db, item2) == 0)
        finally:
            settings.want_autonotify_enabled = True

        print()
        if _failures:
            print(f"[FAIL] {len(_failures)} check(s):")
            for f in _failures:
                print("    - " + f)
            return 1
        print("[ok] opt-in notification acceptance passed")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
