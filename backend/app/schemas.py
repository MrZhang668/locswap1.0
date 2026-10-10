"""Pydantic schemas for the public API.

§7 privacy hard constraint: `ItemCard` is the **fixed whitelist** for buyer-side
cards; seller contact info (email etc.) only ever appears in `CartItem`
(the cart endpoint, i.e. after a successful reservation).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .services.images import check_upload_path

_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")

#: No email-validator dependency — demo-grade validation is enough
Email = Annotated[str, Field(min_length=5, max_length=255)]


def _validate_email(v: str) -> str:
    v = v.strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError("邮箱格式不正确")
    return v


# ---------------- Common ----------------
class LocationOut(BaseModel):
    name: str
    lat: float
    lng: float


class MetaConfigOut(BaseModel):
    confirm_window_hours: int
    default_pickup_range_km: float
    locations: list[LocationOut]
    categories: list[str]
    llm_available: bool
    llm_unavailable_reason: str = ""
    index_stats: dict[str, Any] = Field(default_factory=dict)


# ---------------- Auth / users ----------------
class RegisterIn(BaseModel):
    email: Email
    password: str = Field(min_length=6, max_length=128)

    @field_validator("email")
    @classmethod
    def _norm_email(cls, v: str) -> str:
        return _validate_email(v)


class LoginIn(BaseModel):
    email: Email
    password: str

    @field_validator("email")
    @classmethod
    def _norm_email(cls, v: str) -> str:
        return _validate_email(v)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    avatar: str
    location_name: str | None
    lat: float | None
    lng: float | None
    pickup_range_enabled: bool
    #: Opt-in to being told when something matching a want the buyer wrote is
    #: listed. Account-level, off by default.
    auto_notify: bool = False
    pickup_range_km: float | None
    onboarded: bool = False


class AuthOut(BaseModel):
    token: str
    user: UserOut


class UserUpdateIn(BaseModel):
    location_name: str | None = None
    pickup_range_enabled: bool | None = None
    auto_notify: bool | None = None
    pickup_range_km: float | None = None


# ---------------- Item card (buyer-side fixed whitelist) ----------------
class ItemCard(BaseModel):
    """Every item field a buyer may see. Before adding a field, make sure it
    contains no personal information."""

    item_id: int
    name: str
    summary: str
    price: float
    photo_url: str | None
    distance_km: float | None
    #: What `distance_km` is measured from: "you" when signed in, "campus" for a
    #: signed-out visitor. Without it the frontend cannot tell the two apart and
    #: would label a distance from the campus centre as the visitor's own.
    distance_from: str = "you"
    category: str | None
    #: Seller-declared wear level (one of CONDITIONS). Comes out of attributes,
    #: so older listings that predate the required-field rule may have none.
    condition: str | None = None
    #: Preset campus pickup spot. Public by construction — the map view already
    #: groups cards by it — so it stays inside the whitelist.
    pickup_location_name: str | None = None
    status: str
    reserved_count: int
    my_rank: int | None = None       # my place in the reservation queue ("you're #N in line")
    reserved_by_me: bool = False


# ---------------- Browse board (home page) ----------------
class BrowseOut(BaseModel):
    """One page of the filtered board.

    `total` counts everything that matched, not just this page, so the board can
    say "37 items" while showing the first 24. `distance_available` is false when
    the viewer has no location on file — the board disables the distance filter
    instead of silently returning nothing.
    """

    items: list[ItemCard]
    total: int
    limit: int
    offset: int
    distance_available: bool


# ---------------- Map workbench ----------------
class MapLocationOut(BaseModel):
    """A map pin: one preset location + the currently reservable item cards
    there (still going through the ItemCard whitelist)."""

    name: str
    lat: float
    lng: float
    items: list[ItemCard]


class MapOut(BaseModel):
    locations: list[MapLocationOut]


# ---------------- Item-finder chat ----------------
class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    history: list[ChatTurn] = Field(default_factory=list)
    clarify_round: int = 0


class ChatOut(BaseModel):
    reply: str
    cards: list[ItemCard]
    decision: str
    confidence: float
    clarify_round: int
    debug: dict[str, Any] = Field(default_factory=dict)
    #: The standing want captured this turn (E2). When set, the frontend can
    #: show "notify me when it's in" — one tap upgrades it to an explicit
    #: subscription with no re-parsing, so the incremental cost is zero.
    want_id: int | None = None


# ---------------- Listing ----------------
class PhotoOut(BaseModel):
    photo_path: str
    photo_url: str


class ItemCreateIn(BaseModel):
    name: str | None = None
    description: str = ""
    summary: str | None = None
    attributes: dict[str, Any] | None = None
    price: float = Field(gt=0)
    photo_path: str | None = None
    pickup_location_name: str
    pickup_time_start: str = ""
    pickup_time_end: str = ""

    @field_validator("photo_path")
    @classmethod
    def _uploaded_photo(cls, v: str | None) -> str | None:
        return check_upload_path(v)


class ItemCreateOut(BaseModel):
    item: ItemCard


# ---------------- Cart ----------------
class CartItem(BaseModel):
    """The post-reservation view — the only place that ever returns seller
    contact info (§7)."""

    item_id: int
    name: str
    summary: str
    category: str | None
    price: float
    photo_url: str | None
    distance_km: float | None
    pickup_location_name: str
    pickup_time_range: str
    seller_contact: str            # in the demo this is the seller's email
    status: str
    confirm_deadline: datetime | None
    seconds_left: int | None
    my_rank: int
    reserved_count: int
    deal_confirmed: bool


# ---------------- Seller side ----------------
class BuyerRow(BaseModel):
    buyer_id: int
    display_name: str              # masked email, only so the seller can tell buyers apart
    contact: str | None = None     # filled in once the buyer has an active reservation (§7)
    rank: int
    reserved_at: datetime


class PushToBuyerIn(BaseModel):
    """Push this listing to one anonymised candidate card.

    Only the ordinal the seller can see is accepted. The backend resolves it to
    a want through its own short-lived map, so no buyer identifier ever travels
    over the wire in either direction.
    """

    card_id: str = Field(min_length=1, max_length=40)


class PushToBuyerOut(BaseModel):
    ok: bool
    message: str = ""


class SellerItem(BaseModel):
    item_id: int
    name: str
    price: float
    photo_url: str | None
    status: str
    reserved_count: int
    confirm_deadline: datetime | None
    seconds_left: int | None
    created_at: datetime
    #: 恢复上架时顺带作废了几条「收起来等需求」的出售计划。只有那一次调用会带值，
    #: 列表接口恒为 0。存在的理由：一次静默的撤销和一次静默的隐身是同一类问题——
    #: 卖家点了「恢复上架」，他排的提醒就没了，而他不知道。
    cancelled_sale_plans: int = 0


class ConfirmIn(BaseModel):
    buyer_id: int


class ItemStatusIn(BaseModel):
    """Pause or resume one of your own listings.

    A closed set of two verbs rather than a free `status` field: the only
    transitions a seller drives directly are these two, and accepting a status
    name would invite "sold_out" or "expired" to be set from the outside,
    where the lifecycle rules that own those transitions do not run.
    """

    target: Literal["pause", "resume"]


# ---------------- Standing wants (buyer-side "things I'm waiting for") ----------------
class WantCreateIn(BaseModel):
    text: str = Field(min_length=1, max_length=300)


class WantOut(BaseModel):
    """A buyer viewing their own wants.

    This is the **buyer's own** data outlet, so it may include raw_query;
    C8 constrains seller-side outlets — the two must not share this schema.
    """

    want_id: int
    text: str                       # semantic_text, for display
    raw_query: str
    filters: dict[str, Any]
    status: str
    source: str
    notify_count: int
    created_at: datetime
    expires_at: datetime
    days_left: int


# ---------------- Notifications ----------------
class NotificationOut(BaseModel):
    id: int
    type: str
    title: str
    body: str
    payload: dict[str, Any]
    read: bool
    created_at: datetime
