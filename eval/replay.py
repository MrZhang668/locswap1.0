"""Re-grade finished trials without re-running the agent.

    python eval/replay.py eval/out/run.json eval/out/p0.json --rerun eval/out/rerun_*.json
    npx promptfoo@0.125.0 eval -c eval/replay.yaml -o eval/out/replay.json

Step one collects every saved trial into ``eval/out/replay/trials.json`` (a
later ``--rerun`` file replaces all earlier trials of the tasks it contains).
Step two runs a Promptfoo eval in which each saved trial is one test: the
provider hands back the stored output and the assertions are attached the
same way ``build_tests.py`` does today.

Use it when the graders or rubrics change and the agent did not: only the
LLM judge is called again.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
INDEX = EVAL_DIR / "out" / "replay" / "trials.json"


def _load(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    results = data.get("results", data)
    return results.get("results", []) if isinstance(results, dict) else results


def _trial(r: dict) -> dict:
    response = r.get("response") or {}
    error = response.get("error") or (r.get("error") if r.get("failureReason") == 2 else "")
    return {"output": None if error else response.get("output"), "error": error or ""}


def _task_id(r: dict) -> str:
    return (r.get("vars") or (r.get("testCase") or {}).get("vars") or {}).get("task_id", "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+")
    ap.add_argument("--rerun", nargs="*", default=[])
    args = ap.parse_args()

    trials: dict[str, list[dict]] = {}
    for path in args.results:
        for r in _load(path):
            trials.setdefault(_task_id(r), []).append(_trial(r))
    replaced: dict[str, list[dict]] = {}
    for path in args.rerun:
        for r in _load(path):
            replaced.setdefault(_task_id(r), []).append(_trial(r))
    trials.update(replaced)

    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(trials, ensure_ascii=False), encoding="utf-8")
    total = sum(len(v) for v in trials.values())
    print(f"{len(trials)} 个 task、{total} 个试次 -> {INDEX}"
          f"（其中 {len(replaced)} 个 task 用的是补跑结果）")


if __name__ == "__main__":
    main()
