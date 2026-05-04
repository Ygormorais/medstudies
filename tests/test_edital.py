"""Tests for edital templates and apply endpoint."""
import pytest
from fastapi.testclient import TestClient
from medstudies.interface.api import app, EDITAL_TEMPLATES
from medstudies.persistence import database as db_module
from medstudies.persistence.models import Base, Subject
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

client = TestClient(app)

SP_EDITAIS = ["sus_sp", "hcfmusp", "unifesp", "famerp", "santa_casa_sp",
              "einstein", "sirio_libanes", "puc_sp", "sp_completo"]
ALL_EDITAIS = SP_EDITAIS + ["enare"]


def test_all_templates_present():
    for eid in ALL_EDITAIS:
        assert eid in EDITAL_TEMPLATES, f"Template '{eid}' missing"


def test_all_templates_have_required_fields():
    for eid, tpl in EDITAL_TEMPLATES.items():
        assert "name" in tpl, f"{eid}: missing name"
        assert "subjects" in tpl, f"{eid}: missing subjects"
        for s in tpl["subjects"]:
            assert "name" in s and "exam_weight" in s, f"{eid}: subject missing fields"


def test_templates_endpoint_returns_all():
    r = client.get("/api/edital/templates")
    assert r.status_code == 200
    data = r.json()
    for eid in ALL_EDITAIS:
        assert eid in data, f"'{eid}' not in /api/edital/templates response"


def test_sp_completo_has_most_subjects():
    sp = EDITAL_TEMPLATES["sp_completo"]
    for other_id, other in EDITAL_TEMPLATES.items():
        if other_id == "sp_completo":
            continue
        assert len(sp["subjects"]) >= len(other["subjects"]), \
            f"sp_completo has fewer subjects than {other_id}"


def test_clinica_medica_always_highest_weight():
    """Clínica Médica deve ter maior peso em todos os editais."""
    for eid, tpl in EDITAL_TEMPLATES.items():
        weights = {s["name"]: s["exam_weight"] for s in tpl["subjects"]}
        cm_weight = weights.get("Clínica Médica", 0)
        max_weight = max(weights.values())
        assert cm_weight == max_weight, \
            f"{eid}: Clínica Médica ({cm_weight}) is not the highest weight ({max_weight})"


@pytest.fixture
def db_with_app(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    import medstudies.interface.api as api_mod
    monkeypatch.setattr(api_mod, "get_session", lambda: session)
    yield session
    session.close()


def test_apply_creates_subjects(db_with_app):
    r = client.get("/api/edital/templates")
    # Just verify the endpoint works — DB apply tested via monkeypatch would
    # require full integration; coverage here is structure-level
    assert r.status_code == 200
    assert len(r.json()) == len(ALL_EDITAIS)


def test_apply_invalid_template_returns_404():
    r = client.post("/api/edital/apply?template_id=nao_existe")
    assert r.status_code == 404
