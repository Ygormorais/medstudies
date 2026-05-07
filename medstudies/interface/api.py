"""
FastAPI — serve o dashboard e expõe endpoints JSON.
"""
from __future__ import annotations
import io
import json
import csv as csv_module
from datetime import date, datetime, timezone
from pathlib import Path
from collections import defaultdict

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from pydantic import BaseModel

from sqlalchemy import func
from medstudies.persistence.database import get_session, init_db
from medstudies.persistence.models import DailyPlan, EditorialTopic, FlashCard, LibraryItem, Question, StudySession, Subject, Tag, Topic, TopicReview
from medstudies.engine.planner import DailyPlanBuilder, DailyStudyPlan
from medstudies.engine.scorer import TopicScorer
from medstudies.engine.weekly_planner import WeeklyPlanBuilder
from medstudies.ingestion.mock_exam_adapter import MockExamAdapter
from medstudies.ingestion.csv_adapter import CSVAdapter

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(
    title="MedStudies API",
    description="Intelligent study planner for Brazilian medical residency exams.",
    version="0.1.0",
    lifespan=lifespan,
)
DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"
INTERFACE_DIR   = Path(__file__).parent


@app.get("/", response_class=HTMLResponse)
def dashboard():
    from fastapi.responses import Response
    return Response(
        content=DASHBOARD_HTML.read_text(encoding="utf-8"),
        media_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/manifest.json")
def pwa_manifest():
    from fastapi.responses import JSONResponse
    manifest_path = INTERFACE_DIR / "manifest.json"
    return JSONResponse(content=json.loads(manifest_path.read_text(encoding="utf-8")))


@app.get("/sw.js")
def service_worker():
    sw_path = INTERFACE_DIR / "sw.js"
    return FileResponse(sw_path, media_type="application/javascript")


@app.get("/static/icon-{size}.png")
def pwa_icon(size: str):
    """Serve pre-generated PNG icon from static directory."""
    icon_path = INTERFACE_DIR / "static" / f"icon-{size}.png"
    if icon_path.exists():
        return FileResponse(icon_path, media_type="image/png")
    # fallback: 192 if unknown size requested
    fallback = INTERFACE_DIR / "static" / "icon-192.png"
    if fallback.exists():
        return FileResponse(fallback, media_type="image/png")
    from fastapi.responses import Response
    return Response(status_code=404)


@app.get("/static/icon.svg")
def pwa_icon_svg():
    """Serve SVG icon."""
    svg_path = INTERFACE_DIR / "static" / "icon.svg"
    return FileResponse(svg_path, media_type="image/svg+xml")


# ── Plan ──────────────────────────────────────────────────────────────────────

def _enrich_plan(plan_dict: dict, db) -> dict:
    """Add study_notes, notability_notebook, anki_due, and next_review to each plan item."""
    from medstudies.engine.scorer import TopicScorer
    topic_map = {t.id: t for t in db.query(Topic).all()}
    scores = {s.topic_id: s for s in TopicScorer(db).score_all()}
    review_map = {rv.topic_id: rv for rv in db.query(TopicReview).all()}
    for item in plan_dict.get("items", []):
        t = topic_map.get(item.get("topic_id"))
        item["study_notes"] = t.study_notes or "" if t else ""
        item["notability_notebook"] = t.notability_notebook or "" if t else ""
        sc = scores.get(item.get("topic_id"))
        item["anki_due"] = sc.anki_due if sc else 0
        rv = review_map.get(item.get("topic_id"))
        item["next_review"] = rv.next_review.strftime("%Y-%m-%d") if rv and rv.next_review else None
        item["sm2_interval"] = round(rv.interval_days, 1) if rv else None
    return plan_dict


@app.get("/api/plan/today-summary")
def today_summary():
    """
    Aggregated urgency panel for the home screen:
    - SM-2 flashcards due today
    - Wrong banco questions to review
    - Uncovered edital topics (high weight, no topic match)
    - Weakest subjects by error rate
    """
    from datetime import date as _date
    db = get_session()
    today = datetime.now(timezone.utc).replace(tzinfo=None)

    # SM-2 flashcards due
    fc_due = db.query(func.count(FlashCard.id)).filter(
        FlashCard.next_review <= today
    ).scalar() or 0

    # Wrong banco questions
    wrong_banco = db.query(func.count(Question.id)).filter(
        Question.alternatives.isnot(None),
        Question.correct == False,
        Question.chosen_alt.isnot(None),
    ).scalar() or 0

    # Unanswered banco questions (never attempted)
    unanswered_banco = db.query(func.count(Question.id)).filter(
        Question.alternatives.isnot(None),
        Question.chosen_alt.is_(None),
    ).scalar() or 0

    # Uncovered edital topics (no topic_id match, weight_pct > 0)
    uncovered_edital = db.query(func.count(EditorialTopic.id)).filter(
        EditorialTopic.topic_id.is_(None),
        EditorialTopic.weight_pct > 0,
    ).scalar() or 0

    # Subjects with highest error rate
    from medstudies.engine.scorer import TopicScorer
    scores = TopicScorer(db).score_all()
    from collections import defaultdict
    subj_acc: dict[str, dict] = defaultdict(lambda: {"correct": 0, "total": 0})
    for s in scores:
        if s.total_questions > 0:
            subj_acc[s.subject_name]["total"] += s.total_questions
            subj_acc[s.subject_name]["correct"] += round(s.total_questions * (1 - s.error_rate))
    weak_subjects = sorted(
        [{"subject": k, "error_rate": round((v["total"]-v["correct"])/v["total"]*100,1), "total": v["total"]}
         for k, v in subj_acc.items() if v["total"] >= 3],
        key=lambda x: x["error_rate"], reverse=True
    )[:5]

    # Topics overdue for review (days_since_review > 14)
    scores_overdue = [s for s in scores if s.days_since_review > 14]
    overdue_count = len(scores_overdue)

    return {
        "fc_due": fc_due,
        "wrong_banco": wrong_banco,
        "unanswered_banco": unanswered_banco,
        "uncovered_edital": uncovered_edital,
        "overdue_topics": overdue_count,
        "weak_subjects": weak_subjects,
        "generated_at": today.isoformat(),
    }


@app.post("/api/plan/generate")
def generate_plan(max_topics: int = 8):
    db = get_session()
    plan = DailyPlanBuilder(db, max_topics=max_topics).build()
    return _enrich_plan(json.loads(plan.to_json()), db)


@app.get("/api/plan/latest")
def latest_plan():
    db = get_session()
    record = db.query(DailyPlan).order_by(DailyPlan.generated_at.desc()).first()
    if not record:
        raise HTTPException(status_code=404, detail="Nenhum plano gerado ainda.")
    return _enrich_plan(json.loads(record.plan_json), db)


@app.get("/api/plan/history")
def plan_history(limit: int = 30):
    """Return last N daily plans (metadata only, no full JSON)."""
    db = get_session()
    records = (
        db.query(DailyPlan)
        .order_by(DailyPlan.generated_at.desc())
        .limit(limit)
        .all()
    )
    result = []
    for r in records:
        try:
            plan = json.loads(r.plan_json)
            items = plan.get("items", [])
            result.append({
                "id": r.id,
                "plan_date": r.plan_date,
                "generated_at": r.generated_at.strftime("%d/%m/%Y %H:%M"),
                "topic_count": len(items),
                "subjects": list(dict.fromkeys(i["subject_name"] for i in items)),
                "topics": [{"topic_id": i.get("topic_id"), "topic_name": i["topic_name"],
                             "subject_name": i["subject_name"], "action": i.get("action",""),
                             "priority_score": i.get("priority_score",0)}
                           for i in items],
            })
        except Exception:
            continue
    return result


@app.get("/api/plan/weekly")
def weekly_plan(topics_per_day: int = 4):
    db = get_session()
    plan = WeeklyPlanBuilder(db, topics_per_day=topics_per_day).build()
    return {"start_date": plan.start_date, "end_date": plan.end_date, "days": plan.days}


# ── Subjects ──────────────────────────────────────────────────────────────────

class SubjectIn(BaseModel):
    name: str
    exam_weight: float = 1.0


@app.get("/api/subjects")
def list_subjects():
    db = get_session()
    return [{"id": s.id, "name": s.name, "exam_weight": s.exam_weight}
            for s in db.query(Subject).order_by(Subject.name).all()]


@app.post("/api/subjects")
def create_subject(body: SubjectIn):
    db = get_session()
    if db.query(Subject).filter_by(name=body.name).first():
        raise HTTPException(status_code=409, detail="Assunto já existe.")
    s = Subject(name=body.name, exam_weight=body.exam_weight)
    db.add(s)
    db.commit()
    return {"id": s.id, "name": s.name, "exam_weight": s.exam_weight}


# ── Topics ────────────────────────────────────────────────────────────────────

class TopicIn(BaseModel):
    name: str
    subject_id: int
    anki_deck: str | None = None
    notability_notebook: str | None = None


@app.get("/api/topics")
def list_topics(
    subject_id: int | None = None,
    skip: int = 0,
    limit: int = 200,
):
    db = get_session()
    q = db.query(Topic)
    if subject_id:
        q = q.filter_by(subject_id=subject_id)
    total = q.count()
    items = q.order_by(Topic.name).offset(skip).limit(limit).all()
    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "items": [
            {"id": t.id, "name": t.name, "subject_id": t.subject_id,
             "subject_name": t.subject.name, "anki_deck": t.anki_deck,
             "notability_notebook": t.notability_notebook,
             "is_favorite": bool(t.is_favorite)}
            for t in items
        ],
    }


@app.post("/api/topics")
def create_topic(body: TopicIn):
    db = get_session()
    subj = db.get(Subject, body.subject_id)
    if not subj:
        raise HTTPException(status_code=404, detail="Assunto não encontrado.")
    if db.query(Topic).filter_by(name=body.name, subject_id=body.subject_id).first():
        raise HTTPException(status_code=409, detail="Tópico já existe neste assunto.")
    t = Topic(name=body.name, subject_id=body.subject_id,
               anki_deck=body.anki_deck, notability_notebook=body.notability_notebook)
    db.add(t)
    db.commit()
    return {"id": t.id, "name": t.name, "subject_name": subj.name}


