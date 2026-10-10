"""Vector store wrapper: Chroma (local persistence), Milvus / Zilliz Cloud
(managed), or an in-memory implementation, degrading to in-memory when the
configured backend is unavailable.

Callers (ItemIndexService) only see four methods: upsert / delete / query / reset.

The filter language is Chroma's `where` dict in all three backends — it is the
one the callers already write. `InMemoryVectorStore` interprets it directly via
`match_where`, and `MilvusVectorStore` translates it to a Milvus boolean
expression via `to_milvus_expr`. Keeping one filter dialect at the call sites is
what makes the backends swappable by config.
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)


def match_where(meta: dict[str, Any], where: dict[str, Any] | None) -> bool:
    """Evaluate a Chroma-style `where` clause against one metadata dict.

    Exists so InMemoryVectorStore behaves identically to Chroma: the fallback
    store is what runs whenever chromadb is unavailable, and a filter that
    silently means something different there would be very hard to spot.
    Supports the subset the callers actually use:
    implicit equality, $eq/$ne/$gt/$gte/$lt/$lte/$in/$nin, and $and/$or.
    """
    if not where:
        return True
    for key, cond in where.items():
        if key == "$and":
            if not all(match_where(meta, c) for c in cond):
                return False
        elif key == "$or":
            if not any(match_where(meta, c) for c in cond):
                return False
        elif isinstance(cond, dict):
            value = meta.get(key)
            for op, target in cond.items():
                if op == "$eq":
                    ok = value == target
                elif op == "$ne":
                    ok = value != target
                elif op == "$in":
                    ok = value in target
                elif op == "$nin":
                    ok = value not in target
                elif op in ("$gt", "$gte", "$lt", "$lte"):
                    # A missing/None field can never satisfy a numeric comparison
                    if value is None or not isinstance(value, (int, float)):
                        ok = False
                    elif op == "$gt":
                        ok = value > target
                    elif op == "$gte":
                        ok = value >= target
                    elif op == "$lt":
                        ok = value < target
                    else:
                        ok = value <= target
                else:
                    raise ValueError(f"Unsupported where operator: {op}")
                if not ok:
                    return False
        else:
            if meta.get(key) != cond:
                return False
    return True


#: Field names allowed in a translated expression. Metadata keys reach Milvus as
#: dynamic-field names inside a query string, so anything outside this shape is
#: rejected rather than concatenated into the expression.
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_CMP_OPS = {"$eq": "==", "$ne": "!=", "$gt": ">", "$gte": ">=", "$lt": "<", "$lte": "<="}

#: Well-formed stand-ins for the constants Milvus will not accept on their own.
#: Both lean on the same invariant: every row has a non-empty primary key.
_ALWAYS_TRUE = 'pk != ""'
_ALWAYS_FALSE = 'pk == ""'


def _lit(value: Any) -> str:
    """One Chroma value as a Milvus literal.

    Strings go through json.dumps rather than an f-string: category names carry
    spaces and ampersands ("Books & Textbooks"), and a hand-rolled quote would
    break on the first value containing a quote of its own.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise ValueError(f"Unsupported filter value type: {type(value).__name__}")


def _field(key: str) -> str:
    if not _FIELD_RE.match(key):
        raise ValueError(f"Unsupported metadata field name in filter: {key!r}")
    return key


def to_milvus_expr(where: dict[str, Any] | None) -> str:
    """Translate a Chroma-style `where` dict into a Milvus boolean expression.

    Covers the same subset as `match_where` — implicit equality,
    $eq/$ne/$gt/$gte/$lt/$lte/$in/$nin, and $and/$or — and raises on anything
    else. Raising matters: a filter that quietly translated to "no filter" would
    return results the caller believes were excluded, and the caller here is the
    want-matching prefilter (budget, category, "not my own listing").

    Returns "" for an empty filter, which Milvus reads as "no filter".
    """
    if not where:
        return ""
    parts: list[str] = []
    for key, cond in where.items():
        if key in ("$and", "$or"):
            joiner = " && " if key == "$and" else " || "
            subs = [to_milvus_expr(c) for c in cond]
            subs = [x for x in subs if x]
            if not subs:
                continue
            parts.append("(" + joiner.join(subs) + ")" if len(subs) > 1 else subs[0])
        elif isinstance(cond, dict):
            field = _field(key)
            for op, target in cond.items():
                if op in _CMP_OPS:
                    parts.append(f"{field} {_CMP_OPS[op]} {_lit(target)}")
                elif op in ("$in", "$nin"):
                    values = list(target)
                    if not values:
                        # An empty $in matches nothing; an empty $nin matches
                        # everything. Milvus rejects a bare `false`/`true` as a
                        # predicate ("not a boolean expression"), so both are
                        # expressed against the primary key, which every row has
                        # and none leaves empty (ids are str(int)). `pk != ""` is
                        # the same always-true form `reset()` deletes by.
                        parts.append(_ALWAYS_FALSE if op == "$in" else _ALWAYS_TRUE)
                        continue
                    rendered = ", ".join(_lit(v) for v in values)
                    parts.append(f"{field} {'in' if op == '$in' else 'not in'} [{rendered}]")
                else:
                    raise ValueError(f"Unsupported where operator: {op}")
        else:
            parts.append(f"{_field(key)} == {_lit(cond)}")
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else " && ".join(f"({p})" for p in parts)


