"""SQLAlchemy data models — maps one-to-one to spec §2."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------- Status constants ----------
class ItemStatus:
    SELLING = "selling"
    RESERVED = "reserved"
    #: Taken off sale by the seller, on purpose and reversibly. Distinct from
    #: delisting, which deletes the row: "先不卖了" is a pause in almost every
    #: case a seller means it, and the only irreversible reading of it was the
    #: one the system used to have.
    PAUSED = "paused"
    SOLD_OUT = "sold_out"
    EXPIRED = "expired"

    #: statuses visible to search. `paused` is deliberately absent — that is
    #: what pausing is — which also removes it from the index, from want
    #: matching and from every advisor tool without any of them changing.
    SEARCHABLE = (SELLING, RESERVED)
    #: terminal statuses
    TERMINAL = (SOLD_OUT, EXPIRED)
    #: statuses a seller can pause from, and the one they can resume from.
    #: Reserved is excluded: someone is holding it and a countdown is running.
    PAUSABLE = (SELLING,)
    RESUMABLE = (PAUSED,)

    #: 出售计划能绑上的状态：在售的，**以及被收起来的**。
    #:
    #: 这一条是复用 `PAUSED` 当「隐身」的代价。造一个新状态要审所有枚举状态的
    #: 地方；复用 PAUSED 则要审所有**按 SEARCHABLE 过滤**的地方——因为一件为计划
    #: 收起来的商品恰恰不在 SEARCHABLE 里，而它仍然是计划的对象。
    #: 漏一处的后果是：上传后隐身的东西，计划绑不上它，于是既不走 item 级需求
    #: 分析，也不知道自己收起过谁。
    PLANNABLE = (SELLING, RESERVED, PAUSED)


class ReservationStatus:
    ACTIVE = "active"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class NotificationType:
    DEAL_CONFIRMED = "deal_confirmed"      # your reservation was confirmed by the seller
    DEAL_LOST = "deal_lost"                # the item was sold to someone else (no deal)
    ITEM_EXPIRED = "item_expired"          # seller missed the confirmation window; item expired
    ITEM_DELISTED = "item_delisted"        # delisted by the seller
    WANT_MATCHED = "want_matched"          # something you were looking for is back in stock (E1)
    #: Seller-side, from the deferred-sale watcher. Two types rather than one
    #: because they say different things and a seller should be able to tell
    #: them apart at a glance: a hot week is "people are buying this right
    #: now", a surge is "this is moving faster than last week".
    SALE_PLAN_HOT = "sale_plan_hot"        # your category had a busy week
    SALE_PLAN_SURGE = "sale_plan_surge"    # demand jumped week on week — worth listing early


class SalePlanStatus:
    ACTIVE = "active"        # being watched (or waiting for the watch window)
    DONE = "done"            # the target date passed, or the item got listed
    CANCELLED = "cancelled"  # the seller called it off


class EmailStatus:
    PENDING = "pending"      # written, not yet handed to a mail server
    SENT = "sent"
    FAILED = "failed"        # a send was attempted and refused


class WantStatus:
    ACTIVE = "active"          # currently in effect
    FULFILLED = "fulfilled"    # buyer reserved a matching item
    EXPIRED = "expired"        # no match within WANT_TTL_DAYS
    DISMISSED = "dismissed"    # closed manually by the buyer


class WantSource:
    IMPLICIT = "implicit"      # auto-captured when a search went unfulfilled
    EXPLICIT = "explicit"      # buyer explicitly tapped "notify me when it's in"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar: Mapped[str] = mapped_column(String(255), default="")  # placeholder avatar id
    location_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: 出售计划监控的**总开关**。默认开着，这和 `auto_notify` 默认关着不矛盾：
    #: `auto_notify` 是「有人替你决定要不要打扰你」，而一条出售计划是卖家**自己
    #: 排进来的**——他明说了「12 月再卖，需求起来告诉我」，默认不看反而是失信。
    #: 关掉之后周扫描直接跳过这位卖家，一条计划都不看。
    sale_watch_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Opt-in to being told when something matching a want they wrote shows up.
    #: Account-level and off by default: the notification arrives unasked, so
    #: the default has to be silence. Applies only to wants the buyer wrote on
    #: purpose (source="explicit") — see notify_optin_buyers.
    auto_notify: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pickup_range_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pickup_range_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    items: Mapped[list["Item"]] = relationship(back_populates="seller")


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(String(400), default="")  # ≤2 sentences
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    photo_path: Mapped[str | None] = mapped_column(String(300), nullable=True)  # single photo only
    pickup_location_name: Mapped[str] = mapped_column(String(120), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lng: Mapped[float] = mapped_column(Float, nullable=False)
    pickup_time_start: Mapped[str] = mapped_column(String(60), default="")
    pickup_time_end: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(20), default=ItemStatus.SELLING, index=True)
    confirm_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    # ---- Deal snapshot (E5): sold_out items stay in the DB; these two columns form
    # the per-category "sold-price evidence pool".
    # Why not derive from price + status_log: price can still be changed later by
    # propose_price, while the evidence pool needs "the price at the moment of sale";
    # time-to-sell also needs a home that doesn't depend on status_log surviving.
    sold_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    sold_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    seller: Mapped[User] = relationship(back_populates="items")
    reservations: Mapped[list["Reservation"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )

    @property
    def pickup_time_range(self) -> str:
        if self.pickup_time_start and self.pickup_time_end:
            return f"{self.pickup_time_start} ~ {self.pickup_time_end}"
        return self.pickup_time_start or self.pickup_time_end or "TBD"


class Reservation(Base):
    __tablename__ = "reservations"
    # "The same buyer cannot reserve the same item twice" is enforced by the
    # lifecycle service: a buyer may re-reserve after cancelling, so a
    # (item_id, buyer_id) unique constraint would not work.

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True, nullable=False)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=ReservationStatus.ACTIVE, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    item: Mapped[Item] = relationship(back_populates="reservations")
    buyer: Mapped[User] = relationship()


class StatusLog(Base):
    __tablename__ = "status_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)  # no FK: the item may already be deleted
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(200), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class TokenUsageLog(Base):
    __tablename__ = "token_usage_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    call_site: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    #: The two prompt-caching counters (§7.4). Without them there is no way to
    #: verify whether the cache is actually being hit — cache_read stuck at 0
    #: means something is polluting the prefix, and input_tokens alone won't show it.
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# ---------- Standing wants and traffic instrumentation (event layer E1–E6) ----------
class StandingWant(Base):
    """A buyer's standing want. Most are auto-captured when a search goes
    unfulfilled (E2); a few are created explicitly by the buyer.

    raw_query is for internal debugging only and MUST NOT appear in any
    seller-visible output (constraint C8).
    """

    __tablename__ = "standing_want"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    raw_query: Mapped[str] = mapped_column(String(300), default="")
    filters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    semantic_text: Mapped[str] = mapped_column(String(300), default="")
    category: Mapped[str | None] = mapped_column(String(60), index=True, nullable=True)
    max_price: Mapped[float | None] = mapped_column(Float, index=True, nullable=True)
    min_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    origin_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    origin_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default=WantSource.IMPLICIT)
    status: Mapped[str] = mapped_column(String(20), default=WantStatus.ACTIVE, index=True)
    #: Which item marked this want fulfilled (E4). Used to restore it precisely
    #: when the reservation is cancelled / falls through (E5) — we cannot
    #: re-run semantic matching to guess: thresholds float, and a wrong guess
    #: permanently swallows a real need.
    fulfilled_by_item_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    notify_count: Mapped[int] = mapped_column(Integer, default=0)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)


class ItemTraffic(Base):
    """Per-item traffic counters: one row per item, incremented in place.

    The four counters — impressions / entered candidates / pushed / clicks —
    feed E6 instrumentation and seller-side stats.
    Writes sit on the retrieval hot path: use bulk UPDATEs, never per-row ORM writes.
    """

    __tablename__ = "item_traffic"

    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    impressions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    entered_candidates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pushed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class DemandEventKind:
    """What a buyer did. The four are ordered by how much intent they carry."""

    SEARCH = "search"      # ran a query
    VIEW = "view"          # opened one item's detail
    WANT = "want"          # left a standing want behind
    RESERVE = "reserve"    # reserved an item


class DemandEvent(Base):
    """Append-only log of buyer-side activity — one row per action.

    Exists because `item_traffic` cannot answer a question with a time in it.
    That table holds four running counters per item, so "how many people were
    looking for bikes in week 3" is unanswerable from it: the counts have no
    dates and no actor. Demand analysis needs both — *people* (distinct buyers)
    and *times* (buckets) — which is exactly one row per event.

    `item_traffic` is not replaced. It stays the cheap read for "how is this one
    listing doing", recomputed from this log; this table is where any question
    with a date in it gets answered.

    Append-only by intent: nothing updates a row after it is written, so the
    history cannot be rewritten by a later bug.
    """

    __tablename__ = "demand_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    buyer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    #: The raw query for a search; empty for the other kinds. Never indexed into
    #: any cross-user surface — see constraint C8 (raw buyer phrasing stays put).
    query: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: Resolved category when there is one, so bucketing by category needs no join.
    category: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    #: The item a view/reserve refers to; null for search/want.
    item_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False, default=utcnow)


class RuleSeverity:
    """How a listing rule bites.

    BLOCK is a hard stop — the item cannot be listed at all. WARN is advice the
    seller can act on or ignore. Keeping them apart matters: collapsing every
    rule into "rejected" would kill listings over a wordy description, and
    collapsing the other way would let prohibited items through.
    """

    BLOCK = "block"
    WARN = "warn"

    ALL = (BLOCK, WARN)


class ListingRule(Base):
    """One marketplace rule the publishing flow checks a submission against.

    Lives in the database rather than in the prompt so the policy can change
    without a deploy, and so a verdict can cite the exact rule it hit — an
    unexplained "not allowed" is not something a seller can act on.
    """

    __tablename__ = "listing_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Stable human-readable identifier used in verdicts and tests (e.g. "BAN-WEAPONS")
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    #: The rule text handed to the model. Written as an instruction to a
    #: reviewer, not as marketing copy.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AdvisorThread(Base):
    """One named conversation with the sales agent.

    The transcript itself lives in the LangGraph checkpoint; this table is the
    part a person interacts with — which conversations exist, what they are
    called, who owns them. Splitting it that way keeps the registry queryable
    (list mine, newest first) without teaching the checkpoint about users.

    `id` is the LangGraph thread id, so the two stores share one key and there
    is no mapping table to fall out of sync.
    """

    __tablename__ = "advisor_threads"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    seller_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    #: Auto-filled from the first message, then editable. Never blank — an
    #: unnamed row in a conversation list is unusable.
    title: Mapped[str] = mapped_column(String(80), default="New chat", nullable=False)
    #: False until the seller renames it by hand; auto-titling must not
    #: overwrite a name someone chose.
    title_is_custom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True, nullable=False)


class SalePlan(Base):
    """卖家说「等学期末再挂」时记下来的一条计划。

    这张表的存在理由是一个能力空白：卖家反复表达「以后再卖」，而系统里没有任何
    地方能放这句话，于是路由层把它塞给最近的一个 agent——要么催他现在上传，要么
    给他做一份他没要的需求分析（归因二，10 条 badcase，其中 8 条就是这两种）。

    刻意存**商品名而不是 item_id**：这件东西按定义还没上架，没有 id 可存。
    `item_id` 只在卖家把计划挂到一件已在售商品上时才有值（他想先下架、到点再上）。

    `category` 是推断出来的，可以为空。空的时候周快照只做关键词口径——报一个
    偏大的类别数字，比报「没数据」更容易让人做错决定。
    """

    __tablename__ = "sale_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    #: 卖家自己的说法（「转换器」），不是规范化商品名。
    item_name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: 从商品名推断的类别；推断不出来时为空。
    category: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    #: 已在售商品上的计划才有值。
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), nullable=True)
    #: 卖家打算开卖的日期。
    target_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    #: 这个日期是怎么来的（explicit_date / explicit_month / semester / relative /
    #: fuzzy / seller_confirmed）。回复里会把依据说出来，卖家才有机会纠正。
    date_basis: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    #: 从哪一天开始每周盯着（target_date 减去观察窗口）。
    watch_from: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=SalePlanStatus.ACTIVE, index=True, nullable=False
    )
    #: 卖家原话，留作回复时复述。
    note: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    #: 这条计划有没有**把商品收起来等**。
    #:
    #: 「黑色笔记本量大了再卖」对一件**已经在卖**的东西是自相矛盾的——它现在就在卖。
    #: 所以这句话的落地是：把商品置为「隐身」（在库中、不曝光），等需求起来再放回去。
    #: 隐身用的就是现成的 `ItemStatus.PAUSED`：它已经不在 `SEARCHABLE` 里，搜索索引、
    #: 求购匹配、顾问工具三处一行不用改就会跳过它。
    #:
    #: 这个字段记的是**为什么**收起来的。卖家自己按下暂停和系统为计划收起来，物理
    #: 状态一样但含义不同：前者恢复上架是一次普通操作，后者恢复上架意味着「我现在
    #: 就要卖」，那条计划也就随之作废。没有这个标记，就分不出该不该撤计划。
    hold_item: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: 这一条盯不盯。和 `User.sale_watch_enabled` 是**两个独立的开关**，不是一个：
    #: 「别盯着转换器了」和「所有提醒都停掉」是不同的请求，合并成一个字段之后，
    #: 卖家停掉一条再想恢复时会发现整个功能都被关了。
    watch_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: 上一次周扫描跑到这条计划的时刻。用它判断这一周跑过没有——按「距上次超过
    #: 六天」而不是按日历周算，重启和补跑都不会漏掉或重复。
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class DemandSnapshot(Base):
    """一条计划某一周的需求读数。

    存下来而不是每次现算，是因为**增长率需要上一周**。「相邻两周增长超过 30%」
    这个判据，只有在上一周的数字被写下来过的前提下才存在；现算的版本会在口径、
    时区或补跑顺序变动时悄悄改变历史。

    三个数分开存，不合成一个「需求分数」——和 `analyze_demand` 同一条规矩：
    合成之后卖家就看不出该对哪一个采取行动。
    """

    __tablename__ = "demand_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("sale_plans.id"), index=True, nullable=False)
    #: 这一周的周一。
    week_start: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    #: 类别口径：这一周该类别下的预订数。
    reserve_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 类别口径：去重买家数 / 搜索事件数。两个都留着，因为「三个人搜了二十次」
    #: 不等于「二十个有意向的买家」。
    people: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    events: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 关键词口径：措辞真的和这件东西重合的人数。更小也更真的那个数。
    matching_people: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: 这一行是怎么量出来的：`analyze_demand`（计划挂在一件在售商品上，走的是
    #: 销售顾问那个工具本身）还是 `category_keyword`（东西还没上架，只能按类别
    #: 和名字措辞量）。存下来是因为两种口径的数不可直接比——换了口径的那一周，
    #: 增长率会凭空跳一下，而那不是市场变了。
    source: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    #: 后端算出来的方向（rising / falling / flat / insufficient_data）。只有走
    #: analyze_demand 那条路才有；空字符串表示这一周没有趋势可报。
    trend: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class EmailOutbox(Base):
    """要发出去的邮件，先落这张表。

    发信被拆成「写下来」和「送出去」两步，而不是直接调 SMTP。理由不是谨慎：
    一封邮件是这个系统唯一会离开机器、无法撤回的输出，而 outbox 让它在离开之前
    是一行可以被读、被断言、被删掉的数据。验收脚本因此可以检查邮件**内容**而不
    只是「有没有调用发信函数」，上线也只要填一组 SMTP 配置，代码一行不动。

    没有配置 SMTP 时，行就停在 pending —— 那不是失败，是这个环境的正常终态。
    """

    __tablename__ = "email_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    to_email: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    #: 哪条业务线发的（sale_plan_hot / sale_plan_surge / ...），便于按类退订和排查。
    kind: Mapped[str] = mapped_column(String(40), index=True, default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=EmailStatus.PENDING, index=True, nullable=False
    )
    #: 发送失败时的原因，截断保存。
    error: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
