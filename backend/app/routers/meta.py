from __future__ import annotations

from fastapi import APIRouter, Depends

from ..config import CAMPUS_LOCATIONS, CATEGORY_DICT, SUBCATEGORIES_BY_CATEGORY, settings
from ..deps import get_index
from ..schemas import LocationOut, MetaConfigOut
from ..services.item_index import ItemIndexService
from ..services.llm.client import get_llm_client

router = APIRouter(prefix="/api/meta", tags=["meta"])


@router.get("/config", response_model=MetaConfigOut)
def config(index: ItemIndexService = Depends(get_index)) -> MetaConfigOut:
    client = get_llm_client()
    return MetaConfigOut(
        confirm_window_hours=settings.confirm_window_hours,
        default_pickup_range_km=settings.default_pickup_range_km,
        locations=[LocationOut(**loc) for loc in CAMPUS_LOCATIONS],
        categories=list(CATEGORY_DICT.keys()),
        subcategories=SUBCATEGORIES_BY_CATEGORY,
        llm_available=client.available,
        llm_unavailable_reason=client.unavailable_reason,
        index_stats=index.stats(),
    )
