"""Tests for SM-2 engine."""
from medstudies.engine.sm2 import SM2Engine, _sm2_step, _quality
from medstudies.persistence.models import TopicReview


def test_quality_mapping():
    assert _quality(0.0) == 5
    assert _quality(0.1) == 4
    assert _quality(0.3) == 3
    assert _quality(0.5) == 2
    assert _quality(0.7) == 1
    assert _quality(0.9) == 0


def test_sm2_step_wrong_resets_interval():
    ef, interval, reps = _sm2_step(2.5, 10.0, 3, quality=0)
    assert interval == 1.0
    assert reps == 0
    assert ef < 2.5


def test_sm2_step_correct_increases_interval():
    ef, interval, reps = _sm2_step(2.5, 6.0, 2, quality=5)
    assert interval > 6.0
    assert reps == 3
    assert ef >= 2.5


def test_sm2_step_ef_floor():
    ef, _, _ = _sm2_step(1.3, 1.0, 0, quality=0)
    assert ef >= 1.3


def test_update_all_creates_topic_reviews(seeded_db):
    engine = SM2Engine(seeded_db)
    count = engine.update_all()
    assert count == 2
    reviews = seeded_db.query(TopicReview).all()
    assert len(reviews) == 2


def test_high_error_topic_gets_short_interval(seeded_db):
    engine = SM2Engine(seeded_db)
    engine.update_all()
    reviews = seeded_db.query(TopicReview).all()
    # IC has 75% error → quality 1 → interval resets to 1
    ic_review = next(r for r in reviews
                     if r.topic.name == "Insuficiência Cardíaca")
    assert ic_review.interval_days == 1.0


def test_low_error_topic_gets_longer_interval(seeded_db):
    engine = SM2Engine(seeded_db)
    engine.update_all()
    reviews = seeded_db.query(TopicReview).all()
    # DPOC has 33% error → quality 3 → interval grows
    dpoc_review = next(r for r in reviews if r.topic.name == "DPOC")
    assert dpoc_review.interval_days >= 1.0
