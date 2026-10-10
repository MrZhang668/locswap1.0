"""Buyer-side standing-want endpoints — "notify me when it's in" and
"things I'm waiting for".

This path is **zero LLM** end to end (constraint C1): explicit creation goes
through `parse_query`'s regex + dictionary parsing, and "notify me when it's in"
simply reuses the want E2 already captured — no parsing at all.

⚠️ This router returns the **buyer's own** wants, so raw_query is allowed here.
C8 constrains seller-side outlets — do not reuse this schema there.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import WantCreateIn, WantOut
from ..services.views import build_want
from ..services.wants import (
    create_explicit_want,
    dismiss_want,
    list_wants,
    promote_to_explicit,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/wants", tags=["wants"])


@router.get("", response_model=list[WantOut])
def my_wants(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[WantOut]:
    """Things I'm waiting for. Active wants come first; fulfilled/expired ones
    stay below for the record."""
    return [build_want(w) for w in list_wants(db, user)]


@router.post("", response_model=WantOut, status_code=status.HTTP_201_CREATED)
def create_want(
    payload: WantCreateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WantOut:
    """The buyer writes a standing want by hand (source=explicit)."""
    want = create_explicit_want(db, user, payload.text.strip())
    return build_want(want)


@router.post("/{want_id}/subscribe", response_model=WantOut)
def subscribe(
    want_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WantOut:
    """Upgrade the want just captured on the finder page to an explicit
    subscription, and renew its TTL."""
    want = promote_to_explicit(db, user, want_id)
    if want is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "求购不存在")
    return build_want(want)


@router.delete("/{want_id}")
def close_want(
    want_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """The buyer stops waiting (WantStatus.DISMISSED). Removed from the index immediately."""
    if not dismiss_want(db, user, want_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "求购不存在")
    return {"ok": True}
