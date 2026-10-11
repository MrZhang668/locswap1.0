"""The seller sales advisor's read-only tools — callable and testable without the LLM.

Constraints:
* Zero database side effects: no StandingWant writes, no notify_count bumps,
  no notifications, no item changes;
* Reuse existing services (WantIndexService / ItemRepository.list_sold_evidence)
  — never spin up a second, inconsistent query path;
* Every output goes through the whitelist models in schemas.py, so buyer_id /
  raw_query / exact coordinates have no landing spot at this layer.

Signature contract — every tool is `fn(db, seller_id, params) -> BaseModel`:
* `seller_id` is the **authenticated** seller, passed explicitly rather than
  read off a request/session object. That keeps these functions pure enough to
  be wrapped by something else later (an MCP server, a batch job) without
  dragging the web layer along;
* which listing to act on now arrives inside `params` (the UI no longer pins
  one), so every item-scoped tool starts by calling `resolve_owned_item` —
  ownership is re-checked here, never assumed from the caller.
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ....config import settings
from ....models import (
    DemandEvent,
    DemandEventKind,
    Item,
    ItemStatus,
    ItemTraffic,
    StandingWant,
    WantStatus,
    utcnow,
)
from ....repositories import ItemRepository
from ....repositories.want_repo import StandingWantRepository
from ...geo import haversine_km
from ...text import raw_tokens
from ...want_index import get_want_index
from .schemas import (
    MIN_BUYERS_TO_SHOW_PHRASE,
    PRICE_BINS_MAX,
    AnalyzeDemandInput,
    AnalyzeDemandOutput,
    AnalyzeSoldPricesInput,
    AnalyzeSoldPricesOutput,
    BuyerCard,
    DemandBucket,
    DemandPhrase,
    EmptyResult,
    ListingTraffic,
    FindPotentialBuyersInput,
    FindPotentialBuyersOutput,
    ListMyItemsInput,
    ListMyItemsOutput,
    MyItemRow,
    PriceBin,
    PriceBox,
    PriceDistribution,
    SoldPriceStats,
    SoldSample,
    scrub_pii,
)

logger = logging.getLogger(__name__)

BUDGET_NOT_OFFER_CAVEAT = (
    "预算上限来自买家登记的长期求购——它们不是正式报价，也不构成购买承诺"
)


class ToolError(Exception):
    """A tool refusing to run for a business reason (unknown item, not yours,
    already sold).

    Distinct from an unexpected exception on purpose: the message here is
    written to be read **by the model** and is fed back as a tool_result so it
    can correct itself, whereas a crash is logged and reduced to a generic
    string. Nothing in these messages may confirm the existence of another
    seller's item — "not found" and "not yours" deliberately read the same.
    """


def resolve_owned_item(
    db: Session, seller_id: int, item_id: int, *, allow_held: bool = False
) -> Item:
    """The single gate between a model-supplied item_id and a real row.

    Every item-scoped tool goes through here. The model can name any integer;
    only ids belonging to `seller_id` resolve, and only while the listing is
    still active — the advisor analyzes what is on sale, not history.

    `allow_held` 是**给服务端内部调用用的，不是给模型用的**。它是一个 Python
    关键字参数，不在任何工具 schema 里，所以模型没有渠道打开它。

    它存在的理由是出售计划那条链路：卖家说「等需求量大了再卖」，系统把商品收进
    隐身（PAUSED）然后**每周替他量这件东西的需求**——而「收起来」恰好让这道闸门
    拒绝分析它。两件事都对：对话里问一件已下架商品的买家是无意义的（模型该被
    拦住），而周扫描量的正是一件被刻意收起来的商品（不拦）。区别在调用方，
    所以开关也放在调用方。
    """
    item = ItemRepository(db).get(int(item_id))
    if item is None or item.seller_id != int(seller_id):
        raise ToolError(
            f"item_id={item_id} 不是你的在售商品。"
            "调用 list_my_items 看看实际可用的 id。"
        )
    allowed = ItemStatus.PLANNABLE if allow_held else ItemStatus.SEARCHABLE
    if item.status not in allowed:
        raise ToolError(
            f"item_id={item_id}（{item.name}）当前状态是 {item.status}，已经不在售，"
            "没有可分析的内容。"
        )
    return item


# ============================================================
# Tool 0: list_my_items
# ============================================================
def list_my_items(
    db: Session, seller_id: int, params: ListMyItemsInput
) -> ListMyItemsOutput:
    """The seller's own active listings — the entry point for every other tool.

    Exists because the UI no longer pins one item: without a way to enumerate
    ids, the model would have to guess them, and a guessed id is either a
    refusal or (worse) the wrong item.
    """
    now = utcnow()
    wanted = (
        (*ItemStatus.SEARCHABLE, ItemStatus.PAUSED)
        if params.include_paused
        else ItemStatus.SEARCHABLE
    )
    active = [
        i for i in ItemRepository(db).list_by_seller(int(seller_id))
        if i.status in wanted
    ]
    rows = [
        MyItemRow(
            item_id=i.id,
            name=(i.name or "")[:60],
            price=float(i.price),
            category=(i.attributes or {}).get("category"),
            condition=(i.attributes or {}).get("condition"),
            status=i.status,
            listed_on=f"{i.created_at:%Y-%m-%d}",
            days_listed=max(0, (now - i.created_at).days),
        )
        for i in active[: params.limit]
    ]
    caveats: list[str] = []
    paused = sum(1 for i in active if i.status == ItemStatus.PAUSED)
    if paused:
        caveats.append(
            f"其中 {paused} 件是已下架状态（买家搜不到，也不会有需求数据），"
            "想重新卖就说一声恢复上架"
        )
    if not rows:
        caveats.append(
            "你没有在售商品——先挂一件出来，顾问才有数据可用"
        )
    elif len(active) > len(rows):
        caveats.append(f"共 {len(active)} 件在售商品，这里显示 {len(rows)} 件")
    # Counts what the name says: listings actually on sale. A paused row is in
    # `items` only when it was asked for, and it is not "active".
    total_active = sum(1 for i in active if i.status in ItemStatus.SEARCHABLE)
    return ListMyItemsOutput(total_active=total_active, items=rows, caveats=caveats)


# ============================================================
# card_id -> want_id resolution for the push endpoint
# ============================================================
# card_id stays the plain ordinal ("candidate-1") the seller sees, so nothing
# reversible ever reaches the frontend. The catch is that an ordinal is only
# meaningful **within one result set**: ask again with sort_by="nearest" and
# "candidate-1" is a different person. Pushing on a bare ordinal would
# therefore hit the wrong buyer, silently.
#
# So the mapping lives here, server-side, keyed by (seller, item) and holding
# only the most recent list — which is exactly the list the seller is looking
# at. Short-lived on purpose: a stale mapping is the same wrong-buyer bug in
# slow motion, and re-asking is one click.
CANDIDATE_TTL = timedelta(minutes=30)
_MAX_CACHE_ENTRIES = 500

_candidate_cache: dict[tuple[int, int], tuple[datetime, dict[str, int]]] = {}
_candidate_lock = threading.Lock()


def remember_candidates(seller_id: int, item_id: int, mapping: dict[str, int]) -> None:
    now = utcnow()
    with _candidate_lock:
        # Drop expired entries first; this cache has no other reaper.
        for key in [k for k, (at, _) in _candidate_cache.items() if now - at > CANDIDATE_TTL]:
            _candidate_cache.pop(key, None)
        if len(_candidate_cache) >= _MAX_CACHE_ENTRIES:
            oldest = min(_candidate_cache, key=lambda k: _candidate_cache[k][0])
            _candidate_cache.pop(oldest, None)
        _candidate_cache[(int(seller_id), int(item_id))] = (now, dict(mapping))


def resolve_candidate(seller_id: int, item_id: int, card_id: str) -> int | None:
    """card_id -> want_id, or None if the list expired / the card is unknown."""
    with _candidate_lock:
        entry = _candidate_cache.get((int(seller_id), int(item_id)))
        if entry is None:
            return None
        at, mapping = entry
        if utcnow() - at > CANDIDATE_TTL:
            _candidate_cache.pop((int(seller_id), int(item_id)), None)
            return None
        return mapping.get(card_id)


def reset_candidate_cache() -> None:
    with _candidate_lock:
        _candidate_cache.clear()


# ============================================================
# Tool 1: find_potential_buyers
# ============================================================
def _budget_display(want: StandingWant) -> str:
    if want.max_price is None:
        return "未填写预算"
    # Coarse display: round small amounts to $10, larger ones to $50 — never
    # reproduce the buyer's exact input
    step = 10 if want.max_price < 100 else 50
    rounded = int(round(want.max_price / step) * step) or int(want.max_price)
    if want.min_price is not None:
        low = int(round(want.min_price / step) * step)
        return f"预算大约 ${low}–${rounded}"
    return f"预算上限大约 ${rounded}"


def _distance_km(item: Item, want: StandingWant) -> float | None:
    if want.origin_lat is None or want.origin_lng is None:
        return None
    return haversine_km(want.origin_lat, want.origin_lng, item.lat, item.lng)


def _distance_display(km: float | None) -> str:
    if km is None:
        return "距离未知"
    if km < 1:
        return "1 公里以内"
    if km < 3:
        return "大约 1–3 公里"
    if km < 5:
        return "大约 3–5 公里"
    return "5 公里以上"


def _freshness(want: StandingWant) -> str:
    days = max(0, (utcnow() - want.created_at).days)
    if days == 0:
        fresh = "今天发布"
    elif days <= 3:
        fresh = "3 天内发布"
    elif days <= 7:
        fresh = "7 天内发布"
    else:
        fresh = f"{days} 天前发布"
    return f"{fresh} · 有效期至 {want.expires_at:%m-%d}"


def _demand_summary(want: StandingWant) -> str:
    """Redacted demand summary. **Uses only semantic_text and structured filters** — raw_query never enters (C8)."""
    text = scrub_pii(want.semantic_text or "")[:60]
    if text:
        return f"在找：{text}"
    f = want.filters or {}
    parts = [str(f[k]) for k in ("category", "brand", "color", "condition") if f.get(k)]
    if f.get("max_price"):
        parts.append(f"${int(f['max_price'])} 以内")
    return "在找：" + ("、".join(parts) if parts else "（只有筛选条件的求购，没有文字描述）")


def _match_reasons(item: Item, want: StandingWant, sim: float, km: float | None) -> list[str]:
    reasons: list[str] = []
    item_cat = (item.attributes or {}).get("category")
    if item_cat and want.category and item_cat == want.category:
        reasons.append("类别一致")
    if want.max_price is not None and item.price <= want.max_price:
        reasons.append("价格在预算内")
    if km is not None and km <= 3:
        reasons.append("距离近")
    if sim >= 0.6:
        reasons.append("需求描述高度相似")
    return reasons or ["语义相近"]


def find_potential_buyers(
    db: Session, seller_id: int, params: FindPotentialBuyersInput
) -> FindPotentialBuyersOutput:
    """Look up active Wants matching one of the seller's listings. Read-only; sorting is deterministic here."""
    item = resolve_owned_item(db, seller_id, params.item_id)
    top_n = min(params.top_n, settings.seller_advisor_top_n_max)
    # respect_notify_cap=False: the push cap is nuisance control, not a signal of
    # whether the buyer still wants the item; a seller-initiated lookup sends no
    # push, so it should not be filtered by that cap.
    matches = get_want_index().match_wants_for_item(
        db, item, limit=max(top_n * 4, 20), respect_notify_cap=False
    )
    wants = {
        w.id: w
        for w in StandingWantRepository(db).get_many([m.want_id for m in matches])
    }

    rows: list[tuple[StandingWant, float, float | None]] = []
    for m in matches:
        w = wants.get(m.want_id)
        if w is None:
            continue
        km = _distance_km(item, w)
        if params.radius_km is not None and (km is None or km > params.radius_km):
            continue
        rows.append((w, m.similarity, km))

    if params.sort_by == "nearest":
        rows.sort(key=lambda r: (r[2] is None, r[2] if r[2] is not None else 0.0))
    elif params.sort_by == "highest_budget":
        rows.sort(key=lambda r: (r[0].max_price is None, -(r[0].max_price or 0.0)))
    elif params.sort_by == "newest":
        rows.sort(key=lambda r: r[0].created_at, reverse=True)
    # best_match: keep match_wants_for_item's descending-similarity order

    rows = rows[:top_n]
    # Record which want each ordinal stands for, so the push endpoint can
    # resolve the card the seller clicked. Nothing here leaves the backend.
    remember_candidates(
        item.seller_id,
        item.id,
        {f"candidate-{i}": w.id for i, (w, _, _) in enumerate(rows, start=1)},
    )
    cards = [
        BuyerCard(
            card_id=f"candidate-{i}",
            demand_summary=_demand_summary(w),
            budget_display=_budget_display(w),
            distance_display=_distance_display(km),
            match_score=min(1.0, max(0.0, sim)),
            match_reasons=_match_reasons(item, w, sim, km),
            freshness=_freshness(w),
        )
        for i, (w, sim, km) in enumerate(rows, start=1)
    ]

    caveats = [BUDGET_NOT_OFFER_CAVEAT]
    empty: EmptyResult | None = None
    if not cards:
        caveats.append("当前没有匹配的长期求购——空结果就是真实状态，不是查询失败")
        # The threshold and the radius are named because without them "没有买家"
        # is unfalsifiable. With them the seller can see that the answer is
        # about a 0.53 similarity floor within N km, and can ask for a wider one.
        scope = [f"「{(item.name or '')[:40]}」", f"匹配阈值 {settings.want_match_min_similarity:.2f}"]
        if params.radius_km is not None:
            scope.append(f"{params.radius_km:g} 公里以内")
        else:
            scope.append("不限距离")
        alternatives = [
            "用 analyze_demand 看这个类别整体有没有人在搜——没有达标的公开求购，"
            "和没有人想要它，是两回事",
            "把商品的品牌、型号、成色补全，措辞对得上求购才可能达到阈值",
        ]
        if params.radius_km is not None:
            alternatives.insert(0, "去掉距离限制再找一次（现在被限制在指定半径内）")
        empty = EmptyResult(
            kind="no_matching_buyers",
            scope=" · ".join(scope),
            message="本次匹配没有任何公开求购达到阈值——不是没查到，是查了没有达标的",
            alternatives=alternatives,
        )
    elif len(cards) <= 2:
        caveats.append("匹配数量很少；需求摘要已脱敏——不要试图推测买家是谁")
    return FindPotentialBuyersOutput(
        item_id=item.id,
        item_name=(item.name or "")[:60],
        total=len(cards),
        sort_by=params.sort_by,
        cards=cards,
        empty=empty,
        caveats=caveats,
    )


