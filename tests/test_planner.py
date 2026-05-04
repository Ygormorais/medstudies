"""Tests for DailyPlanBuilder."""
from medstudies.engine.planner import DailyPlanBuilder


def test_plan_respects_max_topics(seeded_db):
    plan = DailyPlanBuilder(seeded_db, max_topics=1).build()
    assert len(plan.items) == 1


def test_plan_items_are_ranked(seeded_db):
    plan = DailyPlanBuilder(seeded_db, max_topics=2).build()
    ranks = [i.rank for i in plan.items]
    assert ranks == [1, 2]


def test_high_error_gets_practice_action(seeded_db):
    plan = DailyPlanBuilder(seeded_db, max_topics=2).build()
    ic_item = next(i for i in plan.items if i.topic_name == "Insuficiência Cardíaca")
    assert ic_item.action == "PRACTICE"


def test_plan_serialization_roundtrip(seeded_db):
    from medstudies.engine.planner import DailyStudyPlan
    plan = DailyPlanBuilder(seeded_db, max_topics=2).build()
    json_str = plan.to_json()
    restored = DailyStudyPlan.from_json(json_str)
    assert restored.plan_date == plan.plan_date
    assert len(restored.items) == len(plan.items)
