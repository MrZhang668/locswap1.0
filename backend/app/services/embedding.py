"""Embedding provider abstraction.

- Defaults to local sentence-transformers (the retrieval path costs **zero API
  tokens**).
- If the model can't be downloaded / the dependency isn't installed, it
  automatically degrades to the built-in hashing encoder so the demo always runs.
Switching models only takes changing EMBEDDING_MODEL / EMBEDDING_BACKEND in
.env; callers never notice.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from abc import ABC, abstractmethod

from ..config import settings

logger = logging.getLogger(__name__)


class EmbeddingProvider(ABC):
    name: str = "base"
    dim: int = 0

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    def embed_query(self, text: str) -> list[float]: ...


class SentenceTransformerEmbedding(EmbeddingProvider):
    """Local sentence-transformers; the model name and query prefix come from
    settings (default BAAI/bge-small-zh-v1.5, 512-dim)."""

    def __init__(self, model_name: str, query_prefix: str = ""):
        from sentence_transformers import SentenceTransformer  # deferred import; only fails when the dep is missing

        self.name = f"sentence_transformers:{model_name}"
        self._model = SentenceTransformer(model_name)
        self._query_prefix = query_prefix
        if hasattr(self._model, "get_embedding_dimension"):  # newer API
            self.dim = int(self._model.get_embedding_dimension())
        else:
            self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, v)) for v in vecs]

    def embed_query(self, text: str) -> list[float]:
        vec = self._model.encode(
            [self._query_prefix + text], normalize_embeddings=True, show_progress_bar=False
        )[0]
        return list(map(float, vec))


class HashingEmbedding(EmbeddingProvider):
    """Dependency-free fallback encoder: hashing trick over unigrams +
    adjacent bigrams, L2-normalized.

    Far weaker semantically than a real model, but deterministic and fully
    offline — it keeps the whole pipeline runnable with no model available,
    going through exactly the same interface and vector-store path as the real
    model.

    Chinese is handled by character bigrams rather than by jieba: this class
    exists for the case where dependencies could not be installed, so it must
    not acquire one of its own. Before the 2026-09-15 localization it only
    matched ``[a-zA-Z0-9]+``, which on a Chinese corpus produced an all-zero
    vector for every document — every similarity 0, every search empty, and no
    error anywhere. A fallback that fails silently is worse than no fallback.
    """

    _ascii_re = re.compile(r"[a-z0-9]+")
    _cjk_re = re.compile(r"[\u4e00-\u9fff]+")

    def __init__(self, dim: int = 512):
        self.name = f"hashing:{dim}"
        self.dim = dim

    def _features(self, text: str) -> list[str]:
        text = text.lower()
        feats: list[str] = []

        toks = self._ascii_re.findall(text)
        feats += toks
        feats += [toks[i] + toks[i + 1] for i in range(len(toks) - 1)]

        for run in self._cjk_re.findall(text):
            feats += list(run)
            feats += [run[i : i + 2] for i in range(len(run) - 1)]
        return feats

    def _encode(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feat in self._features(text):
            h = int.from_bytes(hashlib.md5(feat.encode("utf-8")).digest()[:8], "big")
            idx = h % self.dim
            sign = 1.0 if (h >> 63) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._encode(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._encode(text)


_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """Process-level singleton."""
    global _provider
    if _provider is not None:
        return _provider

    backend = settings.embedding_backend.lower()
    if backend in ("auto", "sentence_transformers"):
        try:
            _provider = SentenceTransformerEmbedding(
                settings.embedding_model, settings.embedding_query_prefix
            )
            logger.info("[embedding] using %s (dim=%d)", _provider.name, _provider.dim)
            return _provider
        except Exception as exc:  # noqa: BLE001
            if backend == "sentence_transformers":
                raise
            logger.warning(
                "[embedding] sentence-transformers unavailable (%s); falling back to the "
                "built-in hashing encoder. Semantic recall quality will drop — install "
                "the dependency/model and restart to recover.",
                exc,
            )

    _provider = HashingEmbedding(settings.embedding_fallback_dim)
    logger.info("[embedding] using %s (dim=%d)", _provider.name, _provider.dim)
    return _provider


def reset_embedding_provider() -> None:
    """For tests/scripts."""
    global _provider
    _provider = None