# ============================================================
# Tool 2: analyze_sold_prices
# ============================================================
def _similarity_score(item: Item, other: Item) -> float:
    """Explainable similarity: brand > condition > item-name token overlap."""
    a, b = item.attributes or {}, other.attributes or {}
    score = 0.0
    if a.get("brand") and a.get("brand") == b.get("brand"):
        score += 2.0
    if a.get("condition") and a.get("condition") == b.get("condition"):
        score += 1.0
    overlap = set(raw_tokens(item.name or "")) & set(raw_tokens(other.name or ""))
    score += min(len(overlap), 3) * 0.5
    return score


def _percentile(sorted_prices: list[float], q: float) -> float:
    """Linear-interpolation percentile (samples may be tiny — avoids statistics.quantiles' n>=2 limit)."""
    if len(sorted_prices) == 1:
        return sorted_prices[0]
    pos = q * (len(sorted_prices) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_prices) - 1)
    return round(sorted_prices[lo] + (sorted_prices[hi] - sorted_prices[lo]) * (pos - lo), 2)


def analyze_sold_prices(
    db: Session, seller_id: int, params: AnalyzeSoldPricesInput
) -> AnalyzeSoldPricesOutput:
    """Read real sold evidence for comparable items and return statistics. Read-only; never changes prices."""
    item = resolve_owned_item(db, seller_id, params.item_id)
    category = (item.attributes or {}).get("category")
    evidence = ItemRepository(db).list_sold_evidence(
        category=category, limit=settings.seller_advisor_evidence_limit
    )
    evidence = [e for e in evidence if e.id != item.id]
    if not category:
        # Without a category, fall back to the explainable "item-name tokens overlap"
        # filter — better too few than too noisy
        evidence = [e for e in evidence if _similarity_score(item, e) > 0]

    caveats: list[str] = []
    if not evidence:
        return AnalyzeSoldPricesOutput(
            item_id=item.id,
            item_name=(item.name or "")[:60],
            category=category,
            current_price=float(item.price),
            stats=SoldPriceStats(sample_size=0),
            distribution=None,
            samples=[],
            confidence="none",
            empty=EmptyResult(
                kind="no_sold_evidence",
                scope=f"「{(item.name or '')[:40]}」 · "
                      + (f"{category} 类目" if category else "按商品名词重叠筛选（该商品未设类别）")
                      + " · 全部历史成交",
                message="平台上还没有任何可比商品的确认成交记录——"
                        "查询范围是全部历史，不是某个时间窗口，所以放宽时间也不会有",
                alternatives=[
                    "补全品牌 / 型号 / 成色后再分析一次——可比性是按这些字段判定的"
                    + ("" if category else "，并且先给这件商品设一个类别"),
                    "参考相近品类的成交价作为量级参考，但要把它当作参考、不是这件商品的行情",
                    "用 analyze_demand 看有没有人在找这类东西——没有成交记录不等于没有需求",
                ],
            ),
            caveats=["平台上还没有可比商品的有效成交价证据，"
                     "因此给不出可验证的价格区间。"
                     "可以考虑：在商品里补上品牌/成色信息，或者等平台积累更多成交后再分析一次。"],
        )

    prices = sorted(float(e.sold_price) for e in evidence)
    dates: list[datetime] = [e.sold_at for e in evidence]
    stats = SoldPriceStats(
        sample_size=len(prices),
        time_range=f"{min(dates):%Y-%m-%d} ~ {max(dates):%Y-%m-%d}",
        min_price=prices[0],
        p25=_percentile(prices, 0.25),
        median=_percentile(prices, 0.5),
        p75=_percentile(prices, 0.75),
        max_price=prices[-1],
    )
    n = len(prices)
    confidence = "high" if n >= 8 else "medium" if n >= 4 else "low"
    if n < 4:
        caveats.append(
            f"只有 {n} 条成交样本——统计量是真实数据，但置信度低，不要当成市场价"
        )
    caveats.append("所有数字都是本平台已确认成交的快照；在售商品的标价不计入")

    ranked = sorted(evidence, key=lambda e: (-_similarity_score(item, e), -e.sold_at.timestamp()))
    samples = [
        SoldSample(
            name=(e.name or "")[:40],
            sold_price=float(e.sold_price),
            sold_on=f"{e.sold_at:%Y-%m-%d}",
            condition=(e.attributes or {}).get("condition"),
        )
        for e in ranked[:5]
    ]
    return AnalyzeSoldPricesOutput(
        item_id=item.id,
        item_name=(item.name or "")[:60],
        category=category,
        current_price=float(item.price),
        stats=stats,
        distribution=_price_distribution(prices, stats, float(item.price)),
        samples=samples,
        confidence=confidence,
        caveats=caveats,
    )