class VectorStore(ABC):
    name = "base"

    @abstractmethod
    def upsert(self, ids: list[str], embeddings: list[list[float]], metadatas: list[dict]) -> None: ...

    @abstractmethod
    def delete(self, ids: list[str]) -> None: ...

    @abstractmethod
    def reset(self) -> None: ...

    @abstractmethod
    def query(
        self,
        embedding: list[float],
        top_k: int,
        allowed_ids: set[str] | None = None,
        where: dict[str, Any] | None = None,
    ) -> list[tuple[str, float]]:
        """Return [(id, cosine_similarity)] sorted by similarity descending.

        `allowed_ids` restricts by the metadata "item_id" key; `where` is a
        free-form metadata filter. Both are pushed down to the ANN stage, and
        when both are given they are combined with $and.
        """

    @abstractmethod
    def count(self) -> int: ...


class ChromaVectorStore(VectorStore):
    def __init__(self, persist_dir: str, collection: str):
        import chromadb  # deferred import

        self.name = f"chroma:{collection}"
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._collection_name = collection
        self._collection = self._client.get_or_create_collection(
            name=collection, metadata={"hnsw:space": "cosine"}
        )

    def _reacquire(self) -> None:
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name, metadata={"hnsw:space": "cosine"}
        )

    def _call(self, fn):
        """The seed script may rebuild the collection from another process,
        invalidating our handle — reacquire it once and retry."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if "does not exist" not in str(exc):
                raise
            logger.warning("[vector] collection handle went stale; reacquiring and retrying: %s", exc)
            self._reacquire()
            return fn()

    def upsert(self, ids: list[str], embeddings: list[list[float]], metadatas: list[dict]) -> None:
        if not ids:
            return
        self._call(lambda: self._collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas))

    def delete(self, ids: list[str]) -> None:
        if not ids:
            return
        self._call(lambda: self._collection.delete(ids=ids))

    def reset(self) -> None:
        """Clear the collection's contents but **never drop the collection
        itself** — dropping it would invalidate handles held by other processes."""
        existing = self._call(lambda: self._collection.get(include=[]))
        ids = (existing or {}).get("ids") or []
        if ids:
            self._call(lambda: self._collection.delete(ids=list(ids)))

    def count(self) -> int:
        try:
            return int(self._call(lambda: self._collection.count()))
        except Exception:  # noqa: BLE001
            return 0

    def query(
        self,
        embedding: list[float],
        top_k: int,
        allowed_ids: set[str] | None = None,
        where: dict[str, Any] | None = None,
    ) -> list[tuple[str, float]]:
        total = self.count()
        if total == 0:
            return []
        clauses: list[dict[str, Any]] = []
        n = min(max(top_k, 1), total)
        if allowed_ids is not None:
            if not allowed_ids:
                return []
            # Push the hard filter down into the vector store: the candidate
            # set is pruned at the ANN search stage already.
            clauses.append({"item_id": {"$in": [int(i) for i in allowed_ids]}})
            n = min(max(top_k, 1), max(len(allowed_ids), 1))
        if where:
            clauses.append(where)
        # Chroma rejects a bare {"$and": [one_clause]}, so only wrap when needed
        where = clauses[0] if len(clauses) == 1 else ({"$and": clauses} if clauses else None)
        res = self._call(
            lambda: self._collection.query(
                query_embeddings=[embedding],
                n_results=n,
                where=where,
                include=["distances"],
            )
        )
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: list[tuple[str, float]] = []
        for i, d in zip(ids, dists):
            out.append((str(i), 1.0 - float(d)))  # cosine distance -> similarity
        return out


class MilvusVectorStore(VectorStore):
    """Milvus / Zilliz Cloud.

    Three things here differ from Chroma in ways that fail silently if copied
    across, so they are called out where they happen:

    * **COSINE returns a similarity, not a distance.** Chroma returns a distance
      and the caller converts with `1.0 - d`; doing that here would invert every
      ranking without raising anything (see `query`).
    * **The schema is created from the first batch's vector width.** Milvus needs
      a fixed dim up front, and taking it from the data rather than from config
      means the collection cannot disagree with the vectors that go into it.
    * **Metadata rides the dynamic field.** The two collections (items, wants)
      carry different metadata, and `build_index_metadata` /
      `build_want_metadata` stay their single definition — a typed schema here
      would be a second copy, and second copies drift.
    """

    def __init__(self, uri: str, token: str, collection: str):
        from pymilvus import MilvusClient  # deferred import

        self.name = f"milvus:{collection}"
        self._collection = collection
        self._timeout = float(settings.milvus_timeout_s)
        self._consistency = settings.milvus_consistency
        self._client = MilvusClient(uri=uri, token=token, timeout=self._timeout)
        self._loaded = False
        self._exists_cache = False
        self._probe()

    def _probe(self) -> None:
        """Liveness check, retried.

        A serverless cluster that has gone to sleep answers the first RPC with a
        deadline error and is fine seconds later. Without the retry that wake-up
        looks like "backend unavailable" to `build_vector_store`, which then
        falls back to an empty in-memory store — and an empty store does not
        error, it just answers every search with nothing.
        """
        attempts = max(1, int(settings.milvus_connect_attempts))
        last: Exception | None = None
        for i in range(attempts):
            try:
                self._client.list_collections()
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                logger.warning(
                    "[vector] Milvus probe %d/%d failed (%s); the cluster may be waking up",
                    i + 1, attempts, type(exc).__name__,
                )
                if i + 1 < attempts:
                    time.sleep(3)
        raise RuntimeError(f"Milvus unreachable after {attempts} attempts: {last}")

    # ---------------- schema ----------------
    def _exists(self) -> bool:
        """Whether the collection exists, asked at most once per process.

        `has_collection` is a network round trip. Memoizing it is what keeps
        `query` down to a single RPC; the answer only ever goes from False to
        True (nothing here drops a collection), so a cached True cannot go stale
        in a way that matters.
        """
        if self._exists_cache:
            return True
        try:
            self._exists_cache = bool(self._client.has_collection(self._collection))
        except Exception:  # noqa: BLE001
            return False
        return self._exists_cache

    def _ensure_collection(self, dim: int) -> None:
        if self._exists():
            return
        from pymilvus import DataType, MilvusClient

        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        index = MilvusClient.prepare_index_params()
        index.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
        self._client.create_collection(
            self._collection,
            schema=schema,
            index_params=index,
            consistency_level=self._consistency,
        )
        logger.info("[vector] created Milvus collection %s (dim=%d)", self._collection, dim)
        self._loaded = True
        self._exists_cache = True

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        try:
            self._client.load_collection(self._collection)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[vector] load_collection(%s): %s", self._collection, exc)
        self._loaded = True

    # ---------------- writes ----------------
    def upsert(self, ids: list[str], embeddings: list[list[float]], metadatas: list[dict]) -> None:
        if not ids:
            return
        self._ensure_collection(len(embeddings[0]))
        rows = [
            {"pk": str(i), "vector": list(e), **(m or {})}
            for i, e, m in zip(ids, embeddings, metadatas)
        ]
        self._client.upsert(self._collection, data=rows, timeout=self._timeout)

    def delete(self, ids: list[str]) -> None:
        if not ids or not self._exists():
            return
        self._client.delete(self._collection, ids=[str(i) for i in ids], timeout=self._timeout)

    def reset(self) -> None:
        """Clear the contents, **never drop the collection** — same rule as
        Chroma (other processes hold handles), and on a free-tier cluster
        `drop_collection` also routinely exceeds its deadline while succeeding
        server-side, which is the worst of both worlds to build on."""
        if not self._exists():
            return
        self._client.delete(self._collection, filter='pk != ""', timeout=self._timeout)

    def count(self) -> int:
        if not self._exists():
            return 0
        try:
            self._ensure_loaded()
            rows = self._client.query(
                self._collection, filter="", output_fields=["count(*)"], timeout=self._timeout
            )
            return int(rows[0]["count(*)"]) if rows else 0
        except Exception:  # noqa: BLE001
            return 0

    # ---------------- reads ----------------
    def query(
        self,
        embedding: list[float],
        top_k: int,
        allowed_ids: set[str] | None = None,
        where: dict[str, Any] | None = None,
    ) -> list[tuple[str, float]]:
        # Deliberately no count() here. Chroma clamps `n_results` to the row
        # count because asking for more than exists is an error there and the
        # count is a local read; over the network that clamp would cost two
        # extra round trips (has_collection + count) on every buyer search —
        # three RPCs where one will do. Milvus is happy with a limit larger
        # than the collection and simply returns fewer rows.
        if not self._exists():
            return []
        clauses: list[dict[str, Any]] = []
        n = max(top_k, 1)
        if allowed_ids is not None:
            if not allowed_ids:
                return []
            clauses.append({"item_id": {"$in": [int(i) for i in allowed_ids]}})
            n = min(n, max(len(allowed_ids), 1))
        if where:
            clauses.append(where)
        combined = clauses[0] if len(clauses) == 1 else ({"$and": clauses} if clauses else None)
        expr = to_milvus_expr(combined)

        self._ensure_loaded()
        res = self._client.search(
            self._collection,
            data=[list(embedding)],
            limit=n,
            filter=expr,
            output_fields=[],
            consistency_level=self._consistency,
            timeout=self._timeout,
        )
        hits = res[0] if res else []
        out: list[tuple[str, float]] = []
        for h in hits:
            # MilvusClient surfaces the primary key as "id"; some versions echo
            # it under the field's own name instead.
            pk = h.get("id", h.get("pk"))
            # COSINE: `distance` is already the similarity. No 1.0 - d here.
            out.append((str(pk), float(h["distance"])))
        return out


class InMemoryVectorStore(VectorStore):
    """Fallback implementation: brute-force cosine. Plenty for demo scale
    (tens to a few thousand rows)."""

    def __init__(self):
        self.name = "memory"
        self._vecs: dict[str, list[float]] = {}
        self._meta: dict[str, dict] = {}

    def upsert(self, ids: list[str], embeddings: list[list[float]], metadatas: list[dict]) -> None:
        for i, e, m in zip(ids, embeddings, metadatas):
            self._vecs[i] = e
            self._meta[i] = m

    def delete(self, ids: list[str]) -> None:
        for i in ids:
            self._vecs.pop(i, None)
            self._meta.pop(i, None)

    def reset(self) -> None:
        self._vecs.clear()
        self._meta.clear()

    def count(self) -> int:
        return len(self._vecs)

    def query(
        self,
        embedding: list[float],
        top_k: int,
        allowed_ids: set[str] | None = None,
        where: dict[str, Any] | None = None,
    ) -> list[tuple[str, float]]:
        scored: list[tuple[str, float]] = []
        for i, v in self._vecs.items():
            if allowed_ids is not None and i not in allowed_ids:
                continue
            if where and not match_where(self._meta.get(i) or {}, where):
                continue
            scored.append((i, sum(a * b for a, b in zip(embedding, v))))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


def build_vector_store(collection: str | None = None) -> VectorStore:
    """`collection` defaults to the items collection; standing wants use a
    separate collection, see settings.want_vector_collection."""
    collection = collection or settings.chroma_collection
    backend = settings.vector_backend.lower()
    if backend == "milvus":
        if not settings.milvus_uri:
            logger.warning("[vector] VECTOR_BACKEND=milvus but MILVUS_URI is empty; falling back.")
        else:
            try:
                # Namespaced here rather than at each call site, so a caller
                # cannot forget and land on the shared name.
                name = f"{settings.milvus_collection_prefix}{collection}"
                store = MilvusVectorStore(settings.milvus_uri, settings.milvus_token, name)
                logger.info("[vector] using %s (uri=%s)", store.name, settings.milvus_uri)
                return store
            except Exception as exc:  # noqa: BLE001
                logger.warning("[vector] Milvus unavailable (%s); falling back to the in-memory vector store.", exc)
    elif backend == "chroma":
        try:
            store = ChromaVectorStore(str(settings.chroma_path), collection)
            logger.info("[vector] using %s (dir=%s, collection=%s)",
                        store.name, settings.chroma_path, collection)
            return store
        except Exception as exc:  # noqa: BLE001
            logger.warning("[vector] Chroma unavailable (%s); falling back to the in-memory vector store.", exc)
    store = InMemoryVectorStore()
    logger.info("[vector] using %s", store.name)
    return store
