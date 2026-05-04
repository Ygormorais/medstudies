"""Tests for CLI commands — question list, session list, topic edit."""
import pytest
from typer.testing import CliRunner
from medstudies.interface.cli import app
from medstudies.persistence.models import Question, StudySession, Topic, Subject
from medstudies.persistence import database as db_module
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from medstudies.persistence.models import Base

runner = CliRunner()


@pytest.fixture(autouse=True)
def patch_db(seeded_db, monkeypatch):
    """Redirect all CLI db calls to the in-memory seeded_db."""
    import medstudies.interface.cli as cli_module
    monkeypatch.setattr(cli_module, "get_session", lambda: seeded_db)
    yield


def test_question_list_shows_results():
    result = runner.invoke(app, ["question", "list"])
    assert result.exit_code == 0
    assert "Insuficiência Cardíaca" in result.output or "DPOC" in result.output


def test_question_list_wrong_only(seeded_db):
    result = runner.invoke(app, ["question", "list", "--wrong"])
    assert result.exit_code == 0
    # wrong filter — should show Errado rows
    assert "Errado" in result.output


def test_question_list_filter_by_subject():
    result = runner.invoke(app, ["question", "list", "--subject", "Cardiologia"])
    assert result.exit_code == 0
    assert "Cardiologia" in result.output


def test_session_list_empty(seeded_db):
    result = runner.invoke(app, ["session", "list"])
    assert result.exit_code == 0
    assert "Nenhuma sessão" in result.output


def test_session_list_with_data(seeded_db):
    ic = seeded_db.query(Topic).filter_by(name="Insuficiência Cardíaca").first()
    seeded_db.add(StudySession(topic_id=ic.id, session_type="review", duration_minutes=45))
    seeded_db.commit()

    result = runner.invoke(app, ["session", "list"])
    assert result.exit_code == 0
    assert "Insuficiência Cardíaca" in result.output
    assert "45min" in result.output


def test_topic_edit_notes(seeded_db):
    result = runner.invoke(app, [
        "topic", "edit", "Insuficiência Cardíaca",
        "--subject", "Cardiologia",
        "--notes", "Minha nota de estudo",
    ])
    assert result.exit_code == 0
    assert "atualizado" in result.output

    ic = seeded_db.query(Topic).filter_by(name="Insuficiência Cardíaca").first()
    assert ic.study_notes == "Minha nota de estudo"


def test_topic_edit_rename(seeded_db):
    result = runner.invoke(app, [
        "topic", "edit", "DPOC",
        "--subject", "Pneumologia",
        "--rename", "DPOC Renomeado",
    ])
    assert result.exit_code == 0
    renamed = seeded_db.query(Topic).filter_by(name="DPOC Renomeado").first()
    assert renamed is not None


def test_topic_edit_not_found(seeded_db):
    result = runner.invoke(app, [
        "topic", "edit", "NaoExiste",
        "--subject", "Cardiologia",
        "--notes", "x",
    ])
    assert result.exit_code == 1
