"""
Integration tests for critical API endpoints.

Fixtures create their own in-memory SQLite DB with a User record so that
Subject.user_id / Topic.user_id / Question.user_id NOT NULL constraints
are satisfied.  get_session is patched so the app uses the test DB.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from medstudies.interface.api import app
from medstudies.persistence.models import Base, Question, Subject, Topic, TopicReview, User


# ── Fixtures ──────────────────────────────────────────────────────────────────

USER_ID = "test-user-0000-0000-000000000001"


@pytest.fixture
def db():
    """Fresh in-memory SQLite session with the full schema.

    StaticPool forces all connections to reuse the same SQLite in-memory
    database, which is required because `:memory:` databases are
    per-connection and would otherwise vanish when a new connection is
    checked out from the pool.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    # Auto-fill user_id on any ORM object that declares it but leaves it None.
    # This is needed because some API helpers (e.g. _get_or_create_review,
    # add_question_and_update_sm2) construct model instances without user_id.
    @event.listens_for(session, "before_flush")
    def _autofill_user_id(sess, flush_context, instances):
        for obj in list(sess.new):
            if hasattr(obj, "user_id") and obj.user_id is None:
                obj.user_id = USER_ID

    # Every row needs a user — create one first
    session.add(User(id=USER_ID, email="test@medstudies.test"))
    session.commit()
    yield session
    session.close()


@pytest.fixture
def seeded_db(db):
    """DB seeded with Cardiologia / Pneumologia subjects, two topics, and questions."""
    cardio = Subject(name="Cardiologia", exam_weight=1.5, user_id=USER_ID)
    pneumo = Subject(name="Pneumologia", exam_weight=1.0, user_id=USER_ID)
    db.add_all([cardio, pneumo])
    db.flush()

    ic = Topic(name="Insuficiência Cardíaca", subject_id=cardio.id, user_id=USER_ID)
    dpoc = Topic(name="DPOC", subject_id=pneumo.id, user_id=USER_ID)
    db.add_all([ic, dpoc])
    db.flush()

    # IC: 3 wrong, 1 correct  (75 % error rate)
    db.add_all([
        Question(topic_id=ic.id, correct=False, source="test", user_id=USER_ID),
        Question(topic_id=ic.id, correct=False, source="test", user_id=USER_ID),
        Question(topic_id=ic.id, correct=False, source="test", user_id=USER_ID),
        Question(topic_id=ic.id, correct=True,  source="test", user_id=USER_ID),
    ])
    # DPOC: 1 wrong, 2 correct (33 % error rate)
    db.add_all([
        Question(topic_id=dpoc.id, correct=False, source="test", user_id=USER_ID),
        Question(topic_id=dpoc.id, correct=True,  source="test", user_id=USER_ID),
        Question(topic_id=dpoc.id, correct=True,  source="test", user_id=USER_ID),
    ])

    # Pre-seed TopicReview rows so _get_or_create_review (called by POST /api/questions)
    # finds existing records and never tries to INSERT without user_id.
    db.add_all([
        TopicReview(topic_id=ic.id,   user_id=USER_ID),
        TopicReview(topic_id=dpoc.id, user_id=USER_ID),
    ])
    db.commit()
    return db


@pytest.fixture
def client(seeded_db):
    """TestClient that patches get_session to return the seeded test DB."""
    with patch("medstudies.interface.api.get_session", return_value=seeded_db):
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_list_subjects(client):
    """GET /api/subjects returns 200, list, includes 'Cardiologia'."""
    resp = client.get("/api/subjects")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    names = [s["name"] for s in data]
    assert "Cardiologia" in names


def test_list_topics(client):
    """GET /api/topics returns 200, items list includes at least one topic."""
    resp = client.get("/api/topics")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert len(data["items"]) > 0


def test_get_topic_not_found(client):
    """GET /api/topics/9999/notes returns 404 for a non-existent topic."""
    resp = client.get("/api/topics/9999/notes")
    assert resp.status_code == 404


def test_add_question(client, seeded_db):
    """POST /api/questions with a valid topic_id returns 200 and correct=True."""
    # Grab any topic id from the seeded DB
    topic = seeded_db.query(Topic).first()
    resp = client.post(
        "/api/questions",
        json={"topic_id": topic.id, "correct": True, "source": "pytest"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["correct"] is True


def test_add_question_invalid_topic(client):
    """POST /api/questions with topic_id=9999 returns 404."""
    resp = client.post(
        "/api/questions",
        json={"topic_id": 9999, "correct": True, "source": "pytest"},
    )
    assert resp.status_code == 404


def test_wrong_topics_report(client):
    """GET /api/questions/wrong-topics returns 200 and list with wrong > 0."""
    resp = client.get("/api/questions/wrong-topics")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) > 0
    # Every entry in the response must have wrong > 0 (that's the endpoint's contract)
    for entry in data:
        assert entry["wrong"] > 0


def test_performance_endpoint(client):
    """GET /api/stats returns 200 with total_questions, correct, accuracy_pct."""
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_questions" in data
    assert "correct_questions" in data
    assert "accuracy_pct" in data
    assert data["total_questions"] > 0


def test_daily_plan(client):
    """GET /api/plan/today-summary returns 200."""
    resp = client.get("/api/plan/today-summary")
    assert resp.status_code == 200
