"""The 500-query seller-advisor intent set — assembled from the parts.

Why synthetic: the platform has no query log table yet (see
docs/buyer_intent_session_log_plan.md — `SearchSession` is still 待实施), so a
genuine "last 7 days" extract does not exist. These 500 stand in for one week
of seller-advisor traffic and are built to a coverage spec rather than sampled
from a log. That is a real limitation and the report says so; what it buys is
control — every intent leaf, expression style and seller state gets a
guaranteed cell count instead of whatever a week happened to contain.

Counting rule (§5 of the brief): one message = one row. No de-duplication by
user, no session grouping. A seller who asks the same thing twice is two rows.

500 + 30 + 4: part_f was added after the first pass and is counted separately. The
original 500 keep their constructed distribution untouched so the coverage
pivots and any earlier run stay comparable; family F is a capability probe
whose gold sequence the current architecture cannot produce, and folding it
into the headline accuracy would report an architecture gap as a model error.
The 4 extra rows are E1 positive controls — see part_cde.
"""

from __future__ import annotations

import part_a  # noqa: F401  side effect: appends into _base.ROWS
import part_b  # noqa: F401
import part_cde  # noqa: F401
import part_f  # noqa: F401
from _base import ROWS

EXPECTED_TOTAL = 534


def load() -> list[dict]:
    """Return the set with stable ids assigned in declaration order."""
    if len(ROWS) != EXPECTED_TOTAL:
        raise SystemExit(f"dataset is {len(ROWS)} rows, expected {EXPECTED_TOTAL}")
    out = []
    for i, r in enumerate(ROWS, start=1):
        row = dict(r)
        row["qid"] = f"Q{i:03d}"
        if len(row["text"]) > 500:
            raise SystemExit(f"{row['qid']} exceeds the API's 500-char message cap")
        out.append(row)
    return out


if __name__ == "__main__":
    rows = load()
    print(f"{len(rows)} queries")
