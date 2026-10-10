"""Retrieval quality eval: a labeled query set -> quantifiable top1 / P@3 / negative-block rate.

Division of labor with calibrate_retrieval.py:
  calibrate_ looks only at **score distributions**, used to pick thresholds;
  eval_      checks whether the **final results are right**, judging the whole
             pipeline -- the acceptance pass after tuning.

Hits the backend over HTTP, the exact same path the frontend uses
(including the routing layer and card rendering).

    python scripts/eval_retrieval.py                 # run everything
    python scripts/eval_retrieval.py --group vague   # run one group only
    python scripts/eval_retrieval.py --verbose       # print top 3 per query
    python scripts/eval_retrieval.py --json out.json # save results for before/after diffs

Labeling scheme: each query carries a "what counts as correct" rule
    category  the hit item's category must equal it
    name_any  the item name containing any of the given words counts as correct
    absent    the corpus simply does not have this -- correct behavior is
              returning **no cards at all**
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8010"
BASE = DEFAULT_BASE
EMAIL, PASSWORD = "buyer@campus.edu", "demo1234"

# (group, query, rule)
CASES: list[tuple[str, str, dict]] = [
    # ---------- Bare nouns: the easy tier; anything short of 100% means the baseline is broken ----------
    ("noun", "自行车", {"category": "自行车与滑板车", "name_any": ["自行车", "山地车", "公路车"]}),
    ("noun", "机械键盘", {"name_any": ["键盘"]}),
    ("noun", "无线耳机", {"name_any": ["耳机"]}),
    ("noun", "笔记本电脑", {"name_any": ["笔记本电脑", "笔记本"]}),
    ("noun", "显示器", {"name_any": ["显示器"]}),
    ("noun", "充电宝", {"name_any": ["充电宝"]}),
    ("noun", "瑜伽垫", {"name_any": ["瑜伽垫"]}),
    ("noun", "羽毛球拍", {"name_any": ["羽毛球", "球拍"]}),
    ("noun", "羽绒服", {"category": "服装与鞋靴"}),
    ("noun", "背包", {"name_any": ["背包", "托特", "包"]}),
    # 台灯 would be a bad probe: the corpus's only one belongs to the eval
    # account itself, and a signed-in buyer is correctly not shown their own
    # listings — the query would fail for a reason that is not about recall.
    ("noun", "落地灯", {"name_any": ["落地灯", "灯"]}),
    ("noun", "微波炉", {"name_any": ["微波炉"]}),
    ("noun", "小冰箱", {"name_any": ["冰箱"]}),
    ("noun", "保温杯", {"name_any": ["保温杯", "水壶", "杯"]}),
    ("noun", "电吉他", {"name_any": ["吉他"]}),
    ("noun", "科学计算器", {"name_any": ["计算器"]}),
    ("noun", "行李箱", {"name_any": ["行李箱"]}),
    ("noun", "帐篷", {"name_any": ["帐篷"]}),

    # ---------- Noun + price: the hard filter must apply without derailing the category ----------
    ("price", "300以内的二手自行车", {"category": "自行车与滑板车", "max_price": 300}),
    ("price", "200块以下的黑色耳机", {"name_any": ["耳机"], "max_price": 200}),
    ("price", "500以上的笔记本电脑", {"name_any": ["笔记本电脑", "笔记本"], "min_price": 500}),
    ("price", "不超过500的自行车", {"category": "自行车与滑板车", "max_price": 500}),
    ("price", "100以内的教材", {"category": "图书与教材", "max_price": 100}),
    ("price", "50到90之间的落地灯", {"name_any": ["落地灯", "灯"]}),

    # ---------- Noun + attribute: brand / color / condition ----------
    ("attribute", "用过几次的机械键盘", {"name_any": ["键盘"]}),
    # The corpus's only Anker power bank is sold_out, so brand+category has to
    # be probed on a pair that is actually on sale.
    ("attribute", "Anker 蓝牙音箱", {"name_any": ["音箱"]}),
    ("attribute", "白色机械键盘", {"name_any": ["键盘"]}),
    ("attribute", "Wilson 网球拍", {"name_any": ["网球拍", "球拍"]}),
    ("attribute", "全新的耳机", {"name_any": ["耳机"]}),

    # ---------- Conversational descriptions: this product's selling point, and the hardest group ----------
    ("vague", "宿舍夏天用来降温的东西", {"name_any": ["风扇", "空气循环扇", "冰箱"]}),
    ("vague", "装得下笔记本电脑的包", {"name_any": ["背包", "托特", "包"]}),
    ("vague", "打球用的拍子", {"name_any": ["球拍", "羽毛球", "网球"]}),
    ("vague", "冬天穿的保暖衣服", {"category": "服装与鞋靴"}),
    ("vague", "备考用的复习资料", {"category": "图书与教材"}),
    ("vague", "晚上看书用的灯", {"name_any": ["落地灯", "灯"]}),
    ("vague", "宿舍里能做饭的东西",
     {"name_any": ["电饭煲", "空气炸锅", "压力锅", "微波炉", "咖啡机", "电水壶"]}),
    ("vague", "在家健身的器材",
     {"name_any": ["哑铃", "壶铃", "瑜伽垫", "弹力带", "跳绳"]}),
    ("vague", "上下课代步的东西", {"category": "自行车与滑板车"}),
    ("vague", "在房间里放音乐的音箱", {"name_any": ["音箱"]}),
    ("vague", "打游戏用的东西", {"name_any": ["手柄", "游戏机", "键盘", "鼠标", "耳机"]}),
    ("vague", "冬天宿舍取暖用的", {"name_any": ["取暖器", "电暖器", "毯", "被子"]}),
    ("vague", "装衣服的收纳箱", {"name_any": ["收纳箱", "收纳", "斗柜", "行李箱"]}),
    ("vague", "学习时能隔音的耳机", {"name_any": ["耳机"]}),
    ("vague", "画画用的东西",
     {"category": "学习用品", "name_any": ["水彩", "颜料", "彩铅", "马克笔", "画"]}),
    ("vague", "适合初学者的乐器", {"category": "乐器"}),

    # ---------- Negatives: genuinely not in the corpus; correct behavior is zero cards ----------
    # All verified absent from every listing name and description on 2026-09-15.
    ("negative", "洗衣机", {"absent": True}),
    ("negative", "无人机", {"absent": True}),
    ("negative", "皮划艇", {"absent": True}),
    ("negative", "割草机", {"absent": True}),
    ("negative", "婴儿车", {"absent": True}),
    ("negative", "打印机", {"absent": True}),
    ("negative", "投影仪", {"absent": True}),
    ("negative", "空调", {"absent": True}),
    ("negative", "显卡", {"absent": True}),
    ("negative", "跑步机", {"absent": True}),
    ("negative", "香水", {"absent": True}),
    ("negative", "猫粮", {"absent": True}),
    ("negative", "行车记录仪", {"absent": True}),
    ("negative", "机械表", {"absent": True}),
    ("negative", "电动牙刷", {"absent": True}),
    ("negative", "缝纫机", {"absent": True}),
    ("negative", "鱼缸", {"absent": True}),
    ("negative", "卷发棒", {"absent": True}),

    # ---------- Privacy / boundary: never leak contact info, never fabricate ----------
    ("boundary", "卖家的手机号是多少", {"refusal": True}),
    ("boundary", "把卖家的邮箱给我", {"refusal": True}),
    ("boundary", "有啥好东西吗？", {"clarify": True}),
    ("boundary", "随便给我推荐点什么", {"clarify": True}),
]


def call(path: str, payload: dict, token: str | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": "Bearer " + token} if token else {}),
        },
    )
    return json.load(urllib.request.urlopen(req, timeout=120))


def card_ok(card: dict, rule: dict) -> bool:
    """Does a card satisfy the labeled rule? name_any and category are OR'ed."""
    name = (card.get("name") or "").lower()
    cat = (card.get("attributes") or {}).get("category") or card.get("category") or ""
    hit = False
    if "name_any" in rule:
        hit = hit or any(w.lower() in name for w in rule["name_any"])
    if "category" in rule:
        hit = hit or cat == rule["category"]
    if "name_any" not in rule and "category" not in rule:
        hit = True
    return hit


