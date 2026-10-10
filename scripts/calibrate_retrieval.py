"""Retrieval threshold calibration: probe the raw score distributions of the three
recall legs with one group of queries that ARE in the corpus and one that is NOT.

The comments on vector_min_similarity / bm25_min_score in config.py say
"recalibrate as the corpus scale changes". This script is that calibration tool --
after the corpus grew from 25 to 500+ items, both the BM25 IDF and the vector
neighbor density shifted, and stale thresholds let queries like "washing machine"
(which the corpus simply does not have) recall a pile of junk.

    python scripts/calibrate_retrieval.py              # print distributions + recommended thresholds
    python scripts/calibrate_retrieval.py --verbose    # also print what each query hit

Criterion: positives (things the corpus really has) must be kept, negatives
(things it really lacks) must be blocked. The script sweeps candidate thresholds
and reports "positive keep rate / negative block rate" for each, picking the
balance point.

Note: the confidence gate (retrieval_min_confidence) is useless against
single-leg recall -- RRF confidence measures "do the legs agree on top1" and is
constantly 1.0 when only one leg is active. So noise can only be blocked by the
recall-level score floors, i.e. the two values this script calibrates.
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401  # isort:skip

from app.config import settings
from app.db import SessionLocal, init_db
from app.models import ItemStatus
from app.services.item_index import get_item_index
from app.services.query_parser import parse_query

#: Positives A: bare product nouns -- the easiest case.
#: Checked against the live corpus: every one of these appears in at least one
#: listing name. A "positive" the corpus lacks would push the floors down.
POSITIVE_NOUN = [
    "自行车", "山地车", "折叠自行车", "机械键盘", "无线耳机", "无线鼠标",
    "笔记本电脑", "平板", "显示器", "充电宝", "智能手表", "路由器", "麦克风",
    "摄像头", "电子书阅读器", "游戏手柄", "蓝牙音箱",
    "物理教材", "微观经济学教材", "西班牙语教材", "线性代数教材", "备考资料",
    "瑜伽垫", "哑铃", "壶铃", "羽毛球拍", "网球拍", "帐篷", "徒步背包", "滑板",
    "羽绒服", "跑鞋", "衬衫", "行李箱", "托特包", "雨衣",
    "台灯", "微波炉", "小冰箱", "床垫", "沙发床", "斗柜", "地毯",
    "保温杯", "咖啡机", "空气炸锅", "电饭煲", "电压力锅", "收纳箱", "被子",
    "电吉他", "尤克里里", "小提琴", "长笛", "科学计算器", "图形计算器", "水彩",
]

#: Positives B: conversational descriptions -- this product's selling point;
#: a threshold set too high kills exactly this class, so it must be calibrated together
POSITIVE_NL = [
    "宿舍夏天用来降温的东西", "装得下笔记本电脑的包",
    "打球用的拍子", "冬天穿的保暖衣服", "备考用的书",
    "宿舍里能做饭的东西", "晚上看书用的灯", "周末出门用的包",
    "在家健身的器材", "上课记笔记用的",
    "上下课代步的东西", "睡起来舒服一点的",
    "冬天宿舍取暖用的", "在房间里放音乐的音箱",
    "打游戏用的东西", "装衣服的收纳箱", "喝水用的杯子",
    "学习时能隔音的耳机", "画画用的东西",
    "适合初学者的乐器",
]

POSITIVE = POSITIVE_NOUN + POSITIVE_NL

#: Things the corpus **really does not have** -- these should be blocked with "nothing found".
#: Careful not to use as negatives words that have a close relative in the corpus
#: (e.g. "扫地机器人" -- a 吸尘器 IS in the corpus): recalling the relative
#: is correct behavior, and treating it as a negative would skew the calibration.
#: All twenty were verified absent from every listing name and description.
NEGATIVE = [
    "洗衣机", "无人机", "皮划艇", "割草机", "婴儿车",
    "打印机", "投影仪", "空调", "显卡", "跑步机",
    "香水", "洗面奶", "猫粮", "行车记录仪", "机械表",
    "电动牙刷", "缝纫机", "鱼缸", "卷发棒", "隔热手套",
]


def probe(index, queries: list[str], candidates: set[int], top_k: int):
    """Run raw recall (no thresholds applied), returning each query's top1 score per leg."""
    rows = []
    for q in queries:
        parsed = parse_query(q)
        text = parsed.semantic_text or q
        vec = index.vector_search(text, top_k, candidates) if text.strip() else []
        bm = index.bm25_search(text, top_k, candidates) if text.strip() else []
        st = index.structured_search(parsed.filters_dict(), parsed.keywords, top_k, candidates)
        rows.append({
            "query": q,
            "vec_top1": vec[0][1] if vec else 0.0,
            "vec_top1_id": vec[0][0] if vec else None,
            "bm25_top1": bm[0][1] if bm else 0.0,
            "bm25_top1_id": bm[0][0] if bm else None,
            "struct_n": len(st),
        })
    return rows


