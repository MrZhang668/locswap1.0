"""Pydantic schemas for the seller sales advisor — tool inputs/outputs and the API contract.

The privacy red line is machine-enforced at this layer (not just in the system prompt):
* BuyerCard is a **fixed whitelist** of seller-visible fields — there is simply
  no slot for buyer_id / raw_query / exact coordinates, and field-level
  validation scrubs PII patterns once more;
* every outward-facing schema uses extra="forbid" so a tool implementation can
  never accidentally smuggle in extra fields.

Since the item picker was removed from the UI, `item_id` is a **model-supplied**
tool parameter rather than backend-injected context. That does not weaken the
ownership boundary: the tool layer re-resolves every item_id against the
authenticated seller (see tools.resolve_owned_item), so an id the seller does
not own simply does not resolve. Every per-item output therefore echoes back
item_id / item_name, both so the frontend can group results and so the seller
can see which listing an answer is actually about.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..images import check_upload_path
from ..publishing.schemas import PublishDraftView, PublishFinding
#: Re-exported: the tool layer imports it from here, and it is shared with the
#: publishing flow, so it lives in services/text.py now.
from ..text import scrub_pii

SortBy = Literal["best_match", "nearest", "highest_budget", "newest"]


#: The shapes of "we looked and there was nothing there". Each one is a real
#: measurement with its own meaning, which is the whole point of enumerating
#: them: `no_demand_in_window` is a statement about a 7-day window and
#: `no_matching_buyers` is a statement about a similarity threshold, and
#: collapsing either into "no demand" turns the limits of the query into a
#: fact about the market.
EmptyKind = Literal[
    "no_sold_evidence",      # nothing comparable has sold on the platform
    "no_demand_in_window",   # nobody searched this category *inside this window*
    "no_matching_buyers",    # no open want cleared the match threshold
    "no_category",           # the listing has no category, so there is nothing to compare against
]


class EmptyResult(BaseModel):
    """An empty tool result, modelled as a conclusion rather than as a gap.

    The failure this exists to stop: a tool returns nothing, the system reads
    that as "I do not know enough yet", and asks the seller for more product
    attributes. No amount of extra attributes will make the platform grow a
    sold record it does not have, so the question is unanswerable by
    construction and the seller is asked it anyway.

    Three parts, because an honest empty answer needs all three and a model
    left to improvise reliably drops the first one:

    * `scope` — what was actually queried (which listing, which window, which
      threshold). Without it "没有数据" is unfalsifiable; with it the seller can
      see that the answer is about the last 7 days and ask for 90.
    * `message` — the measurement, stated as a measurement.
    * `alternatives` — something the seller can actually do next. An empty
      result with no next step reads as a dead end even when it is not one.
    """

    model_config = ConfigDict(extra="forbid")

    kind: EmptyKind
    scope: str                                   # "破壁机 · 家居与厨房 · 近 7 天"
    message: str                                 # "本窗口内没有人搜索家居与厨房"
    alternatives: list[str] = Field(default_factory=list)


class ItemScopedInput(BaseModel):
    """Base for any tool that operates on one of the seller's listings.

    item_id being model-controlled is safe **only** because the tool layer
    resolves it under the authenticated seller_id. Nothing downstream may
    accept a bare item_id from this object without going through
    tools.resolve_owned_item.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: int = Field(ge=1)


# ============================================================
# Tool: list_my_items
# ============================================================
class ListMyItemsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=50)
    #: Paused listings are off sale, so they are absent from every analysis
    #: path by construction. They are still the seller's, though, and a seller
    #: asking "我下架了哪几件" or "把那个恢复上架" needs them enumerable —
    #: otherwise the only way to see them is the other page.
    include_paused: bool = False


class MyItemRow(BaseModel):
    """One of the seller's own active listings. Public listing fields only —
    no reservation identities, no buyer information."""

    model_config = ConfigDict(extra="forbid")

    item_id: int
    name: str = Field(max_length=60)
    price: float
    category: str | None = None
    condition: str | None = None
    status: str
    listed_on: str                     # date only
    days_listed: int

    @field_validator("name")
    @classmethod
    def _scrub(cls, v: str) -> str:
        return scrub_pii(v)


class ListMyItemsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_active: int
    items: list[MyItemRow] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


