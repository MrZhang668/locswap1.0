"""Verify the Milvus backend against the Chroma one, by equivalence rather than by eye.

Two questions, in order:

1. **Does `to_milvus_expr` mean the same thing as the `where` dict it came from?**
   `match_where` is the reference implementation — it is what `InMemoryVectorStore`
   runs, and it was written against Chroma's semantics. So for every filter shape
   the callers actually use, the set of rows Milvus returns must equal the set
   `match_where` accepts. A filter that silently translates to something weaker
   returns rows the caller believes were excluded, and the caller is the
   want-matching prefilter (budget, category, "not my own listing").

2. **Do the two backends rank the same?** Same vectors, same query, compare the
   ordered id lists and the scores. This is where `1.0 - d` would show up: Chroma
   returns a cosine *distance*, Milvus returns a *similarity*, and getting that
   backwards inverts the ranking without raising anything.

Runs against a throwaway collection, cleans up after itself, and needs no API key
(vectors are seeded, not embedded).

    python scripts/verify_milvus.py
"""

from __future__ import annotations

import random
import sys
import time

import _bootstrap  # noqa: F401  injects backend into sys.path

from app.config import settings  # noqa: E402
from app.services.vector_store import (  # noqa: E402
    InMemoryVectorStore,
    MilvusVectorStore,
    match_where,
    to_milvus_expr,
)

PROBE = "_verify_probe"
DIM = 64
N = 200

CATEGORIES = ["电子产品", "图书与教材", "家居与厨房", "__any__"]

#: The filter shapes the real callers build. `want_index._prefilter` is the only
#: producer of `where`, and these cover every branch it can take.
CASES: list[tuple[str, dict | None]] = [
    ("no filter", None),
    ("budget ceiling ($gte)", {"max_price": {"$gte": 120.0}}),
    ("budget floor ($lte)", {"min_price": {"$lte": 40.0}}),
    ("category equality (spaces + &)", {"category": {"$eq": "图书与教材"}}),
    ("implicit equality", {"kind": "want"}),
    ("exclude own listings ($ne)", {"buyer_id": {"$ne": 103}}),
    ("pin one buyer ($eq)", {"buyer_id": {"$eq": 105}}),
    ("id pushdown ($in)", {"item_id": {"$in": [3, 11, 19, 27, 35, 43, 51]}}),
    ("negated set ($nin)", {"item_id": {"$nin": [1, 2, 3, 4, 5]}}),
    (
        "full prefilter ($and x4)",
        {
            "$and": [
                {"max_price": {"$gte": 80.0}},
                {"min_price": {"$lte": 80.0}},
                {"category": {"$eq": "电子产品"}},
                {"buyer_id": {"$ne": 101}},
            ]
        },
    ),
    ("empty $in matches nothing", {"item_id": {"$in": []}}),
]


def make_rows(seed: int = 7):
    rng = random.Random(seed)
    ids, vecs, metas = [], [], []
    for i in range(N):
        ids.append(str(i))
        vecs.append([rng.uniform(-1, 1) for _ in range(DIM)])
        metas.append(
            {
                "item_id": i,
                "kind": "want",
                "buyer_id": 100 + i % 7,
                "category": CATEGORIES[i % len(CATEGORIES)],
                "max_price": float(50 + i),
                "min_price": float(i % 60),
            }
        )
    return ids, vecs, metas


def norm(v):
    mag = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / mag for x in v]


def main() -> int:
    if not settings.milvus_uri:
        print("MILVUS_URI is empty — nothing to verify.")
        return 2

    ids, vecs, metas = make_rows()
    vecs = [norm(v) for v in vecs]
    meta_by_id = dict(zip(ids, metas))

    print(f"target: {settings.milvus_uri}")
    t = time.perf_counter()
    store = MilvusVectorStore(settings.milvus_uri, settings.milvus_token, PROBE)
    print(f"connected in {(time.perf_counter() - t) * 1000:.0f}ms")

    # A previous aborted run may have left rows behind.
    store.reset()
    t = time.perf_counter()
    store.upsert(ids, vecs, metas)
    print(f"upsert {N} rows in {(time.perf_counter() - t) * 1000:.0f}ms")

    mem = InMemoryVectorStore()
    mem.upsert(ids, vecs, metas)

    rng = random.Random(99)
    q = norm([rng.uniform(-1, 1) for _ in range(DIM)])

    failures = 0

    # ---------- 1. filter equivalence ----------
    print("\n--- filter equivalence (Milvus result set vs match_where) ---")
    for label, where in CASES:
        expr = to_milvus_expr(where)
        expected = {i for i in ids if match_where(meta_by_id[i], where)}
        got = {i for i, _ in store.query(q, top_k=N, where=where)}
        ok = got == expected
        failures += 0 if ok else 1
        mark = "OK  " if ok else "FAIL"
        print(f"{mark} {label:34} n={len(expected):3}  expr={expr[:70] or '(none)'}")
        if not ok:
            print(f"     only in Milvus: {sorted(got - expected)[:8]}")
            print(f"     only expected : {sorted(expected - got)[:8]}")

    # ---------- 2. id pushdown via allowed_ids ----------
    print("\n--- allowed_ids pushdown ---")
    allowed = {str(i) for i in range(0, 120, 3)}
    got = {i for i, _ in store.query(q, top_k=N, allowed_ids=allowed)}
    ok = got == allowed
    failures += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} allowed_ids({len(allowed)}) -> {len(got)} rows")

    got = store.query(q, top_k=5, allowed_ids=set())
    ok = got == []
    failures += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} empty allowed_ids -> [] (never 'no filter')")

    # ---------- 3. ranking parity ----------
    print("\n--- ranking parity vs brute-force cosine ---")
    for label, where in [("unfiltered", None), ("full prefilter", CASES[-2][1])]:
        a = store.query(q, top_k=10, where=where)
        b = mem.query(q, top_k=10, where=where)
        same_order = [i for i, _ in a] == [i for i, _ in b]
        max_delta = max(
            (abs(x - y) for (_, x), (_, y) in zip(a, b)), default=0.0
        )
        ok = same_order and max_delta < 1e-3
        failures += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {label:16} order_match={same_order} max_score_delta={max_delta:.2e}")
        if not same_order:
            print(f"     milvus: {[i for i, _ in a]}")
            print(f"     memory: {[i for i, _ in b]}")
            print("     (inverted order here means COSINE was read as a distance)")

    # ---------- 4. write path ----------
    print("\n--- writes ---")
    before = store.count()
    store.delete(["0", "1", "2"])
    after = store.count()
    ok = after == before - 3
    failures += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} delete 3 -> count {before} => {after}")

    store.upsert(["0"], [vecs[0]], [{**metas[0], "category": "电子产品"}])
    hit = [i for i, _ in store.query(vecs[0], top_k=1)]
    ok = hit == ["0"]
    failures += 0 if ok else 1
    print(f"{'OK  ' if ok else 'FAIL'} re-upsert visible to the next search (consistency={settings.milvus_consistency}) -> {hit}")

    # ---------- 5. latency ----------
    print("\n--- latency ---")
    lat = []
    for _ in range(10):
        t = time.perf_counter()
        store.query(q, top_k=8)
        lat.append((time.perf_counter() - t) * 1000)
    lat.sort()
    print(f"search p50={lat[len(lat) // 2]:.0f}ms  p90={lat[int(len(lat) * 0.9)]:.0f}ms  min={lat[0]:.0f}ms")

    store.reset()
    print(f"\ncleaned up ({store.count()} rows left)")
    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
