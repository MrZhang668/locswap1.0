"""Large mock dataset: 1 buyer + 120 sellers + 560 items (default), then rebuild vector + BM25 indexes.

Relationship with scripts/seed.py: seed.py is the **demo** set of 25 hand-tuned items
(each hand-written, statuses deliberately orchestrated); this script builds the **retrieval
evaluation** dataset — the corpus is randomly combined from the product prototype library in
scripts/mock_catalog.py, covering the 9 categories / 5 condition tiers / 14 colors /
BRAND_DICT brands in config.py, with prices distributed per category.

    python scripts/seed_mock.py                          # wipe & rebuild (default 120 sellers / 560 items)
    python scripts/seed_mock.py --sellers 200 --selling 1000
    python scripts/seed_mock.py --no-images              # skip placeholder images (much faster, items have no photo)
    python scripts/seed_mock.py --dry-run                # print distributions only, no DB writes
    python scripts/seed_mock.py --seed 42                # different random data (default seed is fixed => reproducible)

Note: by default this **wipes** data/locswap.db, data/uploads/ and data/chroma/
(including orphan collection directories left by history).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import timedelta

import _bootstrap  # noqa: F401  # isort:skip

from app.config import CAMPUS_LOCATIONS, normalize_color, settings
from app.db import SessionLocal, engine, init_db
from app.models import (
    Base,
    Item,
    ItemStatus,
    Notification,
    NotificationType,
    Reservation,
    ReservationStatus,
    StatusLog,
    User,
    utcnow,
)
from app.security import hash_password
from mock_catalog import (
    CATALOG,
    CONDITION_CLAUSES,
    CONDITION_FAMILY,
    HEAVY_CLAUSES,
    TIME_SLOTS,
    TRADE_CLAUSES,
)

BUYER_EMAIL = "buyer@campus.edu"
PASSWORD = "demo1234"

#: Category sampling weights — tuned to the real supply structure of a campus
#: second-hand market, not a uniform distribution. Retrieval evaluation needs
#: "findable in hot categories, not drowned out in cold ones"; uniform would be unrealistic.
CATEGORY_WEIGHTS: dict[str, int] = {
    "电子产品": 26,
    "图书与教材": 16,
    "家居与厨房": 13,
    "服装与鞋靴": 12,
    "家具与电器": 11,
    "运动与户外": 9,
    "学习用品": 6,
    "自行车与滑板车": 4,
    "乐器": 3,
}

#: Condition distribution (weights) and the matching price factor
CONDITION_WEIGHTS: list[tuple[str, int, float]] = [
    ("全新", 4, 1.15),
    ("几乎全新", 10, 1.05),
    ("成色很好", 34, 0.95),
    ("成色良好", 34, 0.82),
    ("成色一般", 18, 0.65),
]

#: Items in these categories get a "bulky item, pickup only" closing clause
HEAVY_CATEGORIES = {"家具与电器", "自行车与滑板车", "运动与户外", "乐器"}


# ============================================================
# Random helpers
# ============================================================
def weighted_pick(rng, pairs: list[tuple]) -> tuple:
    """pairs like [(value, weight, ...), ...]; returns the whole tuple."""
    total = sum(p[1] for p in pairs)
    x = rng.uniform(0, total)
    upto = 0.0
    for p in pairs:
        upto += p[1]
        if x <= upto:
            return p
    return pairs[-1]


def snap_price(rng, value: float) -> float:
    """Snap a random price to something a person would actually ask: round small
    amounts to $1, medium to $5, large to $10/$50."""
    if value < 30:
        out = round(value)
    elif value < 100:
        out = round(value / 5) * 5
    elif value < 1000:
        out = round(value / 10) * 10
    else:
        out = round(value / 50) * 50
    # 30% chance of a charm price (ending in 9 / 8)
    if out >= 20 and rng.random() < 0.3:
        out = out - 1 if rng.random() < 0.5 else out - 2
    return float(max(5, out))


_CJK_GAP_RE = re.compile(r"(?<=[一-鿿])\s+(?=[一-鿿])")

#: Variants that describe an add-on ("带后货架", "附充电器") read wrong in front
#: of the noun — they go after the base, in brackets.
_TRAILING_VARIANT = ("带", "附", "含", "配")


def zh_join(*parts: str | None) -> str:
    """Join name fragments with spaces, then drop the spaces between two Chinese
    characters: "银色 落地灯" -> "银色落地灯", while Latin brands and model
    numbers keep theirs ("Apple 13 英寸 M1 笔记本电脑")."""
    text = " ".join(" ".join(p for p in parts if p).split())
    return _CJK_GAP_RE.sub("", text)


def clause(s: str) -> str:
    """One clause without its closing punctuation, ready to be joined."""
    return s.strip().rstrip("。.，,；;")


# ============================================================
# Copy generation
#
# Every function below consumes the rng exactly as the English templates did
# (same calls, same option counts), so a given --seed still yields the same
# products, prices and sellers — only the wording is Chinese.
# ============================================================
def make_name(rng, base, brand, variant, color) -> str:
    trailing = variant.startswith(_TRAILING_VARIANT)
    if brand:
        if trailing:
            patterns = [f"{zh_join(brand, base)}（{variant}）", zh_join(brand, base)]
            weights = [70, 30]
        else:
            patterns = [
                zh_join(brand, variant, base),
                zh_join(brand, base, variant),
                zh_join(brand, base),
                zh_join(variant, brand, base),
            ]
            weights = [40, 25, 15, 20]
    else:
        if trailing:
            patterns = [f"{base}（{variant}）", f"{zh_join(color, base)}（{variant}）", zh_join(color, base)]
            weights = [50, 30, 20]
        else:
            patterns = [
                zh_join(variant, base),
                f"{base}（{variant}）",
                zh_join(color, variant, base),
                zh_join(color, base),
            ]
            weights = [35, 30, 20, 15]
    return weighted_pick(rng, list(zip(patterns, weights)))[0][:200]


def make_summary(rng, base, brand, variant, color, condition, specs, uses) -> str:
    spec_head = clause(re.split(r"[,，]", rng.choice(specs))[0])
    use = clause(rng.choice(uses))
    templates = [
        f"{zh_join(color, base)}，{condition}，{use}。",
        f"{zh_join(brand, base)}，{condition}，{spec_head}。",
        f"{zh_join(variant, base)}，{spec_head}，{use}。",
        f"{zh_join(color, base)}，{spec_head}。{use}。",
    ]
    return rng.choice(templates)[:400]


def make_description(rng, category, name, base, brand, variant, color, condition, specs, uses) -> str:
    if variant.startswith(_TRAILING_VARIANT):
        first = f"{zh_join(brand, base)}（{variant}）"
    else:
        first = zh_join(brand, variant, base)
    opening_options = [
        first,
        f"{zh_join(color, base)}，{variant}",
        name,
    ]
    # Don't repeat the color if the item name already carries it
    # (otherwise you get "银色晾衣架，折叠，银色").
    if color not in name:
        opening_options[0] = zh_join(color, opening_options[0])
        opening_options[2] += f"，{color}"
    parts = [rng.choice(opening_options)]

    # 1-2 detail clauses, no repeats
    n_specs = 1 if rng.random() < 0.35 else 2
    parts.extend(rng.sample(specs, min(n_specs, len(specs))))

    family = CONDITION_FAMILY.get(category, "general")
    parts.append(rng.choice(CONDITION_CLAUSES[family][condition]))
    parts.append(rng.choice(uses))

    if category in HEAVY_CATEGORIES and rng.random() < 0.35:
        parts.append(rng.choice(HEAVY_CLAUSES))
    elif rng.random() < 0.7:
        parts.append(rng.choice(TRADE_CLAUSES))

    return "。".join(clause(p) for p in parts) + "。"


def build_item_payload(rng) -> dict:
    """Randomly assemble all business fields of one item (excl. seller / location / status)."""
    category = weighted_pick(rng, list(CATEGORY_WEIGHTS.items()))[0]
    base, brands, variants, (lo, hi), colors, specs, uses = rng.choice(CATALOG[category])

    brand = rng.choice(brands)
    variant = rng.choice(variants)
    color_key = rng.choice(colors)  # the catalog's English key; the photo library uses it
    color = normalize_color(color_key)
    condition, _, price_factor = weighted_pick(rng, CONDITION_WEIGHTS)

    price = snap_price(rng, rng.uniform(lo, hi) * price_factor)
    name = make_name(rng, base, brand, variant, color)

    return {
        "name": name,
        # base + color_key: how the photo library matches "prototype|Color"; not stored
        "base": base,
        "color_key": color_key,
        "summary": make_summary(rng, base, brand, variant, color, condition, specs, uses),
        "description": make_description(
            rng, category, name, base, brand, variant, color, condition, specs, uses
        ),
        "attributes": {
            "category": category,
            "brand": brand,
            "condition": condition,
            "color": color,
        },
        "price": price,
    }


# ============================================================
# Seller holdings: long-tail distribution (a few sellers hold many items,
# most hold one or two)
# ============================================================
def split_counts(rng, total: int, n_sellers: int) -> list[int]:
    if total < n_sellers:
        raise SystemExit(f"Total items {total} is less than sellers {n_sellers}; every seller needs at least 1")
    counts = [1] * n_sellers
    remaining = total - n_sellers
    weights = [rng.random() ** 2.2 + 0.02 for _ in range(n_sellers)]
    total_w = sum(weights)
    alloc = [int(remaining * w / total_w) for w in weights]
    while sum(alloc) < remaining:
        alloc[rng.randrange(n_sellers)] += 1
    return [c + a for c, a in zip(counts, alloc)]


# ============================================================
# Photo library (generated by scripts/gen_item_images.py; seed only reads)
# ============================================================
def load_image_library() -> dict[str, "Path"]:
    """Return {"prototype|Color": absolute image path}; empty dict when the library
    doesn't exist (everything falls back to placeholders)."""
    import json

    manifest = settings.data_path / "item_images" / "manifest.json"
    if not manifest.exists():
        return {}
    library = {}
    for key, rec in json.loads(manifest.read_text("utf-8")).items():
        path = settings.data_path / "item_images" / rec["file"]
        if path.exists():
            library[key] = path
    return library


