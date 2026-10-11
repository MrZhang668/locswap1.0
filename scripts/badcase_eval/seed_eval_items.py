"""Fixtures for the 805-case BadCase expansion run.

    python scripts/badcase_eval/seed_eval_items.py --apply
    python scripts/badcase_eval/seed_eval_items.py --check
    python scripts/badcase_eval/seed_eval_items.py --revert

Why this exists: the expanded case set reasons over 101 products across all 9
categories, and asserts for most of them that the seller *already has one
listed*. The existing test sellers own 13 listings between them, so running the
set unseeded would route almost every row to publishing and score a fixture gap
as a routing defect.

What is created, and the limits kept on it:

* One dedicated seller (`EVAL_EMAIL`). Everything this script makes hangs off
  that user, so `--revert` is "delete this seller's rows" rather than a fuzzy
  match against production data. No existing seller is touched.
* 101 listings, one per product in the case set, written through
  `ItemIndexService.create_item` — the same entry point the HTTP layer uses, so
  the vector index and BM25 index get the same documents production would hold.
  Writing Item rows straight into SQLite would leave the indexes empty and every
  `find_potential_buyers` row would measure nothing.
* Two standing wants per product, through `create_explicit_want`, so the
  buyer-matching and write-action families have something above the similarity
  floor to match against.
* **No wants for the ten cold products** (`gen_cases.COLD`). Those back the
  empty-result and small-sample families — seeding demand for them would delete
  exactly the condition under test.

The wants are written by existing buyers, so they land in the same demand
aggregates the seller tools read. That is the point (the tools must see them)
and also why the database is restored from the pre-run backup afterwards rather
than relying on `--revert` alone.
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
from app.models import Item, ItemStatus, StandingWant, User  # noqa: E402
from app.services.item_index import get_item_index  # noqa: E402
from app.services.agents.sales import tools as T  # noqa: E402
from app.services.agents.sales.schemas import FindPotentialBuyersInput  # noqa: E402
from app.services.wants import create_explicit_want  # noqa: E402

import cases_data as CD  # noqa: E402  the generated case set (same folder)

EVAL_EMAIL = "badcase-eval-seller@campus.edu"
EVAL_PASSWORD = "eval1234"

# Homewood-adjacent pickup point, taken from an existing seller so the distance
# maths in find_potential_buyers stays in the range the real data lives in.
EVAL_LOC = ("Campus Square Apartments (E 29th St)", 39.32357, -76.6128)

#: Buyers whose accounts carry the seeded wants. Deliberately excludes the eval
#: seller and the intent-eval fixtures' sellers so the two suites cannot collide.
BUYER_IDS = [1, 6, 7, 10, 11, 12, 122, 124, 125, 130, 131, 132]

#: Marker carried by every want this script writes, so --revert is exact.
WANT_MARK = "【805评测夹具】"

PRICE_BY_CAT = {
    "电子产品": 180.0, "自行车与滑板车": 220.0, "家具与电器": 90.0,
    "图书与教材": 35.0, "服装与鞋靴": 55.0, "运动与户外": 40.0,
    "家居与厨房": 45.0, "乐器": 160.0, "学习用品": 25.0,
}
COND_CYCLE = ["成色很好", "成色良好", "几乎全新", "成色很好", "成色良好"]
COLOR_CYCLE = ["黑色", "白色", "灰色", "蓝色", "银色", "米色"]

COLD_ITEMS = {name for _, name in CD.COLD}


def _products() -> list[tuple[str, str]]:
    """(category, product) for all 101 products in the case set."""
    return list(CD.FLAT)


def _seller(db) -> User:
    user = db.scalar(select(User).where(User.email == EVAL_EMAIL))
    if user:
        return user
    from app.security import hash_password
    name, lat, lng = EVAL_LOC
    user = User(email=EVAL_EMAIL, password_hash=hash_password(EVAL_PASSWORD),
                avatar="avatar-3", location_name=name, lat=lat, lng=lng,
                auto_notify=None, pickup_range_enabled=False, pickup_range_km=3.0)
    db.add(user)
    db.flush()
    db.commit()
    db.refresh(user)
    print(f"  建卖家 #{user.id} {EVAL_EMAIL}")
    return user


def apply_seeds() -> None:
    index = get_item_index()
    loc_name, lat, lng = EVAL_LOC
    made_items = 0
    with SessionLocal() as db:
        seller = _seller(db)
        have = {it.name: it for it in db.scalars(
            select(Item).where(Item.seller_id == seller.id)).all()}
        for n, (cat, prod) in enumerate(_products()):
            color = COLOR_CYCLE[n % len(COLOR_CYCLE)]
            name = f"{color}{prod}"
            if name in have:
                continue
            cond = COND_CYCLE[n % len(COND_CYCLE)]
            price = round(PRICE_BY_CAT[cat] * (0.7 + 0.03 * (n % 11)), 0)
            index.create_item(
                db,
                seller_id=seller.id,
                name=name,
                summary=f"{color}{prod}，{cond}，校内自提。",
                description=(f"二手{prod}（{cat}）。{cond}，功能正常，正常使用痕迹。"
                             f"毕业清仓出售，校内当面交易，不邮寄。"),
                attributes={"category": cat, "condition": cond, "color": color},
                price=float(price),
                photo_path=None,
                pickup_location_name=loc_name, lat=lat, lng=lng,
                pickup_time_start="工作日晚上", pickup_time_end="周末下午",
                status=ItemStatus.SELLING,
            )
            made_items += 1
        print(f"  建商品 {made_items} 件（已存在的跳过）")

    made_wants = 0
    with SessionLocal() as db:
        for n, (cat, prod) in enumerate(_products()):
            if prod in COLD_ITEMS:
                continue          # 冷门品留空需求，空结果/小样本族要的就是这个
            for k in range(2):
                buyer_id = BUYER_IDS[(n * 2 + k) % len(BUYER_IDS)]
                buyer = db.get(User, buyer_id)
                if buyer is None:
                    continue
                text = (f"想收一个二手{prod}{WANT_MARK}" if k == 0
                        else f"求购{prod}，{cat}，价格合适就要{WANT_MARK}")
                if db.scalar(select(StandingWant.id)
                             .where(StandingWant.raw_query == text)):
                    continue
                create_explicit_want(db, buyer, text)
                made_wants += 1
        db.commit()
    print(f"  建求购 {made_wants} 条（冷门品 {len(COLD_ITEMS)} 件故意不建）")


def revert_seeds() -> None:
    index = get_item_index()
    with SessionLocal() as db:
        wants = db.scalars(select(StandingWant)
                           .where(StandingWant.raw_query.like(f"%{WANT_MARK}%"))).all()
        for w in wants:
            db.delete(w)
        db.commit()
        print(f"  删求购 {len(wants)} 条")

        seller = db.scalar(select(User).where(User.email == EVAL_EMAIL))
        if seller is None:
            print("  评测卖家不存在，无需删商品")
            return
        items = db.scalars(select(Item).where(Item.seller_id == seller.id)).all()
        ids = [int(i.id) for i in items]
        for it in items:
            db.delete(it)
        db.flush()
        db.commit()
        for iid in ids:
            index.remove_item(iid)
        print(f"  删商品 {len(ids)} 件（向量/BM25 索引同步移除）")
        db.delete(seller)
        db.commit()
        print("  删评测卖家")
    print("  注意：向量索引的彻底回滚仍以 DB 备份 + rebuild_from_db 为准")


def check() -> None:
    with SessionLocal() as db:
        seller = db.scalar(select(User).where(User.email == EVAL_EMAIL))
        if seller is None:
            raise SystemExit("评测卖家不存在，先 --apply")
        items = db.scalars(select(Item).where(Item.seller_id == seller.id)).all()
        active = [i for i in items if i.status in ItemStatus.SEARCHABLE]
        print(f"卖家 #{seller.id} 在售 {len(active)} / 共 {len(items)} 件")
        cats = {}
        for i in active:
            c = (i.attributes or {}).get("category", "?")
            cats[c] = cats.get(c, 0) + 1
        print("  品类分布:", cats)

        def _prod(it):
            # names are <2-char colour><product>; strip the colour prefix
            return it.name[2:]

        hot = [i for i in active if _prod(i) not in COLD_ITEMS][:6]
        cold = [i for i in active if _prod(i) in COLD_ITEMS][:4]
        print("  —— 热门品应有候选买家 ——")
        for it in hot:
            out = T.find_potential_buyers(
                db, int(seller.id), FindPotentialBuyersInput(item_id=int(it.id), top_n=5))
            print(f"    {it.name:<22} -> {out.total} 个候选")
        print("  —— 冷门品应为 0 候选（空结果族依赖这个）——")
        for it in cold:
            out = T.find_potential_buyers(
                db, int(seller.id), FindPotentialBuyersInput(item_id=int(it.id), top_n=5))
            print(f"    {it.name:<22} -> {out.total} 个候选")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.revert:
        revert_seeds()
    if args.apply:
        apply_seeds()
    if args.check or args.apply:
        check()
    if not (args.apply or args.revert or args.check):
        ap.error("要 --apply / --revert / --check 之一")


if __name__ == "__main__":
    main()
