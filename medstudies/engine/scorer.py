"""
TopicScorer — implements the priority formula:

    priority_score =
        (error_weight   * error_rate)                +
        (recency_weight * days_since_last_review)    +
        (volume_weight  * normalized_error_count)    +
        (importance_weight * subject_exam_weight)    +
        (anki_weight    * flashcard_difficulty_signal)

Each component is normalized to [0, 1] before weighting.
Weights are configurable via ScoringConfig.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import math

from sqlalchemy.orm import Session, subqueryload

from medstudies.persistence.models import AnkiSnapshot, Question, StudySession, Subject, Topic, TopicReview


@dataclass
class ScoringConfig:
    error_weight: float = 0.28
    recency_weight: float = 0.18
    volume_weight: float = 0.12
    importance_weight: float = 0.17
    anki_weight: float = 0.10
    sm2_weight: float = 0.15

    # Normalisation parameters
    max_days_stale: float = 60.0   # 60+ days without review → full recency score
    max_errors: int = 50           # cap for volume normalisation
    max_days_overdue: float = 30.0 # SM-2 overdue cap


@dataclass
class TopicScore:
    topic_id: int
    topic_name: str
    subject_name: str
    priority_score: float

    # Breakdown (each in [0, 1])
    error_rate: float = 0.0
    days_since_review: float = 0.0
    error_volume: float = 0.0
    subject_importance: float = 0.0
    anki_difficulty: float = 0.0

    # Context for the plan explanation
    total_questions: int = 0
    wrong_questions: int = 0
    last_reviewed_at: Optional[datetime] = None
    anki_due: int = 0
    anki_lapses: int = 0
    sm2_days_overdue: float = 0.0
    sm2_interval: float = 0.0
    reason: str = ""

    def explain(self) -> str:
        parts = []
        if self.error_rate > 0.5:
            parts.append(f"high error rate ({self.error_rate:.0%})")
        if self.sm2_days_overdue > 1:
            parts.append(f"SM-2 overdue {self.sm2_days_overdue:.0f}d")
        elif self.days_since_review > 14:
            parts.append(f"not reviewed in {self.days_since_review:.0f}d")
        if self.anki_due > 0:
            parts.append(f"{self.anki_due} Anki cards due")
        if self.anki_lapses > 10:
            parts.append(f"{self.anki_lapses} card lapses (memory failure)")
        if not parts:
            parts.append("flagged by overall priority score")
        return "; ".join(parts)


class TopicScorer:
    def __init__(self, session: Session, config: Optional[ScoringConfig] = None):
        self._db = session
        self._cfg = config or ScoringConfig()

    def score_all(self) -> list[TopicScore]:
        topics = (self._db.query(Topic)
                  .options(subqueryload(Topic.questions), subqueryload(Topic.sessions))
                  .all())
        # prefetch to avoid N+1
        reviews = {r.topic_id: r for r in self._db.query(TopicReview).all()}
        snapshots: dict[int, AnkiSnapshot] = {}
        for snap in self._db.query(AnkiSnapshot).order_by(AnkiSnapshot.synced_at.asc()).all():
            snapshots[snap.topic_id] = snap  # last write wins = most recent
        scores = [self._score_topic(t, reviews.get(t.id), snapshots.get(t.id)) for t in topics]
        scores.sort(key=lambda s: s.priority_score, reverse=True)
        return scores

    def _score_topic(self, topic: Topic, sm2_review: Optional[TopicReview], snapshot: Optional[AnkiSnapshot]) -> TopicScore:
        cfg = self._cfg
        subject: Subject = topic.subject

        # --- Error signals ---
        questions: list[Question] = topic.questions
        total_q = len(questions)
        wrong_q = sum(1 for q in questions if not q.correct)
        error_rate = wrong_q / total_q if total_q else 0.0
        vol_norm = min(wrong_q, cfg.max_errors) / cfg.max_errors

        # --- Recency signal ---
        sessions: list[StudySession] = topic.sessions
        last_session = max((s.started_at for s in sessions), default=None)
        last_question = max((q.answered_at for q in questions), default=None)
        last_activity = _latest(last_session, last_question)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if last_activity:
            days_stale = (now - last_activity).total_seconds() / 86400
        else:
            days_stale = cfg.max_days_stale  # never reviewed → max urgency
        recency_norm = min(days_stale, cfg.max_days_stale) / cfg.max_days_stale

        # --- Subject importance ---
        importance_norm = min(subject.exam_weight, 3.0) / 3.0

        # --- Anki difficulty signal ---
        anki_difficulty = 0.0
        due_cards = 0
        total_lapses = 0
        if snapshot and snapshot.total_cards:
            due_ratio = snapshot.due_cards / snapshot.total_cards
            ease_penalty = 0.0
            if snapshot.avg_ease:
                ease_penalty = max(0.0, (2500 - snapshot.avg_ease) / 2500)
            lapse_signal = math.log1p(snapshot.total_lapses) / math.log1p(200)
            anki_difficulty = min(1.0, (due_ratio + ease_penalty + lapse_signal) / 3)
            due_cards = snapshot.due_cards
            total_lapses = snapshot.total_lapses

        # --- SM-2 signal ---
        sm2_signal = 0.0
        sm2_days_overdue = 0.0
        sm2_interval = 0.0
        if sm2_review and sm2_review.next_review:
            overdue = (now - sm2_review.next_review).total_seconds() / 86400
            if overdue > 0:
                sm2_days_overdue = overdue
                sm2_signal = min(overdue, cfg.max_days_overdue) / cfg.max_days_overdue
            sm2_interval = sm2_review.interval_days or 0.0

        score = (
            cfg.error_weight * error_rate
            + cfg.recency_weight * recency_norm
            + cfg.volume_weight * vol_norm
            + cfg.importance_weight * importance_norm
            + cfg.anki_weight * anki_difficulty
            + cfg.sm2_weight * sm2_signal
        )

        ts = TopicScore(
            topic_id=topic.id,
            topic_name=topic.name,
            subject_name=subject.name,
            priority_score=round(score, 4),
            error_rate=round(error_rate, 4),
            days_since_review=round(days_stale, 1),
            error_volume=round(vol_norm, 4),
            subject_importance=round(importance_norm, 4),
            anki_difficulty=round(anki_difficulty, 4),
            total_questions=total_q,
            wrong_questions=wrong_q,
            last_reviewed_at=last_activity,
            anki_due=due_cards,
            anki_lapses=total_lapses,
            sm2_days_overdue=round(sm2_days_overdue, 1),
            sm2_interval=round(sm2_interval, 1),
        )
        ts.reason = ts.explain()
        return ts


def _latest(*dts: Optional[datetime]) -> Optional[datetime]:
    valid = [d for d in dts if d is not None]
    return max(valid) if valid else None
