"""Tests for ingestion adapters."""
import json
import csv
import tempfile
from pathlib import Path

from medstudies.ingestion.mock_exam_adapter import MockExamAdapter
from medstudies.ingestion.csv_adapter import CSVAdapter
from medstudies.persistence.models import Question, Subject, Topic


def test_mock_exam_creates_questions(db):
    adapter = MockExamAdapter(db)
    questions = [
        {"subject_name": "Cardiologia", "topic_name": "IC", "correct": "true"},
        {"subject_name": "Cardiologia", "topic_name": "IC", "correct": "false"},
        {"subject_name": "Pneumologia", "topic_name": "DPOC", "correct": "false"},
    ]
    result = adapter.ingest(questions=questions, source="Teste Mock")
    assert result.ok
    assert result.records_created == 3
    assert db.query(Question).count() == 3


def test_mock_exam_auto_creates_subject_and_topic(db):
    adapter = MockExamAdapter(db)
    adapter.ingest(
        questions=[{"subject_name": "Nova Matéria", "topic_name": "Novo Tópico", "correct": "true"}],
        source="test",
    )
    assert db.query(Subject).filter_by(name="Nova Matéria").first() is not None
    assert db.query(Topic).filter_by(name="Novo Tópico").first() is not None


def test_mock_exam_metadata(db):
    adapter = MockExamAdapter(db)
    questions = [
        {"subject_name": "A", "topic_name": "X", "correct": "true"},
        {"subject_name": "A", "topic_name": "X", "correct": "false"},
        {"subject_name": "A", "topic_name": "X", "correct": "false"},
    ]
    result = adapter.ingest(questions=questions, source="test")
    assert result.metadata["total"] == 3
    assert result.metadata["correct"] == 1


def test_mock_exam_skips_invalid_row(db):
    adapter = MockExamAdapter(db)
    questions = [
        {"subject_name": "A", "topic_name": "X", "correct": "true"},
        {"subject_name": "", "topic_name": "", "correct": "true"},  # empty — should error
    ]
    result = adapter.ingest(questions=questions, source="test")
    # 1 valid + 1 error
    assert result.records_created == 1
    assert len(result.errors) == 1


def test_csv_adapter_from_file(db):
    rows = [
        {"subject_name": "Cardiologia", "topic_name": "IC", "correct": "true", "source": "csv-test", "answered_at": "", "notes": ""},
        {"subject_name": "Cardiologia", "topic_name": "IC", "correct": "false", "source": "csv-test", "answered_at": "", "notes": ""},
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        path = f.name

    adapter = CSVAdapter(db)
    result = adapter.ingest(file_path=path)
    assert result.ok
    assert result.records_created == 2
    Path(path).unlink()


def test_csv_adapter_json_file(db):
    rows = [
        {"subject_name": "Pneumologia", "topic_name": "Asma", "correct": "true", "source": "json-test"},
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(rows, f)
        path = f.name

    adapter = CSVAdapter(db)
    result = adapter.ingest(file_path=path)
    assert result.ok
    assert result.records_created == 1
    Path(path).unlink()


def test_csv_adapter_missing_file(db):
    adapter = CSVAdapter(db)
    result = adapter.ingest(file_path="/nao/existe.csv")
    assert not result.ok
    assert result.records_created == 0
