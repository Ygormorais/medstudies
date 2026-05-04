"""Shared pytest fixtures — in-memory SQLite DB per test."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from medstudies.persistence.models import Base, Subject, Topic, Question


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    session = Session(engine)
    yield session
    session.close()


@pytest.fixture
def seeded_db(db):
    """DB with one subject, two topics, and some questions."""
    cardio = Subject(name="Cardiologia", exam_weight=1.5)
    pneumo = Subject(name="Pneumologia", exam_weight=1.0)
    db.add_all([cardio, pneumo])
    db.flush()

    ic = Topic(name="Insuficiência Cardíaca", subject_id=cardio.id)
    dpoc = Topic(name="DPOC", subject_id=pneumo.id)
    db.add_all([ic, dpoc])
    db.flush()

    # IC: 3 wrong, 1 correct (75% error)
    db.add_all([
        Question(topic_id=ic.id, correct=False, source="test"),
        Question(topic_id=ic.id, correct=False, source="test"),
        Question(topic_id=ic.id, correct=False, source="test"),
        Question(topic_id=ic.id, correct=True,  source="test"),
    ])
    # DPOC: 1 wrong, 2 correct (33% error)
    db.add_all([
        Question(topic_id=dpoc.id, correct=False, source="test"),
        Question(topic_id=dpoc.id, correct=True,  source="test"),
        Question(topic_id=dpoc.id, correct=True,  source="test"),
    ])
    db.commit()
    return db