def sweep(pos_scores: list[float], neg_scores: list[float], grid: list[float]) -> None:
    """Sweep thresholds, reporting positive keep rate and negative block rate."""
    print(f"    {'threshold':>10} {'pos kept':>10} {'neg blocked':>12}   {'combined':>8}")
    best = None
    for t in grid:
        keep = sum(1 for s in pos_scores if s >= t) / max(len(pos_scores), 1)
        block = sum(1 for s in neg_scores if s < t) / max(len(neg_scores), 1)
        score = keep + block
        star = ""
        if best is None or score > best[1]:
            best, star = (t, score), ""
        print(f"    {t:10.3f} {keep:9.0%} {block:11.0%}   {score:8.2f}{star}")
    if best:
        print(f"    -> best balance-point threshold ~= {best[0]:.3f} (pos kept + neg blocked = {best[1]:.2f})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true", help="print the item name each query hit")
    args = ap.parse_args()

    init_db()
    index = get_item_index()
    with SessionLocal() as db:
        if not index.records:
            index.rebuild_from_db(db)

    candidates = index.candidate_ids(statuses=ItemStatus.SEARCHABLE)
    print(f"Corpus size: {len(index.records)} searchable items\n")
    print(f"Current thresholds: vector_min_similarity={settings.vector_min_similarity} "
          f"vector_relative_window={settings.vector_relative_window}")
    print(f"                    bm25_min_score={settings.bm25_min_score} "
          f"bm25_relative_cutoff={settings.bm25_relative_cutoff}\n")

    top_k = settings.recall_top_k
    pos = probe(index, POSITIVE, candidates, top_k)
    neg = probe(index, NEGATIVE, candidates, top_k)

    def dump(title: str, rows: list[dict]) -> None:
        print(f"-- {title} --")
        print(f"  {'query':<44} {'vec top1':>9} {'BM25 top1':>9} {'struct':>7}   hit")
        for r in rows:
            hit = ""
            if args.verbose and r["vec_top1_id"]:
                rec = index.get_record(r["vec_top1_id"])
                hit = rec.name[:24] if rec else ""
            print(f"  {r['query']:<44} {r['vec_top1']:9.4f} {r['bm25_top1']:9.2f} "
                  f"{r['struct_n']:7}   {hit}")
        print()

    dump("positives (really in the corpus)", pos)
    dump("negatives (really not in the corpus)", neg)

    pv = sorted(r["vec_top1"] for r in pos)
    nv = sorted(r["vec_top1"] for r in neg)
    pb = sorted(r["bm25_top1"] for r in pos)
    nb = sorted(r["bm25_top1"] for r in neg)

    def stat(name, xs):
        if not xs:
            return
        print(f"  {name:<22} min={xs[0]:8.4f}  p10={xs[len(xs)//10]:8.4f}  "
              f"median={xs[len(xs)//2]:8.4f}  max={xs[-1]:8.4f}")

    print("-- score distributions --")
    stat("pos vector top1", pv)
    stat("neg vector top1", nv)
    stat("pos BM25 top1", pb)
    stat("neg BM25 top1", nb)
    print()

    print("-- vector threshold sweep (vector_min_similarity) --")
    sweep(pv, nv, [round(0.40 + i * 0.01, 3) for i in range(21)])
    print()
    print("-- BM25 threshold sweep (bm25_min_score) --")
    sweep(pb, nb, [round(x, 1) for x in range(0, 40, 2)])
    print()

    # ---- End-to-end joint sweep ----
    # Looking at one leg in isolation misleads: a positive cut by BM25 is often still
    # solidly held by the vector leg, so end-to-end it is unaffected. What really
    # matters is the pipeline's final verdict -- positives must return something,
    # negatives must return empty.
    print("-- end-to-end joint sweep (positives should hit / negatives should be empty) --")
    print(f"    {'vector':>7} {'BM25':>6} {'pos hits':>10} {'neg FPs':>10}   {'combined':>8}")
    best = None
    for vt in [0.42, 0.44, 0.45, 0.46, 0.47, 0.48, 0.50]:
        for bt in [5.0, 10.0, 14.0, 18.0, 20.0, 24.0]:
            hit = sum(1 for r in pos if decides_hit(index, r, vt, bt))
            fp = sum(1 for r in neg if decides_hit(index, r, vt, bt))
            keep = hit / max(len(pos), 1)
            clean = 1 - fp / max(len(neg), 1)
            total = keep + clean
            mark = ""
            if best is None or total > best[2]:
                best = (vt, bt, total)
                mark = ""
            print(f"    {vt:7.2f} {bt:6.1f} {keep:9.0%} {fp:8} rows {total:8.2f}{mark}")
    if best:
        print(f"\n    -> recommended: vector_min_similarity={best[0]}  bm25_min_score={best[1]}"
              f" (pos hits + neg clean = {best[2]:.2f})")
    return 0


def decides_hit(index, row: dict, vec_t: float, bm25_t: float) -> bool:
    """Simulate whether the query still returns items after the recall-level threshold cut.

    Mirrors retrieval._recall: absolute floor + relative window; all three legs
    empty means no result. The structured leg carries its own relative cut, so
    its actual row count is used directly.
    """
    vec_alive = row["vec_top1"] >= max(vec_t, row["vec_top1"] - settings.vector_relative_window)
    vec_alive = vec_alive and row["vec_top1"] >= vec_t
    bm_alive = row["bm25_top1"] >= max(bm25_t, row["bm25_top1"] * settings.bm25_relative_cutoff)
    bm_alive = bm_alive and row["bm25_top1"] >= bm25_t
    return bool(vec_alive or bm_alive or row["struct_n"] > 0)


if __name__ == "__main__":
    sys.exit(main())