def _price_distribution(
    prices: list[float], stats: SoldPriceStats, current_price: float
) -> PriceDistribution:
    """Bin a sorted price list into the histogram the frontend draws.

    Bin count scales with the sample (`ceil(sqrt(n))`, capped at
    `PRICE_BINS_MAX`) rather than being fixed: nine sales across twenty bins is
    a picture of the binning, and forty across three is a picture of nothing.

    Every sample lands in exactly one bin — the last bin is closed on the right
    so the maximum is not dropped, which is the off-by-one that would quietly
    make the bar counts disagree with `sample_size` the model is quoting in the
    same sentence.
    """
    lo, hi = prices[0], prices[-1]
    n = len(prices)
    k = max(1, min(PRICE_BINS_MAX, math.ceil(math.sqrt(n))))
    if hi <= lo:
        # Every sale went for the same price: one bar, and the label says the
        # price rather than a degenerate "$40 – $40" range.
        bins = [PriceBin(lo=lo, hi=hi, count=n, label=f"${lo:g}")]
    else:
        width = (hi - lo) / k
        counts = [0] * k
        for pr in prices:
            idx = min(k - 1, int((pr - lo) / width))
            counts[idx] += 1
        bins = [
            PriceBin(
                lo=round(lo + i * width, 2),
                hi=round(lo + (i + 1) * width, 2),
                count=c,
                label=f"${lo + i * width:.0f} – ${lo + (i + 1) * width:.0f}",
            )
            for i, c in enumerate(counts)
        ]
    return PriceDistribution(
        bins=bins,
        box=PriceBox(
            min_price=stats.min_price,
            p25=stats.p25,
            median=stats.median,
            p75=stats.p75,
            max_price=stats.max_price,
        ),
        current_price=current_price,
        sample_size=n,
        time_range=stats.time_range,
    )


