"""Engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateIndex, CreateTable

from pit.db.models import Base

SQLITE_PREFIX = "sqlite:///"


def make_engine(db_url: str) -> Engine:
    """Create an engine. For SQLite file databases the parent folder is created."""
    if db_url.startswith(SQLITE_PREFIX):
        path = db_url[len(SQLITE_PREFIX) :]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)

    return create_engine(db_url)


def init_db(engine: Engine) -> None:
    """Create all tables that do not exist yet (idempotent)."""
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def render_schema_sql(dialect_name: str = "sqlite") -> str:
    """DDL for every table, as committed in ``src/pit/db/schema.sql``."""
    from sqlalchemy.dialects import postgresql, sqlite

    dialect = sqlite.dialect() if dialect_name == "sqlite" else postgresql.dialect()
    parts = [
        "-- Generated from src/pit/db/models.py by pit.db.session.render_schema_sql().",
        "-- Do not edit by hand: run `pit db-schema > src/pit/db/schema.sql`.",
        "",
    ]
    for table in Base.metadata.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        for index in sorted(table.indexes, key=lambda i: str(i.name)):
            parts.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")
        parts.append("")
    # Strip trailing whitespace emitted by the DDL compiler so the file is lint-clean.
    return "\n".join(line.rstrip() for line in "\n".join(parts).splitlines()) + "\n"
