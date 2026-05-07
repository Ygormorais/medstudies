from __future__ import annotations
import os
from sqlalchemy import create_engine, Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool
from medstudies.persistence.models import Base
from medstudies.persistence.tenant import TenantSession

# SQLite (dev/local) or Postgres (prod) depending on env vars
_DB_URL = os.environ.get(
    "DATABASE_URL",
    f"sqlite:///{os.environ.get('MEDSTUDIES_DB', 'data/medstudies.db')}",
)

_engines: dict[str, Engine] = {}
_session_factories: dict[str, sessionmaker] = {}


def get_engine(url: str = _DB_URL) -> Engine:
    if url not in _engines:
        if url.startswith("sqlite"):
            _engines[url] = create_engine(
                url,
                echo=False,
                poolclass=NullPool,
                connect_args={"check_same_thread": False},
            )
        else:
            _engines[url] = create_engine(url, echo=False, pool_pre_ping=True)
    return _engines[url]


def init_db(url: str = _DB_URL) -> Engine:
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    if url.startswith("sqlite"):
        _sqlite_migrate(engine)
    return engine


def get_session(url: str = _DB_URL) -> Session:
    """Return a plain (non-tenant) session. For admin/migration code only."""
    if url not in _session_factories:
        _session_factories[url] = sessionmaker(bind=get_engine(url))
    return _session_factories[url]()


def get_tenant_session(engine_or_url: "str | Engine", user_id: str) -> TenantSession:
    """Return a TenantSession scoped to user_id."""
    if isinstance(engine_or_url, str):
        engine_or_url = get_engine(engine_or_url)
    return TenantSession(bind=engine_or_url, user_id=user_id)


def _sqlite_migrate(engine: Engine) -> None:
    """Add columns that didn't exist in earlier SQLite schema versions."""
    migrations = [
        ("topics",          "study_notes",           "TEXT"),
        ("topics",          "is_favorite",            "INTEGER DEFAULT 0"),
        ("questions",       "difficulty",             "TEXT DEFAULT 'medio'"),
        ("flashcards",      "ease_factor",            "REAL DEFAULT 2.5"),
        ("flashcards",      "interval_days",          "REAL DEFAULT 1.0"),
        ("flashcards",      "repetitions",            "INTEGER DEFAULT 0"),
        ("flashcards",      "next_review",            "DATETIME"),
        ("questions",       "statement",              "TEXT"),
        ("flashcards",      "hint",                   "TEXT"),
        ("questions",       "alternatives",           "TEXT"),
        ("questions",       "correct_alt",            "TEXT"),
        ("questions",       "chosen_alt",             "TEXT"),
        ("questions",       "explanation",            "TEXT"),
        ("questions",       "year",                   "INTEGER"),
        ("topics",          "anki_tags",              "TEXT"),
        ("topics",          "notability_notebook",    "TEXT"),
        ("topics",          "external_ref",           "TEXT"),
    ]
    from sqlalchemy import text
    with engine.connect() as conn:
        for table, col, col_type in migrations:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
                conn.commit()
            except Exception:
                pass