# ============================================================
# Tool 3: analyze_demand
# ============================================================
#: How long a window has to be before the series is bucketed weekly. Under this
#: a daily series is readable; over it, daily buckets are mostly noise and a
#: 90-point list the model has to re-summarize before it can say anything.
WEEKLY_BUCKET_FROM_DAYS = 31

_WINDOW_DAYS = {"last_7d": 7, "last_30d": 30, "last_90d": 90, "all": None}
_WINDOW_LABEL = {"last_7d": "近 7 天", "last_30d": "近 30 天",
                 "last_90d": "近 90 天", "all": "全部时间"}


def _window_bounds(window: str) -> tuple[datetime | None, datetime]:
    """(start, end) for a window. `all` has no start.

    `end` is now, not the end of the simulated data — a window that quietly
    ended three weeks ago would report "demand collapsed" when what happened is
    that nothing has been written since.
    """
    end = utcnow()
    days = _WINDOW_DAYS.get(window, 30)
    return (None if days is None else end - timedelta(days=days)), end


def _bucket_key(kind: str, at: datetime) -> str:
    return f"{at:%Y-W%W}" if kind == "week" else f"{at:%Y-%m-%d}"


def _trend(series: list[DemandBucket]) -> tuple[str, str]:
    """Compare the back half of the series against the front half.

    Computed here rather than asked of the model. A model reading a column of
    numbers and saying "interest is picking up" produces something that sounds
    exactly like a measurement and is not one; the seller cannot tell the
    difference, and may drop a price over it.

    The 15% band is there so ordinary week-to-week noise does not get reported
    as a direction, and fewer than four buckets is called insufficient rather
    than flat — "no trend" and "not enough data to have one" are different
    answers and only one of them invites waiting.
    """
    if len(series) < 4:
        return "insufficient_data", f"只有 {len(series)} 个时间段有活动"
    half = len(series) // 2
    first = sum(b.events for b in series[:half]) / max(half, 1)
    second = sum(b.events for b in series[half:]) / max(len(series) - half, 1)
    if first == 0 and second == 0:
        return "insufficient_data", "这个时间窗口内没有搜索"
    if first == 0:
        return "rising", f"从零涨到每期 {second:.1f} 次搜索"
    change = (second - first) / first
    if change > 0.15:
        return "rising", f"每期搜索 {first:.1f} -> {second:.1f}（+{change:.0%}）"
    if change < -0.15:
        return "falling", f"每期搜索 {first:.1f} -> {second:.1f}（{change:.0%}）"
    return "flat", f"每期搜索 {first:.1f} -> {second:.1f}，属于正常波动"


