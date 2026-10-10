"""Fixtures v2 — one eval seller per category, every one inside the read window.

    python scripts/badcase_eval/seed_eval_v2.py --apply
    python scripts/badcase_eval/seed_eval_v2.py --check
    python scripts/badcase_eval/seed_eval_v2.py --revert

v1 put all 101 products on one seller. `list_my_items` caps at 50 (defaults to
20), so 51 products were unreachable and 311 rows scored a fixture gap as a
product defect. This version gives each of the 9 categories its own seller, so
the largest holding is 18 items — under the *default* window, which means the
result no longer depends on the model choosing to raise `limit`.

Splitting by category also reads truer: a graduating student clearing out a
dorm has a dozen things, not a hundred across nine categories.

Two consequences the dataset has to respect, handled in dataset805_v2:
* a case that needs the seller to own two products must draw both from the
  same category, because that is now the same seller;
* a case that needs the seller *not* to own something keeps using tester2,
  who owns nothing at all.
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
from app.security import hash_password  # noqa: E402
from app.services.item_index import get_item_index  # noqa: E402
from app.services.seller_advisor import tools as T  # noqa: E402
from app.services.seller_advisor.schemas import (  # noqa: E402
    FindPotentialBuyersInput, ListMyItemsInput,
)
from app.services.wants import create_explicit_want  # noqa: E402

import cases_data as CD  # noqa: E402

#: category -> (email suffix, pickup point). Distinct pickup points keep the
#: distance maths from collapsing to one value across every case.
SELLERS: dict[str, tuple[str, str, float, float]] = {
    "电子产品":      ("elec",  "Charles Commons (N Charles St)",      39.32690, -76.61830),
    "家具与电器":    ("furn",  "Campus Square Apartments (E 29th St)", 39.32357, -76.61280),
    "家居与厨房":    ("home",  "The Social (N Charles St)",            39.32910, -76.61640),
    "运动与户外":    ("sport", "Homewood Apartments (W 33rd St)",      39.33120, -76.62180),
    "图书与教材":    ("book",  "Nine East 33rd (E 33rd St)",           39.32980, -76.61180),
    "服装与鞋靴":    ("cloth", "The Marylander (N Charles St)",        39.32540, -76.61710),
    "学习用品":      ("study", "Wyman Park Apartments (Wyman Pkwy)",   39.33260, -76.62640),
    "乐器":          ("music", "Blackstone Apartments (St Paul St)",   39.32430, -76.61540),
    "自行车与滑板车": ("bike",  "Guilford Ave Apartments (Guilford Ave)", 39.32800, -76.61050),
}
EMAIL = "bc-{}@campus.edu"
PASSWORD = "eval1234"

#: Marker on every want this script writes, so --revert is exact.
WANT_MARK = "【805夹具v2】"

PRICE_BY_CAT = {
    "电子产品": 180.0, "自行车与滑板车": 220.0, "家具与电器": 90.0,
    "图书与教材": 35.0, "服装与鞋靴": 55.0, "运动与户外": 40.0,
    "家居与厨房": 45.0, "乐器": 160.0, "学习用品": 25.0,
}
COND_CYCLE = ["成色很好", "成色良好", "几乎全新", "成色很好", "成色良好"]
COLOR_CYCLE = ["黑色", "白色", "灰色", "蓝色", "银色", "米色"]

#: Products verified to have little or no demand — the empty-result and
#: small-sample families are pinned to these, so they get no seeded wants.
COLD = {name for _, name in CD.COLD}

BUYER_IDS = [1, 6, 7, 10, 11, 12, 122, 124, 125, 130, 131, 132]


def by_category() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for cat, prod in CD.FLAT:
        out.setdefault(cat, []).append(prod)
    return out


def seller_email(cat: str) -> str:
    return EMAIL.format(SELLERS[cat][0])


def _get_seller(db, cat: str) -> User:
    email = seller_email(cat)
    user = db.scalar(select(User).where(User.email == email))
    if user:
        return user
    _, loc, lat, lng = SELLERS[cat]
    user = User(email=email, password_hash=hash_password(PASSWORD),
                avatar="avatar-3", location_name=loc, lat=lat, lng=lng,
                auto_notify=None, pickup_range_enabled=False, pickup_range_km=3.0)
    db.add(user)
    db.flush()
    db.commit()
    db.refresh(user)
    return user


def apply_seeds() -> None:
    index = get_item_index()
    cats = by_category()
    made_items = 0
    with SessionLocal() as db:
        for cat, prods in cats.items():
            seller = _get_seller(db, cat)
            _, loc, lat, lng = SELLERS[cat]
            have = {i.name for i in db.scalars(
                select(Item).where(Item.seller_id == seller.id)).all()}
            for n, prod in enumerate(prods):
                color = COLOR_CYCLE[n % len(COLOR_CYCLE)]
                name = f"{color}{prod}"
                if name in have:
                    continue
                cond = COND_CYCLE[n % len(COND_CYCLE)]
                price = round(PRICE_BY_CAT[cat] * (0.7 + 0.03 * (n % 11)), 0)
                index.create_item(
                    db, seller_id=seller.id, name=name,
                    summary=f"{color}{prod}，{cond}，校内自提。",
                    description=(f"二手{prod}（{cat}）。{cond}，功能正常，正常使用痕迹。"
                                 f"毕业清仓出售，校内当面交易，不邮寄。"),
                    attributes={"category": cat, "condition": cond, "color": color},
                    price=float(price), photo_path=None,
                    pickup_location_name=loc, lat=lat, lng=lng,
                    pickup_time_start="工作日晚上", pickup_time_end="周末下午",
                    status=ItemStatus.SELLING,
                )
                made_items += 1
            print(f"  {cat:<14} 卖家 #{seller.id:<4} {len(prods):>2} 件")
    print(f"  新建商品 {made_items} 件")

    made = 0
    with SessionLocal() as db:
        n = 0
        for cat, prods in cats.items():
            for prod in prods:
                if prod in COLD:
                    continue
                for k in range(2):
                    buyer = db.get(User, BUYER_IDS[(n * 2 + k) % len(BUYER_IDS)])
                    if buyer is None:
                        continue
                    text = (f"想收一个二手{prod}{WANT_MARK}" if k == 0
                            else f"求购{prod}，{cat}，价格合适就要{WANT_MARK}")
                    if db.scalar(select(StandingWant.id)
                                 .where(StandingWant.raw_query == text)):
                        continue
                    create_explicit_want(db, buyer, text)
                    made += 1
                n += 1
        db.commit()
    print(f"  新建求购 {made} 条（冷门品 {len(COLD)} 件故意不建）")


def revert_seeds() -> None:
    index = get_item_index()
    with SessionLocal() as db:
        wants = db.scalars(select(StandingWant)
                           .where(StandingWant.raw_query.like(f"%{WANT_MARK}%"))).all()
        for w in wants:
            db.delete(w)
        db.commit()
        print(f"  删求购 {len(wants)} 条")
        total = 0
        for cat in SELLERS:
            seller = db.scalar(select(User).where(User.email == seller_email(cat)))
            if seller is None:
                continue
            items = db.scalars(select(Item).where(Item.seller_id == seller.id)).all()
            ids = [int(i.id) for i in items]
            for it in items:
                db.delete(it)
            db.flush()
            db.commit()
            for iid in ids:
                index.remove_item(iid)
            db.delete(seller)
            db.commit()
            total += len(ids)
        print(f"  删商品 {total} 件、评测卖家 {len(SELLERS)} 个")
    print("  向量索引的彻底回滚仍以 DB 备份 + rebuild_from_db 为准")


def check() -> None:
    ok = True
    with SessionLocal() as db:
        for cat in SELLERS:
            seller = db.scalar(select(User).where(User.email == seller_email(cat)))
            if seller is None:
                print(f"  {cat:<14} 卖家不存在")
                ok = False
                continue
            out = T.list_my_items(db, int(seller.id), ListMyItemsInput())
            reach = len(out.items)
            flag = "" if reach == out.total_active else "  ← 仍有商品超出默认窗口"
            if reach != out.total_active:
                ok = False
            print(f"  {cat:<14} 在售 {out.total_active:>2} 件，默认窗口可见 {reach:>2} 件{flag}")

        print("\n  —— 抽查候选买家 ——")
        for cat in ("电子产品", "乐器", "家居与厨房"):
            seller = db.scalar(select(User).where(User.email == seller_email(cat)))
            items = db.scalars(select(Item).where(Item.seller_id == seller.id)).all()
            for it in items[:2]:
                o = T.find_potential_buyers(
                    db, int(seller.id), FindPotentialBuyersInput(item_id=int(it.id), top_n=5))
                print(f"    {it.name:<22} -> {o.total} 个候选")
    print("\n  全部商品均在默认窗口内" if ok else "\n  有卖家超窗，检查 SELLERS 配置")


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
