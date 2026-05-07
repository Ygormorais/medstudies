"""
MedStudies Chat Agent — Claude-powered study assistant.

Tools available to Claude:
  - get_daily_plan       : plano de hoje com scores e ações
  - get_weak_topics      : tópicos mais fracos (erro% alto)
  - get_performance      : resumo geral de desempenho
  - get_topic_detail     : detalhes de um tópico específico
  - generate_questions   : gera questões no estilo residência
  - search_topics        : busca tópicos por nome/matéria
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import anthropic
from sqlalchemy.orm import Session

from medstudies.engine.planner import DailyPlanBuilder, DailyStudyPlan
from medstudies.engine.scorer import TopicScorer
from medstudies.engine.sm2 import SM2Engine
from medstudies.persistence.models import DailyPlan, Question, Subject, Topic

MODEL = os.getenv("MEDSTUDIES_CHAT_MODEL", "claude-haiku-4-5-20251001")

SYSTEM_PROMPT = """You are MedBot, an intelligent study assistant integrated with MedStudies — a personal medical residency exam preparation system.

You have access to the student's real performance data: question history, error rates, SM-2 spaced repetition state, and daily study plans. Use this data to give personalized, actionable guidance.

Student context:
- Brazilian medical doctor preparing for residency exams (ENARE/SUS style)
- Uses Medcof for mock exams, Anki for flashcards
- Study data tracked in MedStudies

Your behavior:
- Always answer in Portuguese (Brazilian)
- Be direct and objective — this is a busy medical student
- When asked about a topic, combine the performance data with medical knowledge
- For "what should I study?", use get_daily_plan or get_weak_topics first
- For explanations, use your medical knowledge directly (no tool needed)
- Generate questions in ENARE/SUS style when asked
- Reference actual error rates and scores from the data