def analyze_demand(
    db: Session, seller_id: int, params: AnalyzeDemandInput, *, allow_held: bool = False
) -> AnalyzeDemandOutput:
    """How many people have been looking for something like this listing, and when.

    Read-only. Three different things are reported and kept apart on purpose:
    the *category* series is the market, `listing_traffic` is how this listing
    did inside it, and `active_wants` is the only number that points forward.
    Collapsing them into one "demand score" would hide which of the three a
    seller should actually act on.
    """
    item = resolve_owned_item(db, seller_id, params.item_id, allow_held=allow_held)
    category = (item.attributes or {}).get("category")
    start, end = _window_bounds(params.window)
    span_days = (end - start).days if start else 3650
    bucket = "week" if span_days > WEEKLY_BUCKET_FROM_DAYS else "day"

    caveats: list[str] = []
    if not category:
        # Without a category there is no market to compare against; say so
        # rather than reporting the whole platform's demand as if it were this
        # item's.
        return AnalyzeDemandOutput(
            item_id=item.id,
            item_name=(item.name or "")[:60],
            category=None,
            window=params.window,
            window_range=_range_display(start, end),
            bucket=bucket,
            listing_traffic=_listing_traffic(db, item.id),
            empty=EmptyResult(
                kind="no_category",
                scope=f"「{(item.name or '')[:40]}」 · 未设置类别 · {_range_display(start, end)}",
                message="这件商品没有类别，需求是按类别聚合的，所以这次没有可比的对象——"
                        "这是查询前提不成立，不是需求为零",
                alternatives=[
                    "给这件商品补一个类别，之后这项分析才能做，商品本身也才更容易被搜到",
                    "先用 analyze_sold_prices 看可比成交价——它按商品名词重叠也能跑",
                ],
            ),
            caveats=["这件商品没有设置类别，因此没有可比的需求数据来衡量它。"
                     "补上类别之后这项分析才能做，商品本身也才更容易被搜到。"],
        )

    # ---- category demand over time ----
    q = select(DemandEvent).where(
        DemandEvent.kind == DemandEventKind.SEARCH,
        DemandEvent.category == category,
    )
    if start is not None:
        q = q.where(DemandEvent.at >= start)
    searches = list(db.scalars(q))

    vq = select(DemandEvent).where(
        DemandEvent.kind == DemandEventKind.VIEW,
        DemandEvent.item_id == item.id,
    )
    if start is not None:
        vq = vq.where(DemandEvent.at >= start)
    views = list(db.scalars(vq))

    buckets: dict[str, dict] = {}
    for ev in searches:
        b = buckets.setdefault(_bucket_key(bucket, ev.at), {"people": set(), "events": 0, "views": 0})
        b["people"].add(ev.buyer_id)
        b["events"] += 1
    for ev in views:
        b = buckets.setdefault(_bucket_key(bucket, ev.at), {"people": set(), "events": 0, "views": 0})
        b["views"] += 1

    series = [
        DemandBucket(period=k, people=len(v["people"]), events=v["events"], views=v["views"])
        for k, v in sorted(buckets.items())
    ]
    trend, trend_detail = _trend(series)

    # ---- phrasings, above the k-anonymity floor only ----
    by_phrase: dict[str, set[int]] = {}
    for ev in searches:
        text = (ev.query or "").strip().lower()
        if text:
            by_phrase.setdefault(text, set()).add(ev.buyer_id)

    # Which phrasings are about *this* listing rather than merely its category.
    # Same token-overlap test analyze_sold_prices uses for comparability — one
    # explainable rule beats two that can disagree about the same pair.
    item_tokens = set(raw_tokens(item.name or "")) | set(raw_tokens(item.summary or ""))
    def _matches(phrase: str) -> bool:
        return bool(item_tokens & set(raw_tokens(phrase)))

    matching_buyers: set[int] = set()
    for phrase, buyers in by_phrase.items():
        if _matches(phrase):
            matching_buyers |= buyers

    phrases = sorted(
        (
            DemandPhrase(phrase=p, people=len(b), matches_this_item=_matches(p))
            for p, b in by_phrase.items()
            if len(b) >= MIN_BUYERS_TO_SHOW_PHRASE
        ),
        # Relevant phrasings first, then by how many people used them: the list
        # is read top-down and the top of it should be the part that is about
        # this listing.
        key=lambda d: (not d.matches_this_item, -d.people),
    )[:6]
    suppressed = sum(1 for b in by_phrase.values() if len(b) < MIN_BUYERS_TO_SHOW_PHRASE)

    # ---- forward-looking: wants this listing could still satisfy ----
    active_wants = db.scalar(
        select(func.count()).select_from(StandingWant).where(
            StandingWant.status == WantStatus.ACTIVE,
            StandingWant.category == category,
        )
    ) or 0

    total_people = len({ev.buyer_id for ev in searches})
    empty: EmptyResult | None = None
    if not searches:
        caveats.append(
            f"这个时间窗口内没有人搜索{category}。这就是测量结果，"
            "不是数据缺失——把窗口放宽可以看出这是否属于常态。"
        )
        # The alternatives are ordered by how much they widen the query, and
        # the windows already tried are left out: offering "试试近 30 天" to a
        # seller who just asked about 近 30 天 reads as not having listened.
        wider = [w for w in ("last_30d", "last_90d", "all")
                 if _WINDOW_DAYS[w] is None
                 or _WINDOW_DAYS[w] > (_WINDOW_DAYS[params.window] or 10**6)]
        alternatives = [
            f"把窗口放宽到 {'/'.join(_WINDOW_LABEL[w] for w in wider)} 再看一次"
            "——这个窗口空着，可能是常态，也可能只是这几天安静"
        ] if wider else [
            "这已经是最宽的窗口了，所以这不是窗口太窄的问题"
        ]
        alternatives.append(
            f"用 find_potential_buyers 看有没有登记在册的公开求购——"
            f"没有人搜索{category}，不代表没有人挂着求购在等"
        )
        empty = EmptyResult(
            kind="no_demand_in_window",
            scope=f"「{(item.name or '')[:40]}」 · {category} 类目 · {_range_display(start, end)}",
            # The whole point of this string: it is a statement about a window,
            # and it is worded so it cannot be repeated as "没有人想要这个".
            message=f"在 {_range_display(start, end)} 这个区间里，没有人搜索过{category}。"
                    "这说的是这个窗口，不是这件商品没有需求——"
                    "观测范围的限制和结论是两回事",
            alternatives=alternatives,
        )
    elif total_people < 5:
        caveats.append(
            f"这个窗口内只有 {total_people} 位不同买家；把这个形状当作个例，不要当作趋势。"
        )
    if suppressed:
        caveats.append(
            f"有 {suppressed} 种搜索措辞的使用人数少于 {MIN_BUYERS_TO_SHOW_PHRASE} 人，"
            "已被隐去——展示一个人的原话等于把他指认出来。"
        )
    if searches and not matching_buyers:
        caveats.append(
            f"没有人用与这件商品对得上的措辞搜索——那 {len(searches)} 次搜索"
            f"落在{category}的其他东西上。对类别的关注不等于对这件商品的关注。"
        )
    caveats.append(
        "total_people 统计的是这个类别下所有活跃的人；people_matching_item 是其中措辞真正"
        "与这件商品对得上的那部分，是更小、也更真实的那个数。"
        "listing_traffic 是这件商品自身的表现。"
    )

    return AnalyzeDemandOutput(
        item_id=item.id,
        item_name=(item.name or "")[:60],
        category=category,
        window=params.window,
        window_range=_range_display(start, end),
        bucket=bucket,
        series=series,
        total_people=total_people,
        people_matching_item=len(matching_buyers),
        total_events=len(searches),
        total_views=len(views),
        active_wants=int(active_wants),
        trend=trend,
        trend_detail=trend_detail,
        listing_traffic=_listing_traffic(db, item.id),
        top_phrases=phrases,
        empty=empty,
        caveats=caveats,
    )


def _range_display(start: datetime | None, end: datetime) -> str:
    return f"{start:%Y-%m-%d} ~ {end:%Y-%m-%d}" if start else f"all time ~ {end:%Y-%m-%d}"


def _listing_traffic(db: Session, item_id: int) -> ListingTraffic:
    row = db.get(ItemTraffic, item_id)
    impressions = int(row.impressions) if row else 0
    clicks = int(row.clicks) if row else 0
    return ListingTraffic(
        impressions=impressions,
        entered_candidates=int(row.entered_candidates) if row else 0,
        clicks=clicks,
        # "n/a" rather than 0% when it was never shown: a 0% rate reads as "shown
        # and ignored", which is a different problem with a different fix.
        click_through=f"{clicks / impressions:.1%}" if impressions else "n/a",
    )