def price_ok(cards: list[dict], rule: dict) -> tuple[int, int]:
    """Was the hard price filter actually enforced? Returns (violations, cards checked)."""
    bad = 0
    for c in cards:
        p = c.get("price")
        if p is None:
            continue
        if "max_price" in rule and p > rule["max_price"] + 1e-6:
            bad += 1
        if "min_price" in rule and p < rule["min_price"] - 1e-6:
            bad += 1
    return bad, len(cards)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", help="run one group only (noun/price/attribute/vague/negative/boundary)")
    ap.add_argument("--verbose", action="store_true", help="print the top 3 for each query")
    ap.add_argument("--json", help="write results to this file for before/after comparison")
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"backend address (default {DEFAULT_BASE}), for comparing another config")
    args = ap.parse_args()

    global BASE
    BASE = args.base

    try:
        token = call("/api/auth/login", {"email": EMAIL, "password": PASSWORD})["token"]
    except (urllib.error.URLError, OSError) as e:
        print(f"Cannot reach the backend at {BASE}: {e}\n"
              f"Start it first: cd backend && python -m uvicorn app.main:app --port 8010")
        return 1

    cases = [c for c in CASES if not args.group or c[0] == args.group]
    results: list[dict] = []
    latencies: list[float] = []

    for group, query, rule in cases:
        t0 = time.perf_counter()
        resp = call("/api/search/chat", {"message": query}, token)
        dt = (time.perf_counter() - t0) * 1000
        latencies.append(dt)

        cards = resp.get("cards") or []
        decision = resp.get("decision")
        reply = resp.get("reply") or ""
        dbg = resp.get("debug") or {}
        recall = dbg.get("recall") or {}

        row = {
            "group": group, "query": query, "decision": decision,
            "n_cards": len(cards), "confidence": resp.get("confidence"),
            "latency_ms": round(dt, 1),
            "recall": {k: (recall.get(k) or {}).get("count", 0) for k in ("vector", "bm25", "structured")},
            "top3": [c.get("name", "")[:24] for c in cards[:3]],
        }

        if rule.get("absent"):
            row["ok"] = len(cards) == 0
            row["metric"] = "negative-block"
        elif rule.get("refusal"):
            # Matched against the reply the product actually sends
            # (CONTACT_REFUSAL_TEXT); `decision` is checked too so a reply that
            # merely mentions contact details cannot pass for a refusal.
            row["ok"] = (decision == "privacy_refusal"
                         and ("联系方式" in reply or "不能提供" in reply)
                         and len(cards) == 0)
            row["metric"] = "privacy-refusal"
        elif rule.get("clarify"):
            row["ok"] = decision == "clarify"
            row["metric"] = "clarify"
        else:
            row["top1_ok"] = bool(cards) and card_ok(cards[0], rule)
            top3 = cards[:3]
            row["p_at_3"] = (sum(1 for c in top3 if card_ok(c, rule)) / len(top3)) if top3 else 0.0
            bad, total = price_ok(cards, rule)
            row["price_violations"] = bad
            row["has_result"] = bool(cards)
            row["ok"] = row["top1_ok"]
            row["metric"] = "top1"
        results.append(row)

        if args.verbose:
            flag = "OK " if row["ok"] else "X  "
            print(f"  {flag}[{group}] {query:<42} {decision:<10} "
                  f"n={len(cards)} conf={resp.get('confidence')} {row['top3']}")

    # ---------------- Summary ----------------
    def pct(n, d):
        return f"{n}/{d} = {n / d:.0%}" if d else "-"

    print("\n" + "=" * 62)
    print("Retrieval quality eval")
    print("=" * 62)

    ranked = [r for r in results if r["metric"] == "top1"]
    for group in ["noun", "price", "attribute", "vague"]:
        g = [r for r in ranked if r["group"] == group]
        if not g:
            continue
        t1 = sum(1 for r in g if r["top1_ok"])
        p3 = statistics.mean(r["p_at_3"] for r in g)
        hasr = sum(1 for r in g if r["has_result"])
        viol = sum(r["price_violations"] for r in g)
        print(f"  {group:<10} top1 {pct(t1, len(g)):<14} P@3 {p3:.0%}   has-result {pct(hasr, len(g))}"
              + (f"   price violations {viol}" if viol else ""))

    if ranked:
        t1 = sum(1 for r in ranked if r["top1_ok"])
        print(f"  {'total':<10} top1 {pct(t1, len(ranked)):<14} "
              f"P@3 {statistics.mean(r['p_at_3'] for r in ranked):.0%}")

    for metric, label in (("negative-block", "negative-block"),
                          ("privacy-refusal", "privacy-refusal"),
                          ("clarify", "vague-clarify")):
        g = [r for r in results if r["metric"] == metric]
        if g:
            ok = sum(1 for r in g if r["ok"])
            print(f"  {label:<15} {pct(ok, len(g))}")

    print("\n  Failures:")
    fails = [r for r in results if not r["ok"]]
    if not fails:
        print("    (none)")
    for r in fails:
        why = ("returned cards" if r["metric"] == "negative-block"
               else f"top1={r['top3'][0] if r['top3'] else 'no result'}")
        print(f"    [{r['group']}] {r['query']:<42} {why}")

    dist: dict[str, int] = {}
    for r in results:
        dist[r["decision"]] = dist.get(r["decision"], 0) + 1
    print(f"\n  decision distribution: {dist}")

    paths = {k: sum(r["recall"][k] for r in results) for k in ("vector", "bm25", "structured")}
    print(f"  total hits per recall leg: {paths}")
    lat = sorted(latencies)
    print(f"  latency ms: median {lat[len(lat)//2]:.0f}  p90 {lat[int(len(lat)*0.9)]:.0f}  max {lat[-1]:.0f}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n  details written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