# ============================================================
# Wipe
# ============================================================
def wipe(purge_chroma: bool) -> None:
    """Must run before the first get_item_index() call — the index singleton is lazy."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    removed = 0
    for path in settings.upload_path.glob("*.jpg"):
        path.unlink(missing_ok=True)
        removed += 1

    if purge_chroma:
        shutil.rmtree(settings.chroma_path, ignore_errors=True)
        settings.chroma_path.mkdir(parents=True, exist_ok=True)

    print(f"Wiped business DB, {removed} uploaded images" + (", chroma directory" if purge_chroma else ""))


# ============================================================
# Main flow
# ============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the large mock dataset")
    parser.add_argument("--sellers", type=int, default=120, help="number of sellers (default 120)")
    parser.add_argument("--selling", type=int, default=500, help="items on sale (default 500)")
    parser.add_argument("--reserved", type=int, default=45, help="items already reserved (default 45)")
    parser.add_argument("--sold", type=int, default=15, help="items already sold (default 15)")
    parser.add_argument("--seed", type=int, default=20260818, help="random seed (fixed => reproducible)")
    parser.add_argument("--no-images", action="store_true", help="skip placeholder images; photo_path stays empty")
    parser.add_argument("--keep-chroma", action="store_true", help="do not delete data/chroma")
    parser.add_argument("--dry-run", action="store_true", help="print distributions only, no DB writes")
    args = parser.parse_args()

    import random

    rng = random.Random(args.seed)

    n_items = args.selling + args.reserved + args.sold
    if args.sellers < 1 or n_items < 1:
        raise SystemExit("Seller and item counts must be positive")

    # ---- Generate the content of all items up front (in memory, so dry-run can show distributions) ----
    payloads = [build_item_payload(rng) for _ in range(n_items)]

    if args.dry_run:
        print(f"Sellers {args.sellers} · items {n_items}"
              f" (selling {args.selling} / reserved {args.reserved} / sold {args.sold})\n")
        cat = Counter(p["attributes"]["category"] for p in payloads)
        cond = Counter(p["attributes"]["condition"] for p in payloads)
        brand = Counter(p["attributes"]["brand"] or "(no brand)" for p in payloads)
        prices = sorted(p["price"] for p in payloads)
        print("Categories:", dict(cat.most_common()))
        print("Conditions:", dict(cond.most_common()))
        print("Brand top 10:", dict(brand.most_common(10)))
        print(f"Prices: min={prices[0]:.0f} p50={prices[len(prices)//2]:.0f} "
              f"p90={prices[int(len(prices)*0.9)]:.0f} max={prices[-1]:.0f}")
        print(f"Unique item names: {len(set(p['name'] for p in payloads))} / {n_items}")
        print("\nSamples:")
        for p in payloads[:3]:
            print(f"  {p['name']} | ${p['price']:.0f} | {p['attributes']}")
            print(f"    summary: {p['summary']}")
            print(f"    desc   : {p['description']}")
        return 0

    init_db()
    wipe(purge_chroma=not args.keep_chroma)

    from app.services.images import compress_and_save, make_placeholder
    from app.services.item_index import get_item_index

    index = get_item_index()
    library = {} if args.no_images else load_image_library()
    if library:
        print(f"Photo library: {len(library)} images (data/item_images/), matched by prototype|Color; misses fall back to placeholders")

    with SessionLocal() as db:
        # ---------------- Users ----------------
        # One shared password; hash once and reuse — no point running 120 KDFs
        pwd_hash = hash_password(PASSWORD)

        buyer_loc = CAMPUS_LOCATIONS[0]
        buyer = User(
            email=BUYER_EMAIL,
            password_hash=pwd_hash,
            avatar="avatar-0",
            location_name=buyer_loc["name"],
            lat=buyer_loc["lat"],
            lng=buyer_loc["lng"],
            pickup_range_enabled=False,
            pickup_range_km=settings.default_pickup_range_km,
        )
        db.add(buyer)

        sellers: list[User] = []
        for i in range(args.sellers):
            loc = rng.choice(CAMPUS_LOCATIONS)
            seller = User(
                email=f"seller{i + 1:03d}@campus.edu",
                password_hash=pwd_hash,
                avatar=f"avatar-{i % 8}",
                location_name=loc["name"],
                lat=loc["lat"],
                lng=loc["lng"],
                pickup_range_enabled=False,
                pickup_range_km=settings.default_pickup_range_km,
            )
            db.add(seller)
            sellers.append(seller)
        db.flush()
        print(f"Created 1 buyer + {len(sellers)} sellers (shared password {PASSWORD})")

        # ---------------- Items ----------------
        n_lib_photos = 0
        counts = split_counts(rng, n_items, args.sellers)
        owner_ids: list[int] = []
        for seller, c in zip(sellers, counts):
            owner_ids.extend([seller.id] * c)
        rng.shuffle(owner_ids)

        seller_loc = {s.id: (s.location_name, s.lat, s.lng) for s in sellers}
        items: list[Item] = []
        for idx, (payload, seller_id) in enumerate(zip(payloads, owner_ids)):
            # 70% use the seller's home location, 30% pick another one
            # (real people meet wherever is convenient)
            if rng.random() < 0.7:
                loc_name, lat, lng = seller_loc[seller_id]
            else:
                loc = rng.choice(CAMPUS_LOCATIONS)
                loc_name, lat, lng = loc["name"], loc["lat"], loc["lng"]

            t_start, t_end = rng.choice(TIME_SLOTS)
            if args.no_images:
                photo = None
            else:
                lib_path = library.get(f"{payload['base']}|{payload['color_key']}")
                if lib_path:
                    photo = compress_and_save(lib_path.read_bytes())
                    n_lib_photos += 1
                else:
                    photo = make_placeholder(payload["name"], idx)

            item = Item(
                seller_id=seller_id,
                name=payload["name"],
                description=payload["description"],
                summary=payload["summary"],
                attributes=payload["attributes"],
                price=payload["price"],
                photo_path=photo,
                pickup_location_name=loc_name,
                lat=lat,
                lng=lng,
                pickup_time_start=t_start,
                pickup_time_end=t_end,
                status=ItemStatus.SELLING,
                # Listing times spread over the last 45 days
                created_at=utcnow() - timedelta(minutes=rng.randint(30, 45 * 24 * 60)),
            )
            db.add(item)
            items.append(item)
            if (idx + 1) % 100 == 0:
                print(f"  ...generated {idx + 1}/{n_items} items")
        db.flush()
        for item in items:
            # `at` defaults to now(); passing the listing's own time is what
            # makes the status history reconstructable later. (Every row landing
            # on the seed instant is how 635 of 693 status_log rows ended up
            # carrying the same timestamp.)
            db.add(StatusLog(item_id=item.id, from_status=None,
                             to_status=ItemStatus.SELLING, reason="mock seed listing",
                             at=item.created_at))
        print(f"Created {len(items)} items"
              + ("" if args.no_images else
                 f" (library photos {n_lib_photos} / placeholders {len(items) - n_lib_photos})"))

        # ---------------- Status orchestration ----------------
        pool = items[:]
        rng.shuffle(pool)
        sold_items = pool[: args.sold]
        reserved_items = pool[args.sold: args.sold + args.reserved]

        seller_by_id = {s.id: s for s in sellers}

        def pick_other_buyers(item: Item, k: int) -> list[int]:
            """Pick k sellers who also act as buyers, excluding the item's own seller."""
            out: list[int] = []
            guard = 0
            while len(out) < k and guard < 50:
                guard += 1
                cand = rng.choice(sellers).id
                if cand != item.seller_id and cand not in out:
                    out.append(cand)
            return out

        # Buyer participation: reserve 14 of the reserved items + win 4 of the sold items
        buyer_reserved = set(rng.sample(range(len(reserved_items)), min(14, len(reserved_items))))
        buyer_won = set(rng.sample(range(len(sold_items)), min(4, len(sold_items))))
        # 3 of those become "about to expire" — handy for watching the countdown and the 60s expiry poll
        near_expiry = set(rng.sample(sorted(buyer_reserved), min(3, len(buyer_reserved))))

        n_reservations = 0
        for i, item in enumerate(reserved_items):
            queue: list[int] = []
            if i in buyer_reserved:
                queue.append(buyer.id)
            queue.extend(pick_other_buyers(item, rng.choice([0, 1, 1, 2])))
            if not queue:  # at least one reservation, or the status is inconsistent
                queue = pick_other_buyers(item, 1)

            for order, uid in enumerate(queue):
                db.add(Reservation(
                    item_id=item.id,
                    buyer_id=uid,
                    status=ReservationStatus.ACTIVE,
                    created_at=utcnow() - timedelta(minutes=rng.randint(5, 600) + order * 7),
                ))
                n_reservations += 1

            item.status = ItemStatus.RESERVED
            hours = 2 if i in near_expiry else settings.confirm_window_hours
            item.confirm_deadline = utcnow() + timedelta(hours=hours)
            db.add(StatusLog(item_id=item.id, from_status=ItemStatus.SELLING,
                             to_status=ItemStatus.RESERVED, reason="mock seed reservation",
                             at=utcnow() - timedelta(minutes=rng.randint(10, 700))))

        n_notifications = 0
        for i, item in enumerate(sold_items):
            winner_id = buyer.id if i in buyer_won else pick_other_buyers(item, 1)[0]
            losers = [u for u in pick_other_buyers(item, rng.choice([0, 1, 1, 2]))
                      if u != winner_id]

            snapshot = {
                "item_id": item.id,
                "item_name": item.name,
                "price": item.price,
                "pickup_location_name": item.pickup_location_name,
                "pickup_time_range": f"{item.pickup_time_start} ~ {item.pickup_time_end}",
            }
            sold_at = utcnow() - timedelta(hours=rng.randint(1, 240))

            # The winner's reservation stays active (the cart shows "deal confirmed"),
            # everyone else's goes to closed
            db.add(Reservation(item_id=item.id, buyer_id=winner_id,
                               status=ReservationStatus.ACTIVE,
                               created_at=sold_at - timedelta(hours=2)))
            db.add(Notification(user_id=winner_id, type=NotificationType.DEAL_CONFIRMED,
                                payload=snapshot, read=rng.random() < 0.4,
                                created_at=sold_at))
            n_reservations += 1
            n_notifications += 1

            for uid in losers:
                db.add(Reservation(item_id=item.id, buyer_id=uid,
                                   status=ReservationStatus.CLOSED,
                                   created_at=sold_at - timedelta(hours=3)))
                db.add(Notification(user_id=uid, type=NotificationType.DEAL_LOST,
                                    payload=snapshot, read=rng.random() < 0.4,
                                    created_at=sold_at))
                n_reservations += 1
                n_notifications += 1

            item.status = ItemStatus.SOLD_OUT
            item.confirm_deadline = None
            # Store the sale on the item, not only on the reservation and the
            # notification. Without these two columns `list_sold_evidence`
            # filters the row straight back out (it requires both non-null), so
            # `analyze_sold_prices` reports sample_size=0 for every category and
            # the seller-side price analysis silently has nothing to work with.
            # This was the state of the database until 2026-09-14.
            item.sold_at = sold_at
            item.sold_price = item.price
            db.add(StatusLog(item_id=item.id, from_status=ItemStatus.RESERVED,
                             to_status=ItemStatus.SOLD_OUT, reason="mock seed deal confirmed",
                             at=sold_at))

        db.commit()
        print(f"Wrote {n_reservations} reservations, {n_notifications} notifications")

        # ---------------- Rebuild indexes ----------------
        print("Rebuilding vector + BM25 indexes (first run loads the embedding model, hang on)...")
        stats = index.rebuild_from_db(db)

        # ---------------- Stats ----------------
        status_counts = Counter(i.status for i in items)
        cat_counts = Counter(i.attributes["category"] for i in items)
        cond_counts = Counter(i.attributes["condition"] for i in items)
        per_seller = Counter()
        for i in items:
            per_seller[i.seller_id] += 1
        loc_counts = Counter(i.pickup_location_name for i in items)
        prices = sorted(i.price for i in items)
        buyer_cart = sum(1 for it in reserved_items
                         if it.status == ItemStatus.RESERVED) and len(buyer_reserved)

    top = per_seller.most_common(1)[0][1]
    holdings = sorted(per_seller.values())
    print()
    print("=" * 56)
    print(f"Users     1 buyer + {args.sellers} sellers")
    print(f"Items     {len(items)} -> {dict(status_counts)}")
    print(f"          searchable (selling + reserved) = "
          f"{status_counts[ItemStatus.SELLING] + status_counts[ItemStatus.RESERVED]}")
    print(f"Category  {dict(cat_counts.most_common())}")
    print(f"Condition {dict(cond_counts.most_common())}")
    print(f"Prices    min={prices[0]:.0f}  p50={prices[len(prices)//2]:.0f}  "
          f"p90={prices[int(len(prices)*0.9)]:.0f}  max={prices[-1]:.0f}")
    print(f"Holdings  median {holdings[len(holdings)//2]} / max {top} / min {holdings[0]}")
    print(f"Pickup    {len(loc_counts)} locations; busiest is \"{loc_counts.most_common(1)[0][0]}\""
          f" ({loc_counts.most_common(1)[0][1]} items)")
    print(f"Names     {len(set(i.name for i in items))} unique (duplicates kept on purpose to test same-item ranking)")
    print(f"Index     {stats}")
    print("=" * 56)
    print()
    print("Login accounts (shared password demo1234):")
    print(f"  buyer   {BUYER_EMAIL}          cart has {buyer_cart} reservations, {len(buyer_won)} confirmed deals")
    print(f"  sellers seller001@campus.edu ~ seller{args.sellers:03d}@campus.edu")
    print(f"          biggest holder is seller{sellers.index(seller_by_id[per_seller.most_common(1)[0][0]]) + 1:03d}"
          f"@campus.edu ({top} items)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
