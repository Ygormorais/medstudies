import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from medstudies.persistence.models import Base, User, Subject, Topic, Question
from medstudies.persistence.tenant import TenantSession

USER_A = "aaaaaaaa-0000-0000-0000-000000000001"
USER_B = "bbbbbbbb-0000-0000-0000-000000000002"


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def seeded(engine):
    """Two users, each with one subject + topic + question."""
    with Session(engine) as db:
        db.add_all([
            User(id=USER_A, email="a@test.com"),
            User(id=USER_B, email="b@test.com"),
        ])
        db.flush()
        subj_a = Subject(name="Cardio", user_id=USER_A)
        subj_b = Subject(name="Cardio", user_id=USER_B)
        db.add_all([subj_a, subj_b])
        db.flush()
        topic_a = Topic(name="IC", subject_id=subj_a.id, user_id=USER_A)
        topic_b = Topic(name="IC", subject_id=subj_b.id, user_id=USER_B)
        db.add_all([topic_a, topic_b])
        db.flush()
        db.add_all([
            Question(topic_id=topic_a.id, correct=True, user_id=USER_A),
            Question(topic_id=topic_b.id, correct=False, user_id=USER_B),
        ])
        db.commit()
        return {"subj_a_id": subj_a.id, "subj_b_id": subj_b.id,
                "topic_a_id": topic_a.id, "topic_b_id": topic_b.id}


def test_tenant_session_filters_subjects(engine, seeded):
    """User A should only see their own subjects."""
    db_a = TenantSession(engine, user_id=USER_A)
    subjects = db_a.query(Subject).all()
    assert len(subjects) == 1
    assert subjects[0].user_id == USER_A
    db_a.close()


def test_tenant_session_filters_topics(engine, seeded):
    """User A should not see User B's topics."""
    db_a = TenantSession(engine, user_id=USER_A)
    topics = db_a.query(Topic).all()
    assert all(t.user_id == USER_A for t in topics)
    db_a.close()


def test_tenant_session_filters_questions(engine, seeded):
    """User A should only see their own questions."""
    db_a = TenantSession(engine, user_id=USER_A)
    qs = db_a.query(Question).all()
    assert all(q.user_id == USER_A for q in qs)
    db_a.close()


def test_plain_session_sees_all(engine, seeded):
    """A plain Session (non-tenant) still sees everything (for migration/admin use)."""
    db = Session(engine)
    assert db.query(Subject).count() == 2
    db.close()


def test_tenant_session_auto_sets_user_id_on_insert(engine, seeded):
    """New objects inserted via TenantSession get user_id auto-set."""
    db_a = TenantSession(engine, user_id=USER_A)
    subj_a = db_a.query(Subject).first()
    new_topic = Topic(name="Novo", subject_id=subj_a.id)  # no user_id set
    db_a.add(new_topic)
    db_a.flush()
    assert new_topic.user_id == USER_A
    db_a.close()


def test_tenant_session_cross_tenant_get_returns_none(engine, seeded):
    """User B's subject should not appear in User A's tenant query."""
    db_a = TenantSession(engine, user_id=USER_A)
    result = db_a.query(Subject).filter_by(id=seeded["subj_b_id"]).first()
    assert result is None
    db_a.close()
