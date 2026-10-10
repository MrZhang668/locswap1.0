"""Before/after report for the Chroma -> Milvus swap: does the vector leg return the same things?

Compares the two backends at the **retrieval layer**, not over HTTP. The HTTP
path (`/api/search/chat`, what eval_retrieval.py drives) runs the query through
an LLM first, which both costs tokens and makes the comparison non-deterministic
— if the model phrases a query differently on the second run, a change in the
results says nothing about the vector store. Here both stores are handed the
identical document embeddings and the identical query vector, so any difference
is the store and nothing else.

Both indexes are rebuilt from the same DB rows in this process, so neither can
be stale relative to the other. The Chroma side goes into a throwaway directory
and the Milvus side into a throwaway collection; the live index is not touched.

    python scripts/compare_vector_backends.py
    python scripts/compare_vector_backends.py --top-k 20 --json out.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

import _bootstrap  # noqa: F401  injects backend into sys.path

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Item, ItemStatus  # noqa: E402
from app.services.embedding import get_embedding_provider  # noqa: E402
from app.services.item_index import build_index_document, build_index_metadata  # noqa: E402
from app.services.vector_store import ChromaVectorStore, MilvusVectorStore  # noqa: E402

PROBE_COLLECTION = "_compare_probe"

#: Reused verbatim from eval_retrieval.py's labeled set (query text only — the
#: correctness labels belong to the full pipeline, not to one retrieval leg).
QUERIES = [
    "bike", "mechanical keyboard", "wireless earbuds", "laptop", "monitor",
    "power bank", "yoga mat", "badminton racket", "winter coat", "backpack",
    "floor lamp", "electric kettle", "mini fridge", "insulated water bottle",
    "acoustic guitar", "scientific calculator", "suitcase", "tent",
    "used bike under $300", "black headphones under 200 bucks", "laptop over $500",
    "bicycle for less than $500", "textbooks under $100", "floor lamp between $20 and $60",
    "lightly used mechanical keyboard", "Anker power bank", "white mechanical keyboard",
    "something to keep my drinks cold", "stuff for my dorm room", "i need to furnish my kitchen",
    "gift for a roommate who likes music", "cheap way to get around campus",
    "textbook for organic chemistry", "desk chair", "printer", "washing machine",
    "drone", "kayak", "lawn mower", "baby stroller",
]


def rank_overlap(a: list[str], b: list[str]) -> float:
    """Fraction of the top-K that both stores returned, regardless of order."""
    if not a and not b:
        return 1.0
    return len(set(a) & set(b)) / max(len(set(a) | set(b)), 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--json", help="write the full per-query report here")
    args = ap.parse_args()

    if not settings.milvus_uri:
        print("MILVUS_URI is empty — nothing to compare against.")
        return 2

    # ---------- corpus ----------
    db = SessionLocal()
    try:
        items = list(db.query(Item).filter(Item.status.in_(ItemStatus.SEARCHABLE)).all())
        docs = [build_index_document(i) for i in items]
        metas = [build_index_metadata(i) for i in items]
        ids = [str(i.id) for i in items]
        names = {str(i.id): (i.name or "")[:44] for i in items}
    finally:
        db.close()
    print(f"corpus: {len(items)} searchable items")
    if not items:
        print("nothing indexed; run the seed first.")
        return 2

    # ---------- embed once, share with both stores ----------
    embedder = get_embedding_provider()
    print(f"embedder: {embedder.name} (dim={embedder.dim})")
    t = time.perf_counter()
    doc_vecs = embedder.embed_documents(docs)
    print(f"embedded {len(docs)} documents in {time.perf_counter() - t:.1f}s")
    query_vecs = [embedder.embed_query(q) for q in QUERIES]

    # ---------- build both indexes from the same vectors ----------
    tmpdir = Path(tempfile.mkdtemp(prefix="cmp_chroma_"))
    try:
        chroma = ChromaVectorStore(str(tmpdir), "items")
        t = time.perf_counter()
        chroma.upsert(ids, doc_vecs, metas)
        chroma_build = time.perf_counter() - t

        milvus = MilvusVectorStore(settings.milvus_uri, settings.milvus_token, PROBE_COLLECTION)
        milvus.reset()
        t = time.perf_counter()
        # Batched: one upsert of 500+ rows with 384 floats each is a large gRPC
        # message, and the free tier's per-request ceiling is not documented.
        for start in range(0, len(ids), 200):
            sl = slice(start, start + 200)
            milvus.upsert(ids[sl], doc_vecs[sl], metas[sl])
        milvus_build = time.perf_counter() - t

        print(f"index build: chroma {chroma_build:.1f}s | milvus {milvus_build:.1f}s")
        print(f"row counts : chroma {chroma.count()} | milvus {milvus.count()}")

        # ---------- compare ----------
        rows = []
        top1_same = 0
        overlaps: list[float] = []
        score_deltas: list[float] = []
        c_lat: list[float] = []
        m_lat: list[float] = []

        for q, qv in zip(QUERIES, query_vecs):
            t = time.perf_counter()
            c = chroma.query(qv, top_k=args.top_k)
            c_lat.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            m = milvus.query(qv, top_k=args.top_k)
            m_lat.append((time.perf_counter() - t) * 1000)

            c_ids = [i for i, _ in c]
            m_ids = [i for i, _ in m]
            same_top1 = bool(c_ids) and bool(m_ids) and c_ids[0] == m_ids[0]
            top1_same += int(same_top1)
            ov = rank_overlap(c_ids, m_ids)
            overlaps.append(ov)

            c_scores = dict(c)
            deltas = [abs(s - c_scores[i]) for i, s in m if i in c_scores]
            d = max(deltas) if deltas else 0.0
            score_deltas.append(d)

            rows.append({
                "query": q,
                "top1_same": same_top1,
                "overlap": round(ov, 3),
                "max_score_delta": d,
                "chroma_top1": names.get(c_ids[0], "") if c_ids else None,
                "milvus_top1": names.get(m_ids[0], "") if m_ids else None,
                "chroma_ids": c_ids,
                "milvus_ids": m_ids,
            })

        n = len(QUERIES)
        print("\n" + "=" * 78)
        print(f"{'query':40} {'top1':>5} {'overlap@'+str(args.top_k):>12} {'Δscore':>10}")
        print("-" * 78)
        for r in rows:
            mark = "same" if r["top1_same"] else "DIFF"
            print(f"{r['query'][:40]:40} {mark:>5} {r['overlap']:>12.3f} {r['max_score_delta']:>10.2e}")
            if not r["top1_same"]:
                print(f"    chroma: {r['chroma_top1']}")
                print(f"    milvus: {r['milvus_top1']}")
        print("=" * 78)
        print(f"top-1 identical      : {top1_same}/{n}  ({top1_same / n:.1%})")
        print(f"mean overlap@{args.top_k:<8}: {statistics.mean(overlaps):.4f}")
        print(f"min overlap          : {min(overlaps):.4f}")
        print(f"max score delta      : {max(score_deltas):.2e}")
        c_lat.sort(); m_lat.sort()
        print(f"latency p50          : chroma {c_lat[n // 2]:.1f}ms | milvus {m_lat[n // 2]:.1f}ms")

        verdict = top1_same == n and min(overlaps) >= 0.9 and max(score_deltas) < 1e-3
        print(f"\nVERDICT: {'PASS — the two backends agree' if verdict else 'REVIEW — differences above'}")

        if args.json:
            Path(args.json).write_text(json.dumps({
                "corpus": len(items), "top_k": args.top_k,
                "embedder": embedder.name,
                "top1_identical": top1_same, "queries": n,
                "mean_overlap": statistics.mean(overlaps),
                "min_overlap": min(overlaps),
                "max_score_delta": max(score_deltas),
                "chroma_p50_ms": c_lat[n // 2], "milvus_p50_ms": m_lat[n // 2],
                "rows": rows,
            }, indent=2), encoding="utf-8")
            print(f"report -> {args.json}")

        milvus.reset()
        return 0 if verdict else 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
