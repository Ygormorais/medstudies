from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from medstudies.persistence.models import Base
import os

DB_PATH = os.environ.get("MEDSTUDIES_DB", "data/medstudies.db")

_engines: dict = {}


def get_engine(path: str = DB_PATH):
    if path not in _engines:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _engines[path] = create_engine(f"sqlite:///{path}", echo=False)
    return _engines[path]


def init_db(path: str = DB_PATH):
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    # Safe migrations — add new columns if they don't exist yet
    _migrate(engine)
    return engine


def _migrate(engine):
    """Add columns that didn't exist in earlier schema versions."""
    migrations = [
        ("topics",      "study_notes",   "TEXT"),
        ("topics",      "is_favorite",   "INTEGER DEFAULT 0"),
        ("questions",   "difficulty",    "TEXT DEFAULT 'medio'"),
        ("topic_reviews", None, None),   # table via create_all; skip
        ("flashcards",  "ease_factor",   "REAL DEFAULT 2.5"),
        ("flashcards",  "interval_days", "REAL DEFAULT 1.0"),
        ("flashcards",  "repetitions",   "INTEGER DEFAULT 0"),
        ("flashcards",  "next_review",   "DATETIME"),
        ("tags",        None, None),     # new table; skip alter
        ("topic_tags",  None, None),     # new table; skip alter
        ("questions",   "statement",     "TEXT"),
        ("flashcards",  "hint",          "TEXT"),
    ]
    with engine.connect() as conn:
        for table, col, col_type in migrations:
            if col is None:
                continue
            try:
                conn.execute(__import__("sqlalchemy").text(
                    f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                ))
                conn.commit()
            except Exception:
                pass  # column already exists — ignore


def get_session(path: str = DB_PATH) -> Session:
    engine = get_engine(path)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()