# ============================================================
# Tool 1: find_potential_buyers
# ============================================================
class FindPotentialBuyersInput(ItemScopedInput):
    """Query parameters the model may control, including which of the seller's
    own listings to analyze. Ownership is enforced at execution time."""

    sort_by: SortBy = "best_match"
    radius_km: float | None = Field(default=None, gt=0, le=50)
    top_n: int = Field(default=5, ge=1)


class BuyerCard(BaseModel):
    """Potential-buyer card visible to the seller — a fixed whitelist; any new
    field must pass privacy review first."""

    model_config = ConfigDict(extra="forbid")

    card_id: str                       # ordinal identifier valid only within this response; not reversible
    demand_summary: str
    budget_display: str
    distance_display: str
    match_score: float = Field(ge=0.0, le=1.0)
    match_reasons: list[str] = Field(default_factory=list)
    freshness: str

    @field_validator("demand_summary", "budget_display", "distance_display")
    @classmethod
    def _scrub(cls, v: str) -> str:
        return scrub_pii(v)


class FindPotentialBuyersOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Which listing these cards belong to. The frontend's push button needs it
    #: (card ordinals are only meaningful within one item's candidate list), and
    #: without it a multi-item answer would render ambiguous cards.
    item_id: int
    item_name: str
    total: int
    sort_by: SortBy
    cards: list[BuyerCard] = Field(default_factory=list)
    #: Set when `cards` is empty. Never a substitute for the empty list — it is
    #: the reading of it, carried in a field the renderer and the scorer can
    #: both see rather than buried in prose.
    empty: EmptyResult | None = None
    caveats: list[str] = Field(default_factory=list)


# ============================================================
# Tool 2: analyze_sold_prices
# ============================================================
class AnalyzeSoldPricesInput(ItemScopedInput):
    """Beyond item_id there are no model-tunable parameters: category and
    similarity filtering are executed deterministically by the backend."""