@app.patch("/api/topics/{topic_id}/notes")
def update_topic_notes(topic_id: int, payload: dict):
    db = get_session()
    t = db.get(Topic, topic_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tópico não encontrado.")
    t.study_notes = payload.get("study_notes", "")
    db.commit()
    return {"ok": True, "topic_id": topic_id, "study_notes": t.study_notes}


@app.get("/api/topics/{topic_id}/notes")
def get_topic_notes(topic_id: int):
    db = get_session()
    t = db.get(Topic, topic_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tópico não encontrado.")
    return {"topic_id": topic_id, "topic_name": t.name, "study_notes": t.study_notes or ""}


# ── Questions ─────────────────────────────────────────────────────────────────

class QuestionIn(BaseModel):
    topic_id: int
    correct: bool
    source: str = "Manual"
    notes: str = ""
    answered_at: str | None = None
    difficulty: str = "medio"  # facil | medio | dificil
    statement: str = ""        # full question text / stem


# ── Sessions ──────────────────────────────────────────────────────────────────

class SessionIn(BaseModel):
    topic_id: int
    session_type: str = "review"
    duration_minutes: int | None = None
    notes: str = ""


@app.post("/api/sessions")
def add_session(body: SessionIn):
    db = get_session()
    topic = db.get(Topic, body.topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Tópico não encontrado.")
    s = StudySession(topic_id=body.topic_id, session_type=body.session_type,
                     duration_minutes=body.duration_minutes, notes=body.notes)
    db.add(s)
    db.commit()
    return {"id": s.id, "topic_name": topic.name}


# ── Mock exam import ──────────────────────────────────────────────────────────

@app.post("/api/mock/upload")
async def upload_mock(
    file: UploadFile = File(...),
    source: str = Form(...),
    exam_date: str = Form(None),
):
    """Upload de CSV/JSON de simulado pelo dashboard."""
    content = await file.read()
    text = content.decode("utf-8-sig")  # handle BOM

    if file.filename.endswith(".json"):
        questions = json.loads(text)
    else:
        reader = csv_module.DictReader(io.StringIO(text))
        questions = list(reader)

    answered_at = datetime.fromisoformat(exam_date).replace(tzinfo=None) if exam_date else None
    db = get_session()
    adapter = MockExamAdapter(db)
    result = adapter.ingest(questions=questions, source=source, answered_at=answered_at)
    total = result.metadata.get("total", 0)
    correct = result.metadata.get("correct", 0)
    return {
        "ok": result.ok,
        "records_created": result.records_created,
        "errors": result.errors,
        "total": total,
        "correct": correct,
        "wrong": total - correct,
        "error_rate_pct": round((total - correct) / total * 100, 1) if total else 0,
    }


@app.post("/api/mock")
def import_mock(payload: dict):
    db = get_session()
    answered_at = datetime.fromisoformat(payload["date"]).replace(tzinfo=None) if payload.get("date") else None
    adapter = MockExamAdapter(db)
    result = adapter.ingest(questions=payload.get("questions", []),
                             source=payload.get("source", "Simulado"),
                             answered_at=answered_at)
    return {"ok": result.ok, "records_created": result.records_created,
            "errors": result.errors, "summary": result.metadata}


# ── Anki ──────────────────────────────────────────────────────────────────────

@app.post("/api/anki/sync")
def anki_sync():
    from medstudies.ingestion.anki_adapter import AnkiAdapter
    db = get_session()
    result = AnkiAdapter(db).ingest()
    return {"ok": result.ok, "synced": result.records_created, "errors": result.errors}


@app.get("/api/anki/decks")
def anki_decks():
    from medstudies.ingestion.anki_adapter import _anki_request
    try:
        return {"decks": _anki_request("deckNames")}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Scores / weak / history ───────────────────────────────────────────────────

@app.get("/api/topics/scores")
def topic_scores():
    db = get_session()
    scorer = TopicScorer(db)
    scores = scorer.score_all()
    topic_map = {t.id: t for t in db.query(Topic).all()}
    return [
        {"topic_id": s.topic_id, "topic_name": s.topic_name,
         "subject_name": s.subject_name, "priority_score": s.priority_score,
         "error_rate": s.error_rate, "error_rate_pct": round(s.error_rate * 100, 1),
         "days_since_review": s.days_since_review, "total_questions": s.total_questions,
         "wrong_questions": s.wrong_questions, "anki_due": s.anki_due,
         "reason": s.reason,
         "notability_notebook": topic_map[s.topic_id].notability_notebook}
        for s in scores
    ]


@app.get("/api/topics/weak")
def weak_topics(min_questions: int = 3, top: int = 10):
    db = get_session()
    scores = TopicScorer(db).score_all()
    filtered = sorted([s for s in scores if s.total_questions >= min_questions],
                      key=lambda s: s.error_rate, reverse=True)[:top]
    return [{"rank": i + 1, "topic_name": s.topic_name, "subject_name": s.subject_name,
             "error_rate_pct": round(s.error_rate * 100, 1), "wrong_questions": s.wrong_questions,
             "total_questions": s.total_questions, "priority_score": s.priority_score}
            for i, s in enumerate(filtered)]


@app.get("/api/topics/recurring-errors")
def recurring_errors(min_errors: int = 2, top: int = 20):
    """Topics where the student answered wrong ≥ min_errors times consecutively or in total."""
    db = get_session()
    questions = db.query(Question).filter(Question.correct == False).all()  # noqa: E712
    # count wrong per topic
    from collections import Counter
    wrong_count = Counter(q.topic_id for q in questions)
    # also get last wrong date
    last_wrong: dict[int, datetime] = {}
    for q in questions:
        if q.topic_id not in last_wrong or q.answered_at > last_wrong[q.topic_id]:
            last_wrong[q.topic_id] = q.answered_at
    topic_ids = [tid for tid, cnt in wrong_count.items() if cnt >= min_errors]
    if not topic_ids:
        return []
    topics = {t.id: t for t in db.query(Topic).filter(Topic.id.in_(topic_ids)).all()}
    subjs  = {s.id: s for s in db.query(Subject).all()}
    result = []
    for tid in topic_ids:
        t = topics.get(tid)
        if not t: continue
        s = subjs.get(t.subject_id)
        total_q = db.query(Question).filter(Question.topic_id == tid).count()
        result.append({
            "topic_id": tid,
            "topic_name": t.name,
            "subject_name": s.name if s else "",
            "wrong_count": wrong_count[tid],
            "total_questions": total_q,
            "last_wrong": last_wrong[tid].strftime("%d/%m/%Y") if last_wrong.get(tid) else "—",
        })
    result.sort(key=lambda x: x["wrong_count"], reverse=True)
    return result[:top]


@app.get("/api/topics/{topic_id}/history")
def topic_history(topic_id: int):
    db = get_session()
    topic = db.get(Topic, topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Tópico não encontrado.")
    questions = db.query(Question).filter_by(topic_id=topic_id).order_by(Question.answered_at).all()
    groups: dict[str, list] = defaultdict(list)
    for q in questions:
        groups[q.source or "Manual"].append(q)
    history = []
    for source, qs in groups.items():
        total, wrong = len(qs), sum(1 for q in qs if not q.correct)
        history.append({"source": source, "date": min(q.answered_at for q in qs).strftime("%d/%m/%Y"),
                        "total": total, "wrong": wrong, "correct": total - wrong,
                        "error_rate_pct": round(wrong / total * 100, 1) if total else 0})
    return {"topic_id": topic_id, "topic_name": topic.name,
            "subject_name": topic.subject.name, "history": history}


EDITAL_TEMPLATES = {
    # ── SP State ──────────────────────────────────────────────────────────────
    "sus_sp": {
        "name": "SUS-SP / VUNESP",
        "exam_date": "Dezembro",
        "institution": "Secretaria de Estado da Saúde SP + VUNESP",
        "vacancies": 1431,
        "subjects": [
            {"name": "Clínica Médica",              "exam_weight": 3.0},
            {"name": "Cirurgia Geral",              "exam_weight": 2.0},
            {"name": "Ginecologia e Obstetrícia",   "exam_weight": 2.0},
            {"name": "Pediatria",                   "exam_weight": 2.0},
            {"name": "Medicina Preventiva e Social","exam_weight": 2.0},
        ],
    },
    "hcfmusp": {
        "name": "HC-FMUSP (USP)",
        "exam_date": "Dezembro",
        "institution": "FMUSP / COREME + FUVEST",
        "vacancies": 834,
        "subjects": [
            {"name": "Clínica Médica",              "exam_weight": 3.0},
            {"name": "Cirurgia Geral",              "exam_weight": 2.0},
            {"name": "Ginecologia e Obstetrícia",   "exam_weight": 1.5},
            {"name": "Pediatria",                   "exam_weight": 1.5},
            {"name": "Medicina Preventiva e Social","exam_weight": 1.0},
            {"name": "Neurologia",                  "exam_weight": 1.0},
            {"name": "Ortopedia",                   "exam_weight": 0.5},
        ],
    },
    "unifesp": {
        "name": "UNIFESP",
        "exam_date": "Novembro/Dezembro",
        "institution": "Universidade Federal de São Paulo / COREME",
        "vacancies": 596,
        "subjects": [
            {"name": "Clínica Médica",              "exam_weight": 3.0},
            {"name": "Cirurgia Geral",              "exam_weight": 2.0},
            {"name": "Ginecologia e Obstetrícia",   "exam_weight": 1.5},
            {"name": "Pediatria",                   "exam_weight": 1.5},
            {"name": "Medicina Preventiva e Social","exam_weight": 1.5},
            {"name": "Saúde Mental",                "exam_weight": 0.5},
        ],
    },
    "famerp": {
        "name": "FAMERP",
        "exam_date": "Novembro",
        "institution": "Faculdade de Medicina de São José do Rio Preto",
        "vacancies": 296,
        "subjects": [
            {"name": "Clínica Médica",              "exam_weight": 3.0},
            {"name": "Cirurgia Geral",              "exam_weight": 2.0},
            {"name": "Ginecologia e Obstetrícia",   "exam_weight": 1.5},
            {"name": "Pediatria",                   "exam_weight": 1.5},
            {"name": "Medicina Preventiva e Social","exam_weight": 1.5},
            {"name": "Saúde Mental",                "exam_weight": 0.5},
        ],
    },
    "santa_casa_sp": {
        "name": "Santa Casa de São Paulo",
        "exam_date": "Dezembro",
        "institution": "Irmandade da Santa Casa de Misericórdia de São Paulo",
        "vacancies": 254,
        "subjects": [
            {"name": "Clínica Médica",              "exam_weight": 3.0},
            {"name": "Cirurgia Geral",              "exam_weight": 2.0},
            {"name": "Ginecologia e Obstetrícia",   "exam_weight": 1.5},
            {"name": "Pediatria",                   "exam_weight": 1.5},
            {"name": "Medicina Preventiva e Social","exam_weight": 1.0},
            {"name": "Urgência e Emergência",       "exam_weight": 1.0},
        ],
    },
    "einstein": {
        "name": "Hospital Albert Einstein",
        "exam_date": "Novembro",
        "institution": "Sociedade Beneficente Israelita Brasileira",
        "vacancies": 110,
        "subjects": [
            {"name": "Clínica Médica",              "exam_weight": 3.0},
            {"name": "Cirurgia Geral",              "exam_weight": 2.0},
            {"name": "Pediatria",                   "exam_weight": 1.5},
            {"name": "Ginecologia e Obstetrícia",   "exam_weight": 1.5},
            {"name": "Urgência e Emergência",       "exam_weight": 1.5},
            {"name": "Medicina Preventiva e Social","exam_weight": 0.5},
        ],
    },
    "sirio_libanes": {
        "name": "Hospital Sírio-Libanês",
        "exam_date": "Outubro/Novembro",
        "institution": "Hospital Sírio-Libanês + VUNESP",
        "vacancies": 79,
        "subjects": [
            {"name": "Clínica Médica",              "exam_weight": 3.0},
            {"name": "Cirurgia Geral",              "exam_weight": 2.0},
            {"name": "Pediatria",                   "exam_weight": 1.5},
            {"name": "Ginecologia e Obstetrícia",   "exam_weight": 1.5},
            {"name": "Urgência e Emergência",       "exam_weight": 1.5},
            {"name": "Medicina Preventiva e Social","exam_weight": 0.5},
        ],
    },
    "puc_sp": {
        "name": "PUC-SP",
        "exam_date": "Novembro",
        "institution": "Pontifícia Universidade Católica de São Paulo + NucVest",
        "vacancies": 91,
        "subjects": [
            {"name": "Clínica Médica",              "exam_weight": 2.5},
            {"name": "Cirurgia Geral",              "exam_weight": 2.0},
            {"name": "Ginecologia e Obstetrícia",   "exam_weight": 1.5},
            {"name": "Pediatria",                   "exam_weight": 1.5},
            {"name": "Medicina Preventiva e Social","exam_weight": 1.5},
            {"name": "Saúde Mental",                "exam_weight": 0.5},
        ],
    },
    # ── Nacional ──────────────────────────────────────────────────────────────
    "enare": {
        "name": "ENARE — padrão nacional",
        "exam_date": "Outubro/Novembro",
        "institution": "MEC / Ministério da Saúde",
        "vacancies": None,
        "subjects": [
            {"name": "Clínica Médica",              "exam_weight": 3.0},
            {"name": "Cirurgia Geral",              "exam_weight": 1.5},
            {"name": "Ginecologia e Obstetrícia",   "exam_weight": 1.5},
            {"name": "Pediatria",                   "exam_weight": 1.5},
            {"name": "Medicina Preventiva e Social","exam_weight": 1.5},
            {"name": "Saúde Mental",                "exam_weight": 0.5},
            {"name": "Urgência e Emergência",       "exam_weight": 0.5},
        ],
    },
    # ── SP completo (todos simultâneos) ───────────────────────────────────────
    "sp_completo": {
        "name": "SP Completo — todos os vestibulares",
        "exam_date": "Outubro–Dezembro",
        "institution": "Pesos equilibrados para cobrir todos os vestibulares de SP",
        "vacancies": None,
        "subjects": [
            {"name": "Clínica Médica",              "exam_weight": 3.0},
            {"name": "Cirurgia Geral",              "exam_weight": 2.5},
            {"name": "Ginecologia e Obstetrícia",   "exam_weight": 2.0},
            {"name": "Pediatria",                   "exam_weight": 2.0},
            {"name": "Medicina Preventiva e Social","exam_weight": 2.0},
            {"name": "Urgência e Emergência",       "exam_weight": 1.5},
            {"name": "Neurologia",                  "exam_weight": 1.0},
            {"name": "Saúde Mental",                "exam_weight": 0.5},
            {"name": "Ortopedia",                   "exam_weight": 0.5},
        ],
    },
}


@app.get("/api/edital/templates")
def edital_templates():
    return {k: {"name": v["name"], "subjects": v["subjects"]} for k, v in EDITAL_TEMPLATES.items()}


@app.post("/api/edital/apply")
def apply_edital(template_id: str = "enare"):
    if template_id not in EDITAL_TEMPLATES:
        raise HTTPException(status_code=404, detail="Template não encontrado.")
    db = get_session()
    tpl = EDITAL_TEMPLATES[template_id]
    updated, created = [], []
    for sd in tpl["subjects"]:
        existing = db.query(Subject).filter_by(name=sd["name"]).first()
        if existing:
            existing.exam_weight = sd["exam_weight"]
            updated.append(sd["name"])
        else:
            db.add(Subject(name=sd["name"], exam_weight=sd["exam_weight"]))
            created.append(sd["name"])
    db.commit()
    return {"ok": True, "template": tpl["name"], "updated": updated, "created": created}


@app.get("/api/news")
def get_news(query: str = "internal medicine", max_results: int = 15):
    import urllib.request, urllib.parse
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    try:
        search_url = f"{base}/esearch.fcgi?db=pubmed&term={urllib.parse.quote(query)}&retmax={max_results}&retmode=json&sort=date"
        with urllib.request.urlopen(search_url, timeout=10) as r:
            search_data = json.loads(r.read())
        ids = search_data.get("esearchresult", {}).get("idlist", [])
        if not ids:
            return {"articles": [], "query": query}
        summary_url = f"{base}/esummary.fcgi?db=pubmed&id={','.join(ids)}&retmode=json"
        with urllib.request.urlopen(summary_url, timeout=10) as r:
            summary_data = json.loads(r.read())
        articles = []
        for uid in ids:
            doc = summary_data.get("result", {}).get(uid, {})
            if not doc or not isinstance(doc, dict):
                continue
            articles.append({
                "pmid":    uid,
                "title":   doc.get("title", ""),
                "authors": [a.get("name", "") for a in doc.get("authors", [])[:3]],
                "journal": doc.get("source", ""),
                "date":    doc.get("pubdate", ""),
                "link":    f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
            })
        return {"articles": articles, "query": query, "total": len(articles)}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"PubMed indisponível: {e}")


@app.get("/api/stats")
def stats():
    from datetime import timedelta
    db = get_session()
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_start_dt = datetime(week_start.year, week_start.month, week_start.day)
    today_start = datetime(today.year, today.month, today.day)

    total_q   = db.query(func.count(Question.id)).scalar() or 0
    correct_q = db.query(func.count(Question.id)).filter(Question.correct == True).scalar() or 0
    week_q    = db.query(func.count(Question.id)).filter(Question.answered_at >= week_start_dt).scalar() or 0
    today_q   = db.query(func.count(Question.id)).filter(Question.answered_at >= today_start).scalar() or 0

    # Streak: only fetch distinct dates (much smaller payload)
    distinct_dates = sorted(
        {r[0].date() for r in db.query(Question.answered_at).all() if r[0]},
        reverse=True,
    )
    streak = 0
    check = today
    for d in distinct_dates:
        if d >= check - timedelta(days=1):
            streak += 1
            check = d
        else:
            break

    # Best streak
    best_streak = 0
    best_streak_end = None
    if distinct_dates:
        asc = sorted(distinct_dates)
        run = 1; run_end = asc[0]
        for i in range(1, len(asc)):
            if asc[i] == asc[i-1] + timedelta(days=1):
                run += 1
            else:
                if run > best_streak:
                    best_streak = run; best_streak_end = run_end
                run = 1
            run_end = asc[i]
        if run > best_streak:
            best_streak = run; best_streak_end = run_end

    return {
        "total_questions": total_q,
        "questions_this_week": week_q,
        "streak_days": streak,
        "questions_today": today_q,
        "correct_questions": correct_q,
        "accuracy_pct": round(correct_q / total_q * 100, 1) if total_q else 0,
        "current_streak": streak,
        "best_streak": best_streak,
        "best_streak_end": best_streak_end.isoformat() if best_streak_end else None,
    }


@app.get("/api/stats/summary")
def stats_summary():
    """Extended stats: current_streak, best_streak, best_streak_end, totals."""
    from datetime import timedelta
    db = get_session()
    today = date.today()

    total_q   = db.query(func.count(Question.id)).scalar() or 0
    correct_q = db.query(func.count(Question.id)).filter(Question.correct == True).scalar() or 0
    distinct_dates = sorted(
        {r[0].date() for r in db.query(Question.answered_at).all() if r[0]},
        reverse=True,
    )

    # Current streak
    current_streak = 0
    check = today
    for d in distinct_dates:
        if d >= check - timedelta(days=1):
            current_streak += 1
            check = d
        else:
            break

    # Best streak + end date
    best_streak = 0
    best_streak_end = None
    if distinct_dates:
        asc = sorted(distinct_dates)
        run = 1
        run_end = asc[0]
        for i in range(1, len(asc)):
            if asc[i] == asc[i-1] + timedelta(days=1):
                run += 1
            else:
                if run > best_streak:
                    best_streak = run
                    best_streak_end = run_end
                run = 1
            run_end = asc[i]
        if run > best_streak:
            best_streak = run
            best_streak_end = run_end

    return {
        "total_questions": total_q,
        "correct_questions": correct_q,
        "accuracy_pct": round(correct_q / total_q * 100, 1) if total_q else 0,
        "current_streak": current_streak,
        "best_streak": best_streak,
        "best_streak_end": best_streak_end.isoformat() if best_streak_end else None,
    }


@app.get("/api/stats/daily")
def stats_daily(days: int = 30, max_days: int = 365):
    """Questions per day for the last N days."""
    from datetime import timedelta
    db = get_session()
    today = date.today()
    start = today - timedelta(days=days - 1)
    questions = db.query(Question).filter(
        Question.answered_at >= datetime(start.year, start.month, start.day)
    ).all()
    counts: dict[str, dict] = {}
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        counts[d] = {"date": d, "total": 0, "correct": 0}
    for q in questions:
        d = q.answered_at.date().isoformat()
        if d in counts:
            counts[d]["total"] += 1
            if q.correct:
                counts[d]["correct"] += 1
    return sorted(counts.values(), key=lambda x: x["date"])


@app.delete("/api/questions/{question_id}")
def delete_question(question_id: int):
    db = get_session()
    q = db.get(Question, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="Questão não encontrada.")
    db.delete(q)
    db.commit()
    return {"ok": True, "deleted_id": question_id}


@app.patch("/api/questions/{question_id}")
def patch_question(question_id: int, payload: dict):
    db = get_session()
    q = db.get(Question, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="Questão não encontrada.")
    if "correct" in payload:
        q.correct = bool(payload["correct"])
    if "notes" in payload:
        q.notes = payload["notes"]
    if "source" in payload:
        q.source = payload["source"]
    if "statement" in payload:
        q.statement = payload["statement"] or None
    db.commit()
    return {"ok": True, "id": question_id}


@app.get("/api/questions/history")
def questions_history(
    subject: str = None,
    result: str = None,      # 'correct' | 'wrong' | ''
    source: str = None,
    search: str = None,
    difficulty: str = None,  # 'facil' | 'medio' | 'dificil'
    date_filter: str = None, # YYYY-MM-DD — filter to a single day
    date_from: str = None,   # YYYY-MM-DD — range start
    date_to: str = None,     # YYYY-MM-DD — range end
    sort: str = "date_desc", # date_desc|date_asc|correct_desc|wrong_desc|diff_hard
    page: int = 1,
    per_page: int = 30,
):
    from datetime import timedelta
    db = get_session()
    q = (db.query(Question, Topic, Subject)
         .join(Topic, Question.topic_id == Topic.id)
         .join(Subject, Topic.subject_id == Subject.id))
    if subject:
        q = q.filter(Subject.name == subject)
    if result == 'correct':
        q = q.filter(Question.correct == True)
    elif result == 'wrong':
        q = q.filter(Question.correct == False)
    if source:
        q = q.filter(Question.source == source)
    if difficulty:
        q = q.filter(Question.difficulty == difficulty)
    if search:
        from sqlalchemy import or_
        q = q.filter(or_(
            Topic.name.ilike(f'%{search}%'),
            Question.statement.ilike(f'%{search}%'),
            Question.notes.ilike(f'%{search}%'),
            Question.source.ilike(f'%{search}%'),
        ))
    if date_filter:
        try:
            d = date.fromisoformat(date_filter)
            q = q.filter(Question.answered_at >= datetime(d.year, d.month, d.day),
                         Question.answered_at < datetime(d.year, d.month, d.day) + timedelta(days=1))
        except ValueError:
            pass
    if date_from:
        try:
            d = date.fromisoformat(date_from)
            q = q.filter(Question.answered_at >= datetime(d.year, d.month, d.day))
        except ValueError:
            pass
    if date_to:
        try:
            d = date.fromisoformat(date_to)
            q = q.filter(Question.answered_at < datetime(d.year, d.month, d.day) + timedelta(days=1))
        except ValueError:
            pass
    total = q.count()
    # Sorting
    from sqlalchemy import case as sa_case
    DIFF_ORDER = sa_case(
        (Question.difficulty == 'dificil', 1),
        (Question.difficulty == 'medio',   2),
        (Question.difficulty == 'facil',   3),
        else_=4,
    )
    if sort == 'date_asc':
        q = q.order_by(Question.answered_at.asc())
    elif sort == 'correct_desc':
        q = q.order_by(Question.correct.desc(), Question.answered_at.desc())
    elif sort == 'wrong_desc':
        q = q.order_by(Question.correct.asc(), Question.answered_at.desc())
    elif sort == 'diff_hard':
        q = q.order_by(DIFF_ORDER, Question.answered_at.desc())
    else:
        q = q.order_by(Question.answered_at.desc())
    items = q.offset((page-1)*per_page).limit(per_page).all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, -(-total // per_page)),
        "items": [
            {
                "id": qu.id,
                "topic_id": t.id,
                "topic_name": t.name,
                "subject_name": s.name,
                "correct": qu.correct,
                "source": qu.source or "Manual",
                "answered_at": qu.answered_at.strftime("%d/%m/%Y") if qu.answered_at else "—",
                "notes": qu.notes or "",
                "difficulty": qu.difficulty or "medio",
                "statement": qu.statement or "",
            }
            for qu, t, s in items
        ],
        "subjects": sorted({s.name for _, _, s in
                            db.query(Question, Topic, Subject)
                            .join(Topic, Question.topic_id == Topic.id)
                            .join(Subject, Topic.subject_id == Subject.id).all()}),
        "sources": sorted({qu.source for qu, _, _ in
                           db.query(Question, Topic, Subject)
                           .join(Topic, Question.topic_id == Topic.id)
                           .join(Subject, Topic.subject_id == Subject.id).all()
                           if qu.source}),
    }


@app.get("/api/export/questions/filtered")
def export_questions_filtered(
    subject: str = None, result: str = None, source: str = None,
    search: str = None, difficulty: str = None,
    date_from: str = None, date_to: str = None, sort: str = "date_desc",
):
    """Download filtered questions as CSV (same filters as /api/questions/history)."""
    from datetime import timedelta
    db = get_session()
    q = (db.query(Question, Topic, Subject)
         .join(Topic, Question.topic_id == Topic.id)
         .join(Subject, Topic.subject_id == Subject.id))
    if subject:
        q = q.filter(Subject.name == subject)
    if result == 'correct':
        q = q.filter(Question.correct == True)
    elif result == 'wrong':
        q = q.filter(Question.correct == False)
    if source:
        q = q.filter(Question.source == source)
    if difficulty:
        q = q.filter(Question.difficulty == difficulty)
    if search:
        from sqlalchemy import or_
        q = q.filter(or_(
            Topic.name.ilike(f'%{search}%'),
            Question.statement.ilike(f'%{search}%'),
            Question.notes.ilike(f'%{search}%'),
            Question.source.ilike(f'%{search}%'),
        ))
    if date_from:
        try:
            d = date.fromisoformat(date_from)
            q = q.filter(Question.answered_at >= datetime(d.year, d.month, d.day))
        except ValueError:
            pass
    if date_to:
        try:
            d = date.fromisoformat(date_to)
            q = q.filter(Question.answered_at < datetime(d.year, d.month, d.day) + timedelta(days=1))
        except ValueError:
            pass
    from sqlalchemy import case as sa_case
    DIFF_ORDER = sa_case(
        (Question.difficulty == 'dificil', 1),
        (Question.difficulty == 'medio', 2),
        (Question.difficulty == 'facil', 3),
        else_=4,
    )
    if sort == 'date_asc':
        q = q.order_by(Question.answered_at.asc())
    elif sort == 'correct_desc':
        q = q.order_by(Question.correct.desc(), Question.answered_at.desc())
    elif sort == 'wrong_desc':
        q = q.order_by(Question.correct.asc(), Question.answered_at.desc())
    elif sort == 'diff_hard':
        q = q.order_by(DIFF_ORDER, Question.answered_at.desc())
    else:
        q = q.order_by(Question.answered_at.desc())
    rows = q.all()
    output = io.StringIO()
    writer = csv_module.writer(output)
    writer.writerow(["id", "subject", "topic", "correct", "difficulty", "source", "answered_at", "notes", "statement"])
    for qu, t, s in rows:
        writer.writerow([
            qu.id, s.name, t.name,
            "sim" if qu.correct else "não",
            qu.difficulty or "",
            qu.source or "",
            qu.answered_at.strftime("%Y-%m-%d %H:%M") if qu.answered_at else "",
            qu.notes or "",
            (qu.statement or "")[:200],
        ])
    output.seek(0)
    filename = f"questoes_filtradas_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([output.getvalue().encode('utf-8-sig')]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/export/questions")
def export_questions():
    """Download all questions as CSV."""
    db = get_session()
    questions = (
        db.query(Question, Topic, Subject)
        .join(Topic, Question.topic_id == Topic.id)
        .join(Subject, Topic.subject_id == Subject.id)
        .order_by(Question.answered_at.desc())
        .all()
    )
    output = io.StringIO()
    writer = csv_module.writer(output)
    writer.writerow(["id", "subject", "topic", "correct", "source", "answered_at", "notes"])
    for q, t, s in questions:
        writer.writerow([
            q.id, s.name, t.name,
            "sim" if q.correct else "não",
            q.source or "",
            q.answered_at.strftime("%Y-%m-%d %H:%M") if q.answered_at else "",
            q.notes or "",
        ])
    output.seek(0)
    filename = f"questoes_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/api/questions/import/csv")
async def import_questions_csv(file: UploadFile):
    """
    Import questions from CSV file.
    Expected columns (case-insensitive, order-flexible):
      subject, topic, correct (sim/nao/true/false/1/0), source, date (YYYY-MM-DD), notes, difficulty, statement
    Returns count of imported rows and errors.
    """
    import csv, io
    db = get_session()
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # handle Excel BOM
    except Exception:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    # normalize headers
    rows = []
    for row in reader:
        rows.append({k.strip().lower(): (v.strip() if v else "") for k, v in row.items()})

    imported = 0
    errors = []
    for i, row in enumerate(rows, start=2):
        subj_name  = row.get("subject") or row.get("materia") or row.get("assunto") or ""
        topic_name = row.get("topic")   or row.get("topico")  or row.get("tópico")  or ""
        correct_raw = (row.get("correct") or row.get("correto") or "").lower()
        if not subj_name or not topic_name:
            errors.append(f"Linha {i}: subject/topic ausente")
            continue
        correct = correct_raw in ("sim", "true", "1", "s", "yes")

        subj = db.query(Subject).filter(Subject.name.ilike(subj_name)).first()
        if not subj:
            subj = Subject(name=subj_name); db.add(subj); db.flush()

        topic = db.query(Topic).filter(
            Topic.name.ilike(topic_name), Topic.subject_id == subj.id
        ).first()
        if not topic:
            topic = Topic(name=topic_name, subject_id=subj.id); db.add(topic); db.flush()

        answered_at = None
        date_raw = row.get("date") or row.get("data") or ""
        if date_raw:
            try: answered_at = datetime.fromisoformat(date_raw).replace(tzinfo=None)
            except Exception: pass

        q = Question(
            topic_id=topic.id,
            correct=correct,
            source=row.get("source") or row.get("fonte") or "CSV Import",
            notes=row.get("notes") or row.get("observacao") or row.get("observação") or None,
            difficulty=row.get("difficulty") or row.get("dificuldade") or "medio",
            statement=row.get("statement") or row.get("enunciado") or None,
            answered_at=answered_at or datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(q)
        imported += 1

    db.commit()
    return {"imported": imported, "errors": errors, "total_rows": len(rows)}


@app.get("/api/export/scores")
def export_scores():
    """Download topic scores as CSV."""
    db = get_session()
    scores = TopicScorer(db).score_all()
    output = io.StringIO()
    writer = csv_module.writer(output)
    writer.writerow(["subject", "topic", "total_questions", "wrong_questions", "error_rate_pct", "priority_score", "days_since_review", "anki_due"])
    for s in scores:
        writer.writerow([
            s.subject_name, s.topic_name,
            s.total_questions, s.wrong_questions,
            round(s.error_rate * 100, 1), round(s.priority_score, 4),
            s.days_since_review, s.anki_due,
        ])
    output.seek(0)
    filename = f"scores_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── SM-2 Spaced Repetition ────────────────────────────────────────────────────

def _sm2_apply_to_review(review: TopicReview, quality: int) -> TopicReview:
    """Apply SM-2 algorithm to a TopicReview ORM object, mutating it in place."""
    from datetime import timedelta
    ef = review.ease_factor or 2.5
    reps = review.repetitions or 0

    if quality < 3:
        reps = 0
        interval = 1.0
    else:
        if reps == 0:
            interval = 1.0
        elif reps == 1:
            interval = 6.0
        else:
            interval = round((review.interval_days or 1.0) * ef, 1)
        reps += 1

    ef = ef + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    ef = max(1.3, ef)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    review.ease_factor = round(ef, 4)
    review.interval_days = interval
    review.repetitions = reps
    review.last_reviewed = now
    review.next_review = now + timedelta(days=interval)
    return review


def _get_or_create_review(db, topic_id: int) -> TopicReview:
    rv = db.query(TopicReview).filter_by(topic_id=topic_id).first()
    if not rv:
        rv = TopicReview(topic_id=topic_id)
        db.add(rv)
    return rv


@app.get("/api/reviews/due")
def reviews_due(limit: int = 20):
    """Return topics due for spaced-repetition review today (including never-reviewed)."""
    from sqlalchemy import or_
    db = get_session()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # LEFT JOIN: topics that have a review record due today OR no record at all
    rows = (
        db.query(Topic, Subject, TopicReview)
        .join(Subject, Topic.subject_id == Subject.id)
        .outerjoin(TopicReview, TopicReview.topic_id == Topic.id)
        .filter(
            or_(
                TopicReview.id == None,           # never reviewed
                TopicReview.next_review <= now,   # due today
                TopicReview.next_review == None,  # created but no date set
            )
        )
        .order_by(TopicReview.next_review.asc().nullsfirst())
        .limit(limit)
        .all()
    )
    return [
        {
            "topic_id": t.id,
            "topic_name": t.name,
            "subject_name": s.name,
            "ease_factor": rv.ease_factor if rv else 2.5,
            "interval_days": rv.interval_days if rv else 1.0,
            "repetitions": rv.repetitions if rv else 0,
            "next_review": rv.next_review.isoformat() if rv and rv.next_review else None,
            "last_reviewed": rv.last_reviewed.isoformat() if rv and rv.last_reviewed else None,
        }
        for t, s, rv in rows
    ]


@app.get("/api/reviews/count")
def reviews_count():
    """Number of topics due for review right now."""
    db = get_session()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    count = (
        db.query(TopicReview)
        .filter((TopicReview.next_review <= now) | (TopicReview.next_review == None))
        .count()
    )
    # Also count topics that have no TopicReview record yet (never reviewed)
    total_topics = db.query(Topic).count()
    reviewed_topics = db.query(TopicReview).count()
    never_reviewed = total_topics - reviewed_topics
    return {"due": count + never_reviewed, "never_reviewed": never_reviewed}


@app.post("/api/reviews/{topic_id}/update")
def update_review(topic_id: int, payload: dict):
    """
    Update SM-2 state after a review session.
    payload: { quality: 0-5 }
      0-2 = failed/very hard
      3   = hard (remembered with difficulty)
      4   = good
      5   = easy
    """
    db = get_session()
    topic = db.get(Topic, topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Tópico não encontrado.")
    quality = int(payload.get("quality", 3))
    quality = max(0, min(5, quality))
    rv = _get_or_create_review(db, topic_id)
    rv = _sm2_apply_to_review(rv, quality)
    db.commit()
    return {
        "topic_id": topic_id,
        "ease_factor": rv.ease_factor,
        "interval_days": rv.interval_days,
        "repetitions": rv.repetitions,
        "next_review": rv.next_review.isoformat() if rv.next_review else None,
    }


@app.post("/api/questions")
def add_question_and_update_sm2(body: QuestionIn):
    """Add a question and auto-update SM-2 for the topic."""
    db = get_session()
    topic = db.get(Topic, body.topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Tópico não encontrado.")
    answered_at = datetime.fromisoformat(body.answered_at).replace(tzinfo=None) if body.answered_at else datetime.now(timezone.utc).replace(tzinfo=None)
    q = Question(topic_id=body.topic_id, correct=body.correct,
                 source=body.source, notes=body.notes, answered_at=answered_at,
                 difficulty=body.difficulty, statement=body.statement or None)
    db.add(q)
    # Auto-update SM-2: correct=quality 4, wrong=quality 1
    rv = _get_or_create_review(db, body.topic_id)
    _sm2_apply_to_review(rv, quality=4 if body.correct else 1)
    db.commit()
    return {"id": q.id, "topic_name": topic.name, "correct": body.correct,
            "next_review": rv.next_review.isoformat() if rv.next_review else None}


@app.get("/api/reviews/stats")
def reviews_stats():
    """SM-2 aggregate stats per subject for the progress chart."""
    db = get_session()
    rows = (
        db.query(TopicReview, Topic, Subject)
        .join(Topic, TopicReview.topic_id == Topic.id)
        .join(Subject, Topic.subject_id == Subject.id)
        .all()
    )
    from collections import defaultdict
    by_subject: dict[str, list] = defaultdict(list)
    for rv, t, s in rows:
        by_subject[s.name].append(rv)
    result = []
    for subj, rvs in sorted(by_subject.items()):
        avg_ef  = round(sum(r.ease_factor for r in rvs) / len(rvs), 3)
        avg_int = round(sum(r.interval_days for r in rvs) / len(rvs), 1)
        avg_rep = round(sum(r.repetitions for r in rvs) / len(rvs), 1)
        result.append({"subject": subj, "count": len(rvs),
                       "avg_ef": avg_ef, "avg_interval": avg_int, "avg_repetitions": avg_rep})
    return result


@app.post("/api/topics/import/csv")
async def import_topics_csv(file: UploadFile):
    """
    Import topics from CSV.
    Required columns: subject (or materia/assunto), topic (or topico/tópico).
    Optional: anki_deck, anki_tags, notability_notebook, study_notes, parent_topic.
    """
    import csv, io as _io
    db = get_session()
    content = await file.read()
    try: text = content.decode("utf-8-sig")
    except Exception: text = content.decode("latin-1")
    reader = csv.DictReader(_io.StringIO(text))
    rows = [{k.strip().lower(): (v.strip() if v else "") for k, v in row.items()} for row in reader]

    created, skipped, errors = 0, 0, []
    for i, row in enumerate(rows, start=2):
        subj_name  = row.get("subject") or row.get("materia") or row.get("assunto") or ""
        topic_name = row.get("topic")   or row.get("topico")  or row.get("tópico")  or ""
        if not subj_name or not topic_name:
            errors.append(f"Linha {i}: subject/topic ausente"); continue
        subj = db.query(Subject).filter(Subject.name.ilike(subj_name)).first()
        if not subj:
            subj = Subject(name=subj_name); db.add(subj); db.flush()
        existing = db.query(Topic).filter(Topic.name.ilike(topic_name), Topic.subject_id == subj.id).first()
        if existing:
            skipped += 1; continue
        t = Topic(
            name=topic_name, subject_id=subj.id,
            anki_deck=row.get("anki_deck") or None,
            anki_tags=row.get("anki_tags") or None,
            notability_notebook=row.get("notability_notebook") or None,
            study_notes=row.get("study_notes") or None,
        )
        db.add(t); created += 1
    db.commit()
    return {"created": created, "skipped": skipped, "errors": errors, "total_rows": len(rows)}


@app.post("/api/topics/bulk")
def bulk_create_topics(payload: dict):
    """
    Create multiple topics from a list.
    payload: { topics: [ {name, subject_name} ] }
    Returns created/skipped counts.
    """
    db = get_session()
    items = payload.get("topics", [])
    created, skipped, errors = [], [], []
    for item in items:
        name = str(item.get("name", "")).strip()
        subj_name = str(item.get("subject_name", "")).strip()
        if not name or not subj_name:
            errors.append(f"Linha inválida: {item}")
            continue
        subj = db.query(Subject).filter_by(name=subj_name).first()
        if not subj:
            subj = Subject(name=subj_name)
            db.add(subj)
            db.flush()
        existing = db.query(Topic).filter_by(name=name, subject_id=subj.id).first()
        if existing:
            skipped.append(name)
        else:
            db.add(Topic(name=name, subject_id=subj.id))
            created.append(name)
    db.commit()
    return {"created": len(created), "skipped": len(skipped),
            "errors": errors, "created_names": created[:20]}


@app.delete("/api/subjects/{subject_id}")
def delete_subject(subject_id: int):
    db = get_session()
    s = db.get(Subject, subject_id)
    if not s:
        raise HTTPException(status_code=404, detail="Assunto não encontrado.")
    db.delete(s)
    db.commit()
    return {"ok": True, "deleted": s.name}


@app.patch("/api/subjects/{subject_id}")
def patch_subject(subject_id: int, payload: dict):
    db = get_session()
    s = db.get(Subject, subject_id)
    if not s:
        raise HTTPException(status_code=404, detail="Assunto não encontrado.")
    if "exam_weight" in payload:
        s.exam_weight = float(payload["exam_weight"])
    if "name" in payload:
        s.name = payload["name"]
    db.commit()
    return {"ok": True, "id": s.id, "name": s.name, "exam_weight": s.exam_weight}


@app.get("/api/stats/trend")
def stats_trend():
    """
    Compare error rate per subject: last 14 days vs previous 14 days.
    Returns list of {subject, current_pct, previous_pct, delta, trend: 'up'|'down'|'stable'}
    """
    from datetime import timedelta
    db = get_session()
    today = datetime.now(timezone.utc).date()
    p1_end   = today
    p1_start = today - timedelta(days=13)   # last 14 days
    p2_end   = p1_start - timedelta(days=1)
    p2_start = p2_end - timedelta(days=13)  # previous 14 days

    def dt(d): return datetime(d.year, d.month, d.day)

    rows = (
        db.query(Question, Topic, Subject)
        .join(Topic, Question.topic_id == Topic.id)
        .join(Subject, Topic.subject_id == Subject.id)
        .filter(Question.answered_at >= dt(p2_start))
        .all()
    )

    from collections import defaultdict
    p1: dict[str, list] = defaultdict(list)
    p2: dict[str, list] = defaultdict(list)
    for q, t, s in rows:
        d = q.answered_at.date()
        if p1_start <= d <= p1_end:
            p1[s.name].append(q)
        elif p2_start <= d <= p2_end:
            p2[s.name].append(q)

    all_subjects = sorted(set(list(p1.keys()) + list(p2.keys())))
    result = []
    for subj in all_subjects:
        qs1 = p1.get(subj, [])
        qs2 = p2.get(subj, [])
        def pct(qs): return round(sum(1 for q in qs if not q.correct) / len(qs) * 100, 1) if qs else None
        c = pct(qs1)
        p = pct(qs2)
        if c is None:
            continue
        delta = round(c - p, 1) if p is not None else None
        trend = 'stable'
        if delta is not None:
            if delta <= -5: trend = 'up'    # error rate dropped = improving
            elif delta >= 5: trend = 'down'  # error rate rose = worsening
        result.append({
            "subject": subj,
            "current_pct": c,
            "previous_pct": p,
            "current_total": len(qs1),
            "previous_total": len(qs2),
            "delta": delta,
            "trend": trend,
        })
    return result


@app.get("/api/questions/wrong-topics")
def wrong_topics(top: int = 20):
    """
    Topics ordered by number of wrong answers — used for Repetição de Erros mode.
    Returns topics with at least one wrong answer, sorted by wrong count desc.
    """
    db = get_session()
    rows = (
        db.query(Topic, Subject, Question)
        .join(Subject, Topic.subject_id == Subject.id)
        .join(Question, Question.topic_id == Topic.id)
        .filter(Question.correct == False)
        .all()
    )
    from collections import defaultdict
    by_topic: dict[int, dict] = {}
    for t, s, q in rows:
        if t.id not in by_topic:
            by_topic[t.id] = {"topic_id": t.id, "topic_name": t.name,
                              "subject_name": s.name, "wrong": 0,
                              "error_rate_pct": 0, "total": 0}
        by_topic[t.id]["wrong"] += 1

    # Get totals
    all_q = db.query(Question, Topic).join(Topic, Question.topic_id == Topic.id).all()
    totals: dict[int, int] = defaultdict(int)
    for q, t in all_q:
        totals[t.id] += 1

    result = []
    for tid, d in by_topic.items():
        total = totals.get(tid, d["wrong"])
        d["total"] = total
        d["error_rate_pct"] = round(d["wrong"] / total * 100, 1) if total else 0
        result.append(d)

    return sorted(result, key=lambda x: x["wrong"], reverse=True)[:top]


@app.get("/api/reports/weekly")
def weekly_report():
    """Full weekly stats for PDF report generation."""
    from datetime import timedelta
    db = get_session()
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    week_start_dt = datetime(week_start.year, week_start.month, week_start.day)

    week_q = (db.query(Question, Topic, Subject)
               .join(Topic, Question.topic_id == Topic.id)
               .join(Subject, Topic.subject_id == Subject.id)
               .filter(Question.answered_at >= week_start_dt)
               .all())

    # Per-subject breakdown
    subj_map: dict[str, dict] = defaultdict(lambda: {"total": 0, "correct": 0})
    diff_map: dict[str, int] = defaultdict(int)
    for q, t, s in week_q:
        subj_map[s.name]["total"] += 1
        if q.correct:
            subj_map[s.name]["correct"] += 1
        diff_map[q.difficulty or "medio"] += 1

    # Weak topics
    scores = TopicScorer(db).score_all()
    weak = [s for s in scores if s.total_questions >= 3 and s.error_rate > 0.4][:5]

    # SM2 due
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sm2_due = db.query(TopicReview).filter(
        (TopicReview.next_review <= now) | (TopicReview.next_review == None)
    ).count()
    total_topics = db.query(Topic).count()
    never = total_topics - db.query(TopicReview).count()

    total_w = len(week_q)
    correct_w = sum(1 for q, _, _ in week_q if q.correct)
    return {
        "week_start": week_start.isoformat(),
        "today": today.isoformat(),
        "total_questions": total_w,
        "correct": correct_w,
        "wrong": total_w - correct_w,
        "error_rate_pct": round((total_w - correct_w) / total_w * 100, 1) if total_w else 0,
        "by_subject": sorted([
            {"subject": k, "total": v["total"], "correct": v["correct"],
             "error_rate_pct": round((v["total"] - v["correct"]) / v["total"] * 100, 1) if v["total"] else 0}
            for k, v in subj_map.items()
        ], key=lambda x: x["total"], reverse=True),
        "by_difficulty": {"facil": diff_map["facil"], "medio": diff_map["medio"], "dificil": diff_map["dificil"]},
        "weak_topics": [
            {"topic": s.topic_name, "subject": s.subject_name,
             "error_rate_pct": round(s.error_rate * 100, 1), "total": s.total_questions}
            for s in weak
        ],
        "sm2_due": sm2_due + never,
        "total_topics": total_topics,
        "subjects_studied": len(subj_map),
    }


@app.post("/api/notion/sync")
def notion_sync(payload: dict):
    """Push topics to a Notion database via Notion API."""
    import urllib.request, urllib.error
    notion_token = payload.get("notion_token", "").strip()
    database_id  = payload.get("database_id", "").strip()
    if not notion_token or not database_id:
        raise HTTPException(status_code=400, detail="notion_token e database_id são obrigatórios.")

    db = get_session()
    topics = (db.query(Topic, Subject)
              .join(Subject, Topic.subject_id == Subject.id)
              .order_by(Subject.name, Topic.name).all())
    scores = {s.topic_id: s for s in TopicScorer(db).score_all()}

    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    # First verify the database exists
    try:
        req = urllib.request.Request(
            f"https://api.notion.com/v1/databases/{database_id}",
            headers=headers, method="GET"
        )
        urllib.request.urlopen(req, timeout=8)
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"Notion: database não encontrado ou sem permissão ({e.code})")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Notion indisponível: {e}")

    created, errors = 0, []
    for t, s in topics[:50]:  # cap at 50 to avoid rate limits
        sc = scores.get(t.id)
        props = {
            "Tópico": {"title": [{"text": {"content": t.name[:2000]}}]},
            "Assunto": {"rich_text": [{"text": {"content": s.name}}]},
            "Taxa de Erro (%)": {"number": round(sc.error_rate * 100, 1) if sc else 0},
            "Total Questões": {"number": sc.total_questions if sc else 0},
            "Notas": {"rich_text": [{"text": {"content": (t.study_notes or "")[:2000]}}]},
        }
        body = json.dumps({"parent": {"database_id": database_id}, "properties": props}).encode()
        req = urllib.request.Request(
            "https://api.notion.com/v1/pages",
            data=body, headers=headers, method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            created += 1
        except Exception as e:
            errors.append(f"{t.name}: {str(e)[:80]}")
            if len(errors) >= 5:
                break

    return {"ok": len(errors) == 0, "created": created, "total": len(topics), "errors": errors}


@app.get("/api/simulado/questions")
def simulado_questions(subject_id: int | None = None, count: int = 20):
    """Return N random topics for a timed simulado session."""
    import random
    db = get_session()
    q = db.query(Topic, Subject).join(Subject, Topic.subject_id == Subject.id)
    if subject_id:
        q = q.filter(Topic.subject_id == subject_id)
    topics = q.all()
    if not topics:
        return []
    selected = random.sample(topics, min(count, len(topics)))
    scores = {s.topic_id: s for s in TopicScorer(db).score_all()}
    return [
        {
            "topic_id": t.id,
            "topic_name": t.name,
            "subject_name": s.name,
            "subject_id": s.id,
            "error_rate_pct": round(scores[t.id].error_rate * 100, 1) if t.id in scores else 0,
            "total_questions": scores[t.id].total_questions if t.id in scores else 0,
        }
        for t, s in selected
    ]


@app.get("/api/stats/sources")
def stats_sources():
    """Error rate and volume per question source/banco."""
    db = get_session()
    rows = (
        db.query(Question, Topic, Subject)
        .join(Topic, Question.topic_id == Topic.id)
        .join(Subject, Topic.subject_id == Subject.id)
        .all()
    )
    from collections import defaultdict
    by_src: dict[str, dict] = defaultdict(lambda: {"total": 0, "correct": 0, "topics": set(), "subjects": set()})
    for q, t, s in rows:
        src = q.source or "Manual"
        by_src[src]["total"] += 1
        if q.correct:
            by_src[src]["correct"] += 1
        by_src[src]["topics"].add(t.name)
        by_src[src]["subjects"].add(s.name)
    result = []
    for src, v in by_src.items():
        total = v["total"]
        wrong = total - v["correct"]
        result.append({
            "source": src,
            "total": total,
            "correct": v["correct"],
            "wrong": wrong,
            "error_rate_pct": round(wrong / total * 100, 1) if total else 0,
            "unique_topics": len(v["topics"]),
            "subjects": sorted(v["subjects"]),
        })
    return sorted(result, key=lambda x: x["total"], reverse=True)


@app.get("/api/schedule/auto")
def auto_schedule(weeks: int = 12):
    """
    Generate a multi-week study schedule weighted by exam_weight + priority_score.
    Returns one block per week with the top topics to cover.
    """
    from datetime import timedelta
    import math
    db = get_session()
    scores = TopicScorer(db).score_all()
    subjects_db = {s.id: s for s in db.query(Subject).all()}
    topics_db = {t.id: t for t in db.query(Topic).all()}

    if not scores:
        return {"weeks": [], "total_topics": 0}

    today = date.today()
    # Enrich scores with exam_weight
    enriched = []
    for s in scores:
        t = topics_db.get(s.topic_id)
        subj = subjects_db.get(t.subject_id) if t else None
        exam_weight = subj.exam_weight if subj else 1.0
        # Combined weight: priority_score * exam_weight
        combined = s.priority_score * exam_weight
        enriched.append((combined, s))

    # Sort by combined weight desc, then distribute round-robin per week
    enriched.sort(key=lambda x: x[0], reverse=True)
    sorted_scores = [s for _, s in enriched]
    topics_per_week = max(3, math.ceil(len(sorted_scores) / weeks))

    weeks_data = []
    for w in range(weeks):
        start = today + timedelta(weeks=w)
        end = start + timedelta(days=6)
        # Interleaved distribution so every week gets a variety of priorities
        week_topics = [sorted_scores[i] for i in range(w, len(sorted_scores), weeks)][:topics_per_week]
        weeks_data.append({
            "week": w + 1,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "label": f"{start.day}/{start.month:02d}–{end.day}/{end.month:02d}",
            "topics": [
                {
                    "topic_name": s.topic_name,
                    "subject_name": s.subject_name,
                    "error_rate_pct": round(s.error_rate * 100, 1),
                    "priority_score": round(s.priority_score, 2),
                }
                for s in week_topics
            ],
        })
    return {"weeks": weeks_data, "total_topics": len(sorted_scores), "weeks_count": weeks}


# ── Flash Cards ───────────────────────────────────────────────────────────────

class FlashCardIn(BaseModel):
    question: str
    answer: str
    hint: str | None = None


@app.get("/api/topics/{topic_id}/flashcards")
def list_flashcards(topic_id: int):
    db = get_session()
    cards = db.query(FlashCard).filter_by(topic_id=topic_id).order_by(FlashCard.created_at).all()
    return [{"id": c.id, "question": c.question, "answer": c.answer, "hint": c.hint or "",
             "interval_days": getattr(c, 'interval_days', 1),
             "times_reviewed": c.times_reviewed,
             "last_reviewed": c.last_reviewed.isoformat() if c.last_reviewed else None}
            for c in cards]


@app.post("/api/topics/{topic_id}/flashcards")
def create_flashcard(topic_id: int, body: FlashCardIn):
    db = get_session()
    t = db.get(Topic, topic_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tópico não encontrado.")
    c = FlashCard(topic_id=topic_id, question=body.question, answer=body.answer, hint=body.hint or None)
    db.add(c)
    db.commit()
    return {"id": c.id, "question": c.question, "answer": c.answer, "hint": c.hint or ""}


@app.patch("/api/flashcards/{card_id}/hint")
def update_flashcard_hint(card_id: int, body: dict):
    db = get_session()
    c = db.get(FlashCard, card_id)
    if not c:
        raise HTTPException(status_code=404, detail="Card não encontrado.")
    c.hint = body.get("hint") or None
    db.commit()
    return {"ok": True}


@app.delete("/api/flashcards/{card_id}")
def delete_flashcard(card_id: int):
    db = get_session()
    c = db.get(FlashCard, card_id)
    if not c:
        raise HTTPException(status_code=404, detail="Card não encontrado.")
    db.delete(c)
    db.commit()
    return {"ok": True}


class SM2RatingIn(BaseModel):
    quality: int = 3  # 0=blackout 1=wrong 2=wrong+hint 3=ok 4=good 5=easy


def _sm2_update(ef: float, interval: float, reps: int, quality: int):
    """SuperMemo-2 algorithm. Returns (ease_factor, interval_days, repetitions, next_review)."""
    from datetime import timedelta
    if quality < 3:
        reps = 0
        interval = 1.0
    else:
        if reps == 0:
            interval = 1.0
        elif reps == 1:
            interval = 6.0
        else:
            interval = round(interval * ef, 1)
        reps += 1
        ef = ef + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)
        ef = max(1.3, ef)
    next_rev = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=max(1, interval))
    return round(ef, 4), interval, reps, next_rev


@app.post("/api/flashcards/{card_id}/reviewed")
def mark_flashcard_reviewed(card_id: int, body: SM2RatingIn = None):
    db = get_session()
    c = db.get(FlashCard, card_id)
    if not c:
        raise HTTPException(status_code=404, detail="Card não encontrado.")
    quality = (body.quality if body else 3)
    ef, interval, reps, next_rev = _sm2_update(
        c.ease_factor or 2.5, c.interval_days or 1.0, c.repetitions or 0, quality
    )
    c.times_reviewed = (c.times_reviewed or 0) + 1
    c.last_reviewed  = datetime.now(timezone.utc).replace(tzinfo=None)
    c.ease_factor    = ef
    c.interval_days  = interval
    c.repetitions    = reps
    c.next_review    = next_rev
    db.commit()
    return {"ok": True, "times_reviewed": c.times_reviewed,
            "ease_factor": ef, "interval_days": interval, "next_review": next_rev.isoformat()}


@app.get("/api/flashcards/all")
def all_flashcards(limit: int = 100):
    """All flashcards for quick review mode."""
    db = get_session()
    rows = (db.query(FlashCard, Topic, Subject)
            .join(Topic, FlashCard.topic_id == Topic.id)
            .join(Subject, Topic.subject_id == Subject.id)
            .order_by(FlashCard.times_reviewed.asc(), FlashCard.created_at.asc())
            .limit(limit).all())
    return [{"id": c.id, "question": c.question, "answer": c.answer,
             "topic_name": t.name, "subject_name": s.name,
             "times_reviewed": c.times_reviewed}
            for c, t, s in rows]


# ── Favorites ─────────────────────────────────────────────────────────────────

@app.post("/api/topics/{topic_id}/favorite")
def toggle_favorite(topic_id: int):
    db = get_session()
    t = db.get(Topic, topic_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tópico não encontrado.")
    t.is_favorite = not bool(t.is_favorite)
    db.commit()
    return {"topic_id": topic_id, "is_favorite": bool(t.is_favorite)}


@app.get("/api/topics/favorites")
def list_favorites():
    db = get_session()
    topics = (db.query(Topic, Subject)
              .join(Subject, Topic.subject_id == Subject.id)
              .filter(Topic.is_favorite == True)
              .order_by(Subject.name, Topic.name).all())
    scores = {s.topic_id: s for s in TopicScorer(db).score_all()}
    return [{"id": t.id, "name": t.name, "subject_name": s.name,
             "study_notes": t.study_notes or "",
             "error_rate_pct": round(scores[t.id].error_rate * 100, 1) if t.id in scores else 0,
             "is_favorite": True}
            for t, s in topics]


# ── Stats Evolution & Velocity ─────────────────────────────────────────────────

@app.get("/api/stats/evolution")
def stats_evolution(weeks: int = 8):
    """Error rate per subject per week for the last N weeks."""
    from datetime import timedelta
    db = get_session()
    today = date.today()

    result = {}
    subjects_seen = set()
    for w in range(weeks):
        week_end = today - timedelta(weeks=w)
        week_start = week_end - timedelta(days=6)
        label = f"{week_start.day}/{week_start.month:02d}"

        rows = (db.query(Question, Subject)
                .join(Topic, Question.topic_id == Topic.id)
                .join(Subject, Topic.subject_id == Subject.id)
                .filter(Question.answered_at >= datetime(week_start.year, week_start.month, week_start.day),
                        Question.answered_at <= datetime(week_end.year, week_end.month, week_end.day, 23, 59, 59))
                .all())

        week_data: dict[str, list] = {}
        for q, s in rows:
            subjects_seen.add(s.name)
            week_data.setdefault(s.name, []).append(q)

        result[label] = {
            subj: round(sum(1 for q in qs if not q.correct) / len(qs) * 100, 1)
            for subj, qs in week_data.items()
        }

    labels = list(reversed(list(result.keys())))
    return {"labels": labels, "subjects": sorted(subjects_seen),
            "data": {label: result[label] for label in labels}}


@app.get("/api/stats/velocity")
def stats_velocity():
    """Questions count by hour of day (0-23)."""
    db = get_session()
    questions = db.query(Question).all()
    by_hour = [0] * 24
    correct_by_hour = [0] * 24
    for q in questions:
        h = q.answered_at.hour
        by_hour[h] += 1
        if q.correct:
            correct_by_hour[h] += 1
    peak = by_hour.index(max(by_hour)) if any(by_hour) else 0
    total = sum(by_hour)
    return {
        "by_hour": by_hour,
        "correct_by_hour": correct_by_hour,
        "peak_hour": peak,
        "total": total,
    }


@app.get("/api/stats/by-weekday")
def stats_by_weekday():
    """Questions count and error rate by day of week (0=Mon … 6=Sun)."""
    db = get_session()
    questions = db.query(Question).all()
    by_day = [0] * 7
    wrong_by_day = [0] * 7
    for q in questions:
        d = q.answered_at.weekday()  # 0=Mon
        by_day[d] += 1
        if not q.correct:
            wrong_by_day[d] += 1
    error_rate = [round(wrong_by_day[d] / by_day[d] * 100, 1) if by_day[d] else 0 for d in range(7)]
    worst = error_rate.index(max(error_rate)) if any(by_day) else 0
    return {"by_day": by_day, "wrong_by_day": wrong_by_day, "error_rate": error_rate, "worst_day": worst}


@app.get("/api/history/overview")
def history_overview():
    db = get_session()
    questions = db.query(Question).order_by(Question.answered_at).all()
    groups: dict[str, list] = defaultdict(list)
    for q in questions:
        groups[q.source or "Manual"].append(q)
    result = []
    for source, qs in groups.items():
        total, wrong = len(qs), sum(1 for q in qs if not q.correct)
        result.append({"source": source, "date": min(q.answered_at for q in qs).strftime("%d/%m/%Y"),
                       "total": total, "wrong": wrong,
                       "error_rate_pct": round(wrong / total * 100, 1) if total else 0})
    return sorted(result, key=lambda x: x["date"])


# ── Dificuldade breakdown ─────────────────────────────────────────────────────

@app.get("/api/history/by-difficulty")
def history_by_difficulty():
    db = get_session()
    qs = db.query(Question).all()
    result = {}
    for level in ("facil", "medio", "dificil"):
        bucket = [q for q in qs if (q.difficulty or "medio") == level]
        total = len(bucket)
        correct = sum(1 for q in bucket if q.correct)
        result[level] = {"total": total, "correct": correct, "wrong": total - correct}
    return result


# ── Mapa de Calor ─────────────────────────────────────────────────────────────

@app.get("/api/stats/heatmap")
def stats_heatmap(days: int = 365):
    from datetime import timedelta
    db = get_session()
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    questions = db.query(Question).filter(Question.answered_at >= since).all()
    counts: dict[str, int] = defaultdict(int)
    for q in questions:
        counts[q.answered_at.strftime("%Y-%m-%d")] += 1
    return {"counts": counts}


# ── Flash Card Stats & SM-2 Queue ─────────────────────────────────────────────

@app.get("/api/flashcards/stats")
def flashcard_stats():
    db = get_session()
    today_start = datetime.now(timezone.utc).replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0)
    total = db.query(FlashCard).count()
    reviewed_today = db.query(FlashCard).filter(FlashCard.last_reviewed >= today_start).count()
    # streak: consecutive days with at least one review
    from datetime import timedelta
    all_cards = db.query(FlashCard).filter(FlashCard.last_reviewed.isnot(None)).all()
    reviewed_dates = sorted({c.last_reviewed.date() for c in all_cards}, reverse=True)
    streak = 0
    check = date.today()
    for d in reviewed_dates:
        if d == check:
            streak += 1
            check -= timedelta(days=1)
        elif d < check:
            break
    return {"total": total, "reviewed_today": reviewed_today, "streak_days": streak}


@app.get("/api/flashcards/sm2")
def flashcard_sm2_queue(limit: int = 20):
    """Due cards first (next_review <= now), then new cards, ordered by SM-2 schedule."""
    from sqlalchemy import asc, nulls_first, or_
    db = get_session()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cards = (db.query(FlashCard, Topic, Subject)
               .join(Topic, FlashCard.topic_id == Topic.id)
               .join(Subject, Topic.subject_id == Subject.id)
               .filter(or_(FlashCard.next_review <= now, FlashCard.next_review.is_(None)))
               .order_by(nulls_first(asc(FlashCard.next_review)), asc(FlashCard.times_reviewed))
               .limit(limit).all())
    return [{"id": c.id, "question": c.question, "answer": c.answer,
             "topic_name": t.name, "subject_name": s.name,
             "times_reviewed": c.times_reviewed, "ease_factor": c.ease_factor or 2.5,
             "interval_days": c.interval_days or 1.0,
             "next_review": c.next_review.isoformat() if c.next_review else None}
            for c, t, s in cards]


# ── Export Flash Cards (Anki format) ─────────────────────────────────────────

@app.get("/api/export/flashcards/csv")
def export_flashcards_csv():
    """Export flash cards as CSV (subject, topic, question, answer, ease_factor, interval_days, repetitions, next_review)."""
    import csv, io as _io
    db = get_session()
    cards = (db.query(FlashCard, Topic, Subject)
               .join(Topic, FlashCard.topic_id == Topic.id)
               .join(Subject, Topic.subject_id == Subject.id)
               .order_by(Subject.name, Topic.name).all())
    output = _io.StringIO()
    w = csv.writer(output)
    w.writerow(["subject","topic","question","answer","ease_factor","interval_days","repetitions","next_review","times_reviewed"])
    for c, t, s in cards:
        w.writerow([
            s.name, t.name, c.question, c.answer,
            c.ease_factor, round(c.interval_days, 1), c.repetitions,
            c.next_review.isoformat() if c.next_review else "",
            c.times_reviewed,
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=flashcards_{date.today().isoformat()}.csv"},
    )


@app.get("/api/export/flashcards")
def export_flashcards():
    db = get_session()
    cards = (db.query(FlashCard, Topic, Subject)
               .join(Topic, FlashCard.topic_id == Topic.id)
               .join(Subject, Topic.subject_id == Subject.id)
               .order_by(Subject.name, Topic.name).all())
    lines = ["#separator:tab", "#html:false", "#notetype:Basic", "#deck:MedStudies"]
    for c, t, s in cards:
        q = c.question.replace("\t", " ").replace("\n", "<br>")
        a = c.answer.replace("\t", " ").replace("\n", "<br>")
        lines.append(f"{q}\t{a}\tMedStudies::{s.name}::{t.name}")
    content = "\n".join(lines)
    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=medstudies_flashcards.txt"},
    )


@app.post("/api/flashcards/import/csv")
async def import_flashcards_csv(file: UploadFile):
    """
    Import flash cards from CSV.
    Expected columns: subject, topic, question, answer, hint (opt)
    Creates subject/topic if not found.
    """
    import csv, io as _io
    db = get_session()
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except Exception:
        text = content.decode("latin-1")

    reader = csv.DictReader(_io.StringIO(text))
    imported = 0; errors = []
    for i, row in enumerate({k.strip().lower(): (v.strip() if v else "") for k, v in row.items()} for row in reader):
        subj_name  = row.get("subject") or row.get("materia") or row.get("assunto") or ""
        topic_name = row.get("topic")   or row.get("topico")  or row.get("tópico")  or ""
        question   = row.get("question") or row.get("pergunta") or row.get("frente") or ""
        answer     = row.get("answer")   or row.get("resposta") or row.get("verso")   or ""
        hint       = row.get("hint")     or row.get("dica")     or None
        if not (subj_name and topic_name and question and answer):
            errors.append(f"Linha {i+2}: campos obrigatórios ausentes"); continue

        subj = db.query(Subject).filter(Subject.name.ilike(subj_name)).first()
        if not subj:
            subj = Subject(name=subj_name); db.add(subj); db.flush()

        topic = db.query(Topic).filter(
            Topic.name.ilike(topic_name), Topic.subject_id == subj.id
        ).first()
        if not topic:
            topic = Topic(name=topic_name, subject_id=subj.id); db.add(topic); db.flush()

        db.add(FlashCard(topic_id=topic.id, question=question, answer=answer, hint=hint or None))
        imported += 1

    db.commit()
    return {"imported": imported, "errors": errors}


@app.get("/api/sessions/by-topic")
def sessions_by_topic():
    """Total study minutes per topic (from StudySession)."""
    db = get_session()
    rows = (db.query(Topic.id, Topic.name, Subject.name, func.sum(StudySession.duration_minutes))
              .join(StudySession, StudySession.topic_id == Topic.id)
              .join(Subject, Subject.id == Topic.subject_id)
              .group_by(Topic.id)
              .order_by(func.sum(StudySession.duration_minutes).desc())
              .all())
    return [{"topic_id": r[0], "topic_name": r[1], "subject_name": r[2],
              "minutes": int(r[3] or 0)} for r in rows]


# ── Ranking por Assunto ───────────────────────────────────────────────────────

@app.get("/api/stats/subjects/ranking")
def subjects_ranking():
    db = get_session()
    subjects = db.query(Subject).all()
    result = []
    for s in subjects:
        topic_ids = [t.id for t in s.topics]
        if not topic_ids:
            continue
        qs = db.query(Question).filter(Question.topic_id.in_(topic_ids)).all()
        if not qs:
            continue
        total = len(qs)
        correct = sum(1 for q in qs if q.correct)
        error_pct = round((total - correct) / total * 100, 1)
        result.append({
            "subject": s.name,
            "total": total,
            "correct": correct,
            "accuracy_pct": round(correct / total * 100, 1),
            "error_pct": error_pct,
            "topics": len(s.topics),
        })
    return sorted(result, key=lambda x: x["accuracy_pct"], reverse=True)


# ── Diário de Erros ───────────────────────────────────────────────────────────

@app.get("/api/questions/errors")
def questions_errors(subject_id: int | None = None, topic_id: int | None = None, limit: int = 200):
    """Wrong questions grouped by topic, with optional subject/topic filter."""
    db = get_session()
    q = (db.query(Question, Topic, Subject)
           .join(Topic, Question.topic_id == Topic.id)
           .join(Subject, Topic.subject_id == Subject.id)
           .filter(Question.correct == False))
    if subject_id:
        q = q.filter(Subject.id == subject_id)
    if topic_id:
        q = q.filter(Topic.id == topic_id)
    rows = q.order_by(Subject.name, Topic.name, Question.answered_at.desc()).limit(limit).all()

    groups: dict[int, dict] = {}
    for question, topic, subject in rows:
        if topic.id not in groups:
            groups[topic.id] = {
                "topic_id": topic.id, "topic_name": topic.name,
                "subject_id": subject.id, "subject_name": subject.name,
                "questions": [],
            }
        groups[topic.id]["questions"].append({
            "id": question.id,
            "source": question.source or "Manual",
            "answered_at": question.answered_at.strftime("%d/%m/%Y %H:%M"),
            "difficulty": question.difficulty or "medio",
            "notes": question.notes or "",
        })
    return list(groups.values())


# ── Importar Questões em Massa (CSV) ─────────────────────────────────────────

class BulkQuestionRow(BaseModel):
    topic_name: str
    subject_name: str
    correct: bool
    source: str = "Importado"
    difficulty: str = "medio"
    notes: str = ""


class BulkQuestionsIn(BaseModel):
    rows: list[BulkQuestionRow]


@app.post("/api/questions/bulk")
def bulk_questions(body: BulkQuestionsIn):
    db = get_session()
    created = skipped = errors = 0
    for row in body.rows:
        try:
            subject = db.query(Subject).filter(Subject.name.ilike(row.subject_name.strip())).first()
            if not subject:
                subject = Subject(name=row.subject_name.strip())
                db.add(subject); db.flush()

            topic = db.query(Topic).filter(
                Topic.name.ilike(row.topic_name.strip()),
                Topic.subject_id == subject.id,
            ).first()
            if not topic:
                topic = Topic(name=row.topic_name.strip(), subject_id=subject.id)
                db.add(topic); db.flush()

            q = Question(topic_id=topic.id, correct=row.correct, source=row.source,
                         difficulty=row.difficulty, notes=row.notes)
            db.add(q)
            created += 1
        except Exception:
            errors += 1
    db.commit()
    return {"created": created, "skipped": skipped, "errors": errors}


# ── FC Weekly Progress ────────────────────────────────────────────────────────

@app.get("/api/flashcards/weekly")
def flashcards_weekly():
    """Flash cards reviewed this week (Mon–Sun)."""
    from datetime import timedelta
    db = get_session()
    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_start_dt = datetime(week_start.year, week_start.month, week_start.day)
    reviewed = db.query(FlashCard).filter(FlashCard.last_reviewed >= week_start_dt).count()
    total = db.query(FlashCard).count()
    return {"reviewed_this_week": reviewed, "total": total, "week_start": week_start.isoformat()}


# ── Comparativo de Simulados ──────────────────────────────────────────────────

@app.get("/api/stats/simulados/compare")
def simulados_compare():
    """Per-simulado breakdown: total, correct, wrong, error_rate for each source."""
    db = get_session()
    questions = db.query(Question).filter(Question.source.isnot(None)).all()
    groups: dict[str, dict] = {}
    for q in questions:
        src = q.source or "Manual"
        if src not in groups:
            groups[src] = {"source": src, "total": 0, "correct": 0,
                           "first_date": q.answered_at}
        groups[src]["total"] += 1
        if q.correct:
            groups[src]["correct"] += 1
        if q.answered_at < groups[src]["first_date"]:
            groups[src]["first_date"] = q.answered_at

    result = []
    for src, d in groups.items():
        wrong = d["total"] - d["correct"]
        result.append({
            "source": src,
            "total": d["total"],
            "correct": d["correct"],
            "wrong": wrong,
            "accuracy_pct": round(d["correct"] / d["total"] * 100, 1) if d["total"] else 0,
            "error_pct": round(wrong / d["total"] * 100, 1) if d["total"] else 0,
            "date": d["first_date"].strftime("%d/%m/%Y"),
        })
    return sorted(result, key=lambda x: x["date"])


# ── Busca Global ──────────────────────────────────────────────────────────────

@app.get("/api/search")
def global_search(q: str, limit: int = 40, type: str = "all"):
    """Search topics, flash cards and questions. type=all|topics|questions|flashcards."""
    from sqlalchemy import or_
    db = get_session()
    term = f"%{q}%"

    topic_rows, card_rows, question_rows = [], [], []

    if type in ("all", "topics"):
        topic_rows = (db.query(Topic, Subject)
                    .join(Subject, Topic.subject_id == Subject.id)
                    .filter(or_(Topic.name.ilike(term), Topic.study_notes.ilike(term)))
                    .limit(limit).all())

    if type in ("all", "flashcards"):
        card_rows = (db.query(FlashCard, Topic, Subject)
               .join(Topic, FlashCard.topic_id == Topic.id)
               .join(Subject, Topic.subject_id == Subject.id)
               .filter(or_(FlashCard.question.ilike(term), FlashCard.answer.ilike(term)))
               .limit(limit).all())

    if type in ("all", "questions"):
        question_rows = (db.query(Question, Topic, Subject)
                   .join(Topic, Question.topic_id == Topic.id)
                   .join(Subject, Topic.subject_id == Subject.id)
                   .filter(or_(Question.notes.ilike(term), Question.source.ilike(term),
                               Question.statement.ilike(term)))
                   .order_by(Question.answered_at.desc())
                   .limit(limit).all())

    # Legacy flat list (palette)
    flat = []
    topics_out, questions_out, flashcards_out = [], [], []

    for t, s in topic_rows:
        snippet = ""
        if t.study_notes and q.lower() in (t.study_notes or "").lower():
            idx = t.study_notes.lower().index(q.lower())
            snippet = t.study_notes[max(0, idx-30):idx+60]
        flat.append({"type": "topic", "id": t.id, "title": t.name,
                     "subtitle": s.name, "snippet": snippet})
        topics_out.append({"topic_id": t.id, "topic_name": t.name,
                            "subject_name": s.name, "study_notes": t.study_notes or ""})

    for c, t, s in card_rows:
        flat.append({"type": "card", "id": c.id, "title": c.question[:80],
                     "subtitle": f"{s.name} › {t.name}", "snippet": c.answer[:100],
                     "topic_id": t.id, "topic_name": t.name})
        flashcards_out.append({"id": c.id, "topic_id": t.id, "topic_name": t.name,
                                "subject_name": s.name, "question": c.question, "answer": c.answer})

    for qu, t, s in question_rows:
        snippet = qu.notes or qu.statement or ""
        flat.append({"type": "question", "id": qu.id,
                     "title": f"{'✅' if qu.correct else '❌'} {qu.source or 'Manual'} — {t.name}",
                     "subtitle": f"{s.name} · {qu.answered_at.strftime('%d/%m/%Y') if qu.answered_at else ''}",
                     "snippet": snippet, "topic_id": t.id, "topic_name": t.name})
        questions_out.append({"id": qu.id, "topic_id": t.id, "topic_name": t.name,
                               "subject_name": s.name, "correct": qu.correct,
                               "statement": qu.statement or "", "notes": qu.notes or "",
                               "source": qu.source or "",
                               "answered_at": qu.answered_at.strftime("%d/%m/%Y") if qu.answered_at else ""})

    # Return grouped format for search page; flat list kept for backward compat (palette)
    return {"topics": topics_out, "questions": questions_out, "flashcards": flashcards_out,
            "results": flat[:limit]}


# ── Horas Estudadas (StudySession) ────────────────────────────────────────────

@app.get("/api/sessions/stats")
def sessions_stats():
    """Hours studied today, this week, and per-day for the last 7 days."""
    from datetime import timedelta
    db = get_session()
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    today_dt = datetime(today.year, today.month, today.day)
    week_dt  = datetime(week_start.year, week_start.month, week_start.day)

    sessions = db.query(StudySession).filter(
        StudySession.started_at >= week_dt,
        StudySession.duration_minutes.isnot(None),
    ).all()

    today_min = sum(s.duration_minutes for s in sessions if s.started_at >= today_dt)
    week_min  = sum(s.duration_minutes for s in sessions)

    daily: dict[str, int] = {}
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        daily[d] = 0
    all7 = db.query(StudySession).filter(
        StudySession.started_at >= today_dt - timedelta(days=6),
        StudySession.duration_minutes.isnot(None),
    ).all()
    for s in all7:
        key = s.started_at.date().isoformat()
        if key in daily:
            daily[key] += s.duration_minutes

    return {
        "today_minutes": today_min,
        "week_minutes": week_min,
        "daily": [{"date": d, "minutes": m} for d, m in daily.items()],
    }


@app.get("/api/sessions/by-subject")
def sessions_by_subject():
    """Total study minutes per subject, all time."""
    db = get_session()
    rows = (db.query(Subject.name, func.sum(StudySession.duration_minutes).label("minutes"))
              .join(Topic, StudySession.topic_id == Topic.id)
              .join(Subject, Topic.subject_id == Subject.id)
              .filter(StudySession.duration_minutes.isnot(None))
              .group_by(Subject.name)
              .order_by(func.sum(StudySession.duration_minutes).desc())
              .all())
    return [{"subject": r.name, "minutes": int(r.minutes or 0)} for r in rows]


# ── Sessions History List ─────────────────────────────────────────────────────

@app.get("/api/sessions/history")
def sessions_history(limit: int = 20):
    """Last N study sessions with topic/subject context."""
    from datetime import timedelta
    db = get_session()
    rows = (db.query(StudySession, Topic, Subject)
              .join(Topic, StudySession.topic_id == Topic.id)
              .join(Subject, Topic.subject_id == Subject.id)
              .order_by(StudySession.started_at.desc())
              .limit(limit).all())
    return [
        {
            "id": s.id,
            "topic_id": t.id,
            "topic_name": t.name,
            "subject_name": sub.name,
            "started_at": s.started_at.strftime("%d/%m/%Y %H:%M"),
            "duration_minutes": s.duration_minutes,
            "session_type": s.session_type or "review",
            "notes": s.notes or "",
        }
        for s, t, sub in rows
    ]


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: int):
    db = get_session()
    s = db.get(StudySession, session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Sessão não encontrada.")
    db.delete(s)
    db.commit()
    return {"ok": True}


# ── Ficha de Tópico (para PDF) ────────────────────────────────────────────────

@app.get("/api/topics/{topic_id}/sheet")
def topic_sheet(topic_id: int):
    """Full data for a topic's printable study sheet."""
    db = get_session()
    t = db.get(Topic, topic_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tópico não encontrado.")
    s = db.get(Subject, t.subject_id)
    cards = db.query(FlashCard).filter(FlashCard.topic_id == topic_id).all()
    qs = db.query(Question).filter(Question.topic_id == topic_id).all()
    total = len(qs); wrong = sum(1 for q in qs if not q.correct)
    return {
        "topic_id": t.id, "topic_name": t.name, "subject_name": s.name if s else "",
        "study_notes": t.study_notes or "",
        "anki_deck": t.anki_deck or "",
        "flashcards": [{"question": c.question, "answer": c.answer} for c in cards],
        "stats": {"total": total, "wrong": wrong,
                  "accuracy_pct": round((total-wrong)/total*100,1) if total else None},
    }


# ── Stats com filtro de período ───────────────────────────────────────────────

@app.get("/api/stats/period")
def stats_period(days: int = 30):
    """Key stats filtered to last N days (0 = all time)."""
    from datetime import timedelta
    db = get_session()
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days) if days > 0 else datetime(2000, 1, 1)

    rows = (db.query(Question, Subject)
              .join(Topic, Question.topic_id == Topic.id)
              .join(Subject, Topic.subject_id == Subject.id)
              .filter(Question.answered_at >= since).all())

    total = len(rows)
    correct = sum(1 for q, _ in rows if q.correct)

    subj_data: dict[str, list] = {}
    for q, s in rows:
        subj_data.setdefault(s.name, []).append(q)

    subjects = []
    for name, qs in subj_data.items():
        t = len(qs); c = sum(1 for q in qs if q.correct)
        subjects.append({"subject": name, "total": t, "correct": c,
                         "accuracy_pct": round(c / t * 100, 1) if t else 0})

    return {
        "days": days, "total": total, "correct": correct, "wrong": total - correct,
        "accuracy_pct": round(correct / total * 100, 1) if total else 0,
        "subjects": sorted(subjects, key=lambda x: x["accuracy_pct"], reverse=True),
    }


# ── Tags ──────────────────────────────────────────────────────────────────────

class TagIn(BaseModel):
    name: str
    color: str = "#2979E0"


@app.get("/api/tags")
def list_tags():
    db = get_session()
    return [{"id": t.id, "name": t.name, "color": t.color,
             "topic_count": len(t.topics)} for t in db.query(Tag).order_by(Tag.name).all()]


@app.post("/api/tags")
def create_tag(body: TagIn):
    db = get_session()
    existing = db.query(Tag).filter(Tag.name.ilike(body.name.strip())).first()
    if existing:
        return {"id": existing.id, "name": existing.name, "color": existing.color}
    tag = Tag(name=body.name.strip(), color=body.color)
    db.add(tag); db.commit(); db.refresh(tag)
    return {"id": tag.id, "name": tag.name, "color": tag.color}


@app.delete("/api/tags/{tag_id}")
def delete_tag(tag_id: int):
    db = get_session()
    tag = db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag não encontrada.")
    db.delete(tag); db.commit()
    return {"ok": True}


@app.get("/api/topics/{topic_id}/tags")
def get_topic_tags(topic_id: int):
    db = get_session()
    t = db.get(Topic, topic_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tópico não encontrado.")
    return [{"id": tg.id, "name": tg.name, "color": tg.color} for tg in t.tags]


@app.post("/api/topics/{topic_id}/tags/{tag_id}")
def add_topic_tag(topic_id: int, tag_id: int):
    db = get_session()
    t  = db.get(Topic, topic_id)
    tg = db.get(Tag,   tag_id)
    if not t or not tg:
        raise HTTPException(status_code=404, detail="Tópico ou tag não encontrado.")
    if tg not in t.tags:
        t.tags.append(tg)
        db.commit()
    return {"ok": True}


@app.delete("/api/topics/{topic_id}/tags/{tag_id}")
def remove_topic_tag(topic_id: int, tag_id: int):
    db = get_session()
    t  = db.get(Topic, topic_id)
    tg = db.get(Tag,   tag_id)
    if t and tg and tg in t.tags:
        t.tags.remove(tg)
        db.commit()
    return {"ok": True}


@app.get("/api/tags/topics")
def topics_by_tag_name(tag: str):
    """Return topics that have a given tag name. Used by Quick Review tag filter."""
    db = get_session()
    t = db.query(Tag).filter(Tag.name == tag).first()
    if not t:
        return []
    return [{"id": tp.id, "name": tp.name} for tp in t.topics]


@app.get("/api/tags/{tag_id}/topics")
def topics_by_tag(tag_id: int):
    db = get_session()
    tag = db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag não encontrada.")
    scores = {s.topic_id: s for s in TopicScorer(db).score_all()}
    return [{"id": t.id, "name": t.name,
             "subject_name": db.get(Subject, t.subject_id).name if t.subject_id else "",
             "error_rate_pct": round(scores[t.id].error_rate * 100, 1) if t.id in scores else 0}
            for t in tag.topics]


# ── Relatório Mensal ──────────────────────────────────────────────────────────

@app.get("/api/reports/monthly")
def monthly_report(year: int | None = None, month: int | None = None):
    from datetime import timedelta
    import calendar
    db = get_session()
    today = date.today()
    y = year  or today.year
    m = month or today.month
    _, last_day = calendar.monthrange(y, m)
    start = datetime(y, m, 1)
    end   = datetime(y, m, last_day, 23, 59, 59)

    qs = db.query(Question).filter(Question.answered_at >= start,
                                   Question.answered_at <= end).all()
    sessions = db.query(StudySession).filter(StudySession.started_at >= start,
                                             StudySession.started_at <= end,
                                             StudySession.duration_minutes.isnot(None)).all()
    fc_reviewed = db.query(FlashCard).filter(FlashCard.last_reviewed >= start,
                                             FlashCard.last_reviewed <= end).count()

    total = len(qs); correct = sum(1 for q in qs if q.correct)
    study_minutes = sum(s.duration_minutes for s in sessions)

    # per-subject
    rows = (db.query(Question, Subject)
              .join(Topic, Question.topic_id == Topic.id)
              .join(Subject, Topic.subject_id == Subject.id)
              .filter(Question.answered_at >= start, Question.answered_at <= end).all())
    subj: dict[str, dict] = {}
    for q, s in rows:
        if s.name not in subj:
            subj[s.name] = {"total": 0, "correct": 0}
        subj[s.name]["total"]   += 1
        subj[s.name]["correct"] += int(q.correct)

    # daily activity
    daily: dict[str, dict] = {}
    for q in qs:
        key = q.answered_at.strftime("%Y-%m-%d")
        daily.setdefault(key, {"total": 0, "correct": 0})
        daily[key]["total"]   += 1
        daily[key]["correct"] += int(q.correct)

    subjects_summary = sorted([
        {"subject": name, "total": d["total"], "correct": d["correct"],
         "accuracy_pct": round(d["correct"] / d["total"] * 100, 1) if d["total"] else 0}
        for name, d in subj.items()
    ], key=lambda x: x["accuracy_pct"], reverse=True)

    return {
        "year": y, "month": m,
        "total_questions": total, "correct": correct,
        "accuracy_pct": round(correct / total * 100, 1) if total else 0,
        "study_minutes": study_minutes,
        "fc_reviewed": fc_reviewed,
        "active_days": len(daily),
        "subjects": subjects_summary,
        "daily": [{"date": d, **v} for d, v in sorted(daily.items())],
    }


# ── AnkiConnect Aprofundado ───────────────────────────────────────────────────

@app.post("/api/anki/deep-sync")
def anki_deep_sync():
    """Pull per-card stats (lapses, ease, interval) from AnkiConnect for all linked decks."""
    import urllib.request, json as _json
    from medstudies.persistence.models import AnkiSnapshot

    def anki(action, **params):
        payload = _json.dumps({"action": action, "version": 6, "params": params}).encode()
        try:
            with urllib.request.urlopen("http://localhost:8765", payload, timeout=5) as r:
                return _json.loads(r.read())["result"]
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"AnkiConnect indisponível: {exc}")

    db = get_session()
    topics = db.query(Topic).filter(Topic.anki_deck.isnot(None)).all()
    if not topics:
        return {"synced": 0, "message": "Nenhum tópico com deck Anki configurado."}

    synced = 0
    details = []
    for t in topics:
        try:
            card_ids = anki("findCards", query=f'deck:"{t.anki_deck}"') or []
            if not card_ids:
                continue
            cards_info = anki("cardsInfo", cards=card_ids) or []
            total  = len(cards_info)
            due    = sum(1 for c in cards_info if c.get("due", 0) <= 0 and c.get("queue", -1) in (1, 2, 3))
            lapses = sum(c.get("lapses", 0) for c in cards_info)
            avg_ease     = sum(c.get("factor", 2500) for c in cards_info) / total if total else 2500
            avg_interval = sum(c.get("interval", 0) for c in cards_info) / total if total else 0

            snap = AnkiSnapshot(
                topic_id=t.id, deck_name=t.anki_deck,
                total_cards=total, due_cards=due,
                avg_ease=avg_ease / 1000,  # Anki stores as x1000
                avg_interval=avg_interval,
                total_lapses=lapses,
            )
            db.add(snap)
            synced += 1
            details.append({"topic": t.name, "deck": t.anki_deck,
                            "total": total, "due": due, "lapses": lapses,
                            "avg_ease": round(avg_ease/1000, 2),
                            "avg_interval": round(avg_interval, 1)})
        except HTTPException:
            raise
        except Exception:
            pass

    db.commit()
    return {"synced": synced, "details": details}

# ── Agenda SM-2 ───────────────────────────────────────────────────────────────

@app.get("/api/agenda/sm2")
def agenda_sm2(days: int = 14):
    """Cards and topics due for SM-2 review in the next N days, grouped by date."""
    from datetime import timedelta
    db = get_session()
    now  = datetime.now(timezone.utc).replace(tzinfo=None)
    end  = now + timedelta(days=days)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Flash cards due (include today from midnight)
    cards = (db.query(FlashCard, Topic, Subject)
               .join(Topic, FlashCard.topic_id == Topic.id)
               .join(Subject, Topic.subject_id == Subject.id)
               .filter(FlashCard.next_review.isnot(None),
                       FlashCard.next_review >= today_start,
                       FlashCard.next_review <= end)
               .order_by(FlashCard.next_review).all())

    # Topics due (TopicReview)
    topic_reviews = (db.query(TopicReview, Topic, Subject)
                       .join(Topic, TopicReview.topic_id == Topic.id)
                       .join(Subject, Topic.subject_id == Subject.id)
                       .filter(TopicReview.next_review.isnot(None),
                               TopicReview.next_review >= today_start,
                               TopicReview.next_review <= end)
                       .order_by(TopicReview.next_review).all())

    by_date: dict[str, dict] = {}
    for c, t, s in cards:
        d = c.next_review.strftime("%Y-%m-%d")
        by_date.setdefault(d, {"date": d, "cards": [], "topics": []})
        by_date[d]["cards"].append({
            "id": c.id, "question": c.question[:80], "topic_name": t.name,
            "subject_name": s.name, "ease_factor": round(c.ease_factor or 2.5, 2),
            "interval_days": round(c.interval_days or 1.0, 1),
        })

    for tr, t, s in topic_reviews:
        d = tr.next_review.strftime("%Y-%m-%d")
        by_date.setdefault(d, {"date": d, "cards": [], "topics": []})
        by_date[d]["topics"].append({
            "id": t.id, "name": t.name, "subject_name": s.name,
            "interval_days": round(tr.interval_days or 1.0, 1),
            "ease_factor": round(tr.ease_factor or 2.5, 2),
        })

    return sorted(by_date.values(), key=lambda x: x["date"])


# ── Questão com Enunciado ─────────────────────────────────────────────────────

@app.get("/api/questions/{question_id}/statement")
def get_question_statement(question_id: int):
    db = get_session()
    q = db.get(Question, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="Questão não encontrada.")
    return {"id": q.id, "statement": getattr(q, "statement", "") or ""}


# ── Export iCal ───────────────────────────────────────────────────────────────

@app.get("/api/export/ical")
def export_ical(weeks: int = 4):
    """Export auto-schedule as iCal .ics file."""
    from datetime import timedelta
    import uuid as _uuid
    db = get_session()
    scores = TopicScorer(db).score_all()
    if not scores:
        raise HTTPException(status_code=404, detail="Sem tópicos para agendar.")

    today = date.today()
    # Monday of current week
    start = today - timedelta(days=today.weekday())

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Study Agent Hub//MedStudies//PT",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Study Agent Hub",
        "X-WR-TIMEZONE:America/Sao_Paulo",
    ]

    # Distribute topics across days (Mon–Sat, skip Sun)
    work_days = []
    d = start
    while len(work_days) < weeks * 6:
        if d.weekday() != 6:  # skip Sunday
            work_days.append(d)
        d += timedelta(days=1)

    per_day = max(1, len(scores) // max(1, len(work_days)))
    idx = 0
    for day_date in work_days:
        day_topics = scores[idx: idx + per_day]
        idx += per_day
        if idx >= len(scores):
            idx = 0
        for i, ts in enumerate(day_topics):
            dtstart  = datetime(day_date.year, day_date.month, day_date.day, 7 + i * 2, 0)
            dtend    = datetime(day_date.year, day_date.month, day_date.day, 8 + i * 2, 0)
            uid      = str(_uuid.uuid4())
            fmt      = "%Y%m%dT%H%M%S"
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTART:{dtstart.strftime(fmt)}",
                f"DTEND:{dtend.strftime(fmt)}",
                f"SUMMARY:📚 {ts.topic_name}",
                f"DESCRIPTION:{ts.subject_name} — Score {ts.priority_score:.2f} — {ts.reason}",
                "STATUS:CONFIRMED",
                "END:VEVENT",
            ]

    lines.append("END:VCALENDAR")
    content = "\r\n".join(lines)
    return StreamingResponse(
        io.BytesIO(content.encode("utf-8")),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=medstudies_schedule.ics"},
    )


# ── Conquistas (servidor) ─────────────────────────────────────────────────────

@app.get("/api/conquistas")
def get_conquistas():
    """Compute all achievement states server-side for accuracy."""
    from datetime import timedelta
    db = get_session()

    questions = db.query(Question).all()
    total_q   = len(questions)
    correct_q = sum(1 for q in questions if q.correct)

    fc_total    = db.query(FlashCard).count()
    fc_reviewed = db.query(FlashCard).filter(FlashCard.times_reviewed > 0).count()
    sm2_cards   = db.query(FlashCard).filter(FlashCard.repetitions >= 3).count()

    topics_count   = db.query(Topic).count()
    tags_count     = db.query(Tag).count()
    sessions_count = db.query(StudySession).count()
    session_mins   = db.query(func.sum(StudySession.duration_minutes)).scalar() or 0

    # Streak (current)
    days_with_q: set[date] = {q.answered_at.date() for q in questions}
    streak = 0
    check  = date.today()
    while check in days_with_q:
        streak += 1
        check -= timedelta(days=1)

    # Best streak all-time
    best_streak = 0
    best_streak_end: date | None = None
    if days_with_q:
        sorted_days = sorted(days_with_q)
        run = 1; run_end = sorted_days[0]
        for i in range(1, len(sorted_days)):
            if sorted_days[i] - sorted_days[i-1] == timedelta(days=1):
                run += 1
            else:
                if run > best_streak:
                    best_streak = run; best_streak_end = run_end
                run = 1
            run_end = sorted_days[i]
        if run > best_streak:
            best_streak = run; best_streak_end = run_end
    best_streak_end_str = best_streak_end.isoformat() if best_streak_end else None

    sources = {q.source for q in questions if q.source}
    accuracy = round(correct_q / total_q * 100, 1) if total_q else 0
    _topic_total: dict[int, int] = defaultdict(int)
    _topic_wrong: dict[int, int] = defaultdict(int)
    for q in questions:
        _topic_total[q.topic_id] += 1
        if not q.correct:
            _topic_wrong[q.topic_id] += 1
    weak = sum(
        1 for tid, tot in _topic_total.items()
        if tot >= 3 and _topic_wrong[tid] / tot >= 0.5
    )

    fc_mastered = db.query(FlashCard).filter(FlashCard.interval_days >= 21).count()
    state = {
        "total_q": total_q, "correct_q": correct_q, "accuracy": accuracy,
        "streak": streak, "best_streak": best_streak, "best_streak_end": best_streak_end_str,
        "topics": topics_count, "tags": tags_count,
        "fc_total": fc_total, "fc_reviewed": fc_reviewed, "sm2_cards": sm2_cards,
        "sessions": sessions_count, "session_mins": session_mins,
        "sources": len(sources), "weak": weak,
        # aliases used by frontend conquistas
        "total_flashcards": fc_total,
        "fc_mastered": fc_mastered,
        "total_sessions": sessions_count,
    }
    return state


# ── Flash Cards — Maturidade SM-2 ────────────────────────────────────────────

@app.get("/api/flashcards/maturity")
def flashcards_maturity():
    """Cards bucketed by interval_days for maturity doughnut chart."""
    db = get_session()
    cards = db.query(FlashCard).all()
    buckets = {"Novo (0-1d)": 0, "Aprendendo (2-6d)": 0,
               "Jovem (7-20d)": 0, "Maduro (21-60d)": 0, "Veterano (>60d)": 0}
    for c in cards:
        iv = c.interval_days or 0
        if iv <= 1:      buckets["Novo (0-1d)"] += 1
        elif iv <= 6:    buckets["Aprendendo (2-6d)"] += 1
        elif iv <= 20:   buckets["Jovem (7-20d)"] += 1
        elif iv <= 60:   buckets["Maduro (21-60d)"] += 1
        else:            buckets["Veterano (>60d)"] += 1
    return {"total": len(cards), "buckets": [{"label": k, "count": v} for k, v in buckets.items()]}


# ── Bulk Flash Cards ──────────────────────────────────────────────────────────

class BulkFCIn(BaseModel):
    topic_id: int
    lines: list[str]   # each "question;answer" or "question|answer"

@app.post("/api/flashcards/bulk")
def bulk_create_flashcards(body: BulkFCIn):
    """Create multiple flash cards at once from a list of Q;A pairs."""
    db = get_session()
    t = db.get(Topic, body.topic_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tópico não encontrado.")
    created = 0; skipped = 0
    for raw in body.lines:
        raw = raw.strip()
        if not raw:
            continue
        sep = ';' if ';' in raw else '|' if '|' in raw else None
        if not sep:
            skipped += 1; continue
        parts = raw.split(sep, 1)
        if len(parts) != 2:
            skipped += 1; continue
        q_text, a_text = parts[0].strip(), parts[1].strip()
        if not q_text or not a_text:
            skipped += 1; continue
        db.add(FlashCard(topic_id=body.topic_id, question=q_text, answer=a_text))
        created += 1
    db.commit()
    return {"created": created, "skipped": skipped}


# ── Topic Detail ──────────────────────────────────────────────────────────────

@app.get("/api/topics/{topic_id}/detail")
def topic_detail(topic_id: int):
    """Full topic detail: stats, last questions, flash cards, sessions, SM-2 state, tags."""
    db = get_session()
    t = db.get(Topic, topic_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tópico não encontrado.")
    s = db.get(Subject, t.subject_id)

    qs = (db.query(Question).filter(Question.topic_id == topic_id)
            .order_by(Question.answered_at.desc()).limit(10).all())
    total_qs = db.query(Question).filter(Question.topic_id == topic_id).count()
    correct_qs = db.query(Question).filter(Question.topic_id == topic_id, Question.correct == True).count()

    cards = db.query(FlashCard).filter(FlashCard.topic_id == topic_id).all()
    due_cards = sum(1 for c in cards if c.next_review is None or c.next_review <= datetime.now(timezone.utc).replace(tzinfo=None))

    sessions = (db.query(StudySession).filter(StudySession.topic_id == topic_id)
                  .order_by(StudySession.started_at.desc()).limit(5).all())
    total_min = sum((s_.duration_minutes or 0) for s_ in
                    db.query(StudySession).filter(StudySession.topic_id == topic_id).all())

    review = db.query(TopicReview).filter(TopicReview.topic_id == topic_id).first()
    tags = [{"id": tg.id, "name": tg.name, "color": tg.color} for tg in t.tags]

    return {
        "id": t.id, "name": t.name, "subject_name": s.name if s else "",
        "study_notes": t.study_notes or "",
        "is_favorite": bool(t.is_favorite),
        "anki_deck": t.anki_deck or "",
        "notability_notebook": t.notability_notebook or "",
        "tags": tags,
        "stats": {
            "total_questions": total_qs,
            "correct": correct_qs,
            "accuracy_pct": round(correct_qs / total_qs * 100, 1) if total_qs else None,
            "total_flashcards": len(cards),
            "due_flashcards": due_cards,
            "total_sessions": db.query(StudySession).filter(StudySession.topic_id == topic_id).count(),
            "total_study_minutes": total_min,
        },
        "sm2": {
            "ease_factor": round(review.ease_factor, 2) if review else None,
            "interval_days": round(review.interval_days, 1) if review else None,
            "repetitions": review.repetitions if review else 0,
            "next_review": review.next_review.strftime("%d/%m/%Y") if review and review.next_review else None,
            "last_reviewed": review.last_reviewed.strftime("%d/%m/%Y") if review and review.last_reviewed else None,
        },
        "recent_questions": [
            {"id": q.id, "correct": q.correct, "source": q.source or "Manual",
             "answered_at": q.answered_at.strftime("%d/%m/%Y") if q.answered_at else "—",
             "notes": q.notes or "", "difficulty": q.difficulty or "medio",
             "statement": q.statement or ""}
            for q in qs
        ],
        "sessions": [
            {"id": s_.id, "started_at": s_.started_at.strftime("%d/%m/%Y") if s_.started_at else "—",
             "duration_minutes": s_.duration_minutes, "session_type": s_.session_type}
            for s_ in sessions
        ],
        "flashcards": [
            {"id": c.id, "question": c.question, "answer": c.answer,
             "interval_days": round(c.interval_days or 1, 1),
             "times_reviewed": c.times_reviewed or 0}
            for c in cards
        ],
    }


# ── XP & Nível ────────────────────────────────────────────────────────────────

XP_PER_QUESTION_CORRECT = 10
XP_PER_QUESTION_WRONG   = 3
XP_PER_SESSION_MINUTE   = 1
XP_PER_FC_REVIEW        = 5
XP_PER_FC_CORRECT       = 3   # bonus for quality >= 3
LEVEL_BASE = 100              # XP needed for level 1→2; grows by 50 per level

def xp_for_level(level: int) -> int:
    return LEVEL_BASE * level + 50 * level * (level - 1) // 2

def level_from_xp(xp: int) -> tuple[int, int, int]:
    """Returns (level, xp_in_level, xp_for_next)."""
    level = 1
    while xp_for_level(level) <= 0 or xp >= xp_for_level(level):
        if xp < xp_for_level(level):
            break
        level += 1
    prev = xp_for_level(level - 1) if level > 1 else 0
    nxt  = xp_for_level(level)
    return level, xp - prev, nxt - prev

@app.get("/api/xp")
def get_xp():
    """Calculate total XP and level from all activities."""
    db = get_session()
    questions = db.query(Question).all()
    q_xp = sum(XP_PER_QUESTION_CORRECT if q.correct else XP_PER_QUESTION_WRONG for q in questions)
    sess_xp = sum((s.duration_minutes or 0) * XP_PER_SESSION_MINUTE
                  for s in db.query(StudySession).all())
    fc_reviews = db.query(FlashCard).filter(FlashCard.times_reviewed > 0).all()
    fc_xp = sum(XP_PER_FC_REVIEW + (XP_PER_FC_CORRECT if (c.repetitions or 0) > 0 else 0) for c in fc_reviews)
    total_xp = q_xp + sess_xp + fc_xp
    level, xp_in, xp_needed = level_from_xp(total_xp)
    return {
        "total_xp": total_xp,
        "level": level,
        "xp_in_level": xp_in,
        "xp_for_next": xp_needed,
        "pct": round(xp_in / xp_needed * 100, 1) if xp_needed else 100,
        "breakdown": {"questions": q_xp, "sessions": sess_xp, "flashcards": fc_xp},
    }

# ── XP History (last 30 days) ─────────────────────────────────────────────────

@app.get("/api/xp/history")
def get_xp_history(days: int = 30):
    """Return daily XP earned for the last N days (cumulative line chart)."""
    from datetime import timedelta
    db = get_session()
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    # Daily XP from questions
    day_xp: dict[str, int] = defaultdict(int)
    for q in db.query(Question).filter(Question.answered_at >= cutoff).all():
        day = q.answered_at.strftime("%Y-%m-%d")
        day_xp[day] += XP_PER_QUESTION_CORRECT if q.correct else XP_PER_QUESTION_WRONG

    # Daily XP from sessions
    for s in db.query(StudySession).filter(StudySession.started_at >= cutoff).all():
        day = s.started_at.strftime("%Y-%m-%d")
        day_xp[day] += (s.duration_minutes or 0) * XP_PER_SESSION_MINUTE

    # Daily XP from FC reviews
    for c in db.query(FlashCard).filter(FlashCard.last_reviewed >= cutoff, FlashCard.last_reviewed != None).all():
        day = c.last_reviewed.strftime("%Y-%m-%d")
        day_xp[day] += XP_PER_FC_REVIEW + (XP_PER_FC_CORRECT if (c.repetitions or 0) > 0 else 0)

    # Build ordered list for last N days
    today = date.today()
    result = []
    running = 0
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        earned = day_xp.get(d, 0)
        running += earned
        result.append({"date": d, "xp_earned": earned, "xp_cumulative": running})

    return result


# ── Subject accuracy goals (stored as JSON column in a config table) ──────────

@app.get("/api/subjects/accuracy-goals")
def get_accuracy_goals():
    """Return per-subject accuracy goals (stored in daily_plans as config row)."""
    db = get_session()
    row = db.query(DailyPlan).filter(DailyPlan.plan_date == '__accuracy_goals__').first()
    if not row:
        return {}
    import json
    return json.loads(row.plan_json)

@app.post("/api/subjects/accuracy-goals")
def save_accuracy_goals(payload: dict):
    """Save per-subject accuracy goals (reuses DailyPlan table as config store)."""
    import json
    db = get_session()
    row = db.query(DailyPlan).filter(DailyPlan.plan_date == '__accuracy_goals__').first()
    if row:
        row.plan_json = json.dumps(payload)
    else:
        db.add(DailyPlan(plan_date='__accuracy_goals__', plan_json=json.dumps(payload)))
    db.commit()
    return {"ok": True}


# ── Export Erros CSV ──────────────────────────────────────────────────────────

@app.get("/api/questions/errors/export")
def export_errors_csv(subject_id: int | None = None, difficulty: str | None = None):
    """Export wrong questions as CSV."""
    db = get_session()
    q = (db.query(Question, Topic, Subject)
           .join(Topic, Question.topic_id == Topic.id)
           .join(Subject, Topic.subject_id == Subject.id)
           .filter(Question.correct == False))
    if subject_id:
        q = q.filter(Subject.id == subject_id)
    if difficulty:
        q = q.filter(Question.difficulty == difficulty)
    rows = q.order_by(Subject.name, Topic.name, Question.answered_at.desc()).all()

    output = io.StringIO()
    writer = csv_module.writer(output)
    writer.writerow(["Matéria", "Tópico", "Fonte", "Data", "Dificuldade", "Anotação"])
    for question, topic, subject in rows:
        writer.writerow([
            subject.name,
            topic.name,
            question.source or "Manual",
            question.answered_at.strftime("%d/%m/%Y") if question.answered_at else "",
            question.difficulty or "medio",
            question.notes or "",
        ])
    output.seek(0)
    filename = f"erros_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),  # utf-8-sig for Excel
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Backup & Restore ──────────────────────────────────────────────────────────

@app.get("/api/backup")
def backup_db():
    """Download the raw SQLite database file."""
    import shutil, tempfile
    db_path = os.environ.get("MEDSTUDIES_DB", "data/medstudies.db")
    if not os.path.exists(db_path):
        raise HTTPException(status_code=404, detail="Banco não encontrado.")
    # Copy to temp to avoid locking issues
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    shutil.copy2(db_path, tmp.name)
    tmp.close()
    filename = f"medstudies_backup_{date.today().isoformat()}.db"
    return FileResponse(tmp.name, media_type="application/octet-stream",
                        filename=filename)


@app.post("/api/restore")
async def restore_db(file: UploadFile = File(...)):
    """Restore database from uploaded .db file."""
    import shutil, tempfile
    if not file.filename.endswith(".db"):
        raise HTTPException(status_code=400, detail="Arquivo deve ser .db")
    db_path = os.environ.get("MEDSTUDIES_DB", "data/medstudies.db")
    content = await file.read()
    # Validate: SQLite magic bytes
    if not content.startswith(b"SQLite format 3"):
        raise HTTPException(status_code=400, detail="Arquivo não é um banco SQLite válido.")
    # Backup current before overwrite
    if os.path.exists(db_path):
        shutil.copy2(db_path, db_path + ".bak")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with open(db_path, "wb") as f:
        f.write(content)
    return {"ok": True, "size_kb": round(len(content) / 1024, 1)}


# ── Biblioteca ────────────────────────────────────────────────────────────────

LIBRARY_DIR = Path(os.environ.get("MEDSTUDIES_DB", "data/medstudies.db")).parent / "library"
LIBRARY_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mp3", ".txt", ".md"}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


class LibraryItemCreate(BaseModel):
    title: str
    item_type: str          # pdf | link | note | video
    description: str | None = None
    url: str | None = None
    content: str | None = None
    subject_id: int | None = None
    topic_id: int | None = None
    tags: str | None = None
    source: str | None = None
    year: int | None = None


@app.get("/api/library")
def library_list(
    item_type: str | None = None,
    subject_id: int | None = None,
    topic_id: int | None = None,
    favorite: bool | None = None,
    q: str | None = None,
    skip: int = 0,
    limit: int = 200,
):
    with get_session() as db:
        query = db.query(LibraryItem).order_by(LibraryItem.created_at.desc())
        if item_type:
            query = query.filter(LibraryItem.item_type == item_type)
        if subject_id:
            query = query.filter(LibraryItem.subject_id == subject_id)
        if topic_id:
            query = query.filter(LibraryItem.topic_id == topic_id)
        if favorite is True:
            query = query.filter(LibraryItem.is_favorite == True)
        if q:
            q_pat = f"%{q}%"
            from sqlalchemy import or_
            query = query.filter(
                or_(
                    LibraryItem.title.ilike(q_pat),
                    LibraryItem.description.ilike(q_pat),
                    LibraryItem.tags.ilike(q_pat),
                )
            )
        total = query.count()
        items = query.offset(skip).limit(limit).all()

        # prefetch subjects to avoid N+1
        subject_ids = {i.subject_id for i in items if i.subject_id}
        subjects = {s.id: s.name for s in db.query(Subject).filter(Subject.id.in_(subject_ids)).all()} if subject_ids else {}

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "items": [
                {
                    "id": i.id,
                    "title": i.title,
                    "item_type": i.item_type,
                    "description": i.description,
                    "url": i.url,
                    "content": i.content,
                    "file_path": i.file_path,
                    "file_size": i.file_size,
                    "subject_id": i.subject_id,
                    "subject_name": subjects.get(i.subject_id),
                    "topic_id": i.topic_id,
                    "tags": i.tags,
                    "source": i.source,
                    "year": i.year,
                    "is_favorite": i.is_favorite,
                    "created_at": i.created_at.isoformat() if i.created_at else None,
                }
                for i in items
            ],
        }


@app.post("/api/library")
def library_create(body: LibraryItemCreate):
    with get_session() as db:
        item = LibraryItem(**body.model_dump())
        db.add(item)
        db.commit()
        db.refresh(item)
        return {"id": item.id}


@app.post("/api/library/upload")
async def library_upload(
    file: UploadFile = File(...),
    title: str = Form(...),
    description: str = Form(""),
    subject_id: str = Form(""),
    topic_id: str = Form(""),
    tags: str = Form(""),
    source: str = Form(""),
    year: str = Form(""),
):
    suffix = Path(file.filename or "file").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Extensão não permitida: {suffix}")

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Arquivo muito grande (max 100 MB)")

    import uuid
    fname = f"{uuid.uuid4().hex}{suffix}"
    fpath = LIBRARY_DIR / fname

    item_type = (
        "pdf"   if suffix == ".pdf" else
        "video" if suffix in {".mp4"} else
        "audio" if suffix in {".mp3"} else
        "image" if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"} else
        "note"  if suffix in {".txt", ".md"} else "file"
    )

    fpath.write_bytes(content)
    try:
        with get_session() as db:
            item = LibraryItem(
                title=title,
                item_type=item_type,
                description=description or None,
                file_path=fname,
                file_size=len(content),
                subject_id=int(subject_id) if subject_id.isdigit() else None,
                topic_id=int(topic_id) if topic_id.isdigit() else None,
                tags=tags or None,
                source=source or None,
                year=int(year) if year.isdigit() else None,
            )
            db.add(item)
            db.commit()
            db.refresh(item)
            return {"id": item.id, "file_path": fname, "size_bytes": len(content)}
    except Exception:
        fpath.unlink(missing_ok=True)
        raise


@app.get("/api/library/{item_id}/file")
def library_serve_file(item_id: int):
    with get_session() as db:
        item = db.get(LibraryItem, item_id)
        if not item or not item.file_path:
            raise HTTPException(status_code=404)
        fpath = (LIBRARY_DIR / item.file_path).resolve()
        if not str(fpath).startswith(str(LIBRARY_DIR.resolve())):
            raise HTTPException(status_code=400, detail="Caminho inválido")
        if not fpath.exists():
            raise HTTPException(status_code=404, detail="Arquivo não encontrado no disco")
        suffix = fpath.suffix.lower()
        media_types = {
            ".pdf": "application/pdf",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp",
            ".mp4": "video/mp4", ".mp3": "audio/mpeg",
            ".txt": "text/plain", ".md": "text/markdown",
        }
        return FileResponse(str(fpath), media_type=media_types.get(suffix, "application/octet-stream"),
                            filename=item.title + suffix)


@app.patch("/api/library/{item_id}")
def library_update(item_id: int, body: dict):
    with get_session() as db:
        item = db.get(LibraryItem, item_id)
        if not item:
            raise HTTPException(status_code=404)
        allowed = {"title", "description", "url", "content", "subject_id", "topic_id",
                   "tags", "source", "year", "is_favorite"}
        for k, v in body.items():
            if k in allowed:
                setattr(item, k, v)
        db.commit()
        return {"ok": True}


@app.delete("/api/library/{item_id}")
def library_delete(item_id: int):
    with get_session() as db:
        item = db.get(LibraryItem, item_id)
        if not item:
            raise HTTPException(status_code=404)
        # delete file from disk if exists
        if item.file_path:
            fpath = LIBRARY_DIR / item.file_path
            if fpath.exists():
                fpath.unlink()
        db.delete(item)
        db.commit()
        return {"ok": True}


@app.get("/api/library/stats")
def library_stats():
    with get_session() as db:
        from sqlalchemy import func as sqlfunc
        rows = db.query(LibraryItem.item_type, sqlfunc.count(LibraryItem.id))\
                 .group_by(LibraryItem.item_type).all()
        total_size = db.query(sqlfunc.sum(LibraryItem.file_size)).scalar() or 0
        return {
            "by_type": {r[0]: r[1] for r in rows},
            "total": sum(r[1] for r in rows),
            "total_size_mb": round(total_size / 1024 / 1024, 1),
        }



# ── Edital (Curriculum Map) ────────────────────────────────────────────────────

class EditorialTopicCreate(BaseModel):
    exam_name: str
    subject_name: str
    topic_name: str
    weight_pct: float = 0.0
    topic_id: int | None = None


@app.get("/api/editorial")
def editorial_list(exam_name: str | None = None):
    with get_session() as db:
        q = db.query(EditorialTopic)
        if exam_name:
            q = q.filter(EditorialTopic.exam_name == exam_name)
        items = q.order_by(EditorialTopic.subject_name, EditorialTopic.topic_name).all()
        return [
            {
                "id": i.id, "exam_name": i.exam_name,
                "subject_name": i.subject_name, "topic_name": i.topic_name,
                "weight_pct": i.weight_pct, "topic_id": i.topic_id,
                "covered": i.topic_id is not None,
            }
            for i in items
        ]


@app.get("/api/editorial/exams")
def editorial_exams():
    with get_session() as db:
        rows = db.query(EditorialTopic.exam_name, func.count(EditorialTopic.id))\
                 .group_by(EditorialTopic.exam_name).all()
        return [{"exam_name": r[0], "total": r[1]} for r in rows]


@app.get("/api/editorial/coverage")
def editorial_coverage(exam_name: str | None = None):
    with get_session() as db:
        q = db.query(EditorialTopic)
        if exam_name:
            q = q.filter(EditorialTopic.exam_name == exam_name)
        items = q.all()
        total = len(items)
        covered = sum(1 for i in items if i.topic_id)
        by_subject: dict = {}
        for i in items:
            s = by_subject.setdefault(i.subject_name, {"total": 0, "covered": 0, "weight": 0.0})
            s["total"] += 1
            s["weight"] += i.weight_pct or 0
            if i.topic_id:
                s["covered"] += 1
        return {
            "total": total, "covered": covered,
            "pct": round(covered / total * 100, 1) if total else 0,
            "by_subject": [
                {
                    "subject": k,
                    "total": v["total"],
                    "covered": v["covered"],
                    "pct": round(v["covered"] / v["total"] * 100, 1) if v["total"] else 0,
                    "weight": round(v["weight"], 1),
                }
                for k, v in sorted(by_subject.items(), key=lambda x: -x[1]["weight"])
            ],
        }


@app.post("/api/editorial")
def editorial_create(body: EditorialTopicCreate):
    with get_session() as db:
        item = EditorialTopic(**body.model_dump())
        db.add(item); db.commit(); db.refresh(item)
        return {"id": item.id}


@app.post("/api/editorial/import-csv")
async def editorial_import_csv(
    file: UploadFile = File(...),
    exam_name: str = Form(...),
):
    content = (await file.read()).decode("utf-8-sig")
    import csv as csv_m, io
    reader = csv_m.DictReader(io.StringIO(content))
    rows = list(reader)
    with get_session() as db:
        all_topics = db.query(Topic).all()
        topic_map = {t.name.lower().strip(): t.id for t in all_topics}
        imported = 0
        for row in rows:
            subj = (row.get("subject_name") or row.get("materia") or "").strip()
            top  = (row.get("topic_name") or row.get("topico") or "").strip()
            wt   = float(row.get("weight_pct") or row.get("peso") or 0)
            if not subj or not top:
                continue
            matched_id = topic_map.get(top.lower())
            item = EditorialTopic(
                exam_name=exam_name, subject_name=subj,
                topic_name=top, weight_pct=wt, topic_id=matched_id,
            )
            db.add(item)
            imported += 1
        db.commit()
    return {"imported": imported}


@app.patch("/api/editorial/{item_id}/match")
def editorial_match(item_id: int, body: dict):
    with get_session() as db:
        item = db.get(EditorialTopic, item_id)
        if not item:
            raise HTTPException(404)
        item.topic_id = body.get("topic_id")
        db.commit()
        return {"ok": True}


@app.delete("/api/editorial/exam/{exam_name}")
def editorial_delete_exam(exam_name: str):
    with get_session() as db:
        db.query(EditorialTopic).filter(EditorialTopic.exam_name == exam_name).delete()
        db.commit()
        return {"ok": True}


@app.delete("/api/editorial/{item_id}")
def editorial_delete(item_id: int):
    with get_session() as db:
        item = db.get(EditorialTopic, item_id)
        if not item:
            raise HTTPException(404)
        db.delete(item); db.commit()
        return {"ok": True}


# ── Question Bank (MCQ with alternatives) ─────────────────────────────────────

class QuestionBankCreate(BaseModel):
    topic_id: int
    statement: str
    alternatives: list[str]
    correct_alt: str
    explanation: str | None = None
    source: str | None = None
    year: int | None = None
    difficulty: str = "medio"


def _fmt_question(q: Question, db, topic=None, subject=None) -> dict:
    import json as _json
    if topic is None:
        topic = db.get(Topic, q.topic_id) if q.topic_id else None
    if subject is None:
        subject = db.get(Subject, topic.subject_id) if topic and topic.subject_id else None
    return {
        "id": q.id, "topic_id": q.topic_id,
        "topic_name": topic.name if topic else None,
        "subject_name": subject.name if subject else None,
        "statement": q.statement,
        "alternatives": _json.loads(q.alternatives) if q.alternatives else [],
        "correct_alt": q.correct_alt,
        "chosen_alt": q.chosen_alt,
        "correct": q.correct,
        "explanation": q.explanation,
        "source": q.source, "year": q.year,
        "difficulty": q.difficulty,
        "answered_at": q.answered_at.isoformat() if q.answered_at else None,
        "notes": q.notes,
    }


@app.get("/api/questions/bank")
def question_bank_list(
    topic_id: int | None = None,
    subject_id: int | None = None,
    skip: int = 0,
    limit: int = 50,
):
    with get_session() as db:
        q = (db.query(Question, Topic, Subject)
               .join(Topic, Question.topic_id == Topic.id)
               .join(Subject, Topic.subject_id == Subject.id)
               .filter(Question.alternatives.isnot(None)))
        if topic_id:
            q = q.filter(Question.topic_id == topic_id)
        if subject_id:
            q = q.filter(Topic.subject_id == subject_id)
        total = q.count()
        rows = q.order_by(Question.answered_at.desc()).offset(skip).limit(limit).all()
        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "items": [_fmt_question(qs, db, topic=t, subject=s) for qs, t, s in rows],
        }


@app.post("/api/questions/bank")
def question_bank_create(body: QuestionBankCreate):
    import json as _json
    with get_session() as db:
        q = Question(
            topic_id=body.topic_id,
            statement=body.statement,
            alternatives=_json.dumps(body.alternatives, ensure_ascii=False),
            correct_alt=body.correct_alt.upper(),
            correct=False,
            explanation=body.explanation,
            source=body.source, year=body.year,
            difficulty=body.difficulty,
        )
        db.add(q); db.commit(); db.refresh(q)
        return {"id": q.id}


@app.post("/api/questions/{question_id}/answer")
def question_answer(question_id: int, body: dict):
    chosen = (body.get("chosen_alt") or "").upper()
    with get_session() as db:
        q = db.get(Question, question_id)
        if not q:
            raise HTTPException(404)
        q.chosen_alt = chosen
        q.correct = (chosen == (q.correct_alt or "").upper())
        q.answered_at = datetime.now(timezone.utc).replace(tzinfo=None)
        if body.get("notes"):
            q.notes = body["notes"]
        db.commit()
        return {"correct": q.correct, "correct_alt": q.correct_alt, "explanation": q.explanation}


@app.get("/api/questions/bank/stats")
def question_bank_stats():
    with get_session() as db:
        total = db.query(func.count(Question.id)).filter(Question.alternatives.isnot(None)).scalar() or 0
        answered = db.query(func.count(Question.id)).filter(
            Question.alternatives.isnot(None), Question.chosen_alt.isnot(None)
        ).scalar() or 0
        correct = db.query(func.count(Question.id)).filter(
            Question.alternatives.isnot(None), Question.correct == True
        ).scalar() or 0
        return {
            "total": total, "answered": answered, "correct": correct,
            "accuracy": round(correct / answered * 100, 1) if answered else 0,
        }


@app.post("/api/questions/bank/import-csv")
async def question_bank_import_csv(file: UploadFile = File(...)):
    """
    CSV columns (header required):
    topic_name, subject_name, statement, alt_a, alt_b, alt_c, alt_d, alt_e,
    correct_alt, explanation, source, year, difficulty
    alt_c/d/e optional. correct_alt = A|B|C|D|E
    """
    import csv, io, json as _json
    content = await file.read()
    text = content.decode("utf-8-sig")  # handle BOM
    reader = csv.DictReader(io.StringIO(text))

    # normalize headers
    def _hdr(r): return {k.strip().lower(): (v.strip() if v else "") for k, v in r.items()}

    imported = 0
    skipped = 0
    errors = []

    with get_session() as db:
        # build topic map (name -> id) for fast lookup
        all_topic_rows = db.query(Topic, Subject).join(Subject, Topic.subject_id == Subject.id).all()
        topic_map: dict[str, int] = {t.name.lower().strip(): t.id for t, _ in all_topic_rows}

        # subject -> first topic fallback (single query, no N+1)
        subj_map: dict[str, int] = {}
        for t, s in all_topic_rows:
            if s.name.lower().strip() not in subj_map:
                subj_map[s.name.lower().strip()] = t.id

        for i, row in enumerate(reader, 1):
            r = _hdr(row)
            stmt = r.get("statement", "")
            if not stmt:
                skipped += 1; continue

            # resolve topic
            topic_name = r.get("topic_name", "").lower().strip()
            subject_name = r.get("subject_name", "").lower().strip()
            topic_id = topic_map.get(topic_name) or subj_map.get(subject_name)
            if not topic_id:
                errors.append(f"Row {i}: topic '{r.get('topic_name')}' not found — skipped")
                skipped += 1; continue

            # build alternatives list (A-E, skip empty)
            alts = [r.get(f"alt_{c}", "") for c in ("a","b","c","d","e")]
            alts = [a for a in alts if a]
            if len(alts) < 2:
                errors.append(f"Row {i}: need ≥2 alternatives — skipped")
                skipped += 1; continue

            correct_alt = (r.get("correct_alt") or "").strip().upper()
            if correct_alt not in list("ABCDE")[:len(alts)]:
                errors.append(f"Row {i}: correct_alt '{correct_alt}' invalid — skipped")
                skipped += 1; continue

            year_raw = r.get("year", "")
            try: year = int(year_raw) if year_raw else None
            except ValueError: year = None

            diff = r.get("difficulty", "medio").lower()
            if diff not in ("facil","medio","dificil"): diff = "medio"

            q = Question(
                topic_id=topic_id,
                statement=stmt,
                alternatives=_json.dumps(alts, ensure_ascii=False),
                correct_alt=correct_alt,
                correct=False,
                explanation=r.get("explanation") or None,
                source=r.get("source") or None,
                year=year,
                difficulty=diff,
            )
            db.add(q)
            imported += 1

        db.commit()

    return {"imported": imported, "skipped": skipped, "errors": errors}


@app.get("/api/questions/bank/wrong")
def question_bank_wrong(skip: int = 0, limit: int = 50):
    """Questions answered at least once and got wrong — for error review."""
    with get_session() as db:
        q = (
            db.query(Question, Topic, Subject)
            .join(Topic, Question.topic_id == Topic.id)
            .join(Subject, Topic.subject_id == Subject.id)
            .filter(Question.alternatives.isnot(None), Question.correct == False,
                    Question.chosen_alt.isnot(None))
            .order_by(Question.answered_at.desc())
        )
        total = q.count()
        rows = q.offset(skip).limit(limit).all()
        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "items": [_fmt_question(qs, db, topic=t, subject=s) for qs, t, s in rows],
        }
