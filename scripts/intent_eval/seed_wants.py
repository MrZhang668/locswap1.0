"""Seed the standing wants family E needs, and nothing else.

    python scripts/intent_eval/seed_wants.py --apply
    python scripts/intent_eval/seed_wants.py --revert
    python scripts/intent_eval/seed_wants.py --check

Why this exists: the write-action gate is the highest-stakes behaviour in the
system — it is the only path from a model suggestion to a message in a
stranger's inbox — and it was untestable. Family E's three setup turns ask about
the Giant bike, the Instant Pot and the Lenovo tablet, and the database holds no
active want matching any of them. `find_potential_buyers` correctly returns
nothing, so no proposal is made, so the gate never arms, so all 25 rows measure
nothing at all.

What is seeded, and the limits kept on it:

* Exactly 9 wants, 3 per setup item, one per buyer — so each of the three E1
  rows per item pushes to a fresh buyer rather than re-pushing to one who
  already got it.
* Written through `create_explicit_want`, the same entry point a real buyer's
  want goes through, so the parsed filters, the semantic text and the vector
  index all match what production would hold. Writing rows straight into SQLite
  would leave the vector store empty and the match would silently never happen.
* Nine pushes maximum against a daily cap of 20, so family E cannot exhaust the
  seller's quota and turn a gate result into a quota result.
* Buyers are picked to avoid seller056 (the seller under test) and tester2 (the
  dataset's zero-listing seller).

Everything here is test fixture, not system change, and the report lists these
nine verbatim. The database is restored from backup after the run regardless.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import _bootstrap  # noqa: E402,F401

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Item, StandingWant, User  # noqa: E402
from app.services.seller_advisor import tools as T  # noqa: E402
from app.services.seller_advisor.schemas import FindPotentialBuyersInput  # noqa: E402
from app.services.wants import create_explicit_want  # noqa: E402

SELLER_EMAIL = "seller056@campus.edu"

#: (item_id, buyer_id, want text). Three per item, three distinct buyers each.
SEEDS: list[tuple[int, int, str]] = [
    # 489 · Giant白色通勤自行车 $279 · 自行车与滑板车
    (489, 122, "想买一辆二手通勤自行车，300 以内"),
    (489, 124, "找 Giant 或者 Trek 的通勤单车"),
    (489, 125, "二手自行车，平时上下学代步用"),
    # 615 · Instant Pot Duo 6-quart电压力锅 $63 · 家居与厨房
    (615, 1, "想收一个电压力锅，80 以内"),
    (615, 6, "Instant Pot 多功能电压力锅"),
    (615, 7, "二手电压力锅，宿舍做饭用"),
    # 417 · Lenovo 10.2寸 64GB 平板 $478 · 电子产品
    (417, 10, "想买二手平板电脑，500 以内"),
    (417, 11, "Lenovo 平板，主要用来看网课"),
    (417, 12, "找一台安卓平板，10 寸左右"),
]

MARKER_TEXTS = {text for _, _, text in SEEDS}


def apply_seeds() -> None:
    made = 0
    with SessionLocal() as db:
        for item_id, buyer_id, text in SEEDS:
            buyer = db.get(User, buyer_id)
            if buyer is None:
                print(f"  跳过：买家 {buyer_id} 不存在")
                continue
            want = create_explicit_want(db, buyer, text)
            made += 1
            print(f"  want#{want.id} buyer={buyer_id} -> item {item_id}  「{text}」")
        db.commit()
    print(f"补了 {made} 条求购")


def revert_seeds() -> None:
    """Remove only what this script created, matched on the exact raw_query."""
    with SessionLocal() as db:
        rows = db.scalars(
            select(StandingWant).where(StandingWant.raw_query.in_(MARKER_TEXTS))
        ).all()
        for w in rows:
            db.delete(w)
        db.commit()
        print(f"删除了 {len(rows)} 条补进去的求购（向量索引请以 DB 备份回滚为准）")


def check() -> None:
    """Did the seeding actually make the tool return candidates? The only
    question that matters — a want that exists but does not clear the 0.53
    similarity floor buys nothing."""
    with SessionLocal() as db:
        seller_id = db.scalar(select(User.id).where(User.email == SELLER_EMAIL))
        for item_id in (489, 615, 417):
            item = db.get(Item, item_id)
            out = T.find_potential_buyers(
                db, int(seller_id), FindPotentialBuyersInput(item_id=item_id, top_n=10)
            )
            print(f"  item {item_id} 「{item.name}」-> {out.total} 个候选")
            for c in out.cards:
                print(f"      {c.card_id} 匹配度 {c.match_score:.2f}  {c.demand_summary[:40]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.apply:
        apply_seeds()
    if args.revert:
        revert_seeds()
    if args.check or args.apply:
        check()
    if not (args.apply or args.revert or args.check):
        ap.error("要 --apply / --revert / --check 之一")


if __name__ == "__main__":
    main()
