"""Promptfoo provider: one test case = one trial in a fresh sandbox.

Promptfoo calls ``call_api`` once per test (and once per repeat). The trial runs
in a child process (``harness/run_trial.py``) because the app's settings, DB
engine and indexes are process-level and must be set before the app is
imported.

Returned ``output`` is the trial as a JSON string. Python graders parse it;
``llm-rubric`` assertions read only its ``judge_view`` through
``transform: JSON.parse(output).judge_view``.

A trial that never reached the agent (missing photo, failed precondition)
comes back as a provider ``error``, so Promptfoo counts it as an error rather
than a failed test — an environment fault is not an agent failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_DIR.parent
TRIAL = EVAL_DIR / "harness" / "run_trial.py"
TRACE_DIR = Path(os.environ.get("LOCSWAP_EVAL_TRACE_DIR", EVAL_DIR / "out" / "traces"))
TIMEOUT = int(os.environ.get("LOCSWAP_EVAL_TRIAL_TIMEOUT", "900"))
MARK = "@@RESULT@@"


def _replay(ref: str) -> dict:
    """Stored output of one finished trial (eval/replay.py)."""
    index = json.loads(Path(os.environ["LOCSWAP_EVAL_REPLAY"]).read_text(encoding="utf-8"))
    tid, k = ref.rsplit("#", 1)
    trial = index[tid][int(k)]
    if trial.get("error"):
        return {"error": trial["error"]}
    return {"output": trial["output"], "metadata": {"replayed": True}}


def call_api(prompt, options, context):
    vars_ = (context or {}).get("vars") or {}
    if os.environ.get("LOCSWAP_EVAL_REPLAY") and vars_.get("trial_ref"):
        return _replay(vars_["trial_ref"])
    spec = {
        "task_id": vars_.get("task_id", ""),
        "fixture": vars_.get("fixture", ""),
        "turns": vars_.get("turns") or [],
        "trace_dir": str(TRACE_DIR),
    }
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(TRIAL)],
            input=json.dumps(spec, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(PROJECT_ROOT), timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"环境错误：试次超过 {TIMEOUT} 秒未结束"}

    line = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith(MARK)), None)
    if line is None:
        tail = (proc.stderr or proc.stdout or "")[-1500:]
        return {"error": f"环境错误：试次没有返回结果（exit {proc.returncode}）\n{tail}"}
    result = json.loads(line[len(MARK):])
    if result.get("env_error"):
        return {"error": "环境错误：" + result["env_error"]}

    result["judge_view"] = judge_view(result)
    turns = [t for t in result.get("turns", []) if t.get("kind") == "say"]
    return {
        "output": json.dumps(result, ensure_ascii=False, default=str),
        "tokenUsage": {
            "prompt": sum(t.get("input_tokens", 0) for t in turns),
            "completion": sum(t.get("output_tokens", 0) for t in turns),
            "total": sum(t.get("input_tokens", 0) + t.get("output_tokens", 0) for t in turns),
        },
        "metadata": {
            "llm_calls": sum(t.get("llm_calls", 0) for t in turns),
            "seconds": result.get("seconds"),
            "routes": [t.get("route") for t in turns],
        },
    }


def judge_view(result: dict) -> str:
    """What an LLM judge sees: the conversation, then the backend facts the
    advisor had. The judge compares claims with facts; it never judges the
    market on its own."""
    names = (result.get("world") or {}).get("item_names") or {}
    lines = ["【对话】"]
    for t in result.get("turns", []):
        if t.get("kind") == "approve":
            lines.append(f"卖家：（在草稿卡上点了「批准上架」，表单 {json.dumps(t.get('form'), ensure_ascii=False)}）")
            lines.append(f"系统：{json.dumps(t.get('out'), ensure_ascii=False)[:600]}")
            continue
        photo = f"（附照片 {t['photo']}）" if t.get("photo") else ""
        lines.append(f"卖家：{t.get('say', '')}{photo}")
        lines.append(f"顾问：{t.get('reply', '')}")
    lines.append("")
    lines.append("【后端事实】")
    lines.append("卖家在售商品：" + ("、".join(names.values()) or "无"))
    for t in result.get("turns", []):
        if t.get("kind") != "say":
            continue
        lines.append(f"第 {t['n']} 轮：路由 {t.get('route') or '-'}；"
                     f"调用工具 {', '.join(t.get('used_tools') or []) or '无'}")
        cards = t.get("cards") or {}
        for kind in ("buyers", "pricing", "demand"):
            for c in cards.get(kind) or []:
                lines.append(f"  {kind} 卡片：{json.dumps(c, ensure_ascii=False)}")
        if t.get("pending_action"):
            pa = t["pending_action"]
            lines.append(f"  待卖家确认的动作：{pa.get('action')}（{pa.get('item_name', '')}，"
                         f"{pa.get('recipients', '')} 人）")
        if t.get("action_result"):
            lines.append(f"  已执行：{json.dumps(t['action_result'], ensure_ascii=False)[:400]}")
        if t.get("publish_findings"):
            lines.append(f"  合规审核：{json.dumps(t['publish_findings'], ensure_ascii=False)[:400]}")
        if cards.get("draft"):
            lines.append(f"  上架草稿：{json.dumps(cards['draft'], ensure_ascii=False)[:600]}")
    # Tool outputs go in whole (up to the trace's own cap): a judge that sees a
    # truncated output answers 「无法判断」 for every number past the cut.
    for call in result.get("tool_calls") or []:
        lines.append(f"工具 {call['tool']}（第 {call['turn']} 轮）入参 "
                     f"{json.dumps(call.get('args'), ensure_ascii=False)} → "
                     f"{call.get('output') or call.get('error') or ''}")
    diff = result.get("diff") or {}
    lines.append("实际写入：推送 {} 条；状态变化 {}；新建商品 {} 件；新建计划 {} 条；改动计划 {} 条".format(
        len(diff.get("pushes") or []), json.dumps(diff.get("status_changes") or {}, ensure_ascii=False),
        len(diff.get("new_items") or []), len(diff.get("plans_created") or []),
        len(diff.get("plans_changed") or [])))
    return "\n".join(lines)
