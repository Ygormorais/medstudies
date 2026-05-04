"""
WeeklyPlanBuilder — distribui os tópicos prioritários nos próximos 7 dias.

Regras:
- Manhã (08h–13h): PRACTICE — questões, modo ativo
- Tarde (14h–19h): REVIEW / REINFORCE — revisão e Anki
- Máximo de tópicos por dia configurável (padrão 4)
- Distribui por prioridade, rodando pelos dias
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from sqlalchemy.orm import Session

from medstudies.engine.scorer import ScoringConfig, TopicScorer
from medstudies.engine.planner import DailyPlanBuilder, PlanItem

DAYS_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]

MORNING_DURATION = {"PRACTICE": 50, "REINFORCE": 40, "REVIEW": 40}
AFTERNOON_DURATION = {"REVIEW": 40, "REINFORCE": 40, "PRACTICE": 50}


@dataclass
class WeeklySlot:
    day_name: str
    day_date: str          # YYYY-MM-DD
    period: Literal["morning", "afternoon"]
    start_time: str        # HH:MM
    end_time: str          # HH:MM
    topic_name: str
    subject_name: str
    action: str
    priority_score: float
    reason: str
    error_rate_pct: float


@dataclass
class WeeklyPlan:
    start_date: str
    end_date: str
    days: list[dict]       # {date, day_name, morning: [...], afternoon: [...]}


class WeeklyPlanBuilder:
    def __init__(
        self,
        session: Session,
        topics_per_day: int = 4,
        scoring_config: ScoringConfig | None = None,
        topic_filter: set[int] | None = None,
    ):
        self._db = session
        self._tpd = topics_per_day
        self._scorer = TopicScorer(session, scoring_config)
        self._filter = topic_filter

    def build(self, start: date | None = None) -> WeeklyPlan:
        start = start or date.today()
        scores = self._scorer.score_all()
        if self._filter is not None:
            scores = [s for s in scores if s.topic_id in self._filter]

        # Separate by action preference
        practice  = [s for s in scores if s.total_questions >= 1]
        review    = scores  # all topics can be reviewed

        week_days = [start + timedelta(days=i) for i in range(7)]
        days_out = []

        practice_queue  = list(practice)
        review_queue    = list(review)

        for day in week_days:
            morning_slots   = []
            afternoon_slots = []

            # Morning: PRACTICE / REINFORCE items
            m_time = 8 * 60  # minutes from midnight
            for _ in range(self._tpd // 2):
                if not practice_queue:
                    break
                item = practice_queue.pop(0)
                action = "PRACTICE" if item.error_rate > 0.3 else "REINFORCE" if item.anki_due > 3 else "PRACTICE"
                dur = MORNING_DURATION[action]
                morning_slots.append(WeeklySlot(
                    day_name=DAYS_PT[day.weekday()],
                    day_date=day.isoformat(),
                    period="morning",
                    start_time=_fmt(m_time),
                    end_time=_fmt(m_time + dur),
                    topic_name=item.topic_name,
                    subject_name=item.subject_name,
                    action=action,
                    priority_score=item.priority_score,
                    reason=item.reason,
                    error_rate_pct=round(item.error_rate * 100, 1),
                ))
                m_time += dur + 10  # 10min break

            # Afternoon: REVIEW items
            a_time = 14 * 60
            for _ in range(self._tpd // 2):
                if not review_queue:
                    break
                item = review_queue.pop(0)
                action = "REVIEW"
                dur = AFTERNOON_DURATION[action]
                afternoon_slots.append(WeeklySlot(
                    day_name=DAYS_PT[day.weekday()],
                    day_date=day.isoformat(),
                    period="afternoon",
                    start_time=_fmt(a_time),
                    end_time=_fmt(a_time + dur),
                    topic_name=item.topic_name,
                    subject_name=item.subject_name,
                    action=action,
                    priority_score=item.priority_score,
                    reason=item.reason,
                    error_rate_pct=round(item.error_rate * 100, 1),
                ))
                a_time += dur + 10

            days_out.append({
                "date": day.isoformat(),
                "day_name": DAYS_PT[day.weekday()],
                "morning": [_slot_dict(s) for s in morning_slots],
                "afternoon": [_slot_dict(s) for s in afternoon_slots],
            })

        return WeeklyPlan(
            start_date=start.isoformat(),
            end_date=(start + timedelta(days=6)).isoformat(),
            days=days_out,
        )


def _fmt(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}"


def _slot_dict(s: WeeklySlot) -> dict:
    return {
        "day_name": s.day_name,
        "day_date": s.day_date,
        "period": s.period,
        "start_time": s.start_time,
        "end_time": s.end_time,
        "topic_name": s.topic_name,
        "subject_name": s.subject_name,
        "action": s.action,
        "priority_score": s.priority_score,
        "reason": s.reason,
        "error_rate_pct": s.error_rate_pct,
    }
