"""Merge run + assertions + judge into a reviewable workbook.

    python scripts/badcase_eval/export805.py --run runs/full.jsonl \
        --scored runs/full.scored.json --judged runs/full.judged.json \
        --out "BadCase表_805跑批结果_v1.xlsx"

Written as a fresh workbook rather than a new sheet in the source file: the
source carries 22 anchored screenshots, and a new sheet means rewriting
workbook.xml, its rels and [Content_Types].xml inside that zip. A separate
results file costs one extra file and risks nothing.

The verdict column is the point of the whole run, so it is computed in one
place and spelled out: 是 (a real badcase — some assertion or the judge said
so), 否 (everything that applied to this row passed), 未执行 (no fixture, and
therefore no claim either way).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER = [
    "评测Case编号", "是否为Badcase", "数据有效性", "失败断言", "Case总类", "Case一级子分类",
    "Case二级子分类", "用例类型", "优先级", "商品品类", "目标商品",
    "前置条件/会话上下文", "测试输入（用户query）", "预期系统行为（判定标准）",
    "预期路由", "实际路由", "路由判定",
    "预期scenario", "实际scenario", "场景判定",
    "预期工具调用", "实际调用工具", "工具判定", "缺失的工具",
    "是否发生写操作", "未授权写操作",
    "裁判结论", "裁判证据", "裁判理由", "模型回复", "未执行原因",
]
WIDTHS = [13, 13, 14, 22, 20, 22, 36, 10, 8, 13, 14, 38, 30, 46,
          22, 14, 10, 18, 14, 10, 36, 30, 10, 20, 12, 12,
          10, 40, 28, 60, 40]

#: Families whose expected outcome is a write.
WRITE_EXPECTED = {"明确同意未被识别", "确认后正确执行", "推送执行偏差"}

P0 = {"写操作与确认", "安全与隐私"}
P2 = {"图像识别", "模型返回结果", "数据与分析口径", "鲁棒性与异常输入"}


def priority(rec):
    if rec.get("用例类型") == "回归基线":
        return "P1"
    if rec.get("总类") in P0:
        return "P0"
    return "P2" if rec.get("总类") in P2 else "P1"


def tick(v):
    return "" if v is None else ("✓" if v else "✗")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--scored", required=True)
    ap.add_argument("--judged", required=True)
    ap.add_argument("--visible", default="", help="agent 可见商品名 JSON（夹具有效性标记用）")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    # Items the agent can actually see. `list_my_items` truncates to its
    # `limit` (20 by default) and the eval seller was seeded with 101 listings,
    # so 81 products are unreachable: any case targeting one of them fails at
    # item_ref resolution for a reason that belongs to the fixture, not to the
    # system. Those rows are marked rather than dropped — the run happened, its
    # result just does not support a claim about the product.
    visible = set(json.loads(Path(args.visible).read_text(encoding="utf-8")))         if args.visible else set()

    run = [json.loads(line) for line in Path(args.run).open(encoding="utf-8")]
    scored = json.loads(Path(args.scored).read_text(encoding="utf-8"))["rows"]
    judged = {r["qid"]: r for r in
              json.loads(Path(args.judged).read_text(encoding="utf-8"))["results"]}

    wb = Workbook()
    ws = wb.active
    ws.title = "跑批结果"
    ws.append(HEADER)

    counts = Counter()
    fail_by_lvl2: dict[tuple, Counter] = defaultdict(Counter)

    for rec in run:
        qid = rec["qid"]
        s = scored.get(qid, {})
        j = judged.get(qid, {})
        turn = rec.get("turn") or {}

        # Same rule as the board (dashboard_pm.py): a row is a badcase when the
        # OUTCOME was wrong, not when an internal checkpoint differed from the
        # path the case author happened to write down. The checkpoint columns
        # stay in the sheet as diagnosis — they are how you tell a routing
        # problem from a wording problem — but they do not by themselves
        # condemn a row. Keeping this in sync matters: a sheet and a board that
        # disagree on the headline count are worse than either alone.
        wrote = bool((turn.get("action_result") or {}).get("succeeded"))
        # The intent model's answer was cut off mid-sentence, so the system
        # never actually decided anything — the row cannot be called a badcase
        # OR a pass. Counting it either way would move a rate on the strength
        # of a truncation. It gets its own verdict, and the board leaves it out
        # of every denominator for the same reason.
        trunc = (turn.get("router_fallback") or "").startswith("router call failed")
        if rec.get("skip"):
            verdict, failed = "未执行", ""
        elif rec.get("error"):
            verdict, failed = "是", "运行报错"
        elif trunc:
            verdict, failed = "无法判定", "意图识别输出被截断"
        else:
            bad = []
            if s.get("route_ok") is False:
                bad.append("路由")
            if s.get("scenario_ok") is False:
                bad.append("场景")
            if s.get("tools_ok") is False:
                bad.append("工具")
            if s.get("no_unauthorised_write") is False:
                bad.append("未授权写")
            if j.get("verdict") == "fail":
                bad.append("行为判定")
            failed = "+".join(bad)
            # Strict rule, same as the board: any checkpoint that did not do
            # what the case asked for counts, even when the reply came out
            # acceptable anyway. Column D says which checkpoint, so a reader
            # who wants only the "seller actually got a bad answer" subset can
            # filter on 行为判定.
            if rec.get("一级") in WRITE_EXPECTED and not wrote:
                is_bad = True               # 卖家点了头却没发出去
            else:
                is_bad = bool(bad)
            verdict = "是" if is_bad else "否"

        # Fixtures v2 keeps every holding inside the read window, so the old
        # contamination check finds nothing; truncation is what still needs
        # flagging, and `trunc` above already found it.
        if rec.get("skip"):
            valid = ""
        elif trunc:
            valid = "无法判定(意图识别输出被截断)"
        else:
            valid = "有效"
        counts[verdict] += 1
        counts[valid or "未执行"] += 1
        if verdict == "是":
            fail_by_lvl2[(rec.get("总类"), rec.get("一级"), rec.get("二级"))][failed] += 1

        ws.append([
            qid, verdict, valid, failed,
            rec.get("总类", ""), rec.get("一级", ""), rec.get("二级", ""),
            rec.get("用例类型", ""), priority(rec),
            rec.get("品类", ""), rec.get("商品", ""),
            rec.get("前置", ""), rec.get("text", ""), rec.get("预期", ""),
            rec.get("路由原文", ""), turn.get("route", ""), tick(s.get("route_ok")),
            rec.get("scen原文", ""), turn.get("scenario", ""), tick(s.get("scenario_ok")),
            rec.get("工具原文", ""), ", ".join(turn.get("used_tools") or []),
            tick(s.get("tools_ok")), ", ".join(s.get("tools_missing") or []),
            "是" if s.get("wrote") else ("否" if s.get("status") == "ran" else ""),
            tick(s.get("no_unauthorised_write")),
            j.get("verdict", ""), j.get("evidence", ""), j.get("reason", ""),
            (turn.get("reply") or "")[:1500],
            rec.get("skip", ""),
        ])

    hdr_fill = PatternFill("solid", fgColor="DDEBF7")
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = hdr_fill
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(HEADER))}{ws.max_row}"
    for i, w in enumerate(WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ---------------- summary sheet ----------------
    s2 = wb.create_sheet("失败聚合")
    s2.append(["Case总类", "一级子分类", "二级子分类", "badcase数", "失败断言分布"])
    for (top, l1, l2), c in sorted(fail_by_lvl2.items(), key=lambda kv: -sum(kv[1].values())):
        s2.append([top, l1, l2, sum(c.values()),
                   "; ".join(f"{k}×{v}" for k, v in c.most_common())])
    for c in s2[1]:
        c.font = Font(bold=True)
        c.fill = hdr_fill
    s2.freeze_panes = "A2"
    for i, w in enumerate([20, 24, 40, 11, 40], 1):
        s2.column_dimensions[get_column_letter(i)].width = w

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    wb.save(args.out)
    print(f"写入 {args.out}")
    print("  ", dict(counts))


if __name__ == "__main__":
    main()
