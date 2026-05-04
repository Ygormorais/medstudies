"""
MockExamAdapter — registra um bloco inteiro de questões de um simulado.

Aceita uma lista de dicts com:
    topic_name, subject_name, correct (bool), notes (opcional)

Agrupa tudo sob um mesmo source (ex: "Medcof Mock 5") e answered_at.
"""
from __future__ import annotations
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from medstudies.ingestion.base import BaseIngestionAdapter, IngestResult
from medstudies.persistence.models import Question, Subject, Topic


class MockExamAdapter(BaseIngestionAdapter):

    def __init__(self, session: Session):
        self._session = session

    @property
    def source_name(self) -> str:
        return "mock_exam"

    def ingest(
        self,
        questions: list[dict],
        source: str,
        answered_at: datetime | None = None,
        **kwargs,
    ) -> IngestResult:
        result = IngestResult(source=self.source_name)
        answered_at = answered_at or datetime.now(timezone.utc).replace(tzinfo=None)

        for i, q in enumerate(questions, start=1):
            try:
                self._process(q, source, answered_at, result)
            except Exception as exc:
                result.errors.append(f"Questão {i}: {exc}")

        try:
            self._session.commit()
        except Exception as exc:
            self._session.rollback()
            result.errors.append(f"DB commit failed: {exc}")

        result.metadata["source"] = source
        result.metadata["total"] = len(questions)
        result.metadata["correct"] = sum(1 for q in questions if str(q.get("correct","")).lower() in ("true","1","yes","correct"))
        return result

    def _process(self, row: dict, source: str, answered_at: datetime, result: IngestResult):
        subject_name = row["subject_name"].strip()
        topic_name   = row["topic_name"].strip()
        if not subject_name or not topic_name:
            raise ValueError(f"subject_name e topic_name obrigatórios (got: '{subject_name}'/'{topic_name}')")
        correct_raw  = str(row.get("correct", "false")).strip().lower()
        correct      = correct_raw in ("true", "1", "yes", "correct")

        subject = self._session.query(Subject).filter_by(name=subject_name).first()
        if not subject:
            subject = Subject(name=subject_name)
            self._session.add(subject)
            self._session.flush()

        topic = self._session.query(Topic).filter_by(name=topic_name, subject_id=subject.id).first()
        if not topic:
            topic = Topic(name=topic_name, subject_id=subject.id)
            self._session.add(topic)
            self._session.flush()

        self._session.add(Question(
            topic_id=topic.id,
            source=source,
            answered_at=answered_at,
            correct=correct,
            notes=row.get("notes", "") or None,
            statement=row.get("statement", "") or None,
        ))
        result.records_created += 1