Never make up performance data — always query tools for real numbers."""


def _tools() -> list[dict]:
    return [
        {
            "name": "get_daily_plan",
            "description": "Retorna o plano de estudos de hoje com tópicos priorizados, scores e ações recomendadas (REVIEW/PRACTICE/REINFORCE).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "max_topics": {
                        "type": "integer",
                        "description": "Número máximo de tópicos no plano (padrão 8)",
                        "default": 8,
                    }
                },
                "required": [],
            },
        },
        {
            "name": "get_weak_topics",
            "description": "Lista os tópicos com maior taxa de erro, ordenados por prioridade.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "top": {
                        "type": "integer",
                        "description": "Quantos tópicos retornar (padrão 10)",
                        "default": 10,
                    },
                    "min_questions": {
                        "type": "integer",
                        "description": "Mínimo de questões respondidas para incluir (padrão 3)",
                        "default": 3,
                    },
                },
                "required": [],
            },
        },
        {
            "name": "get_performance",
            "description": "Retorna resumo geral de desempenho: total de questões, acertos, erro%, streak, distribuição por matéria.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "get_topic_detail",
            "description": "Retorna detalhes de um tópico específico: erro%, questões respondidas, estado SM-2, última revisão.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "topic_name": {
                        "type": "string",
                        "description": "Nome do tópico (ex: 'DPOC', 'Sepse')",
                    },
                    "subject_name": {
                        "type": "string",
                        "description": "Nome da matéria (opcional, para desambiguar)",
                    },
                },
                "required": ["topic_name"],
            },
        },
        {
            "name": "search_topics",
            "description": "Busca tópicos por nome ou matéria.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Texto para buscar no nome do tópico ou matéria",
                    }
                },
                "required": ["query"],
            },
        },
    ]


def _execute_tool(tool_name: str, tool_input: dict, db: Session) -> str:
    if tool_name == "get_daily_plan":
        return _tool_daily_plan(tool_input, db)
    elif tool_name == "get_weak_topics":
        return _tool_weak_topics(tool_input, db)
    elif tool_name == "get_performance":
        return _tool_performance(db)
    elif tool_name == "get_topic_detail":
        return _tool_topic_detail(tool_input, db)
    elif tool_name == "search_topics":
        return _tool_search_topics(tool_input, db)
    return f"Ferramenta '{tool_name}' não encontrada."


def _tool_daily_plan(inp: dict, db: Session) -> str:
    max_topics = inp.get("max_topics", 8)
    SM2Engine(db).update_all()
    plan = DailyPlanBuilder(db, max_topics=max_topics).build()
    items = []
    for item in plan.items:
        items.append({
            "rank": item.rank,
            "action": item.action,
            "subject": item.subject_name,
            "topic": item.topic_name,
            "score": item.priority_score,
            "error_pct": item.error_rate_pct,
            "days_stale": item.days_since_review,
            "reason": item.reason,
        })
    return json.dumps({"plan_date": plan.plan_date, "items": items}, ensure_ascii=False)


def _tool_weak_topics(inp: dict, db: Session) -> str:
    top = inp.get("top", 10)
    min_q = inp.get("min_questions", 3)
    scorer = TopicScorer(db)
    scores = scorer.score_all()
    filtered = [s for s in scores if s.total_questions >= min_q]
    by_error = sorted(filtered, key=lambda s: s.error_rate, reverse=True)[:top]
    result = []
    for s in by_error:
        result.append({
            "subject": s.subject_name,
            "topic": s.topic_name,
            "error_pct": round(s.error_rate * 100, 1),
            "wrong": s.wrong_questions,
            "total": s.total_questions,
            "priority_score": s.priority_score,
            "sm2_overdue_days": s.sm2_days_overdue,
        })
    return json.dumps(result, ensure_ascii=False)


def _tool_performance(db: Session) -> str:
    questions = db.query(Question).all()
    total = len(questions)
    correct = sum(1 for q in questions if q.correct)
    wrong = total - correct
    accuracy = round(correct / total * 100, 1) if total else 0

    from collections import defaultdict
    by_subject: dict[str, dict] = defaultdict(lambda: {"total": 0, "correct": 0})
    for q in questions:
        subj = q.topic.subject.name
        by_subject[subj]["total"] += 1
        if q.correct:
            by_subject[subj]["correct"] += 1

    subjects = []
    for name, d in sorted(by_subject.items(), key=lambda x: x[1]["total"], reverse=True):
        err = round((d["total"] - d["correct"]) / d["total"] * 100, 1) if d["total"] else 0
        subjects.append({"subject": name, "total": d["total"], "error_pct": err})

    from datetime import timedelta
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    days_with_q = {q.answered_at.date() for q in questions}
    streak = 0
    check = now.date()
    while check in days_with_q:
        streak += 1
        check -= timedelta(days=1)

    topics_count = db.query(Topic).count()
    return json.dumps({
        "total_questions": total,
        "correct": correct,
        "wrong": wrong,
        "accuracy_pct": accuracy,
        "streak_days": streak,
        "topics_tracked": topics_count,
        "by_subject": subjects,
    }, ensure_ascii=False)


def _tool_topic_detail(inp: dict, db: Session) -> str:
    name = inp["topic_name"]
    subject = inp.get("subject_name")
    q = db.query(Topic)
    if subject:
        subj = db.query(Subject).filter(Subject.name.ilike(f"%{subject}%")).first()
        if subj:
            q = q.filter_by(subject_id=subj.id)
    topics = q.filter(Topic.name.ilike(f"%{name}%")).all()
    if not topics:
        return f"Tópico '{name}' não encontrado."
    results = []
    for t in topics[:3]:
        questions = t.questions
        total_q = len(questions)
        wrong_q = sum(1 for q in questions if not q.correct)
        error_rate = wrong_q / total_q if total_q else None
        last_q = max((q.answered_at for q in questions), default=None)
        sm2 = db.query(__import__("medstudies.persistence.models", fromlist=["TopicReview"]).TopicReview).filter_by(topic_id=t.id).first()
        results.append({
            "subject": t.subject.name,
            "topic": t.name,
            "total_questions": total_q,
            "wrong": wrong_q,
            "error_pct": round(error_rate * 100, 1) if error_rate is not None else None,
            "last_answered": last_q.strftime("%Y-%m-%d") if last_q else None,
            "anki_deck": t.anki_deck,
            "study_notes": t.study_notes or "",
            "sm2_interval_days": sm2.interval_days if sm2 else None,
            "sm2_next_review": sm2.next_review.strftime("%Y-%m-%d") if sm2 and sm2.next_review else None,
        })
    return json.dumps(results, ensure_ascii=False)


def _tool_search_topics(inp: dict, db: Session) -> str:
    query = inp["query"]
    topics = db.query(Topic).join(Subject).filter(
        Topic.name.ilike(f"%{query}%") | Subject.name.ilike(f"%{query}%")
    ).limit(20).all()
    result = [{"subject": t.subject.name, "topic": t.name, "anki_deck": t.anki_deck} for t in topics]
    return json.dumps(result, ensure_ascii=False)


class MedStudiesAgent:
    def __init__(self, db: Session, api_key: str | None = None):
        self._db = db
        self._client = anthropic.Anthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY"))
        self._messages: list[dict] = []

    def chat(self, user_message: str, stream_cb=None) -> str:
        """Send a message and return the assistant response."""
        self._messages.append({"role": "user", "content": user_message})

        while True:
            with self._client.messages.stream(
                model=MODEL,
                max_tokens=4096,
                system=[{
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=_tools(),
                messages=self._messages,
            ) as stream:
                response = stream.get_final_message()

            # Collect text from response
            text_blocks = [b.text for b in response.content if b.type == "text"]
            full_text = "\n".join(text_blocks)

            # Collect tool calls
            tool_calls = [b for b in response.content if b.type == "tool_use"]

            if not tool_calls:
                # Done — no more tool calls
                self._messages.append({"role": "assistant", "content": response.content})
                if stream_cb and full_text:
                    stream_cb(full_text)
                return full_text

            # Execute tools
            self._messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for tool in tool_calls:
                result = _execute_tool(tool.name, tool.input, self._db)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool.id,
                    "content": result,
                })
            self._messages.append({"role": "user", "content": tool_results})

    def reset(self):
        self._messages.clear()
