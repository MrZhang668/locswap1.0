"""ItemIndexService — the single gateway to the business DB / vector store / BM25.

Spec §3 "index consistency": every create, update, and delete of an item
anywhere in the project must go through this service, so we never end up with
"in the DB but not in the index" or the reverse.

Hard privacy constraint (§7): document fields entering the index go through a
whitelist, so seller emails, user_id and the like can never reach the index by
construction — and therefore can never appear in any LLM context either.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Item, ItemStatus
from .bm25_index import BM25Index
from .embedding import EmbeddingProvider, get_embedding_provider
from .text import MODIFIER_PENALTY, SUBSTRING_PENALTY, match_position, normalize, raw_tokens
from .vector_store import VectorStore, build_vector_store

logger = logging.getLogger(__name__)

#: Whitelist of fields allowed into the retrieval index — think about the
#: privacy impact before changing this.
INDEX_TEXT_FIELDS = (
    "name",
    "summary",
    "description",
    "category",
    "brand",
    "color",
    "condition",
    "pickup_location_name",
)
#: Fields that must never enter the index
FORBIDDEN_INDEX_FIELDS = ("seller_id", "seller", "email", "password_hash", "user_id", "phone", "contact")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


@dataclass
class IndexedItem:
    """In-memory mirror used by the retrieval pipeline (contains no personal data)."""

    item_id: int
    name: str
    summary: str
    description: str
    price: float
    status: str
    lat: float
    lng: float
    pickup_location_name: str
    category: str | None = None
    brand: str | None = None
    color: str | None = None
    condition: str | None = None
    extra_attributes: dict[str, Any] = field(default_factory=dict)
    haystack: str = ""  # normalized text used by structured matching
    name_tokens: list[str] = field(default_factory=list)  # word tokens of the name, for the head-word test
    name_norm: str = ""  # normalized item name, for the substring fallback


def _attr(item: Item, key: str) -> str | None:
    value = (item.attributes or {}).get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def build_index_document(item: Item) -> str:
    """Build the document text that goes into the index — whitelist fields only."""
    parts: list[str] = []
    for f in INDEX_TEXT_FIELDS:
        value = getattr(item, f, None)
        if value is None:
            value = _attr(item, f)
        if value:
            parts.append(str(value).strip())
    for key, value in (item.attributes or {}).items():
        if key in FORBIDDEN_INDEX_FIELDS or key in INDEX_TEXT_FIELDS:
            continue
        if value is None or value == "":
            continue
        parts.append(f"{key}: {value}")
    parts.append(f"price ${int(item.price)}")

    doc = " | ".join(parts)
    if _EMAIL_RE.search(doc):  # safety assertion: an email must never appear in the index
        logger.error("[index] index document for item %s matched the email pattern; stripped", item.id)
        doc = _EMAIL_RE.sub("", doc)
    return doc


def build_index_metadata(item: Item) -> dict[str, Any]:
    """Vector-store metadata — used only for hard filtering; also free of personal data."""
    return {
        "item_id": int(item.id),
        "status": item.status,
        "price": float(item.price),
        "lat": float(item.lat),
        "lng": float(item.lng),
        "category": _attr(item, "category") or "",
    }


def build_indexed_item(item: Item) -> IndexedItem:
    rec = IndexedItem(
        item_id=int(item.id),
        name=item.name or "",
        summary=item.summary or "",
        description=item.description or "",
        price=float(item.price),
        status=item.status,
        lat=float(item.lat),
        lng=float(item.lng),
        pickup_location_name=item.pickup_location_name or "",
        category=_attr(item, "category"),
        brand=_attr(item, "brand"),
        color=_attr(item, "color"),
        condition=_attr(item, "condition"),
        extra_attributes={
            k: v
            for k, v in (item.attributes or {}).items()
            if k not in ("category", "brand", "color", "condition") and k not in FORBIDDEN_INDEX_FIELDS
        },
    )
    rec.name_tokens = raw_tokens(rec.name)
    rec.name_norm = normalize(rec.name)
    rec.haystack = normalize(
        " ".join(
            filter(
                None,
                [
                    rec.name,
                    rec.summary,
                    rec.category,
                    rec.brand,
                    rec.color,
                    rec.condition,
                    rec.pickup_location_name,
                    " ".join(f"{k} {v}" for k, v in rec.extra_attributes.items() if v),
                ],
            )
        )
    )
    return rec


class ItemIndexService:
    def __init__(
        self,
        embedder: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
        bm25: BM25Index | None = None,
    ):
        self.embedder = embedder or get_embedding_provider()
        self.vector = vector_store or build_vector_store()
        self.bm25 = bm25 or BM25Index()
        self._records: dict[int, IndexedItem] = {}
        self._lock = threading.Lock()

    # ---------------- Reads ----------------
    @property
    def records(self) -> dict[int, IndexedItem]:
        return self._records

    def get_record(self, item_id: int) -> IndexedItem | None:
        return self._records.get(int(item_id))

    def stats(self) -> dict[str, Any]:
        return {
            "records": len(self._records),
            "vector_count": self.vector.count(),
            "bm25_count": self.bm25.count(),
            "embedder": self.embedder.name,
            "embedding_dim": self.embedder.dim,
            "vector_backend": self.vector.name,
        }

    # ---------------- Writes (single entry point) ----------------
    def index_item(self, item: Item) -> None:
        """Create or update the index for one item. Only searchable statuses are
        indexed; any other status is removed immediately."""
        if item.status not in ItemStatus.SEARCHABLE:
            self.remove_item(item.id)
            return
        doc = build_index_document(item)
        embedding = self.embedder.embed_documents([doc])[0]
        with self._lock:
            self.vector.upsert([str(item.id)], [embedding], [build_index_metadata(item)])
            self.bm25.upsert(item.id, doc)
            self._records[int(item.id)] = build_indexed_item(item)

    def update_status(self, item: Item) -> None:
        """Sync the index after a status change (reserved stays searchable,
        sold_out/expired leave the index immediately)."""
        self.index_item(item)

    def remove_item(self, item_id: int) -> None:
        item_id = int(item_id)
        with self._lock:
            self.vector.delete([str(item_id)])
            self.bm25.delete(item_id)
            self._records.pop(item_id, None)

    def create_item(self, db: Session, **fields: Any) -> Item:
        """Atomic entry point: create the DB row + build the index."""
        item = Item(**fields)
        db.add(item)
        db.flush()
        db.commit()
        db.refresh(item)
        self.index_item(item)
        return item

    def delete_item(self, db: Session, item: Item) -> None:
        """Permanently delete an item (delisted / expired): DB first, then the index.

        Notifications pending in the same transaction are committed together —
        they must take effect atomically with the deletion.
        """
        item_id = int(item.id)
        db.delete(item)
        db.flush()
        db.commit()
        self.remove_item(item_id)

    def rebuild_from_db(self, db: Session) -> dict[str, Any]:
        """Full rebuild on service startup / after seeding.

        Refuses one specific case: an empty database about to reset a
        populated index. That combination almost never means "we deleted
        everything" — it means this process is looking at the wrong database.
        A volume that failed to mount, a DB_URL pointing at a path that does not
        exist yet, a fresh container aimed at a shared cluster. The index is the
        expensive artefact and the database is the cheap one to check, so the
        rebuild stops and says so rather than completing successfully and
        leaving nothing behind.
        """
        items = list(db.query(Item).filter(Item.status.in_(ItemStatus.SEARCHABLE)).all())
        if not items:
            existing = self.vector.count()
            if existing:
                raise RuntimeError(
                    f"Refusing to rebuild: the database has no searchable items but the "
                    f"vector index holds {existing}. Rebuilding would delete them. This "
                    f"usually means the data volume is not mounted or DB_URL points "
                    f"somewhere unexpected — check those before clearing the index by hand."
                )
        docs = {i.id: build_index_document(i) for i in items}
        embeddings = self.embedder.embed_documents([docs[i.id] for i in items]) if items else []
        with self._lock:
            self.vector.reset()
            if items:
                self.vector.upsert(
                    [str(i.id) for i in items], embeddings, [build_index_metadata(i) for i in items]
                )
            self.bm25.reset(docs)
            self._records = {int(i.id): build_indexed_item(i) for i in items}
        stats = self.stats()
        logger.info("[index] full rebuild done: %s", stats)
        return stats

    # ---------------- Retrieval (foundation of the 3-way recall) ----------------
    def candidate_ids(
        self,
        *,
        statuses: Iterable[str] = ItemStatus.SEARCHABLE,
        max_price: float | None = None,
        min_price: float | None = None,
        category: str | None = None,
        origin: tuple[float, float] | None = None,
        max_distance_km: float | None = None,
        exclude_seller_item_ids: set[int] | None = None,
    ) -> set[int]:
        """Hard filtering: produce the candidate set shared by the vector/BM25/structured paths."""
        from .geo import haversine_km

        statuses = set(statuses)
        out: set[int] = set()
        for rec in self._records.values():
            if rec.status not in statuses:
                continue
            if max_price is not None and rec.price > max_price + 1e-9:
                continue
            if min_price is not None and rec.price < min_price - 1e-9:
                continue
            if category and (rec.category or "") != category:
                continue
            if exclude_seller_item_ids and rec.item_id in exclude_seller_item_ids:
                continue
            if origin and max_distance_km is not None:
                if haversine_km(origin[0], origin[1], rec.lat, rec.lng) > max_distance_km:
                    continue
            out.add(rec.item_id)
        return out

    def vector_search(
        self, query_text: str, top_k: int, allowed_ids: set[int]
    ) -> list[tuple[int, float]]:
        if not allowed_ids:
            return []
        embedding = self.embedder.embed_query(query_text)
        raw = self.vector.query(embedding, top_k, allowed_ids={str(i) for i in allowed_ids})
        return [(int(i), float(s)) for i, s in raw if int(i) in allowed_ids]

    def bm25_search(self, query_text: str, top_k: int, allowed_ids: set[int]) -> list[tuple[int, float]]:
        if not allowed_ids:
            return []
        return self.bm25.search(query_text, top_k, allowed_ids=allowed_ids)

    def structured_search(
        self, filters: dict[str, Any], keywords: list[str], top_k: int, allowed_ids: set[int]
    ) -> list[tuple[int, float]]:
        """Structured recall: field-level matching on name / attributes, the
        fallback behind the semantic + keyword paths."""
        if not allowed_ids:
            return []
        wanted = {
            "category": filters.get("category"),
            "brand": filters.get("brand"),
            "color": filters.get("color"),
            "condition": filters.get("condition"),
        }
        wanted = {k: v for k, v in wanted.items() if v}
        norm_keywords = [normalize(k) for k in keywords if k and len(normalize(k)) >= 2]

        # Category hits weigh less than brand/color/condition: a category is so
        # broad that on its own it would drag in the whole aisle.
        field_weights = {"category": 1.0, "brand": 2.0, "color": 2.0, "condition": 2.0}

        scored: list[tuple[int, float]] = []
        for item_id in allowed_ids:
            rec = self._records.get(item_id)
            if not rec:
                continue
            score = 0.0
            for field_name, value in wanted.items():
                actual = getattr(rec, field_name, None)
                if not actual:
                    continue
                weight = field_weights.get(field_name, 1.0)
                if normalize(value) == normalize(actual):
                    score += weight
                elif normalize(value) in normalize(actual):
                    score += weight * 0.5
            keyword_score = 0.0
            matched_keywords = 0
            for kw in norm_keywords:
                # Three tiers — head / modifier / literal substring: searching
                # 自行车, a 自行车锁 only earns the modifier score, and a keyword
                # that is merely a literal substring scores lower still.
                position = match_position(rec.name_tokens, rec.name_norm, kw)
                if position == "head":
                    keyword_score += 1.5
                    matched_keywords += 1
                elif position == "modifier":
                    keyword_score += 1.5 * MODIFIER_PENALTY
                    matched_keywords += 1
                elif position == "substring":
                    keyword_score += 1.5 * SUBSTRING_PENALTY
                    matched_keywords += 1
                elif kw in rec.haystack:
                    keyword_score += 0.6
                    matched_keywords += 1

            # A multi-word query that matches on one word alone is usually a
            # fragment hit, not a match. Chinese makes this sharp: jieba cuts
            # 行车记录仪 into 行车 + 记录仪, and 行车 is a suffix of 自行车 — which
            # the head-final rule then scores as a *head* hit, so every bicycle
            # in the corpus came back for a dashcam. Halving a lone hit drops it
            # under the 1.5 floor while leaving genuine multi-word matches
            # (无线 + 耳机 on the same listing) untouched.
            if len(norm_keywords) >= 2 and matched_keywords == 1:
                keyword_score *= 0.5
            score += keyword_score

            if score > 0:
                scored.append((item_id, score))

        if not scored:
            return []
        scored.sort(key=lambda x: (-x[1], x[0]))
        # Relative cutoff: items that sneak in on "same category" alone don't
        # count as valid hits.
        best = scored[0][1]
        floor = max(1.5, best * settings.structured_relative_cutoff)
        return [pair for pair in scored if pair[1] >= floor][:top_k]


# ---------------- Process-level singleton ----------------
_service: ItemIndexService | None = None


def get_item_index() -> ItemIndexService:
    global _service
    if _service is None:
        _service = ItemIndexService()
    return _service


def reset_item_index() -> None:
    global _service
    _service = None