class SoldSample(BaseModel):
    """Redacted summary of one sold-price evidence row — public item info only,
    no buyer or seller identity."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=40)
    sold_price: float
    sold_on: str                       # date only, never the exact time
    condition: str | None = None

    @field_validator("name")
    @classmethod
    def _scrub(cls, v: str) -> str:
        return scrub_pii(v)


class SoldPriceStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_size: int
    time_range: str = ""               # e.g. "2026-06-01 ~ 2026-08-20"
    min_price: float | None = None
    p25: float | None = None
    median: float | None = None
    p75: float | None = None
    max_price: float | None = None


#: How many bars a price histogram gets. Small on purpose: these samples run
#: from three rows to a few dozen, and a 20-bin histogram over nine sales is a
#: picture of the binning rather than of the market.
PRICE_BINS_MAX = 8


class PriceBin(BaseModel):
    """One bar of the sold-price histogram."""

    model_config = ConfigDict(extra="forbid")

    lo: float
    hi: float
    count: int = Field(ge=0)
    label: str                             # "$20 – $35", pre-rendered


class PriceBox(BaseModel):
    """The five-number summary, repeated here rather than referenced.

    It duplicates `SoldPriceStats`, and that is deliberate: a chart contract
    that has to be joined against a second object to be drawn is one the
    renderer can get subtly wrong (drawing a box from this turn's quartiles
    over last turn's bars). Everything a price chart needs is in this object.
    """

    model_config = ConfigDict(extra="forbid")

    min_price: float
    p25: float
    median: float
    p75: float
    max_price: float


class PriceDistribution(BaseModel):
    """The renderable shape of a sold-price result — the price-side counterpart
    of `AnalyzeDemandOutput.series`.

    Why this exists: asked to "画个图" the model had the full statistics and no
    channel to return a chart, so it drew one out of ├──────┤ and █████.
    The numbers were right and the output was still unusable, because a
    character drawing is not something a frontend can render, zoom, or label.
    The gap was in the tool's return contract, not in the model's judgement —
    demand analysis had a series to hand back and pricing had nothing.
    """

    model_config = ConfigDict(extra="forbid")

    bins: list[PriceBin] = Field(default_factory=list)
    box: PriceBox
    #: Where the seller's own asking price sits against the bars — the one
    #: comparison every pricing question is really asking for.
    current_price: float
    sample_size: int = Field(ge=0)
    time_range: str = ""


class AnalyzeSoldPricesOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int
    item_name: str
    category: str | None
    current_price: float
    stats: SoldPriceStats
    #: None exactly when there is nothing to draw (sample_size == 0).
    distribution: PriceDistribution | None = None
    samples: list[SoldSample] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low", "none"]
    empty: EmptyResult | None = None
    caveats: list[str] = Field(default_factory=list)


# ============================================================
# Orchestrator final output (model-generated, backend-validated)
# ============================================================
# ============================================================
# Tool 3: analyze_demand
# ============================================================
#: Timeframes the model may ask for. The same closed set the router extracts
#: into `slots.time_window`, so a routing hint can be passed straight through
#: without a translation step that could disagree with itself.
DemandWindow = Literal["last_7d", "last_30d", "last_90d", "all"]

#: Minimum distinct buyers before a search phrasing may be shown to the seller.
#: Counts are safe to report — "14 people looked for bikes" identifies nobody.
#: The *words* are not: a single buyer's phrasing is close to a quote, and a
#: seller who can see one verbatim query plus a timestamp plus a category is
#: some of the way to knowing who wrote it. Three is the floor that keeps the
#: useful signal ("lots of people are typing 'cheap laptop for notes'") while
#: dropping the one-off that reads like eavesdropping. Constraint C8.
MIN_BUYERS_TO_SHOW_PHRASE = 3


class DemandBucket(BaseModel):
    """One point on the demand chart.

    `people` and `events` are both here because they answer different
    questions and diverge in a way that matters: twenty searches from three
    people is a handful of determined buyers, twenty from twenty is a market.
    Reporting only one of them lets the seller read the wrong story.
    """

    model_config = ConfigDict(extra="forbid")

    period: str                      # "2026-W35" for weekly, "2026-09-08" for daily
    people: int = Field(ge=0)        # distinct buyers, never ids
    events: int = Field(ge=0)        # searches in this bucket
    views: int = Field(ge=0)         # views of *this* listing in this bucket


class DemandPhrase(BaseModel):
    """A way buyers phrase this demand, shown only above the k-anonymity floor.

    `matches_this_item` is the difference between market context and a lead.
    Phrases are gathered per category, and a category is wide: 台灯,
    小冰箱 and 电饭煲 are all 家居与厨房. A seller reading an
    unlabelled list of the category's top phrases next to their rice cooker
    will read all of them as people who might buy it. The flag says which ones
    actually overlap the listing, so the useful context can be shown without
    inviting that reading.
    """

    model_config = ConfigDict(extra="forbid")

    phrase: str
    people: int = Field(ge=MIN_BUYERS_TO_SHOW_PHRASE)
    matches_this_item: bool = False

    @field_validator("phrase")
    @classmethod
    def _scrub(cls, v: str) -> str:
        return scrub_pii(v)


class ListingTraffic(BaseModel):
    """How this one listing has done, lifetime. Distinct from the category
    series: the series is the market, this is the listing's share of it."""

    model_config = ConfigDict(extra="forbid")

    impressions: int = Field(ge=0)          # times it was shown in results
    entered_candidates: int = Field(ge=0)   # times it was recalled at all
    clicks: int = Field(ge=0)               # times someone opened it
    click_through: str                      # rendered, e.g. "4.1%" or "n/a"


class AnalyzeDemandInput(ItemScopedInput):
    """Which listing, and over what window. `bucket` is deliberately not a
    parameter — it is derived from the window, so the model cannot ask for
    daily buckets over 90 days and get a 90-point series it then has to
    summarize back down."""

    window: DemandWindow = "last_30d"


class AnalyzeDemandOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int
    item_name: str
    category: str | None = None
    window: DemandWindow
    window_range: str                       # "2026-08-15 ~ 2026-09-14"
    bucket: Literal["day", "week"]
    series: list[DemandBucket] = Field(default_factory=list)

    total_people: int = 0
    total_events: int = 0
    total_views: int = 0
    #: Wants still open that this listing could satisfy — the only
    #: forward-looking number here; everything else already happened.
    active_wants: int = 0

    #: "rising" / "falling" / "flat" / "insufficient_data". Computed from the
    #: series server-side, not asked of the model: a trend the model eyeballs
    #: off a list of numbers is a guess presented in the same voice as a fact.
    trend: str = "insufficient_data"
    trend_detail: str = ""

    #: Of `total_people`, how many used a phrasing that overlaps this listing.
    #: The honest headline number for "people looking for *this*", as opposed to
    #: people active anywhere in its category.
    people_matching_item: int = 0

    listing_traffic: ListingTraffic
    top_phrases: list[DemandPhrase] = Field(default_factory=list)
    #: Set when the window held no searches at all, or when the listing has no
    #: category to compare against. `no_demand_in_window` is a fact about the
    #: window and says nothing about whether the thing is wanted.
    empty: EmptyResult | None = None
    caveats: list[str] = Field(default_factory=list)


class PriceSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The label is shown to the seller verbatim, so it is Chinese like the rest
    #: of the product — and the prompt hands back exactly these three strings.
    #: Changing one here without changing prompts/seller_advisor.md silently
    #: drops every tier the model produces.
    label: Literal["快速出手", "均衡", "高价试探"]
    price: float = Field(gt=0)
    rationale: str = Field(max_length=200)


class ItemPriceSuggestions(BaseModel):
    """Three tiers, bound to the listing they were computed for.

    One turn can now analyze several listings, so an unkeyed list of tiers
    would be unattributable — and pinning the wrong price to the wrong item is
    worse than showing none.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: int = Field(ge=1)
    suggestions: list[PriceSuggestion] = Field(default_factory=list)


class OrchestratorFinal(BaseModel):
    """The JSON the model must output on its final turn. Buyer cards and sold-price
    stats are **not** here — they come straight from tool outputs, so the model
    has no channel to fabricate them."""

    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=2000)
    pricing: list[ItemPriceSuggestions] | None = None


# ============================================================
# API contract
# ============================================================
class AdvisorChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2000)


class AdvisorChatIn(BaseModel):
    """No item_id: the seller no longer picks a listing in the UI. No history
    either — the server owns the conversation and the client only carries the
    thread id back, so what the model reasons over is no longer client input."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=500)
    thread_id: str | None = Field(default=None, max_length=80)
    #: One uploaded photo, as returned by POST /api/items/photo. Only the
    #: publishing flow reads it; anything not shaped like an upload is
    #: rejected here, before it can be joined onto DATA_DIR and read.
    photo_path: str | None = Field(default=None, max_length=200)

    @field_validator("photo_path")
    @classmethod
    def _uploaded_photo(cls, v: str | None) -> str | None:
        return check_upload_path(v)


class AdvisorThreadRow(BaseModel):
    """One conversation in the seller's list."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    title: str
    updated_at: str


class ThreadRenameIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Empty means "go back to an automatic title", not "make it blank"
    title: str = Field(default="", max_length=80)


class ThreadTranscript(BaseModel):
    """A reopened conversation.

    Text only, by design: buyer cards, price stats and listing drafts are
    rendered from live tool results, and replaying a stored copy would show
    yesterday's buyers as though they were current.
    """

    model_config = ConfigDict(extra="forbid")

    thread_id: str
    title: str
    turns: list[AdvisorChatTurn] = Field(default_factory=list)


class PricingAnalysisView(BaseModel):
    """Pricing card: stats come from the tool (verifiable), the three tiers come
    from the model (explicitly labeled as suggestions)."""

    model_config = ConfigDict(extra="forbid")

    item_id: int
    item_name: str
    stats: SoldPriceStats
    #: Straight from the tool, same as `demand_analysis` — the frontend draws
    #: the histogram from this, so the model never has to describe a picture.
    distribution: PriceDistribution | None = None
    samples: list[SoldSample] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low", "none"]
    empty: EmptyResult | None = None
    suggestions: list[PriceSuggestion] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


#: The literal cell written where a listing has no value for a column. Written
#: rather than left blank on purpose: a gap in a comparison table reads as
#: "zero" or as "I forgot", and both readings are wrong in a way that changes
#: what the seller does next.
NO_DATA_CELL = "无数据"


class ComparisonRow(BaseModel):
    """One listing's row. `cells` is positional against `ComparisonTable.columns`
    and is validated to the same length, so a row can never silently shift."""

    model_config = ConfigDict(extra="forbid")

    item_id: int
    item_name: str
    cells: list[str] = Field(default_factory=list)


class ComparisonTable(BaseModel):
    """Several listings compared on one fixed set of metrics, built server-side.

    Asked to compare two listings the model returned prose plus a bullet list,
    and described the two at different grains — matching buyers and want count
    for one, a bare "matched 0" plus an explanation for the other. Neither half
    was false; they just could not be read against each other, which is the
    only thing a comparison is for.

    So the table is assembled here from the tool results rather than requested
    of the model: the column set and its order are the same for every row by
    construction, and a listing with nothing to report gets `NO_DATA_CELL` in
    that column instead of dropping out of it.
    """

    model_config = ConfigDict(extra="forbid")

    #: "需求对比" / "定价对比" / "需求与定价对比"
    title: str
    kind: Literal["demand", "price", "both"]
    columns: list[str] = Field(default_factory=list)
    rows: list[ComparisonRow] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)

    @field_validator("rows")
    @classmethod
    def _rectangular(cls, rows: list[ComparisonRow], info) -> list[ComparisonRow]:
        width = len(info.data.get("columns") or [])
        for r in rows:
            if len(r.cells) != width:
                raise ValueError(
                    f"row {r.item_id} has {len(r.cells)} cells, expected {width}"
                )
        return rows


class AdvisorChatOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    reply: str = ""
    #: Echoed back so the next turn continues this conversation; a new one is
    #: minted whenever the client sends nothing usable.
    thread_id: str = ""
    #: Which specialist handled the turn ("sales_agent" / "publishing_agent" /
    #: "clarify"). Surfaced so a surprising answer can be explained by where it
    #: was routed, without re-running anything.
    route: str = ""
    used_tools: list[str] = Field(default_factory=list)
    #: True when the seller has no active listings at all — the frontend shows a
    #: "list something first" hint instead of an empty chat.
    no_active_items: bool = False
    potential_buyers: list[FindPotentialBuyersOutput] = Field(default_factory=list)
    pricing_analysis: list[PricingAnalysisView] = Field(default_factory=list)
    #: Demand-over-time results, one per listing the turn analyzed. Sent as the
    #: tool returned them — the frontend draws the chart from `series`.
    demand_analysis: list[AnalyzeDemandOutput] = Field(default_factory=list)
    #: Set only when the turn analyzed more than one listing the same way.
    #: Built from the tool results, never from the model's text.
    comparison: ComparisonTable | None = None
    #: Publishing flow output. The draft is display-only until the seller
    #: approves it through its own endpoint.
    publish_draft: PublishDraftView | None = None
    publish_findings: list[PublishFinding] = Field(default_factory=list)
    #: Set when a hard rule stopped the submission
    publish_blocked: bool = False
    #: The advisor is waiting on a yes/no before doing something. While this is
    #: set the graph is genuinely suspended — the next message is read as the
    #: answer, not as a new question.
    pending_action: dict | None = None
    #: What a confirmed action actually did, including who it could not reach.
    action_result: dict | None = None
    #: What this one question cost, metered across every model call the turn
    #: made — routing, the tool loop, the proposal check, publishing. Shown to
    #: the seller per message, so it is the turn's bill and not the thread's.
    input_tokens: int = 0
    output_tokens: int = 0
    #: How many model calls went into the turn. Sent alongside the totals
    #: because "0 tokens" means something very different with 0 calls (nothing
    #: ran — degraded, or no listings) than with 3 (usage went unreported).
    llm_calls: int = 0
    #: Per-call-site split, keyed by the same call-site ids token_usage_log
    #: uses. Powers the breakdown behind the per-message badge.
    token_detail: dict[str, dict[str, int]] = Field(default_factory=dict)
    degraded: bool = False
    #: A deferred-sale plan this turn wrote down — what it is watching, from
    #: when, and the date it was translated into. Present so the frontend can
    #: show the plan rather than making the seller trust a sentence, and so an
    #: inferred date is visible in one place instead of buried in prose.
    #:
    #: 保留给确定性兜底那条路（没有 API key 时）。出售计划 agent 走的是下面的
    #: `sale_plans`——一轮可能动好几条。
    sale_plan: dict | None = None
    #: 这一轮动过或查过的出售计划，直接来自工具输出。和买家卡片同一条规矩：
    #: 结构化结果不经过模型，所以模型没有渠道编造一条不存在的计划，也没有渠道
    #: 把承诺写成系统做不到的样子。
    sale_plans: list[dict] = Field(default_factory=list)
    #: `peek_plan_demand` 的输出：每周读数 + 这一周的实时读数 + 两条判据离触发
    #: 有多远。
    plan_demand: list[dict] = Field(default_factory=list)
    #: 出售计划监控总开关的当前状态。每一轮都回带，因为界面上那个按钮的亮灭必须
    #: 跟着对话走——卖家用嘴关掉之后按钮还亮着，那个按钮就在说假话。
    #: None 表示这一轮和计划无关，前端不要动按钮。
    watch_enabled: bool | None = None
    message: str = ""
