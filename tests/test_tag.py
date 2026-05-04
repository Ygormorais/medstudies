"""Tests for Tag commands and planner tag filter."""
import pytest
from medstudies.persistence.models import Tag, Topic, Subject
from medstudies.engine.planner import DailyPlanBuilder
from medstudies.engine.sm2 import SM2Engine


def test_tag_filter_restricts_plan(seeded_db):
    # Create tag and attach to IC only
    tag = Tag(name="cardio", color="#FF0000")
    seeded_db.add(tag)
    seeded_db.flush()
    ic = seeded_db.query(Topic).filter_by(name="Insuficiência Cardíaca").first()
    ic.tags.append(tag)
    seeded_db.commit()

    tag_filter = {ic.id}
    plan = DailyPlanBuilder(seeded_db, max_topics=10, topic_filter=tag_filter).build()
    assert len(plan.items) == 1
    assert plan.items[0].topic_name == "Insuficiência Cardíaca"


def test_tag_filter_none_returns_all(seeded_db):
    plan = DailyPlanBuilder(seeded_db, max_topics=10, topic_filter=None).build()
    assert len(plan.items) == 2


def test_tag_filter_empty_set_returns_nothing(seeded_db):
    plan = DailyPlanBuilder(seeded_db, max_topics=10, topic_filter=set()).build()
    assert len(plan.items) == 0


def test_tag_attach_detach(seeded_db):
    tag = Tag(name="prova", color="#00FF00")
    seeded_db.add(tag)
    seeded_db.flush()
    ic = seeded_db.query(Topic).filter_by(name="Insuficiência Cardíaca").first()

    ic.tags.append(tag)
    seeded_db.commit()
    assert tag in ic.tags

    ic.tags.remove(tag)
    seeded_db.commit()
    assert tag not in ic.tags


def test_tag_topics_relationship(seeded_db):
    tag = Tag(name="multi", color="#0000FF")
    seeded_db.add(tag)
    seeded_db.flush()
    for t in seeded_db.query(Topic).all():
        t.tags.append(tag)
    seeded_db.commit()

    fetched = seeded_db.query(Tag).filter_by(name="multi").first()
    assert len(fetched.topics) == 2
