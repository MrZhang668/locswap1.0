"""FastAPI entry point.

Startup flow: create tables → fully rebuild the vector + BM25 indexes from the
primary DB → attach the expiry polling task.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import SessionLocal, init_db
from .routers import (
    auth,
    items,
    meta,
    notifications,
    reservations,
    search,
    seller,
    seller_advisor,
    wants,
)
from .services.item_index import get_item_index
from .services.publishing.rules import rebuild_index as rebuild_rule_index
from .services.publishing.rules import seed_rules
from .services.scheduler import expiry_loop, sale_plan_loop
from .services.want_index import get_want_index

# On Windows, logging redirected to a file/pipe falls back to cp1252, which can
# raise UnicodeEncodeError on non-ASCII log output — force UTF-8 to be safe.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("locswap")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    index = get_item_index()
    want_index = get_want_index()
    with SessionLocal() as db:
        stats = await asyncio.to_thread(index.rebuild_from_db, db)
        # The want vector store must be rebuilt together with the primary DB:
        # after seeding / manual DB edits the two drift apart, and the drift
        # shows up as E1 pushing to wants that were closed long ago — very hard
        # to spot from the logs.
        want_stats = await asyncio.to_thread(want_index.rebuild_from_db, db)
        # The publishing flow refuses to review anything with an empty rule
        # book, so seeding is part of startup rather than a manual step.
        seed_rules(db)
        rule_stats = await asyncio.to_thread(rebuild_rule_index, db)
    logger.info("index ready: %s; standing wants: %s; listing rules: %s",
                stats, want_stats, rule_stats)

    tasks = [
        asyncio.create_task(expiry_loop(index)),
        asyncio.create_task(sale_plan_loop()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


# Checked before the app object exists, so a misconfigured production instance
# fails at import rather than starting and serving.
settings.assert_production_ready()

app = FastAPI(
    title=f"{settings.app_name} · Smart Campus Marketplace Demo",
    version="0.1.0",
    lifespan=lifespan,
    # The interactive docs are a development convenience and a free map of the
    # API for anyone else. Off in production.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    # No wildcard in production — `assert_production_ready` has already refused
    # to start if the origins were not named, so this list is real there.
    allow_origins=settings.cors_origin_list or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

settings.ensure_dirs()
# Only the uploads directory, never DATA_DIR itself.
#
# Mounting DATA_DIR served the whole of it over HTTP — `GET /static/locswap.db`
# returned the entire database (emails, password hashes, every want and
# notification), and `/static/advisor_threads.sqlite` every seller's
# conversation with the advisor. Harmless on a laptop, a one-URL data breach the
# moment this is reachable from anywhere else.
#
# Every URL the app builds is already `/static/uploads/...` (photo_path is
# stored relative to DATA_DIR and always begins with "uploads/"), so serving the
# narrower directory at the same prefix changes no link. Keep it this way: the
# mount point is a public directory listing, and DATA_DIR is where the secrets
# live.
app.mount(
    "/static/uploads", StaticFiles(directory=str(settings.upload_path)), name="static"
)

for router in (auth.router, meta.router, search.router, items.router,
               reservations.router, seller.router, seller_advisor.router,
               notifications.router, wants.router):
    app.include_router(router)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "index": get_item_index().stats()}
