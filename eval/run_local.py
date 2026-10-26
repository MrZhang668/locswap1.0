"""Run a few tasks without Promptfoo — for debugging a task or a fix.

    python eval/run_local.py IR-04 WO-02 --workers 4

Uses the same provider and the same code graders as the Promptfoo config.
LLM-rubric assertions are not run here (they are listed as skipped), so a pass
here means "the code graders pass", not "the task passes".
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(EVAL_DIR / "graders"))

import build_tests  # noqa: E402
import checks  # noqa: E402
import provider  # noqa: E402


def load_tasks() -> dict[str, dict]:
    out = {}
    for path in sorted((EVAL_DIR / "tasks").glob("*.yaml")):
        for t in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            out[t["vars"]["task_id"]] = t
    return out


def run_one(task: dict) -> dict:
    ctx = {"vars": task["vars"]}
    resp = provider.call_api("", {}, ctx)
    if resp.get("error"):
        return {"task": task["vars"]["task_id"], "error": resp["error"][:500]}
    # The same assertions Promptfoo attaches to this task (build_tests.py).
    graders = [a["value"].rsplit(":", 1)[1] for a in build_tests.assertions_for(task)
               if a["type"] == "python"]
    results = {g: getattr(checks, g)(resp["output"], ctx) for g in dict.fromkeys(graders)}
    trial = json.loads(resp["output"])
    return {
        "task": task["vars"]["task_id"],
        "pass": all(r["pass"] for r in results.values()),
        "failed": {g: r["reason"] for g, r in results.items() if not r["pass"]},
        "rubrics_skipped": sum(1 for a in task.get("assert") or [] if a.get("type") == "llm-rubric"),
        "routes": [t.get("route") for t in trial["turns"] if t.get("kind") == "say"],
        "last_reply": next((t.get("reply") for t in reversed(trial["turns"])
                            if t.get("kind") == "say"), "")[:300],
        "seconds": trial.get("seconds"),
        "llm_calls": resp.get("metadata", {}).get("llm_calls"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_ids", nargs="+")
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()
    tasks = load_tasks()
    unknown = [t for t in args.task_ids if t not in tasks]
    if unknown:
        raise SystemExit(f"没有这些 task：{unknown}")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for r in pool.map(run_one, [tasks[t] for t in args.task_ids]):
            print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
