"""
SM-2 spaced repetition engine for topics.

Maps a batch of question results → quality score (0–5) → SM-2 update.
TopicReview stores the SM-2 state; scorer reads it for priority boosting.

Quality mapping (from error rate):
  error 0%   → quality 5
  error 1-20% → quality 4
  error 21-40%→ quality 3
  error 41-60%→ quality 2
  error 61-80%→ quality 1
  error 81%+  → quality 0
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session, subqueryload

from medstudies.persistence.models import Question, Topic, TopicReview


def _quality(error_rate: float) -> int:
    if error_rate <= 0.00: return 5
    if error_rate <= 0.20: return 4
    if error_rate <= 0.40: return 3
    if error_rate <= 0.60: return 2
    if error_rate <= 0.80: return 1
    return 0


def _sm2_step(ef: float, interval: float, reps: int, quality: int):
    """Returns (new_ef, new_interval, new_reps)."""
    if quality < 3:
        return max(1.3, ef - 0.20), 1.0, 0
    new_ef = max(1.3, ef + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    if reps == 0:
        new_interval = 1.0
    elif reps == 1:
        new_interval = 6.0
    else:
        new_interval = round(interval * ef, 1)
    return new_ef, new_interval, reps + 1


class SM2Engine:
    def __init__(self, session: Session):
        self._db = session

    def update_all(self) -> int:
        """Recompute SM-2 state for every topic. Returns count updated."""
        topics = self._db.query(Topic).options(subqueryload(Topic.questions)).all()
        updated = 0
        for topic in topics:
            if self._update_topic(topic):
                updated += 1
        self._db.commit()
        return updated

    def update_topics(self, topic_ids: list[int]) -> None:
        """Recompute SM-2 state for specific topics."""
        topics = self._db.query(Topic).filter(Topic.id.in_(topic_ids)).all()
        for topic in topics:
            self._update_topic(topic)
        self._db.commit()

    def _update_topic(self, topic: Topic) -> bool:
        questions: list[Question] = topic.questions
        if not questions:
            return False

        total = len(questions)
        wrong = sum(1 for q in questions if not q.correct)
        error_rate = wrong / total
        quality = _quality(error_rate)

        review = (
            self._db.query(TopicReview)
            .filter_by(topic_id=topic.id)
            .first()
        )
        if not review:
            review = TopicReview(
                topic_id=topic.id,
                ease_factor=2.5,
                interval_days=1.0,
                repetitions=0,
            )
            self._db.add(review)

        new_ef, new_interval, new_reps = _sm2_step(
            review.ease_factor, review.interval_days, review.repetitions, quality
        )
        review.ease_factor = new_ef
        review.interval_days = new_interval
        review.repetitions = new_reps
        review.last_reviewed = datetime.now(timezone.utc).replace(tzinfo=None)
        review.next_review = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=new_interval)
        return True
