"""Indexing and two-way matching for standing wants — the core of event layer
E1/E2, zero LLM throughout.

Mirrors the design of ItemIndexService: all writes to the business DB and the
vector store converge in this one service so the two stay consistent. This
module makes **no LLM calls** and adds no perceptible latency to the buyer's
synchronous search path (constraints C1/C2).

Two directions:
  * match_wants_for_item — new listing (E1): who is waiting for this?
  * match_items_for_want — new want (E2): is it already in stock?

Matching thresholds are always read from settings.* — never copy the numbers:
they're calibrated on the real corpus by scripts/calibrate_retrieval.py; when
the corpus changes just rerun the script, this module needs no edits.

This module uses settings.want_match_* , NOT settings.vector_min_similarity:
retrieval compares query -> item, while everything here compares item -> want.
Different text lengths, different prefix handling, different score
distributions — one shared threshold cannot serve both.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Item, ItemStatus, StandingWant, WantStatus
from .embedding import EmbeddingProvider, get_embedding_provider
from .geo import haversine_km
from .item_index import ItemIndexService, build_index_document
from .vector_store import VectorStore, build_vector_store

logger = logging.getLogger(__name__)


@dataclass
class WantMatch:
    want_id: int
    buyer_id: int
    similarity: float


@dataclass
class ItemMatch:
    item_id: int
    similarity: float
    price: float


#: Sentinels for the prefilter metadata. Chroma cannot store None, and a
#: missing key makes a numeric comparison unsatisfiable — so "no constraint"
#: has to be encoded as a value that always passes.
#: ⚠️ Adding a new prefilter field means updating build_want_metadata AND the
#: where clause in match_wants_for_item together; they are one contract.
#: Stored in the metadata but intentionally not filtered on today — see the
#: note in _prefilter before using it in a where clause.
ANY_CATEGORY = "__any__"      # the want names no category -> matches every category
NO_PRICE_CAP = 1e9            # the want names no budget cap -> any price fits
NO_PRICE_FLOOR = 0.0


def build_want_metadata(want: StandingWant) -> dict[str, Any]:
    """Vector-store metadata for one want.

    Only fields that are **immutable for the lifetime of a want** may live
    here: they are used as an ANN-stage prefilter, and stale metadata silently
    drops matches instead of raising. category / max_price / min_price /
    buyer_id qualify — `touch()` only renews status + TTL, and a want whose
    filters differ is a different row (StandingWantRepository.find_duplicate
    requires exact equality to reuse one).

    Volatile state (status, notify_count, expires_at) is deliberately NOT here
    and stays in the business DB, which _passes_hard re-checks afterwards.
    """
    return {
        # The id key is called "item_id" because ChromaVectorStore.query's
        # allowed_ids pushdown hardcodes that name; it holds want.id here.
        "item_id": int(want.id),
        "kind": "want",
        "buyer_id": int(want.buyer_id),
        "category": want.category or ANY_CATEGORY,
        "max_price": float(want.max_price) if want.max_price is not None else NO_PRICE_CAP,
        "min_price": float(want.min_price) if want.min_price is not None else NO_PRICE_FLOOR,
    }


def build_want_document(want: StandingWant) -> str:
    """The want's index document.

    Uses only semantic_text + a readable form of the structured filters;
    **raw_query never enters the index** (constraint C8: raw input may contain
    personal phrasing and must not enter any cross-user comparison surface).
    """
    parts = [want.semantic_text or ""]
    f = want.filters or {}
    for key in ("category", "brand", "color", "condition"):
        if f.get(key):
            parts.append(str(f[key]))
    return " ".join(p for p in parts if p).strip()


class WantIndexService:
    """Single gateway to the wants vector store."""

    def __init__(
        self,
        embedder: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
    ):
        self.embedder = embedder or get_embedding_provider()
        self.vector = vector_store or build_vector_store(settings.want_vector_collection)
        self._lock = threading.Lock()

    # ---------------- Writes ----------------
    def index_want(self, want: StandingWant) -> None:
        if want.status != WantStatus.ACTIVE:
            self.remove_want(want.id)
            return
        doc = build_want_document(want)
        if not doc:
            # A want with no semantic content can't take part in vector
            # matching (e.g. a pure price constraint) and isn't indexed; it
            # stays in the business DB for the seller-side price-distribution
            # stats.
            self.remove_want(want.id)
            return
        embedding = self.embedder.embed_documents([doc])[0]
        with self._lock:
            self.vector.upsert([str(want.id)], [embedding], [build_want_metadata(want)])

    def remove_want(self, want_id: int) -> None:
        with self._lock:
            self.vector.delete([str(int(want_id))])

    def rebuild_from_db(self, db: Session) -> dict[str, Any]:
        wants = list(
            db.query(StandingWant).filter(StandingWant.status == WantStatus.ACTIVE).all()
        )
        docs = [(w, build_want_document(w)) for w in wants]
        docs = [(w, d) for w, d in docs if d]
        embeddings = self.embedder.embed_documents([d for _, d in docs]) if docs else []
        with self._lock:
            self.vector.reset()
            if docs:
                self.vector.upsert(
                    [str(w.id) for w, _ in docs],
                    embeddings,
                    [build_want_metadata(w) for w, _ in docs],
                )
        stats = {"wants": len(docs), "vector_count": self.vector.count()}
        logger.info("[want_index] rebuild done: %s", stats)
        return stats

    # ---------------- Matching ----------------
    def match_wants_for_item(
        self,
        db: Session,
        item: Item,
        limit: int = 20,
        *,
        buyer_id: int | None = None,
        respect_notify_cap: bool = True,
    ) -> list[WantMatch]:
        """E1: new listing → who is waiting for this.

        Three stages, split by how volatile each condition is:

          1. prefilter, pushed down to the ANN stage (_prefilter) — only fields
             that never change over a want's lifetime: category, budget,
             buyer. Cutting these before the search is what keeps coarse recall
             from silently truncating real matches once the corpus grows.
          2. vector recall over the free-text remainder (semantic_text) — the
             part of the buyer's need that structured filters could not capture.
          3. re-check against the business DB (_passes_hard) — everything
             volatile (status, notify counters) plus distance, which no
             metadata filter can express. Vector-store metadata goes stale; the
             DB is the source of truth, so stage 1 is only ever an
             optimisation, never the source of correctness.

        The relative window is applied last, over the stage-3 survivors.

        buyer_id
            Only look at one buyer's wants. Used by E4 ("the buyer got one,
            close their similar wants") because **category-string equality
            cannot decide sameness**: rule parsing often extracts no category
            ("a used mini fridge under $50" parses to filters holding only
            max_price), so that want's category is None and would never be
            closed. Semantic matching is the right test.
        respect_notify_cap
            Closing wants should not be limited by the push cap — that's nag
            control, not a matching condition.
        """
        if item.status not in ItemStatus.SEARCHABLE:
            return []
        doc = build_index_document(item)
        embedding = self.embedder.embed_query(doc)
        # Widen coarse recall to 4x: the volatile filters still follow
        # (status / notify cap / distance).
        quota = max(limit * 4, 20)
        raw = self.vector.query(
            embedding, quota, allowed_ids=None,
            where=self._prefilter(item, buyer_id=buyer_id),
        )
        if not raw:
            return []
        if len(raw) >= quota:
            # Coarse recall came back full, so the ANN stage may have truncated
            # genuine matches ranked below the quota. Post-filtering can only
            # remove, never restore — so this is the moment the recall ceiling
            # starts to bind, and it is otherwise completely silent.
            logger.warning(
                "[want_index] coarse recall saturated (%d/%d) for item=%s — "
                "raise the quota or tighten the prefilter", len(raw), quota, item.id
            )

        sims = {int(i): float(s) for i, s in raw}
        q = db.query(StandingWant).filter(StandingWant.id.in_(list(sims)))
        if buyer_id is not None:
            q = q.filter(StandingWant.buyer_id == buyer_id)
        wants = list(q.all())

        # Hard conditions first, *then* take the best score — over the
        # survivors only. Taking it over the raw recall lets a want that is
        # about to be rejected (the seller's own, wrong price, already
        # fulfilled) set the relative-window floor and suppress the genuine
        # matches underneath it.
        survivors: list[tuple[StandingWant, float]] = []
        for w in wants:
            sim = sims.get(w.id, 0.0)
            if self._passes_hard(item, w, sim,
                                 respect_notify_cap=respect_notify_cap,
                                 skip_seller_check=buyer_id is not None):
                survivors.append((w, sim))
        if not survivors:
            return []
        best = max(sim for _, sim in survivors)

        out = [
            WantMatch(want_id=w.id, buyer_id=w.buyer_id, similarity=round(sim, 4))
            for w, sim in survivors
            if best - sim <= settings.want_match_relative_window
        ]
        out.sort(key=lambda m: m.similarity, reverse=True)
        return out[:limit]

    @staticmethod
    def _prefilter(item: Item, *, buyer_id: int | None) -> dict[str, Any]:
        """ANN-stage prefilter — immutable want fields only (see build_want_metadata).

        Everything volatile stays in _passes_hard. The conditions here are
        re-checked there as well: the prefilter is an optimisation, never the
        source of correctness, so drifted metadata still cannot leak a match
        through.
        """
        clauses: list[dict[str, Any]] = [
            # Budget: the want's cap must cover the asking price, its floor must not exceed it
            {"max_price": {"$gte": float(item.price)}},
            {"min_price": {"$lte": float(item.price)}},
        ]
        if buyer_id is not None:
            # E4: only this buyer's wants. Pushing it down matters — previously
            # their wants had to survive a *global* top-N cut before the DB
            # filter ever saw them.
            clauses.append({"buyer_id": {"$eq": int(buyer_id)}})
        else:
            # Sellers must never be shown their own standing wants
            clauses.append({"buyer_id": {"$ne": int(item.seller_id)}})
        # ⚠️ Category is deliberately NOT filtered on, even though the metadata
        # carries it. Category is a **soft** signal in this system: _passes_hard
        # has never gated on it, and _match_reasons only uses it as a positive
        # reason. The taxonomy is coarse and the query parse is heuristic — a
        # buyer typing "book" parses to "Books & Textbooks" while a watercolor
        # notebook is filed under "School Supplies", so an AND on category
        # silently drops genuine matches (measured: it dropped the top
        # candidate for item 561). Almost all the pruning value here comes from
        # the buyer clause anyway. Do not add a category clause without
        # re-measuring.
        return {"$and": clauses}

    def match_items_for_want(
        self, index: ItemIndexService, want: StandingWant, limit: int = 5
    ) -> list[ItemMatch]:
        """E2: new want → is it already in stock.

        Reuses the item index's hard filtering + vector recall instead of
        reimplementing them.
        """
        doc = build_want_document(want)
        if not doc:
            return []
        origin = (
            (want.origin_lat, want.origin_lng)
            if want.origin_lat is not None and want.origin_lng is not None
            else None
        )
        candidates = index.candidate_ids(
            statuses=ItemStatus.SEARCHABLE,
            max_price=want.max_price,
            min_price=want.min_price,
            origin=origin,
            max_distance_km=want.max_distance_km,
        )
        # Buyers must not be pushed their own listings
        candidates = {i for i in candidates if index.get_record(i)}
        if not candidates:
            return []

        hits = index.vector_search(doc, max(limit * 3, 10), candidates)
        if not hits:
            return []
        best = hits[0][1]
        # Threshold follows the RETRIEVAL line (vector_min_similarity), not the
        # want<->item one. What decides that is the *shape* of the comparison,
        # not which side happens to be a want: here the short want text is the
        # query and long item documents are the corpus — exactly the buyer
        # search shape the 0.64 line was calibrated on. match_wants_for_item is
        # the reversed direction and is the one that needs its own line.
        # Getting this wrong is loud and buyer-visible: at the want<->item
        # threshold this outlet answered "kayak" with a skateboard and a
        # gibberish query with fairy lights.
        # want_stock_hint_min_similarity stays as an override if this one outlet
        # ever needs tightening on its own.
        floor = (
            settings.want_stock_hint_min_similarity
            if settings.want_stock_hint_min_similarity is not None
            else settings.vector_min_similarity
        )
        out: list[ItemMatch] = []
        for item_id, sim in hits:
            if sim < floor:
                continue
            if best - sim > settings.vector_relative_window:
                continue
            rec = index.get_record(item_id)
            if rec is None:
                continue
            out.append(ItemMatch(item_id=item_id, similarity=round(sim, 4), price=rec.price))
        return out[:limit]

    def passes_hard_for_push(self, item: Item, want: StandingWant) -> bool:
        """Re-check one (item, want) pair immediately before a seller-initiated push.

        Deliberately skips the semantic threshold. The similarity was already
        cleared when the candidate card was built, and re-embedding here would
        make the push depend on a score the seller cannot see and did not
        choose — the seller looked at the demand summary and decided. What is
        re-checked is everything **objective and mutable**: the want may have
        been dismissed or fulfilled, the price may have moved out of budget,
        the buyer's pickup radius still has to reach.

        The lifetime notify cap is intentionally not applied — see
        want_max_notify_total in config.py.
        """
        return self._passes_hard(
            item, want, sim=1.0, respect_notify_cap=False, skip_seller_check=False
        )

    # ---------------- Internal ----------------
    @staticmethod
    def _passes_hard(
        item: Item,
        want: StandingWant,
        sim: float,
        *,
        respect_notify_cap: bool = True,
        skip_seller_check: bool = False,
    ) -> bool:
        """Every condition that does NOT depend on the rest of the result set.

        The relative window is deliberately not here — it needs the best score
        among the survivors, which is only known once this has run for all of
        them. See match_wants_for_item.

        Note the overlap with _prefilter: price and seller are checked in both
        places on purpose. The prefilter is an ANN-stage optimisation working
        off possibly-stale metadata; this is the authoritative check against
        the business DB.
        """
        if want.status != WantStatus.ACTIVE:
            return False
        if not skip_seller_check and want.buyer_id == item.seller_id:
            return False
        # Nag cap: a want that's been pushed too many times loses its meaning
        if respect_notify_cap and (want.notify_count or 0) >= settings.want_max_notify_total:
            return False
        # Semantic bar: absolute floor. Calibrated separately from the
        # retrieval pipeline — see the comment on want_match_min_similarity in
        # config.py for why one shared line was wrong for both directions.
        if sim < settings.want_match_min_similarity:
            return False
        # Price is an objective hard condition
        if want.max_price is not None and item.price > want.max_price:
            return False
        if want.min_price is not None and item.price < want.min_price:
            return False
        # Distance
        if want.max_distance_km and want.origin_lat is not None:
            d = haversine_km(want.origin_lat, want.origin_lng, item.lat, item.lng)
            if d > want.max_distance_km:
                return False
        return True


_want_index: WantIndexService | None = None
_want_index_lock = threading.Lock()


def get_want_index() -> WantIndexService:
    global _want_index
    if _want_index is None:
        with _want_index_lock:
            if _want_index is None:
                _want_index = WantIndexService()
    return _want_index


def reset_want_index() -> None:
    global _want_index
    with _want_index_lock:
        _want_index = None
