"""Static checks on the task files — run before every eval and in CI.

    python eval/validate_tasks.py

Catches the mistakes that would otherwise surface as a wasted, paid run:
an unknown fixture or expectation key, a missing photo, a duplicate task id,
a message the chat endpoint would reject. Prints the composition table.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR / "harness"))

from world import FIXTURES  # noqa: E402

TURN_KEYS = {"route", "out_of_scope", "item", "no_foreign_items", "cards", "cards_not",
             "no_cards", "tools", "tools_not", "no_tools", "tool_args", "pending",
             "reply_has", "reply_not", "publish"}
DB_KEYS = {"pushes", "pushes_match_proposal", "status", "plans_created", "plan",
           "plans_cancelled", "plan_changed", "new_items", "new_item"}
FACES = ["意图路由", "认出商品", "工具选择与参数", "基于证据作答", "写操作", "发布", "会话状态"]
TYPES = ["典型", "边界", "对抗"]
#: Photos that are known to be supplied by hand, outside the repository.
EXTERNAL_PHOTOS = {"prohibited.jpg"}
#: publishing/schemas.py CONDITIONS — approve refuses any other value.
CONDITIONS = {"全新", "几乎全新", "成色很好", "成色良好", "成色一般"}


def main() -> int:
    errors: list[str] = []
    tasks = []
    for path in sorted((EVAL_DIR / "tasks").glob("*.yaml")):
        for t in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            tasks.append((path.name, t))

    ids = Counter(t["vars"]["task_id"] for _, t in tasks)
    errors += [f"重复的 task_id：{k}" for k, n in ids.items() if n > 1]
    missing_photos = set()
    for fname, t in tasks:
        v, meta = t.get("vars") or {}, t.get("metadata") or {}
        tid = v.get("task_id", "?")
        where = f"{fname} {tid}"
        if v.get("fixture") not in FIXTURES:
            errors.append(f"{where}：未知夹具 {v.get('fixture')}")
        if meta.get("face") not in FACES:
            errors.append(f"{where}：未知能力面 {meta.get('face')}")
        if meta.get("type") not in TYPES:
            errors.append(f"{where}：未知类型 {meta.get('type')}")
        if meta.get("risk") not in ("P0", "P1", "P2"):
            errors.append(f"{where}：未知风险等级 {meta.get('risk')}")
        turns = v.get("turns") or []
        if not turns:
            errors.append(f"{where}：没有对话")
        for n, step in enumerate(turns, start=1):
            if "say" not in step and "approve" not in step:
                errors.append(f"{where} 第{n}步：既不是 say 也不是 approve")
            approve = step.get("approve")
            if approve is not None and approve.get("condition") not in CONDITIONS:
                errors.append(f"{where} 第{n}步：批准上架的成色必须是 {sorted(CONDITIONS)} 之一")
            if approve is not None and not approve.get("price"):
                errors.append(f"{where} 第{n}步：批准上架要给价格")
            if len(step.get("say") or "") > 500:
                errors.append(f"{where} 第{n}步：消息超过 500 字，接口会拒收")
            photo = step.get("photo")
            if photo and not (EVAL_DIR / "fixtures" / "photos" / photo).exists():
                if photo in EXTERNAL_PHOTOS:
                    missing_photos.add(photo)
                else:
                    errors.append(f"{where} 第{n}步：照片不存在 {photo}")
            for k in (step.get("expect") or {}):
                if k not in TURN_KEYS:
                    errors.append(f"{where} 第{n}步：未知期望键 {k}")
        for k in (v.get("expect") or {}):
            if k not in TURN_KEYS:
                errors.append(f"{where}：未知期望键 {k}")
        for k in (v.get("db") or {}):
            if k not in DB_KEYS:
                errors.append(f"{where}：未知写入期望键 {k}")
        has_check = (v.get("expect") or v.get("db") or any(s.get("expect") for s in turns)
                     or t.get("assert"))
        if not has_check and meta.get("risk") != "P0":
            errors.append(f"{where}：除护栏外没有任何断言")

    by_face = Counter(t["metadata"]["face"] for _, t in tasks)
    by_type = Counter(t["metadata"]["type"] for _, t in tasks)
    print(f"task 总数：{len(tasks)}")
    print("类型：" + "、".join(f"{k} {by_type[k]}" for k in TYPES)
          + f"（典型占 {by_type['典型'] / max(1, len(tasks)):.0%}）")
    for face in FACES:
        sub = [t for _, t in tasks if t["metadata"]["face"] == face]
        c = Counter(t["metadata"]["type"] for t in sub)
        print(f"  {face}：{by_face[face]}（" + " / ".join(str(c[k]) for k in TYPES) + "）")
    p0 = sum(1 for _, t in tasks if t["metadata"]["risk"] == "P0")
    kf = [t["vars"]["task_id"] for _, t in tasks if t["metadata"].get("known_fail")]
    print(f"P0 task：{p0}；已知失败：{', '.join(kf) or '无'}")
    print(f"多轮 task：{sum(1 for _, t in tasks if len(t['vars']['turns']) > 1)}")
    for p in sorted(missing_photos):
        print(f"提醒：eval/fixtures/photos/{p} 需要手动放入，否则用到它的 task 记为环境错误")
    if by_type["典型"] * 2 < len(tasks):
        errors.append("典型 task 少于一半")
    for e in errors:
        print("错误：" + e)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
