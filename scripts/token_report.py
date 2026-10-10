"""Summarize token usage by call site.

    python scripts/token_report.py
    python scripts/token_report.py --recent 20
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401  # isort:skip

from app.db import SessionLocal, init_db
from app.models import TokenUsageLog
from app.repositories import TokenUsageRepository
from app.services.llm import estimate_cost_usd


def _hit_rate(input_tokens: int, cache_read: int, cache_write: int) -> str:
    """Cache hit rate. The denominator is the total input side (incl. cache reads/writes)."""
    total = input_tokens + cache_read + cache_write
    return f"{100 * cache_read / total:.0f}%" if total else "-"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recent", type=int, default=0, help="also list the most recent N rows")
    args = parser.parse_args()

    init_db()
    with SessionLocal() as db:
        rows = TokenUsageRepository(db).summary_by_call_site()

        if not rows:
            print("token_usage_log has no records yet.")
            print("Hint: set ANTHROPIC_API_KEY, then hit \"AI generate\" once in the Inventory room,")
            print("      or type something very vague into Search (triggers call site B), then come back.")
            return 0

        width = max(len(r["call_site"]) for r in rows) + 2
        print(f"{'call site':<{width}}{'model':<24}{'calls':>6}{'input':>9}{'cache rd':>9}"
              f"{'output':>9}{'hit':>8}{'cost $':>9}")
        print("-" * (width + 74))
        total_in = total_out = total_calls = total_read = total_write = 0
        total_cost = 0.0
        for r in rows:
            total_in += r["input_tokens"]
            total_out += r["output_tokens"]
            total_read += r["cache_read_tokens"]
            total_write += r["cache_write_tokens"]
            total_calls += r["calls"]
            cost = estimate_cost_usd(r["model"], r["input_tokens"], r["output_tokens"],
                                     r["cache_read_tokens"], r["cache_write_tokens"])
            total_cost += cost
            hit = _hit_rate(r["input_tokens"], r["cache_read_tokens"], r["cache_write_tokens"])
            print(
                f"{r['call_site']:<{width}}{r['model']:<24}{r['calls']:>6}"
                f"{r['input_tokens']:>9}{r['cache_read_tokens']:>9}"
                f"{r['output_tokens']:>9}{hit:>8}{cost:>9.4f}"
            )
        print("-" * (width + 74))
        print(
            f"{'total':<{width}}{'':<24}{total_calls:>6}{total_in:>9}{total_read:>9}"
            f"{total_out:>9}"
            f"{_hit_rate(total_in, total_read, total_write):>8}{total_cost:>9.4f}"
        )
        if total_calls:
            print(f"\nAverage per call: input {total_in / total_calls:.0f} tok, output {total_out / total_calls:.0f} tok")

        if args.recent:
            print(f"\nMost recent {args.recent} rows:")
            recent = (
                db.query(TokenUsageLog)
                .order_by(TokenUsageLog.at.desc())
                .limit(args.recent)
                .all()
            )
            for row in recent:
                print(
                    f"  {row.at:%Y-%m-%d %H:%M:%S}  {row.call_site:<24} {row.model:<24}"
                    f" in={row.input_tokens:<6} cache_read={row.cache_read_tokens:<6}"
                    f" out={row.output_tokens}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
