"""Batch 0 database cleanup: drop dead tables, clear the polluted traffic counters.

Two things, both decided in the 2026-09-14 architecture review:

1. **Drop `agent_episode` / `agent_action` / `agent_knowledge` / `agent_outcome`.**
   All four are empty, none appears in `models.py`, and nothing in backend/,
   scripts/ or frontend/ references them — they are left over from an earlier
   design (docs/agent_dev_prompt.md). Dead tables in a schema get mistaken for
   live ones.

2. **Clear `item_traffic`.** `entered_candidates` had accumulated 573,101 across
   548 rows because the retrieval path bumps it once per item per search, which
   makes the number a count of searches, not of interest. The impressions and
   clicks in there came from our own debugging, not from simulated users. Batch 1
   rebuilds this table by aggregating the new `demand_event` log per day, so the
   rows are not worth migrating. Deleting them is safe: `ItemTrafficRepository`
   upserts and `stats()` returns a zeroed row when one is missing.

Prints a before/after count and asks for confirmation unless --yes is given.

    python scripts/cleanup_batch0.py            # dry run, shows what would change
    python scripts/cleanup_batch0.py --apply
"""

from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401  injects backend into sys.path

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402

DEAD_TABLES = ("agent_episode", "agent_action", "agent_knowledge", "agent_outcome")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write; without it this is a dry run")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        existing = {
            r[0] for r in db.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }

        print("--- dead tables ---")
        to_drop = []
        for t in DEAD_TABLES:
            if t not in existing:
                print(f"  {t:18} absent already")
                continue
            n = db.execute(text(f'SELECT count(*) FROM "{t}"')).scalar_one()
            if n:
                # Refuse rather than delete data nobody expected to be there.
                print(f"  {t:18} has {n} rows — NOT dropping, inspect it first")
                continue
            print(f"  {t:18} 0 rows -> drop")
            to_drop.append(t)

        print("\n--- item_traffic ---")
        row = db.execute(text(
            "SELECT count(*), coalesce(sum(impressions),0), coalesce(sum(entered_candidates),0), "
            "coalesce(sum(pushed),0), coalesce(sum(clicks),0) FROM item_traffic"
        )).one()
        print(f"  rows={row[0]}  impressions={row[1]}  entered_candidates={row[2]}  "
              f"pushed={row[3]}  clicks={row[4]}")

        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply to perform the above.")
            return 0

        for t in to_drop:
            db.execute(text(f'DROP TABLE "{t}"'))
        deleted = db.execute(text("DELETE FROM item_traffic")).rowcount
        db.commit()

        print(f"\napplied: dropped {len(to_drop)} table(s), deleted {deleted} item_traffic row(s)")
        left = {
            r[0] for r in db.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        print("remaining agent_* tables:", sorted(t for t in left if t.startswith("agent_")) or "none")
        print("item_traffic rows now:",
              db.execute(text("SELECT count(*) FROM item_traffic")).scalar_one())
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
