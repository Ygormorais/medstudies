"""Tests for TopicScorer priority formula."""
from medstudies.engine.scorer import TopicScorer, ScoringConfig


def test_score_all_returns_sorted(seeded_db):
    scorer = TopicScorer(seeded_db)
    scores = scorer.score_all()
    assert len(scores) == 2
    # sorted descending
    assert scores[0].priority_score >= scores[1].priority_score


def test_high_error_topic_scores_higher(seeded_db):
    scorer = TopicScorer(seeded_db)
    scores = scorer.score_all()
    ic = next(s for s in scores if s.topic_name == "Insuficiência Cardíaca")
    dpoc = next(s for s in scores if s.topic_name == "DPOC")
    assert ic.error_rate > dpoc.error_rate
    assert ic.priority_score > dpoc.priority_score


def test_error_rate_calculation(seeded_db):
    scorer = TopicScorer(seeded_db)
    scores = scorer.score_all()
    ic = next(s for s in scores if s.topic_name == "Insuficiência Cardíaca")
    assert ic.total_questions == 4
    assert ic.wrong_questions == 3
    assert abs(ic.error_rate - 0.75) < 0.01


def test_no_questions_topic_gets_recency_score(seeded_db):
    from medstudies.persistence.models import Topic, Subject
    new_subj = Subject(name="Neurologia", exam_weight=1.0)
    seeded_db.add(new_subj)
    seeded_db.flush()
    new_topic = Topic(name="AVC", subject_id=new_subj.id)
    seeded_db.add(new_topic)
    seeded_db.commit()

    scorer = TopicScorer(seeded_db)
    scores = scorer.score_all()
    avc = next(s for s in scores if s.topic_name == "AVC")
    # no questions → max recency signal applied
    assert avc.days_since_review >= 60.0


def test_custom_config_weights(seeded_db):
    cfg = ScoringConfig(error_weight=1.0, recency_weight=0.0, volume_weight=0.0,
                        importance_weight=0.0, anki_weight=0.0, sm2_weight=0.0)
    scorer = TopicScorer(seeded_db, config=cfg)
    scores = scorer.score_all()
    ic = next(s for s in scores if s.topic_name == "Insuficiência Cardíaca")
    # with only error_weight, score ≈ error_rate
    assert abs(ic.priority_score - ic.error_rate) < 0.01
