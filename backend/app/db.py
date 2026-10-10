"""Database engine and session management. Switching to Postgres only requires
changing DB_URL (the repository layer already isolates all query details)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_connect_args = {"check_same_thread": False} if settings.sqlalchemy_url.startswith("sqlite") else {}

engine = create_engine(settings.sqlalchemy_url, echo=False, future=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    from . import models  # noqa: F401  make sure the models are registered

    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Add columns that were added to the models to already-existing tables.

    The project deliberately has no alembic (spec §1: no extra infrastructure
    dependencies), and create_all only creates new tables — it never alters
    existing ones, so an old DB running new code would blow up with
    "no such column" on the first query.
    This does **adding columns** and nothing else, and only nullable columns
    or ones with a default:
      * no type changes, no drops, no constraint changes — those need a human;
      * a non-nullable column without a default is skipped with a warning —
        SQLite couldn't add it anyway.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue          # just created by create_all — columns are complete
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                if not column.nullable and column.default is None and column.server_default is None:
                    logger.warning(
                        "[db] %s.%s is a new non-nullable column without a default; "
                        "cannot add it automatically — migrate by hand",
                        table.name, column.name,
                    )
                    continue
                ddl = column.type.compile(engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}'))
                filled = _backfill_default(conn, table.name, column)
                logger.info(
                    "[db] added column %s.%s (%s)%s",
                    table.name, column.name, ddl,
                    f", backfilled {filled} row(s)" if filled else "",
                )


def _backfill_default(conn, table_name: str, column) -> int:
    """把新加的列在已有行上填成它的默认值。

    `ALTER TABLE ... ADD COLUMN` 不带默认值，所以已有行拿到的是 NULL——而模型上
    写着 `default=True` 的那个默认值只在**插入新行**时生效。两者的差别在布尔开关
    上是灾难性的：`sale_watch_enabled` 对所有老用户会是 NULL，读出来是假，于是
    一个「默认开着」的功能对每一个现存卖家都是关着的，而且没有任何地方会报错。

    只处理标量默认值。可调用的默认值（`default=utcnow` 那种）跳过：它们的语义是
    「这一行是什么时候创建的」，给历史行统一填上今天反而是在编造事实。
    """
    default = getattr(column, "default", None)
    if default is None or getattr(default, "is_callable", False):
        return 0
    value = getattr(default, "arg", None)
    if value is None or callable(value):
        return 0
    result = conn.execute(
        text(f'UPDATE "{table_name}" SET "{column.name}" = :v WHERE "{column.name}" IS NULL'),
        {"v": value},
    )
    return int(result.rowcount or 0)


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for scripts / background tasks."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
