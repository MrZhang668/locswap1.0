"""Promptfoo test generator: each task gets only the assertions it declares.

    tests: file://build_tests.py:create_tests

A grader is attached only when the task names the aspect it checks (``route``
→ 意图识别准确率, ``db`` → 写操作正确率, ...). Without this every task carried
all twelve code assertions and the ones it did not declare showed up in the
web view as a 1.00 「不适用」 — indistinguishable from a real pass.

The three guards that need no declaration (unauthorised write, false
completion, buyer PII) go on every task. The task's own assertions (LLM
rubrics) are kept as written.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).resolve().parent
CHECKS = "file://graders/checks.py:"

ALWAYS = [
    ("guard_unconsented_write", "护栏-未授权写操作"),
    ("guard_false_completion", "护栏-虚假完成"),
    ("guard_buyer_pii", "护栏-隐私泄露"),
]

#: (grader, metric, expectation keys that switch it on). Order is the order
#: the assertions appear in the web view.
DECLARED = [
    ("check_route", "意图识别准确率", {"route", "out_of_scope"}),
    ("check_item", "商品识别准确率", {"item", "no_foreign_items"}),
    ("check_tools", "工具选择准确率", {"tools", "tools_not", "no_tools", "tool_args"}),
    ("check_cards", "任务完成", {"cards", "cards_not", "no_cards"}),
    ("check_pending", "写操作正确率", {"pending"}),
    ("check_reply", "回复规则", {"reply_has", "reply_not"}),
    ("check_publish", "发布", {"publish"}),
]


def _declared_keys(vars_: dict) -> set[str]:
    keys = set(vars_.get("expect") or {})
    for step in vars_.get("turns") or []:
        keys |= set(step.get("expect") or {})
    return keys


def _blocks_expected(vars_: dict) -> bool:
    exps = [vars_.get("expect") or {}] + [s.get("expect") or {} for s in vars_.get("turns") or []]
    return any((e.get("publish") or {}).get("blocked") for e in exps)


def _assert(grader: str, metric: str) -> dict:
    return {"type": "python", "value": CHECKS + grader, "metric": metric}


def assertions_for(task: dict) -> list[dict]:
    vars_ = task.get("vars") or {}
    keys = _declared_keys(vars_)
    out = [_assert(g, m) for g, m in ALWAYS]
    if _blocks_expected(vars_):
        out.append(_assert("check_publish_block", "护栏-违规漏放"))
    out += [_assert(g, m) for g, m, on in DECLARED if keys & on]
    if vars_.get("db") is not None:
        out.append(_assert("check_writes", "写操作正确率"))
    return out + list(task.get("assert") or [])


def load_tasks() -> list[dict]:
    tasks = []
    for path in sorted((EVAL_DIR / "tasks").glob("*.yaml")):
        tasks += yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return tasks


def create_tests(config=None):
    replay = os.environ.get("LOCSWAP_EVAL_REPLAY")
    if replay:
        return replay_tests(Path(replay))
    return [{**task, "assert": assertions_for(task)} for task in load_tasks()]


def replay_tests(index_path: Path) -> list[dict]:
    """One test per saved trial (see replay.py). The provider returns the
    stored output for ``trial_ref``; run with --repeat 1."""
    trials = json.loads(index_path.read_text(encoding="utf-8"))
    out = []
    for task in load_tasks():
        tid = task["vars"]["task_id"]
        for k in range(len(trials.get(tid, []))):
            out.append({**task, "description": f"{task['description']} #{k + 1}",
                        "vars": {**task["vars"], "trial_ref": f"{tid}#{k}"},
                        "assert": assertions_for(task)})
    return out
