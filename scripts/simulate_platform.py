"""Simulate 12 weeks of platform operation on top of the existing corpus.

Why this exists: the demand and pricing analysis the sales agent is supposed to
do has nothing to read today.

* `items.sold_at` / `sold_price` are NULL for every row in the database, so
  `analyze_sold_prices` returns `sample_size=0` for every item — the price
  analysis is not weak, it is empty. (Root cause: seed_mock computed a `sold_at`
  for the reservation/notification timestamps but never assigned it to the item.
  Fixed there too, so a fresh seed no longer has the hole.)
* `status_log.at` was never backdated either — 635 of 693 rows carry the single
  instant the seed ran — so the sale timeline cannot even be reconstructed.
* Standing wants: 28 rows, all expired, created inside one 3-day window, three
  of them the same sentence. "How many people wanted this, and when" has no
  answer in that.
* `item_traffic` holds four running counters per item and no dates at all, so
  nothing in the database can answer a question with a time in it.

What it does, in five phases (each reports before/after; nothing is written
without --apply):

  1. items    fill the two gaps around the existing corpus — 2026-06-22..07-07
              and 2026-08-27..09-14 — reusing mock_catalog's prototypes so the
              new rows are indistinguishable in style from the 515 already there.
  2. sales    raise the sold count to ~18% of the corpus, spread across the
              window, with sold_price drawn from the asking price by condition,
              and reservations / notifications / status_log all carrying the
              *sale's* timestamp rather than the moment this script ran.
  3. wants    ~180 standing wants across the window, statuses mixed
              (active / expired / fulfilled) instead of uniformly expired.
  4. demand   the `demand_event` log: one row per buyer action (search / view /
              want / reserve), shaped by a campus calendar — a summer lull, a
              move-in spike in late August, a settled semester level after.
  5. traffic  recompute `item_traffic` from the same simulation: for each search,
              RECALL_TOP_K items enter the candidate set and FINAL_TOP_K are
              shown, so the counters mean what their names say instead of
              counting searches.

Existing rows are not rewritten except where they are being *filled in*: the 15
items already marked sold_out get their missing sold_at/sold_price, and their
status_log timestamps are corrected. The 515 existing items keep their ids,
their created_at and their text, so the vector index does not have to be
rebuilt from scratch — only the new rows are added to it.

    python scripts/simulate_platform.py              # dry run: prints the plan
    python scripts/simulate_platform.py --apply
    python scripts/simulate_platform.py --apply --seed 7
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import _bootstrap  # noqa: F401  injects backend into sys.path

from app.config import CAMPUS_LOCATIONS, settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    DemandEvent,
    DemandEventKind,
    Item,
    ItemStatus,
    ItemTraffic,
    Notification,
    NotificationType,
    Reservation,
    ReservationStatus,
    StandingWant,
    StatusLog,
    User,
    WantSource,
    WantStatus,
)
from app.services.images import compress_and_save, make_placeholder  # noqa: E402
from mock_catalog import TIME_SLOTS  # noqa: E402
from seed_mock import build_item_payload, load_image_library  # noqa: E402

# ============================================================
# The window
# ============================================================
WINDOW_END = datetime(2026, 9, 14, 23, 0, 0)
WINDOW_START = WINDOW_END - timedelta(days=84)          # 12 weeks

#: The existing corpus occupies 2026-07-08 .. 08-26. These are the two holes.
GAP_EARLY = (WINDOW_START, datetime(2026, 7, 7, 23, 59))
GAP_LATE = (datetime(2026, 8, 27, 0, 0), WINDOW_END)
N_EARLY, N_LATE = 120, 150

#: Fraction of the whole corpus that has changed hands by the end of the window.
SOLD_RATE = 0.18
N_WANTS_TARGET = 180

#: How much of the asking price a sale actually fetches, by condition. Buyers
#: haggle harder the more wear there is; nothing sells above asking.
PRICE_REALIZATION = {
    "全新": (0.93, 1.00),
    "几乎全新": (0.88, 0.99),
    "成色很好": (0.82, 0.96),
    "成色良好": (0.75, 0.93),
    "成色一般": (0.62, 0.85),
}

#: Searches per day, before weekday noise. A campus marketplace is not flat:
#: summer is quiet, move-in week is the spike everything else is measured
#: against, and the semester settles above the summer baseline. Without a shape
#: like this the "demand over time" chart is a straight line and says nothing.
def daily_intensity(day: datetime) -> float:
    d = day.date()
    if d < datetime(2026, 7, 20).date():
        base = 18.0                     # summer lull
    elif d < datetime(2026, 8, 17).date():
        base = 32.0                     # people start planning the term
    elif d < datetime(2026, 8, 24).date():
        base = 62.0                     # lease turnover begins
    elif d < datetime(2026, 9, 3).date():
        base = 95.0                     # move-in week
    elif d < datetime(2026, 9, 10).date():
        base = 70.0                     # first week of classes
    else:
        base = 55.0                     # settled
    # Weekends run hotter for pickups and browsing alike.
    if day.weekday() >= 5:
        base *= 1.25
    return base


#: Which categories people search for, relative to each other.
CATEGORY_DEMAND = {
    "电子产品": 26,
    "图书与教材": 18,
    "家居与厨房": 14,
    "家具与电器": 13,
    "服装与鞋靴": 10,
    "运动与户外": 9,
    "学习用品": 5,
    "自行车与滑板车": 4,
    "乐器": 1,
}

#: Natural-language queries that name no product directly. Hand-written because
#: no catalogue contains them, and a search log without any of them reads like a
#: keyword list rather than people talking.
VAGUE_QUERIES = {
    "电子产品": ["记笔记用的便宜笔记本电脑", "上网课视频通话用的"],
    "图书与教材": ["下学期要用的教材", "便宜点的教材"],
    "家居与厨房": ["给宿舍添点东西", "能冰饮料的"],
    "家具与电器": ["刚搬进来想置办点家具", "能坐的东西"],
    "服装与鞋靴": ["冬天穿的保暖外套", "去健身房穿的"],
    "运动与户外": ["露营装备", "在家能练的器材"],
    "学习用品": ["下学期用的本子", "上课要用的东西"],
    "自行车与滑板车": ["校园里代步便宜点的", "通勤能骑的"],
    "乐器": ["想学着弹的乐器"],
}


def build_query_vocabulary() -> dict[str, list[str]]:
    """Search phrasings taken from the catalogue the listings themselves come from.

    The first version of this was a hand-written list of 6-10 phrases per
    category, and it left **34% of live listings with no matching search
    phrasing at all** — a third of sellers asking "who is looking for my item"
    would have been told "nobody used wording like this", which reads as a
    broken tool rather than as a fact about the market.

    The cause was two vocabularies: items are generated from mock_catalog's
    prototypes, queries were invented separately, and the overlap was whatever
    happened to coincide. Deriving the queries from the same prototypes makes
    the two vocabularies the same one by construction, so every kind of item
    that exists is also a thing somebody searched for.
    """
    from mock_catalog import CATALOG

    vocab: dict[str, list[str]] = {}
    for category, prototypes in CATALOG.items():
        # prototype[0] is the head noun ("mechanical keyboard", "rice cooker") —
        # already exactly what a buyer types.
        bases = sorted({proto[0] for proto in prototypes})
        vocab[category] = bases + VAGUE_QUERIES.get(category, [])
    return vocab


#: Superseded by build_query_vocabulary(); kept only as the source of
#: VAGUE_QUERIES' tone.
_LEGACY_QUERY_TEMPLATES = {
    "电子产品": ["机械键盘", "无线耳机", "500 以内的笔记本电脑", "显示器",
                    "充电宝", "usb-c 扩展坞", "蓝牙音箱", "上课用的摄像头",
                    "记笔记用的便宜笔记本电脑", "降噪耳机"],
    "图书与教材": ["有机化学教材", "微积分教材", "二手教材",
                          "经济学入门的书", "下学期要用的教材", "便宜点的教材",
                          "心理学教材", "实验手册"],
    "家居与厨房": ["电热水壶", "小冰箱", "电饭煲", "台灯",
                       "给宿舍添点东西", "微波炉", "空气炸锅", "咖啡机"],
    "家具与电器": ["书桌椅", "书架", "升降桌", "沙发床",
                               "刚搬进来想置办点家具", "斗柜", "落地灯"],
    "服装与鞋靴": ["冬天的外套", "跑鞋", "背包", "冲锋衣",
                         "冬天穿的保暖外套", "卫衣"],
    "运动与户外": ["瑜伽垫", "哑铃", "羽毛球拍", "帐篷",
                          "睡袋", "篮球", "露营装备"],
    "学习用品": ["科学计算器", "下学期用的本子",
                        "线圈本", "图形计算器", "桌面收纳"],
    "自行车与滑板车": ["300 以内的二手自行车", "通勤自行车", "山地车",
                         "校园里代步便宜点的", "电动滑板车", "折叠自行车"],
    "乐器": ["木吉他", "电子琴", "尤克里里"],
}


#: Built once at import; the catalogue is static data.
QUERY_VOCAB = build_query_vocabulary()


def snap(x: float) -> float:
    return round(x, 2)


def rand_dt(rng: random.Random, lo: datetime, hi: datetime) -> datetime:
    span = max(int((hi - lo).total_seconds()), 1)
    # Daytime-weighted: nobody lists a couch at 04:00.
    base = lo + timedelta(seconds=rng.randrange(span))
    hour = rng.choices(range(24), weights=[1, 1, 1, 1, 1, 1, 2, 4, 6, 7, 7, 8,
                                           8, 8, 8, 8, 9, 10, 11, 11, 10, 8, 5, 3])[0]
    return base.replace(hour=hour, minute=rng.randrange(60), second=rng.randrange(60))


# ============================================================
# Phase 1 — fill the two gaps with new listings
# ============================================================
def phase_items(db, rng, apply: bool, no_images: bool) -> list[Item]:
    sellers = list(db.query(User).all())
    if not sellers:
        raise SystemExit("no users in the database — run the seed first")
    library = {} if no_images else load_image_library()

    created: list[Item] = []
    plan = [(GAP_EARLY, N_EARLY, "early"), (GAP_LATE, N_LATE, "late")]
    for (lo, hi), n, label in plan:
        print(f"  {label:6} gap {lo:%Y-%m-%d} .. {hi:%Y-%m-%d}: +{n} listings")
        if not apply:
            continue
        for i in range(n):
            payload = build_item_payload(rng)
            loc = rng.choice(CAMPUS_LOCATIONS)
            t_start, t_end = rng.choice(TIME_SLOTS)
            photo = None
            if not no_images:
                lib_path = library.get(f"{payload['base']}|{payload['attributes']['color']}")
                photo = (compress_and_save(lib_path.read_bytes()) if lib_path
                         else make_placeholder(payload["name"], 10_000 + len(created)))
            at = rand_dt(rng, lo, hi)
            item = Item(
                seller_id=rng.choice(sellers).id,
                name=payload["name"],
                description=payload["description"],
                summary=payload["summary"],
                attributes=payload["attributes"],
                price=payload["price"],
                photo_path=photo,
                pickup_location_name=loc["name"],
                lat=loc["lat"],
                lng=loc["lng"],
                pickup_time_start=t_start,
                pickup_time_end=t_end,
                status=ItemStatus.SELLING,
                created_at=at,
            )
            db.add(item)
            created.append(item)
        db.flush()
    if apply:
        for item in created:
            db.add(StatusLog(item_id=item.id, from_status=None,
                             to_status=ItemStatus.SELLING,
                             reason="simulated listing", at=item.created_at))
    return created


# ============================================================
# Phase 2 — sales, with prices and timestamps that mean something
# ============================================================
def phase_sales(db, rng, apply: bool, _phase1_ran: bool) -> int:
    all_items = list(db.query(Item).all())
    users = [u.id for u in db.query(User).all()]
    # On a dry run phase 1 wrote nothing, so the corpus here is still the old
    # one. Project the new listings in anyway — a dry run that reports a plan
    # different from what --apply will do is worse than no dry run.
    projected = len(all_items) + (0 if all_items and _phase1_ran else N_EARLY + N_LATE)
    target = int(projected * SOLD_RATE)

    already = [i for i in all_items if i.status == ItemStatus.SOLD_OUT]
    # Items locked by a live reservation are mid-transaction; leave them alone.
    reserved_ids = {
        r.item_id for r in db.query(Reservation).filter(
            Reservation.status == ReservationStatus.ACTIVE
        )
    }
    candidates = [
        i for i in all_items
        if i.status == ItemStatus.SELLING and i.id not in reserved_ids
    ]
    need = max(target - len(already), 0)
    print(f"  corpus {projected}, already sold {len(already)}, "
          f"target {target} ({SOLD_RATE:.0%}) -> converting {need}")

    if not apply:
        return need

    # --- fill in the sales that already happened but recorded nothing ---
    _backfill_dates = assign_sale_dates(rng, [i for i in already if i.sold_at is None])
    for item in already:
        if item.sold_at is None:
            item.sold_at = _backfill_dates[item.id]
        if item.sold_price is None:
            item.sold_price = realized_price(rng, item)
        # Their status_log rows all carry the instant the seed ran; move them
        # onto the sale's own timeline so the history is readable.
        for log in db.query(StatusLog).filter(StatusLog.item_id == item.id):
            if log.to_status == ItemStatus.SELLING and log.from_status is None:
                log.at = item.created_at
            elif log.to_status == ItemStatus.RESERVED:
                log.at = item.sold_at - timedelta(hours=6)
            elif log.to_status == ItemStatus.SOLD_OUT:
                log.at = item.sold_at

    # --- convert more listings into sales, spread across the window ---
    rng.shuffle(candidates)
    chosen = [c for c in candidates
              if max(c.created_at + timedelta(hours=8), WINDOW_START) < WINDOW_END][:need]
    when = assign_sale_dates(rng, chosen)
    converted = 0
    for item in chosen:
        sold_at = when[item.id]

        winner = rng.choice([u for u in users if u != item.seller_id])
        losers = [u for u in rng.sample(users, k=min(len(users), rng.choice([0, 1, 1, 2])))
                  if u not in (winner, item.seller_id)]
        snapshot = {
            "item_id": item.id,
            "item_name": item.name,
            "price": item.price,
            "pickup_location_name": item.pickup_location_name,
            "pickup_time_range": f"{item.pickup_time_start} ~ {item.pickup_time_end}",
        }

        item.status = ItemStatus.SOLD_OUT
        item.confirm_deadline = None
        item.sold_at = sold_at
        item.sold_price = realized_price(rng, item)

        db.add(StatusLog(item_id=item.id, from_status=ItemStatus.SELLING,
                         to_status=ItemStatus.RESERVED, reason="simulated reservation",
                         at=sold_at - timedelta(hours=rng.randint(3, 40))))
        db.add(StatusLog(item_id=item.id, from_status=ItemStatus.RESERVED,
                         to_status=ItemStatus.SOLD_OUT, reason="simulated deal confirmed",
                         at=sold_at))
        db.add(Reservation(item_id=item.id, buyer_id=winner,
                           status=ReservationStatus.ACTIVE,
                           created_at=sold_at - timedelta(hours=2)))
        db.add(Notification(user_id=winner, type=NotificationType.DEAL_CONFIRMED,
                            payload=snapshot, read=rng.random() < 0.55, created_at=sold_at))
        for uid in losers:
            db.add(Reservation(item_id=item.id, buyer_id=uid,
                               status=ReservationStatus.CLOSED,
                               created_at=sold_at - timedelta(hours=3)))
            db.add(Notification(user_id=uid, type=NotificationType.DEAL_LOST,
                                payload=snapshot, read=rng.random() < 0.4, created_at=sold_at))
        converted += 1
    return converted


def assign_sale_dates(rng: random.Random, items: list, n: int | None = None) -> dict:
    """Decide *when* each of these items sold, as a set rather than one at a time.

    The obvious way — draw a lag from each item's listing date — is wrong, and
    wrong in a way that is invisible until you plot it. Listings are close to
    uniform across the window, so a per-item lag convolves uniform with a short
    tail and lands back on uniform: a flat sales curve sitting underneath a
    demand curve that spikes five-fold at move-in. Nothing in the data would
    contradict it, and an agent reading it would tell a seller there is no good
    time to sell.

    So the shape comes first. Sale *dates* are drawn against `daily_intensity`
    — the same curve the search log is generated from, which is what makes the
    two series consistent — and then matched to items under the one hard
    constraint that exists: nothing sells before it was listed.

    The matching is a sweep. Both sides are sorted by time; as the sweep passes
    each sale date, every item listed by then joins the eligible pool and one is
    drawn from it. An item that is never eligible (listed after the last drawn
    date) gets a feasible date of its own at the end rather than being dropped.
    """
    n = n or len(items)
    days = int((WINDOW_END - WINDOW_START).days)
    day_list = [WINDOW_START + timedelta(days=d) for d in range(days)]
    weights = [daily_intensity(d) for d in day_list]
    dates = sorted(rand_dt(rng, d, d + timedelta(days=1))
                   for d in rng.choices(day_list, weights=weights, k=n))

    pending = sorted(items, key=lambda it: it.created_at)
    cursor = 0
    pool: list = []
    out: dict = {}
    for when in dates:
        while cursor < len(pending) and pending[cursor].created_at + timedelta(hours=8) <= when:
            pool.append(pending[cursor])
            cursor += 1
        if not pool:
            continue
        chosen = pool.pop(rng.randrange(len(pool)))
        out[chosen.id] = when

    # Anything still unplaced was listed too late for any drawn date; give it a
    # date in its own remaining span so no sale silently disappears.
    for it in items:
        if it.id in out:
            continue
        lo = max(it.created_at + timedelta(hours=8), WINDOW_START)
        if lo >= WINDOW_END:
            out[it.id] = WINDOW_END
        else:
            out[it.id] = rand_dt(rng, lo, WINDOW_END)
    return out


def realized_price(rng: random.Random, item: Item) -> float:
    cond = (item.attributes or {}).get("condition") or "成色良好"
    lo, hi = PRICE_REALIZATION.get(cond, PRICE_REALIZATION["成色良好"])
    return snap(float(item.price) * rng.uniform(lo, hi))


# ============================================================
# Phase 3 — standing wants across the window, statuses mixed
# ============================================================
def phase_wants(db, rng, apply: bool) -> int:
    existing = db.query(StandingWant).count()
    users = [u.id for u in db.query(User).all()]
    need = max(N_WANTS_TARGET - existing, 0)
    print(f"  standing wants {existing} -> {N_WANTS_TARGET} (+{need})")
    if not apply:
        return need

    ttl = timedelta(days=settings.want_ttl_days)
    cats = list(CATEGORY_DEMAND)
    weights = list(CATEGORY_DEMAND.values())
    sold_by_cat = defaultdict(list)
    for it in db.query(Item).filter(Item.status == ItemStatus.SOLD_OUT):
        sold_by_cat[(it.attributes or {}).get("category")].append(it)

    for _ in range(need):
        cat = rng.choices(cats, weights=weights)[0]
        created = rand_dt(rng, WINDOW_START, WINDOW_END)
        query = rng.choice(QUERY_VOCAB[cat])
        # Two thirds are captured from a search (implicit); the rest are the
        # buyer explicitly asking to be told — the split the product actually has.
        source = WantSource.IMPLICIT if rng.random() < 0.66 else WantSource.EXPLICIT
        max_price = None
        if rng.random() < 0.45:
            max_price = float(rng.choice([25, 40, 50, 75, 100, 150, 200, 300, 500]))
        loc = rng.choice(CAMPUS_LOCATIONS)

        expires = created + ttl
        if expires <= WINDOW_END:
            status = WantStatus.EXPIRED
        else:
            status = WantStatus.ACTIVE
        fulfilled_by = None
        # A want that a matching sale landed on top of is closed as fulfilled.
        pool = [s for s in sold_by_cat.get(cat, [])
                if s.sold_at and created <= s.sold_at <= expires]
        if pool and rng.random() < 0.28:
            hit = rng.choice(pool)
            status = WantStatus.FULFILLED
            fulfilled_by = hit.id

        db.add(StandingWant(
            buyer_id=rng.choice(users),
            raw_query=query,
            filters={"category": cat} | ({"max_price": max_price} if max_price else {}),
            semantic_text=query,
            category=cat if rng.random() < 0.7 else None,
            max_price=max_price,
            min_price=None,
            origin_lat=loc["lat"],
            origin_lng=loc["lng"],
            max_distance_km=float(rng.choice([2, 3, 5, 5, 8])) if rng.random() < 0.5 else None,
            source=source,
            status=status,
            fulfilled_by_item_id=fulfilled_by,
            notify_count=0,
            created_at=created,
            expires_at=expires,
        ))
    return need


# ============================================================
# Phases 4 & 5 — the demand log, and the traffic counters derived from it
# ============================================================
def phase_demand(db, rng, apply: bool) -> tuple[int, dict]:
    users = [u.id for u in db.query(User).all()]
    items = list(db.query(Item).all())
    by_cat = defaultdict(list)
    for it in items:
        by_cat[(it.attributes or {}).get("category")].append(it)

    days = int((WINDOW_END - WINDOW_START).days)
    planned = sum(int(daily_intensity(WINDOW_START + timedelta(days=d))) for d in range(days))
    print(f"  {days} days, ~{planned} searches planned "
          f"(peak/day {max(int(daily_intensity(WINDOW_START + timedelta(days=d))) for d in range(days))}, "
          f"floor {min(int(daily_intensity(WINDOW_START + timedelta(days=d))) for d in range(days))})")
    if not apply:
        return planned, {}

    cats = list(CATEGORY_DEMAND)
    weights = list(CATEGORY_DEMAND.values())
    traffic = defaultdict(lambda: {"impressions": 0, "entered_candidates": 0, "clicks": 0})
    n_search = n_view = 0
    rows: list[DemandEvent] = []

    for d in range(days):
        day = WINDOW_START + timedelta(days=d)
        n = max(1, int(rng.gauss(daily_intensity(day), daily_intensity(day) * 0.18)))
        for _ in range(n):
            cat = rng.choices(cats, weights=weights)[0]
            at = rand_dt(rng, day, day + timedelta(days=1))
            buyer = rng.choice(users)
            rows.append(DemandEvent(buyer_id=buyer, kind=DemandEventKind.SEARCH,
                                    query=rng.choice(QUERY_VOCAB[cat]),
                                    category=cat, item_id=None, at=at))
            n_search += 1

            # What the retrieval pipeline would have done with it: RECALL_TOP_K
            # items enter the candidate set, FINAL_TOP_K of them get shown.
            # Only items that were actually on sale at that moment can appear.
            live = [it for it in by_cat.get(cat, ())
                    if it.created_at <= at and (it.sold_at is None or it.sold_at > at)]
            if not live:
                continue
            cand = rng.sample(live, k=min(settings.recall_top_k, len(live)))
            for it in cand:
                traffic[it.id]["entered_candidates"] += 1
            shown = cand[: settings.final_top_k]
            for it in shown:
                traffic[it.id]["impressions"] += 1
            # Click-through: most searches end without opening anything.
            if rng.random() < 0.34:
                clicked = rng.choice(shown)
                traffic[clicked.id]["clicks"] += 1
                rows.append(DemandEvent(buyer_id=buyer, kind=DemandEventKind.VIEW,
                                        query="", category=cat, item_id=clicked.id,
                                        at=at + timedelta(seconds=rng.randint(4, 90))))
                n_view += 1
        if len(rows) > 4000:
            db.add_all(rows)
            db.flush()
            rows = []
    db.add_all(rows)
    db.flush()

    # The other two kinds are not invented here — they mirror rows that exist.
    n_want = n_res = 0
    for w in db.query(StandingWant).all():
        db.add(DemandEvent(buyer_id=w.buyer_id, kind=DemandEventKind.WANT,
                           query=w.raw_query or "", category=w.category,
                           item_id=None, at=w.created_at))
        n_want += 1
    for r in db.query(Reservation).all():
        it = db.get(Item, r.item_id)
        db.add(DemandEvent(buyer_id=r.buyer_id, kind=DemandEventKind.RESERVE,
                           query="", category=(it.attributes or {}).get("category") if it else None,
                           item_id=r.item_id, at=r.created_at))
        n_res += 1

    print(f"  events: search {n_search}, view {n_view}, want {n_want}, reserve {n_res}")
    return n_search + n_view + n_want + n_res, traffic


def phase_traffic(db, traffic: dict, apply: bool) -> int:
    print(f"  item_traffic rows to write: {len(traffic)}")
    if not apply:
        return len(traffic)
    db.query(ItemTraffic).delete()
    for item_id, c in traffic.items():
        db.add(ItemTraffic(item_id=item_id, impressions=c["impressions"],
                           entered_candidates=c["entered_candidates"],
                           pushed=0, clicks=c["clicks"], updated_at=WINDOW_END))
    return len(traffic)


# ============================================================
def summarize(db) -> None:
    from sqlalchemy import func

    print("\n--- resulting distributions ---")
    total = db.query(Item).count()
    sold = db.query(Item).filter(Item.sold_at.is_not(None)).count()
    print(f"items {total}, with sold_at {sold} ({sold / max(total,1):.1%})")
    def by_week(col, *filters):
        # SQLAlchemy rejects positional group_by(1) — group by the expression.
        week = func.strftime("%Y-W%W", col)
        q = db.query(week, func.count()).filter(col.is_not(None))
        for f in filters:
            q = q.filter(f)
        return sorted(q.group_by(week).all())

    fmt = lambda rows: " ".join(f"{w.split('-')[1]}:{c}" for w, c in rows)
    print("listings/week:", fmt(by_week(Item.created_at)))
    print("sales/week   :", fmt(by_week(Item.sold_at)))
    print("searches/week:", fmt(by_week(DemandEvent.at, DemandEvent.kind == DemandEventKind.SEARCH)))
    print("want status  :", dict(db.query(StandingWant.status, func.count()).group_by(StandingWant.status).all()))
    print("want source  :", dict(db.query(StandingWant.source, func.count()).group_by(StandingWant.source).all()))
    r = db.query(func.count(), func.sum(ItemTraffic.impressions),
                 func.sum(ItemTraffic.entered_candidates), func.sum(ItemTraffic.clicks)).one()
    print(f"item_traffic : rows={r[0]} impressions={r[1]} candidates={r[2]} clicks={r[3]}")


def retime_sales(db, rng, apply: bool) -> int:
    """Redraw `sold_at` for sales that are already in the database, and carry
    every timestamp that hangs off it along.

    Exists so a database written by an earlier run of this script can be
    corrected without regenerating the corpus (which would orphan 270 already
    written item photos). The *set* of sold items does not change — only when
    each sale happened.
    """
    sold = [i for i in db.query(Item).filter(Item.sold_at.is_not(None))]
    print(f"  re-timing {len(sold)} sales against the demand curve")
    if not apply:
        return len(sold)
    when = assign_sale_dates(rng, sold)
    for item in sold:
        new_at = when[item.id]
        old_at = item.sold_at
        item.sold_at = new_at
        for log in db.query(StatusLog).filter(StatusLog.item_id == item.id):
            if log.to_status == ItemStatus.SOLD_OUT:
                log.at = new_at
            elif log.to_status == ItemStatus.RESERVED:
                log.at = new_at - timedelta(hours=rng.randint(3, 40))
        for r in db.query(Reservation).filter(Reservation.item_id == item.id):
            delta = old_at - r.created_at if old_at else timedelta(hours=2)
            r.created_at = new_at - delta
        for n in db.query(Notification).filter(
            Notification.type.in_((NotificationType.DEAL_CONFIRMED, NotificationType.DEAL_LOST))
        ):
            if (n.payload or {}).get("item_id") == item.id:
                n.created_at = new_at
    # The reserve events mirror reservation rows, so they move too.
    for ev in db.query(DemandEvent).filter(DemandEvent.kind == DemandEventKind.RESERVE):
        r = db.query(Reservation).filter(
            Reservation.item_id == ev.item_id, Reservation.buyer_id == ev.buyer_id
        ).first()
        if r is not None:
            ev.at = r.created_at
    return len(sold)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    ap.add_argument("--retime-sales-only", action="store_true",
                    help="skip generation; only redraw sold_at for existing sales")
    ap.add_argument("--redo-demand-only", action="store_true",
                    help="skip generation; rebuild demand_event and item_traffic from scratch")
    ap.add_argument("--seed", type=int, default=20260914)
    ap.add_argument("--no-images", action="store_true", help="new listings get no photo")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    init_db()
    db = SessionLocal()
    try:
        print(f"window {WINDOW_START:%Y-%m-%d} .. {WINDOW_END:%Y-%m-%d}  "
              f"({(WINDOW_END - WINDOW_START).days} days)\n")
        if args.redo_demand_only:
            # Items, sales and wants stay exactly as they are; only the buyer
            # activity log and the counters derived from it are rebuilt.
            n = db.query(DemandEvent).delete()
            print(f"[redo] cleared {n} demand_event rows")
            n_events, traffic = phase_demand(db, rng, args.apply)
            phase_traffic(db, traffic, args.apply)
            if not args.apply:
                db.rollback()
                print("\nDRY RUN — nothing written.")
                return 0
            db.commit()
            print("\ncommitted.")
            summarize(db)
            return 0
        if args.retime_sales_only:
            print("[retime] sales")
            retime_sales(db, rng, args.apply)
            if not args.apply:
                db.rollback()
                print("\nDRY RUN — nothing written.")
                return 0
            db.commit()
            print("\ncommitted.")
            summarize(db)
            return 0
        print("[1/5] listings")
        new_items = phase_items(db, rng, args.apply, args.no_images)
        print(f"      created {len(new_items)}")
        print("[2/5] sales")
        n_sold = phase_sales(db, rng, args.apply, bool(new_items))
        print(f"      converted {n_sold}")
        print("[3/5] standing wants")
        n_wants = phase_wants(db, rng, args.apply)
        print(f"      added {n_wants}")
        if args.apply:
            db.flush()
        print("[4/5] demand events")
        n_events, traffic = phase_demand(db, rng, args.apply)
        print("[5/5] traffic counters")
        phase_traffic(db, traffic, args.apply)

        if not args.apply:
            db.rollback()
            print("\nDRY RUN — nothing written. Re-run with --apply.")
            return 0
        db.commit()
        print("\ncommitted.")
        summarize(db)
        print("\nNOTE: restart the backend so the vector index picks up the new "
              "listings and drops the ones that just sold.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
