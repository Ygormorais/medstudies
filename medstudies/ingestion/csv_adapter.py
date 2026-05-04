"""
CSV/JSON mock-exam results adapter.

Expected CSV columns:
  topic_name, subject_name, source, answered_at (ISO), correct (true/false), notes

Expected JSON: list of objects with the same keys.
"""
from __future__ import annotations
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from medstudies.ingestion.base import BaseIngestionAdapter, IngestResult
from medstudies.persistence.models import Question, Subject, Topic


class CSVAdapter(BaseIngestionAdapter):

    def __init__(self, session: Session):
        self._session = session

    @property
    def source_name(self) -> str:
        return "csv"

    def ingest(self, file_path: str, **kwargs) -> IngestResult:
        result = IngestResult(source=self.source_name)
        path = Path(file_path)

        if not path.exists():
            result.errors.append(f"File not found: {file_path}")
            return result

        rows = self._load(path)
        for i, row in enumerate(rows, start=1):
            try:
                self._process_row(row, result)
            except Exception as exc:
                result.errors.append(f"Row {i}: {exc}")

        try:
            self._session.commit()
        except Exception as exc:
            self._session.rollback()
            result.errors.append(f"DB commit failed: {exc}")

        return result

    def _load(self, path: Path) -> list[dict]:
        if path.suffix.lower() == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
        rows = []
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(row)
        return rows

    def _process_row(self, row: dict, result: IngestResult) -> None:
        subject_name = row["subject_name"].strip()
        topic_name = row["topic_name"].strip()
        correct_raw = str(row.get("correct", "false")).strip().lower()
        correct = correct_raw in ("true", "1", "yes", "correct")

        subject = self._session.query(Subject).filter_by(name=subject_name).first()
        if not subject:
            subject = Subject(name=subject_name)
            self._session.add(subject)
            self._session.flush()

        topic = (
            self._session.query(Topic)
            .filter_by(name=topic_name, subject_id=subject.id)
            .first()
        )
        if not topic:
            topic = Topic(name=topic_name, subject_id=subject.id)
            self._session.add(topic)
            self._session.flush()

        answered_at_raw = row.get("answered_at", "")
        try:
            answered_at = datetime.fromisoformat(answered_at_raw)
        except (ValueError, TypeError):
            answered_at = datetime.now(timezone.utc).replace(tzinfo=None)

        q = Question(
            topic_id=topic.id,
            source=row.get("source", ""),
            answered_at=answered_at,
            correct=correct,
            notes=row.get("notes", ""),
        )
        self._session.add(q)
        result.records_created += 1
