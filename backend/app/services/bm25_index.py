"""In-memory BM25 keyword index.

Fully rebuilt from the business DB on startup; item creates/updates/deletes
maintain the corpus incrementally, and the index object is rebuilt lazily
before the next query (rank_bm25's BM25Okapi has no true incremental mode, but
at demo scale the rebuild cost is negligible).
"""

from __future__ import annotations

import logging
import threading

from rank_bm25 import BM25Okapi

from .text import tokenize

logger = logging.getLogger(__name__)


class BM25Index:
    def __init__(self):
        self._docs: dict[int, list[str]] = {}
        self._ids: list[int] = []
        self._bm25: BM25Okapi | None = None
        self._dirty = True
        self._lock = threading.Lock()

    # ---------- Maintenance ----------
    def upsert(self, item_id: int, text: str) -> None:
        with self._lock:
            self._docs[int(item_id)] = tokenize(text)
            self._dirty = True

    def delete(self, item_id: int) -> None:
        with self._lock:
            if self._docs.pop(int(item_id), None) is not None:
                self._dirty = True

    def reset(self, docs: dict[int, str] | None = None) -> None:
        with self._lock:
            self._docs = {int(k): tokenize(v) for k, v in (docs or {}).items()}
            self._dirty = True

    def count(self) -> int:
        return len(self._docs)

    def _ensure(self) -> None:
        if not self._dirty:
            return
        with self._lock:
            self._ids = list(self._docs.keys())
            corpus = [self._docs[i] for i in self._ids]
            self._bm25 = BM25Okapi(corpus) if corpus else None
            self._dirty = False

    # ---------- Query ----------
    def search(
        self, query: str, top_k: int, allowed_ids: set[int] | None = None
    ) -> list[tuple[int, float]]:
        self._ensure()
        if self._bm25 is None:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        pairs = [
            (item_id, float(score))
            for item_id, score in zip(self._ids, scores)
            if score > 0 and (allowed_ids is None or item_id in allowed_ids)
        ]
        pairs.sort(key=lambda x: x[1], reverse=True)
        return pairs[:top_k]
