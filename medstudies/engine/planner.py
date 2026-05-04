"""
DailyPlanBuilder — converts scored topics into actionable plan items.

Three action types:
  - REVIEW   : topic not seen in a while → read/watch lecture
  - PRACTICE : high error rate → do questions
  - REINFORCE: Anki cards due or high lapse rate → flashcard session

The plan is serialised to JSON and persisted in DailyPlan.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, date
from typing import Literal

from sqlalchemy.orm import Session

from medstudies.engine.scorer import ScoringConfig, TopicScore, TopicScorer
from medstudies.persistence.models import DailyPlan


ActionType = Literal["REVIEW", "PRACTICE", "REINFORCE"]


@dataclass
class PlanItem:
    rank: int
    topic_id: int
    topic_name: str
    subject_name: str
    action: ActionType
    priority_score: float
    reason: str
    anki_due: int
    error_rate_pct: float   # 0–100
    days_since_review: float


@dataclass
class DailyStudyPlan:
    plan_date: str
    generated_at: str
    items: list[PlanItem]

    def to_json(self) -> str:
        return json.dumps(
            {"plan_date": self.plan_date, "generated_at": self.generated_at,
             "items": [asdict(i) for i in self.items]},
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> "DailyStudyPlan":
        d = json.loads(raw)
        items = [PlanItem(**i) for i in d["items"]]
        return cls(plan_date=d["plan_date"], generated_at=d["generated_at"], items=items)


class DailyPlanBuilder:
    def __init__(
        self,
        session: Session,
        max_topics: int = 8,
        scoring_config: ScoringConfig | None = None,
        topic_filter: set[int] | None = None,
    ):
        self._db = session
        self._max = max_topics
        self._scorer = TopicScorer(session, scoring_config)
        self._filter = topic_filter

    def build(self, target_date: date | None = None) -> DailyStudyPlan:
        target_date = target_date or date.today()
        scores: list[TopicScore] = self._scorer.score_all()
        if self._filter is not None:
            scores = [s for s in scores if s.topic_id in self._filter]
        top = scores[: self._max]

        items: list[PlanItem] = []
        for rank, ts in enumerate(top, start=1):
            action = self._decide_action(ts)
            items.append(
                PlanItem(
                    rank=rank,
                    topic_id=ts.topic_id,
                    topic_name=ts.topic_name,
                    subject_name=ts.subject_name,
                    action=action,
                    priority_score=ts.priority_score,
                    reason=ts.reason,
                    anki_due=ts.anki_due,
                    error_rate_pct=round(ts.error_rate * 100, 1),
                    days_since_review=ts.days_since_review,
                )
            )

        plan = DailyStudyPlan(
            plan_date=target_date.isoformat(),
            generated_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            items=items,
        )
        self._persist(plan)
        return plan

    def _decide_action(self, ts: TopicScore) -> ActionType:
        # Anki cards due takes precedence if many are overdue
        if ts.anki_due > 5 or ts.anki_lapses > 5:
            return "REINFORCE"
        if ts.error_rate > 0.45 and ts.total_questions >= 3:
            return "PRACTICE"
        return "REVIEW"

    def _persist(self, plan: DailyStudyPlan) -> None:
        record = DailyPlan(
            plan_date=plan.plan_date,
            generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            plan_json=plan.to_json(),
        )
        self._db.add(record)
        self._db.commit()
