"""
Medical Study Agent Hub — CLI

Commands:
  init                  initialise the database
  subject add           add a subject
  topic add             add a topic
  topic list            list all topics
  question add          record a question result
  session add           record a study session
  import csv            import mock-exam results from CSV/JSON
  anki sync             sync Anki stats via AnkiConnect
  anki decks            list Anki decks (requires Anki open)
  plan generate         generate today's daily study plan
  plan show             show the latest plan
  report weak           show weak topics report
"""
from __future__ import annotations
import sys
from datetime import datetime, date

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from medstudies.persistence.database import get_session, init_db
from medstudies.persistence.models import DailyPlan, Question, StudySession, Subject, Tag, Topic
from medstudies.ingestion.anki_adapter import AnkiAdapter
from medstudies.ingestion.csv_adapter import CSVAdapter
from medstudies.ingestion.medcof_adapter import MedcofAdapter
from medstudies.ingestion.mock_exam_adapter import MockExamAdapter
from medstudies.engine.planner import DailyPlanBuilder, DailyStudyPlan
from medstudies.engine.scorer import TopicScorer
from medstudies.engine.sm2 import SM2Engine
from medstudies.engine.weekly_planner import WeeklyPlanBuilder
from medstudies.notifications.notifier import Notifier

app = typer.Typer(name="medstudies", help="Medical Study Agent Hub")
subject_app = typer.Typer(help="Manage subjects")
topic_app = typer.Typer(help="Manage topics")
question_app = typer.Typer(help="Record question results")
session_app = typer.Typer(help="Manage study sessions")
anki_app = typer.Typer(help="Anki integration")
plan_app = typer.Typer(help="Daily study plan")
report_app = typer.Typer(help="Reports")

app.add_typer(subject_app, name="subject")
app.add_typer(topic_app, name="topic")
app.add_typer(question_app, name="question")
app.add_typer(session_app, name="session")
app.add_typer(anki_app, name="anki")
medcof_app = typer.Typer(help="Integração Medcof")
app.add_typer(medcof_app, name="medcof")
app.add_typer(plan_app, name="plan")
mock_app = typer.Typer(help="Modo simulado — registrar bloco de questões")
app.add_typer(mock_app, name="mock")
app.add_typer(report_app, name="report")
notify_app = typer.Typer(help="Notificações — email e Telegram")
app.add_typer(notify_app, name="notify")
tag_app = typer.Typer(help="Tags para filtrar tópicos")
app.add_typer(tag_app, name="tag")
fc_app = typer.Typer(help="Flashcards com repetição espaçada")
app.add_typer(fc_app, name="flashcard")

console = Console()


# ── Init ──────────────────────────────────────────────────────────────────────

@app.command()
def init(
    edital: str = typer.Option("sp_completo", "--edital", "-e",
        help="Template de pesos: sp_completo | sus_sp | hcfmusp | unifesp | famerp | enare | ..."),
):
    """Inicializa banco de dados e configura pesos do edital."""
    import os, requests as _req
    from pathlib import Path

    # 1. Criar pastas
    Path("data/exports").mkdir(parents=True, exist_ok=True)
    Path("data/imports").mkdir(parents=True, exist_ok=True)

    # 2. Inicializar DB
    init_db()
    rprint("[green]✓ Banco de dados inicializado.[/green]")

    # 3. Aplicar template de edital
    db = get_session()
    try:
        from medstudies.interface.api import EDITAL_TEMPLATES
        if edital not in EDITAL_TEMPLATES:
            rprint(f"[yellow]Edital '{edital}' não encontrado. Usando sp_completo.[/yellow]")
            edital = "sp_completo"
        tpl = EDITAL_TEMPLATES[edital]
        for sd in tpl["subjects"]:
            existing = db.query(Subject).filter_by(name=sd["name"]).first()
            if existing:
                existing.exam_weight = sd["exam_weight"]
            else:
                db.add(Subject(name=sd["name"], exam_weight=sd["exam_weight"]))
        db.commit()
        rprint(f"[green]✓ Edital '{tpl['name']}' aplicado ({len(tpl['subjects'])} matérias).[/green]")
    except Exception as e:
        rprint(f"[yellow]Aviso: não foi possível aplicar edital: {e}[/yellow]")

    # 4. Instruções próximos passos
    console.print("\n[bold]Próximos passos:[/bold]")
    console.print("  1. Importe seu simulado:  [cyan]medstudies mock run simulado.csv --source \"Medcof 1\"[/cyan]")
    console.print("  2. Gere seu plano:        [cyan]medstudies plan generate[/cyan]")
    console.print("  3. Abra o dashboard:      [cyan]uvicorn medstudies.interface.api:app --reload[/cyan]")
    console.print("     → http://localhost:8000\n")


# ── Subject ───────────────────────────────────────────────────────────────────

@subject_app.command("add")
def subject_add(
    name: str = typer.Argument(..., help="Subject name, e.g. 'Cardiology'"),
    weight: float = typer.Option(1.0, "--weight", "-w", help="Exam importance weight (1.0 = normal)"),
):
    """Add a subject."""
    db = get_session()
    existing = db.query(Subject).filter_by(name=name).first()
    if existing:
        rprint(f"[yellow]Subject '{name}' already exists.[/yellow]")
        return
    subj = Subject(name=name, exam_weight=weight)
    db.add(subj)
    db.commit()
    rprint(f"[green]✓ Subject '{name}' added (weight={weight}).[/green]")


# ── Topic ─────────────────────────────────────────────────────────────────────

@topic_app.command("add")
def topic_add(
    name: str = typer.Argument(..., help="Topic name"),
    subject: str = typer.Option(..., "--subject", "-s", help="Subject name"),
    anki_deck: str = typer.Option(None, "--anki-deck", help="Linked Anki deck name"),
    anki_tags: str = typer.Option(None, "--anki-tags", help="Comma-separated Anki tags"),
    notability: str = typer.Option(None, "--notability", help="Notability notebook path, ex: 'Cardio/IC'"),
    parent: str = typer.Option(None, "--parent", help="Parent topic name (for sub-topics)"),
):
    """Add a topic under a subject."""
    db = get_session()
    subj = db.query(Subject).filter_by(name=subject).first()
    if not subj:
        rprint(f"[red]Subject '{subject}' not found. Run: medstudies subject add '{subject}'[/red]")
        raise typer.Exit(1)

    parent_topic = None
    if parent:
        parent_topic = db.query(Topic).filter_by(name=parent, subject_id=subj.id).first()
        if not parent_topic:
            rprint(f"[red]Parent topic '{parent}' not found under '{subject}'.[/red]")
            raise typer.Exit(1)

    existing = db.query(Topic).filter_by(name=name, subject_id=subj.id).first()
    if existing:
        rprint(f"[yellow]Topic '{name}' already exists under '{subject}'.[/yellow]")
        return

    t = Topic(
        name=name,
        subject_id=subj.id,
        parent_id=parent_topic.id if parent_topic else None,
        anki_deck=anki_deck,
        anki_tags=anki_tags,
        notability_notebook=notability,
    )
    db.add(t)
    db.commit()
    rprint(f"[green]✓ Topic '{name}' added under '{subject}'.[/green]")


@topic_app.command("edit")
def topic_edit(
    name: str = typer.Argument(..., help="Nome atual do tópico"),
    subject: str = typer.Option(..., "--subject", "-s"),
    anki_deck: str = typer.Option(None, "--anki-deck"),
    notability: str = typer.Option(None, "--notability"),
    notes: str = typer.Option(None, "--notes"),
    new_name: str = typer.Option(None, "--rename"),
):
    """Edita campos de um tópico existente."""
    db = get_session()
    subj = db.query(Subject).filter_by(name=subject).first()
    if not subj:
        rprint(f"[red]Matéria '{subject}' não encontrada.[/red]")
        raise typer.Exit(1)
    t = db.query(Topic).filter_by(name=name, subject_id=subj.id).first()
    if not t:
        rprint(f"[red]Tópico '{name}' não encontrado.[/red]")
        raise typer.Exit(1)
    if anki_deck is not None:
        t.anki_deck = anki_deck
    if notability is not None:
        t.notability_notebook = notability
    if notes is not None:
        t.study_notes = notes
    if new_name is not None:
        t.name = new_name
    db.commit()
    rprint(f"[green]✓ Tópico '{name}' atualizado.[/green]")


@topic_app.command("list")
def topic_list(subject: str = typer.Option(None, "--subject", "-s")):
    """List all topics."""
    db = get_session()
    q = db.query(Topic)
    if subject:
        subj = db.query(Subject).filter_by(name=subject).first()
        if not subj:
            rprint(f"[red]Subject '{subject}' not found.[/red]")
            raise typer.Exit(1)
        q = q.filter_by(subject_id=subj.id)

    topics = q.all()
    table = Table(title="Topics", show_lines=True)
    table.add_column("ID", style="dim", width=4, no_wrap=True)
    table.add_column("Subject", min_width=16, no_wrap=True)
    table.add_column("Topic", min_width=24, no_wrap=True)
    table.add_column("Anki Deck", min_width=20, no_wrap=True)
    table.add_column("Notability", no_wrap=True)
    for t in topics:
        table.add_row(
            str(t.id), t.subject.name, t.name,
            t.anki_deck or "—",
            t.notability_notebook or "—",
        )
    console.print(table)


# ── Question ──────────────────────────────────────────────────────────────────

@question_app.command("add")
def question_add(
    topic: str = typer.Argument(..., help="Topic name"),
    subject: str = typer.Option(..., "--subject", "-s"),
    correct: bool = typer.Option(..., "--correct/--wrong", help="Was the answer correct?"),
    source: str = typer.Option("manual", "--source"),
    notes: str = typer.Option("", "--notes"),
):
    """Record a question result."""
    db = get_session()
    subj = db.query(Subject).filter_by(name=subject).first()
    if not subj:
        rprint(f"[red]Subject '{subject}' not found.[/red]")
        raise typer.Exit(1)
    t = db.query(Topic).filter_by(name=topic, subject_id=subj.id).first()
    if not t:
        rprint(f"[red]Topic '{topic}' not found.[/red]")
        raise typer.Exit(1)

    q = Question(topic_id=t.id, correct=correct, source=source, notes=notes)
    db.add(q)
    db.commit()
    label = "[green]correct[/green]" if correct else "[red]wrong[/red]"
    rprint(f"✓ Question recorded as {label} for '{topic}'.")


# ── Question list ─────────────────────────────────────────────────────────────

@question_app.command("list")
def question_list(
    subject: str = typer.Option(None, "--subject", "-s"),
    topic: str = typer.Option(None, "--topic", "-t"),
    wrong_only: bool = typer.Option(False, "--wrong", help="Só erradas"),
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """Lista questões respondidas recentes."""
    db = get_session()
    q = db.query(Question).join(Topic).join(Subject)
    if subject:
        q = q.filter(Subject.name == subject)
    if topic:
        q = q.filter(Topic.name == topic)
    if wrong_only:
        q = q.filter(Question.correct == False)
    questions = q.order_by(Question.answered_at.desc()).limit(limit).all()
    if not questions:
        rprint("[yellow]Nenhuma questão encontrada.[/yellow]")
        return
    table = Table(title=f"Questões (últimas {limit})", show_lines=True)
    table.add_column("Data", min_width=12, no_wrap=True)
    table.add_column("Resultado", no_wrap=True)
    table.add_column("Matéria › Tópico", min_width=30, no_wrap=True)
    table.add_column("Fonte", no_wrap=True)
    table.add_column("Notas", max_width=35)
    for qq in questions:
        result = "[green]✓ Certo[/green]" if qq.correct else "[red]✗ Errado[/red]"
        table.add_row(
            qq.answered_at.strftime("%Y-%m-%d"),
            result,
            f"{qq.topic.subject.name} › {qq.topic.name}",
            qq.source or "—",
            qq.notes or "—",
        )
    console.print(table)


# ── Session ───────────────────────────────────────────────────────────────────

@session_app.command("add")
def session_add(
    topic: str = typer.Argument(..., help="Topic name"),
    subject: str = typer.Option(..., "--subject", "-s"),
    session_type: str = typer.Option("review", "--type", help="review | practice | lecture"),
    duration: int = typer.Option(None, "--duration", help="Duration in minutes"),
    notes: str = typer.Option("", "--notes"),
):
    """Record a study session."""
    db = get_session()
    subj = db.query(Subject).filter_by(name=subject).first()
    if not subj:
        rprint(f"[red]Subject '{subject}' not found.[/red]")
        raise typer.Exit(1)
    t = db.query(Topic).filter_by(name=topic, subject_id=subj.id).first()
    if not t:
        rprint(f"[red]Topic '{topic}' not found.[/red]")
        raise typer.Exit(1)

    s = StudySession(
        topic_id=t.id,
        session_type=session_type,
        duration_minutes=duration,
        notes=notes,
    )
    db.add(s)
    db.commit()
    rprint(f"[green]✓ Session recorded for '{topic}' ({session_type}, {duration or '?'}min).[/green]")


@session_app.command("list")
def session_list(
    subject: str = typer.Option(None, "--subject", "-s"),
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """Lista sessões de estudo recentes."""
    db = get_session()
    q = db.query(StudySession).join(Topic).join(Subject)
    if subject:
        q = q.filter(Subject.name == subject)
    sessions = q.order_by(StudySession.started_at.desc()).limit(limit).all()
    if not sessions:
        rprint("[yellow]Nenhuma sessão registrada.[/yellow]")
        return
    table = Table(title=f"Sessões (últimas {limit})", show_lines=True)
    table.add_column("Data", min_width=12, no_wrap=True)
    table.add_column("Matéria › Tópico", min_width=30, no_wrap=True)
    table.add_column("Tipo", no_wrap=True)
    table.add_column("Duração", justify="right", no_wrap=True)
    table.add_column("Notas", max_width=40)
    for s in sessions:
        table.add_row(
            s.started_at.strftime("%Y-%m-%d %H:%M"),
            f"{s.topic.subject.name} › {s.topic.name}",
            s.session_type,
            f"{s.duration_minutes}min" if s.duration_minutes else "—",
            s.notes or "—",
        )
    console.print(table)


# ── CSV Import ────────────────────────────────────────────────────────────────

@app.command("import")
def import_csv(
    file: str = typer.Argument(..., help="Path to CSV or JSON file"),
):
    """Import mock-exam results from CSV/JSON."""
    db = get_session()
    adapter = CSVAdapter(db)
    result = adapter.ingest(file_path=file)
    if result.ok:
        rprint(f"[green]✓ Imported {result.records_created} records from {file}.[/green]")
    else:
        rprint(f"[yellow]Imported with {len(result.errors)} error(s):[/yellow]")
        for e in result.errors:
            rprint(f"  [red]• {e}[/red]")


# ── Anki ──────────────────────────────────────────────────────────────────────

@anki_app.command("sync")
def anki_sync():
    """Sync Anki card stats for all topics with anki_deck set."""
    db = get_session()
    adapter = AnkiAdapter(db)
    with console.status("Syncing with AnkiConnect…"):
        result = adapter.ingest()
    if result.ok:
        rprint(f"[green]✓ Synced {result.records_created} deck snapshot(s).[/green]")
    else:
        rprint(f"[yellow]Sync completed with {len(result.errors)} error(s):[/yellow]")
        for e in result.errors:
            rprint(f"  [red]• {e}[/red]")


@anki_app.command("decks")
def anki_decks():
    """List all decks in your Anki collection."""
    from medstudies.ingestion.anki_adapter import _anki_request
    try:
        decks = _anki_request("deckNames")
        rprint("[bold]Anki Decks:[/bold]")
        for d in sorted(decks):
            rprint(f"  • {d}")
    except Exception as e:
        rprint(f"[red]Cannot connect to AnkiConnect: {e}[/red]")
        rprint("[dim]Make sure Anki is open and AnkiConnect add-on is installed.[/dim]")


# ── Plan ──────────────────────────────────────────────────────────────────────

@plan_app.command("generate")
def plan_generate(
    max_topics: int = typer.Option(8, "--max", "-n", help="Max topics in the plan"),
    tag: str = typer.Option(None, "--tag", "-t", help="Filtrar por tag (ex: 'prova-revalida')"),
):
    """Generate today's daily study plan."""
    db = get_session()
    tag_filter: set[int] | None = None
    if tag:
        tag_obj = db.query(Tag).filter_by(name=tag).first()
        if not tag_obj:
            rprint(f"[red]Tag '{tag}' não encontrada.[/red]")
            raise typer.Exit(1)
        tag_filter = {t.id for t in tag_obj.topics}
        rprint(f"[dim]Filtrando por tag '{tag}' — {len(tag_filter)} tópico(s).[/dim]")
    with console.status("Scoring topics and building plan…"):
        SM2Engine(db).update_all()
        plan = DailyPlanBuilder(db, max_topics=max_topics, topic_filter=tag_filter).build()
    _print_plan(plan)


@plan_app.command("show")
def plan_show():
    """Show the most recently generated plan."""
    db = get_session()
    record = (
        db.query(DailyPlan)
        .order_by(DailyPlan.generated_at.desc())
        .first()
    )
    if not record:
        rprint("[yellow]No plan found. Run: medstudies plan generate[/yellow]")
        return
    plan = DailyStudyPlan.from_json(record.plan_json)
    _print_plan(plan)


def _print_plan(plan: DailyStudyPlan) -> None:
    action_color = {"REVIEW": "cyan", "PRACTICE": "yellow", "REINFORCE": "magenta"}
    action_icon = {"REVIEW": "📖", "PRACTICE": "✏️ ", "REINFORCE": "🔁"}

    console.print(
        Panel(
            f"[bold]Daily Study Plan — {plan.plan_date}[/bold]\n"
            f"[dim]Generated {plan.generated_at}[/dim]",
            style="blue",
        )
    )

    table = Table(show_lines=True)
    table.add_column("#", style="dim", width=3, no_wrap=True)
    table.add_column("Action", min_width=14, no_wrap=True)
    table.add_column("Subject / Topic", min_width=30, no_wrap=True)
    table.add_column("Score", justify="right", min_width=7, no_wrap=True)
    table.add_column("Error%", justify="right", min_width=7, no_wrap=True)
    table.add_column("Days stale", justify="right", min_width=10, no_wrap=True)
    table.add_column("Anki due", justify="right", min_width=9, no_wrap=True)
    table.add_column("Why", max_width=50)

    for item in plan.items:
        color = action_color[item.action]
        icon = action_icon[item.action]
        table.add_row(
            str(item.rank),
            f"[{color}]{icon} {item.action}[/{color}]",
            f"[bold]{item.subject_name}[/bold] › {item.topic_name}",
            f"{item.priority_score:.3f}",
            f"{item.error_rate_pct:.0f}%",
            f"{item.days_since_review:.0f}d",
            str(item.anki_due) if item.anki_due else "—",
            f"[dim]{item.reason}[/dim]",
        )

    console.print(table)


# ── Reports ───────────────────────────────────────────────────────────────────

@report_app.command("weak")
def report_weak(
    top: int = typer.Option(10, "--top", "-n"),
    min_questions: int = typer.Option(3, "--min-questions", help="Minimum questions answered"),
):
    """Show weak topics ranked by error rate (minimum questions threshold)."""
    db = get_session()
    scorer = TopicScorer(db)
    scores = scorer.score_all()

    filtered = [s for s in scores if s.total_questions >= min_questions]
    by_error = sorted(filtered, key=lambda s: s.error_rate, reverse=True)[:top]

    table = Table(title=f"Weak Topics (min {min_questions} questions)", show_lines=True)
    table.add_column("Rank", style="dim", width=5, no_wrap=True)
    table.add_column("Subject", min_width=16, no_wrap=True)
    table.add_column("Topic", min_width=24, no_wrap=True)
    table.add_column("Error rate", justify="right", min_width=10, no_wrap=True)
    table.add_column("Wrong / Total", justify="right", min_width=13, no_wrap=True)
    table.add_column("Priority score", justify="right", min_width=14, no_wrap=True)

    for rank, s in enumerate(by_error, 1):
        color = "red" if s.error_rate > 0.6 else "yellow" if s.error_rate > 0.4 else "white"
        table.add_row(
            str(rank),
            s.subject_name,
            s.topic_name,
            f"[{color}]{s.error_rate:.0%}[/{color}]",
            f"{s.wrong_questions}/{s.total_questions}",
            f"{s.priority_score:.3f}",
        )

    console.print(table)


# ── Medcof ───────────────────────────────────────────────────────────────────

@medcof_app.command("sync")
def medcof_sync(
    email: str = typer.Option(None, "--email", "-e", help="Email Medcof (ou MEDCOF_EMAIL)"),
    password: str = typer.Option(None, "--password", "-p", help="Senha Medcof (ou MEDCOF_PASSWORD)"),
    since: str = typer.Option(None, "--since", help="Importar só simulados após esta data (YYYY-MM-DD)"),
    playwright: bool = typer.Option(False, "--playwright", help="Usar Playwright (SPA JS rendering)"),
):
    """Sincroniza resultados de simulados da conta Medcof."""
    import os
    from medstudies.ingestion.medcof_adapter import MedcofConfig

    since_dt = None
    if since:
        try:
            from datetime import datetime as _dt
            since_dt = _dt.fromisoformat(since)
        except ValueError:
            rprint(f"[red]Data inválida: {since}. Use YYYY-MM-DD.[/red]")
            raise typer.Exit(1)

    db = get_session()
    cfg = MedcofConfig(
        email=email or os.getenv("MEDCOF_EMAIL", ""),
        password=password or os.getenv("MEDCOF_PASSWORD", ""),
        use_playwright=playwright or bool(os.getenv("MEDCOF_USE_PLAYWRIGHT")),
    )
    if not cfg.email or not cfg.password:
        rprint("[red]Credenciais não configuradas.[/red]")
        rprint("  Defina MEDCOF_EMAIL e MEDCOF_PASSWORD, ou use --email / --password.")
        raise typer.Exit(1)

    adapter = MedcofAdapter(db, cfg)
    with console.status("Conectando ao Medcof e sincronizando simulados…"):
        result = adapter.ingest(since=since_dt)

    if result.ok:
        rprint(f"[green]✓ {result.records_created} questões importadas.[/green]")
        if result.metadata.get("simulados_synced"):
            rprint(f"  Simulados sincronizados: {result.metadata['simulados_synced']}")
    else:
        rprint(f"[yellow]Sincronização concluída com {len(result.errors)} erro(s):[/yellow]")
        for e in result.errors:
            rprint(f"  [red]• {e}[/red]")


@medcof_app.command("config")
def medcof_config():
    """Mostra variáveis de ambiente para integração Medcof."""
    import os
    console.print("\n[bold]Configuração Medcof[/bold]\n")
    vars_info = [
        ("MEDCOF_EMAIL",           "E-mail de login na plataforma Medcof"),
        ("MEDCOF_PASSWORD",        "Senha da conta Medcof"),
        ("MEDCOF_USE_PLAYWRIGHT",  "Definir como '1' se o site não responder via API (SPA JS)"),
    ]
    table = Table(show_lines=False, box=None)
    table.add_column("Variável", style="cyan", no_wrap=True)
    table.add_column("Descrição", no_wrap=True)
    table.add_column("Status", justify="right", no_wrap=True)
    for var, desc in vars_info:
        status = "[green]✓ definida[/green]" if os.getenv(var) else "[dim]não definida[/dim]"
        table.add_row(var, desc, status)
    console.print(table)
    console.print()


# ── Reports extra ─────────────────────────────────────────────────────────────

@report_app.command("subject")
def report_subject():
    """Desempenho agregado por matéria."""
    db = get_session()
    scorer = TopicScorer(db)
    scores = scorer.score_all()

    from collections import defaultdict
    by_subject: dict[str, dict] = defaultdict(lambda: {"total": 0, "wrong": 0, "topics": 0, "score_sum": 0.0})
    for s in scores:
        d = by_subject[s.subject_name]
        d["total"] += s.total_questions
        d["wrong"] += s.wrong_questions
        d["topics"] += 1
        d["score_sum"] += s.priority_score

    rows = []
    for subj, d in by_subject.items():
        err = d["wrong"] / d["total"] if d["total"] else 0.0
        avg_score = d["score_sum"] / d["topics"]
        rows.append((subj, d["topics"], d["total"], d["wrong"], err, avg_score))

    rows.sort(key=lambda r: r[4], reverse=True)

    table = Table(title="Desempenho por Matéria", show_lines=True)
    table.add_column("Matéria", min_width=20, no_wrap=True)
    table.add_column("Tópicos", justify="right", no_wrap=True)
    table.add_column("Questões", justify="right", no_wrap=True)
    table.add_column("Erros", justify="right", no_wrap=True)
    table.add_column("Erro%", justify="right", no_wrap=True)
    table.add_column("Score médio", justify="right", no_wrap=True)

    for subj, topics, total, wrong, err, avg_score in rows:
        color = "red" if err > 0.6 else "yellow" if err > 0.4 else "green"
        table.add_row(
            subj,
            str(topics),
            str(total),
            str(wrong),
            f"[{color}]{err:.0%}[/{color}]",
            f"{avg_score:.3f}",
        )
    console.print(table)


@report_app.command("trend")
def report_trend(
    weeks: int = typer.Option(8, "--weeks", "-w", help="Número de semanas para analisar"),
):
    """Evolução do erro% semana a semana."""
    from datetime import timedelta
    from collections import defaultdict

    db = get_session()
    questions = db.query(Question).order_by(Question.answered_at).all()

    if not questions:
        rprint("[yellow]Nenhuma questão registrada.[/yellow]")
        return

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    week_buckets: dict[int, dict] = defaultdict(lambda: {"total": 0, "wrong": 0})

    for q in questions:
        age_days = (now - q.answered_at).total_seconds() / 86400
        week_ago = int(age_days // 7)
        if week_ago < weeks:
            week_buckets[week_ago]["total"] += 1
            if not q.correct:
                week_buckets[week_ago]["wrong"] += 1

    table = Table(title=f"Tendência de Erro — últimas {weeks} semanas", show_lines=True)
    table.add_column("Semana", min_width=14, no_wrap=True)
    table.add_column("Questões", justify="right", no_wrap=True)
    table.add_column("Erros", justify="right", no_wrap=True)
    table.add_column("Erro%", justify="right", no_wrap=True)
    table.add_column("Progresso", no_wrap=True)

    for w in range(weeks - 1, -1, -1):
        d = week_buckets[w]
        if d["total"] == 0:
            continue
        err = d["wrong"] / d["total"]
        label = "esta semana" if w == 0 else f"{w}w atrás"
        color = "red" if err > 0.6 else "yellow" if err > 0.4 else "green"
        bar_filled = int((1 - err) * 20)
        bar = f"[green]{'█' * bar_filled}[/green][red]{'░' * (20 - bar_filled)}[/red]"
        table.add_row(
            label,
            str(d["total"]),
            str(d["wrong"]),
            f"[{color}]{err:.0%}[/{color}]",
            bar,
        )
    console.print(table)


# ── Mock Exam ─────────────────────────────────────────────────────────────────

@mock_app.command("run")
def mock_run(
    file: str = typer.Argument(..., help="CSV/JSON com as questões do simulado"),
    source: str = typer.Option(..., "--source", "-s", help='Nome do simulado, ex: "Medcof Mock 5"'),
    date_str: str = typer.Option(None, "--date", help="Data do simulado (YYYY-MM-DD), padrão hoje"),
):
    """
    Importa um bloco completo de questões de um simulado.

    O CSV deve ter: topic_name, subject_name, correct (true/false), notes (opcional).
    Não precisa de answered_at — usa --date ou hoje.
    """
    import json
    from pathlib import Path

    db = get_session()
    path = Path(file)
    if not path.exists():
        rprint(f"[red]Arquivo não encontrado: {file}[/red]")
        raise typer.Exit(1)

    if path.suffix.lower() == ".json":
        questions = json.loads(path.read_text(encoding="utf-8"))
    else:
        import csv
        with path.open(encoding="utf-8") as f:
            questions = list(csv.DictReader(f))

    answered_at = None
    if date_str:
        try:
            answered_at = datetime.fromisoformat(date_str)
        except ValueError:
            rprint(f"[red]Data inválida: {date_str}. Use YYYY-MM-DD.[/red]")
            raise typer.Exit(1)

    adapter = MockExamAdapter(db)
    with console.status(f"Registrando {len(questions)} questões de '{source}'…"):
        result = adapter.ingest(questions=questions, source=source, answered_at=answered_at)
        SM2Engine(db).update_all()

    total = result.metadata.get("total", 0)
    correct = result.metadata.get("correct", 0)
    wrong = total - correct
    pct = round(wrong / total * 100) if total else 0

    console.print(f"\n[bold]Simulado:[/bold] {source}")
    console.print(f"  Total de questões : {total}")
    console.print(f"  Corretas          : [green]{correct}[/green]")
    console.print(f"  Erradas           : [red]{wrong}[/red] ({pct}% de erro)")

    if result.errors:
        rprint(f"\n[yellow]{len(result.errors)} erro(s):[/yellow]")
        for e in result.errors:
            rprint(f"  [red]• {e}[/red]")
    else:
        rprint(f"\n[green]✓ {result.records_created} questões importadas com sucesso.[/green]")


# ── Plan Week ─────────────────────────────────────────────────────────────────

@plan_app.command("week")
def plan_week(
    topics_per_day: int = typer.Option(4, "--topics", "-n", help="Tópicos por dia"),
    tag: str = typer.Option(None, "--tag", "-t", help="Filtrar por tag"),
):
    """Gera plano semanal distribuído por manhã e tarde."""
    db = get_session()
    tag_filter: set[int] | None = None
    if tag:
        tag_obj = db.query(Tag).filter_by(name=tag).first()
        if not tag_obj:
            rprint(f"[red]Tag '{tag}' não encontrada.[/red]")
            raise typer.Exit(1)
        tag_filter = {t.id for t in tag_obj.topics}
        rprint(f"[dim]Filtrando por tag '{tag}' — {len(tag_filter)} tópico(s).[/dim]")
    builder = WeeklyPlanBuilder(db, topics_per_day=topics_per_day, topic_filter=tag_filter)
    with console.status("Construindo plano semanal…"):
        plan = builder.build()

    action_color = {"REVIEW": "cyan", "PRACTICE": "yellow", "REINFORCE": "magenta"}
    action_icon  = {"REVIEW": "📖", "PRACTICE": "✏️ ", "REINFORCE": "🔁"}

    console.print(f"\n[bold blue]Plano Semanal — {plan.start_date} a {plan.end_date}[/bold blue]\n")

    for day in plan.days:
        console.rule(f"[bold]{day['day_name']} {day['date']}[/bold]")

        for period, label in [("morning", "Manhã"), ("afternoon", "Tarde")]:
            slots = day[period]
            if not slots:
                continue
            table = Table(show_header=True, box=None, padding=(0, 1))
            table.add_column(f"[dim]{label}[/dim]", min_width=8, no_wrap=True)
            table.add_column("Horário", no_wrap=True)
            table.add_column("Ação", no_wrap=True)
            table.add_column("Tópico", min_width=30, no_wrap=True)
            table.add_column("Erro%", justify="right", no_wrap=True)

            for s in slots:
                color = action_color.get(s["action"], "white")
                icon  = action_icon.get(s["action"], "")
                table.add_row(
                    "",
                    f"{s['start_time']}–{s['end_time']}",
                    f"[{color}]{icon} {s['action']}[/{color}]",
                    f"[bold]{s['subject_name']}[/bold] › {s['topic_name']}",
                    f"{s['error_rate_pct']:.0f}%",
                )
            console.print(table)

    console.print()


# ── Notify ────────────────────────────────────────────────────────────────────

@notify_app.command("send")
def notify_send(
    channel: str = typer.Option("all", "--channel", "-c", help="email | telegram | all"),
):
    """Envia o plano de hoje por email e/ou Telegram."""
    db = get_session()
    record = db.query(DailyPlan).order_by(DailyPlan.generated_at.desc()).first()
    if not record:
        rprint("[yellow]Nenhum plano encontrado. Execute: medstudies plan generate[/yellow]")
        raise typer.Exit(1)

    import json as _json
    plan_dict = _json.loads(record.plan_json)

    notifier = Notifier()
    with console.status("Enviando notificação…"):
        result = notifier.send_daily_plan(plan_dict)

    if result.email_sent:
        rprint("[green]✓ Email enviado.[/green]")
    if result.telegram_sent:
        rprint("[green]✓ Telegram enviado.[/green]")
    if result.errors:
        for e in result.errors:
            rprint(f"[red]✗ {e}[/red]")


@notify_app.command("config")
def notify_config():
    """Mostra variáveis de ambiente necessárias para notificações."""
    console.print("\n[bold]Configuração de Notificações[/bold]\n")
    vars_info = [
        ("MEDSTUDIES_EMAIL_FROM",     "Remetente Gmail, ex: seuemail@gmail.com"),
        ("MEDSTUDIES_EMAIL_TO",       "Destinatário (padrão = FROM)"),
        ("MEDSTUDIES_EMAIL_PASSWORD", "Senha de app Gmail (Segurança → Senhas de app)"),
        ("MEDSTUDIES_SMTP_HOST",      "Padrão: smtp.gmail.com"),
        ("MEDSTUDIES_SMTP_PORT",      "Padrão: 587"),
        ("MEDSTUDIES_TG_TOKEN",       "Token do bot Telegram (@BotFather)"),
        ("MEDSTUDIES_TG_CHAT_ID",     "Chat ID do usuário ou grupo"),
    ]
    import os
    table = Table(show_lines=False, box=None)
    table.add_column("Variável", style="cyan", no_wrap=True)
    table.add_column("Descrição", no_wrap=True)
    table.add_column("Status", justify="right", no_wrap=True)
    for var, desc in vars_info:
        set_status = "[green]✓ definida[/green]" if os.getenv(var) else "[dim]não definida[/dim]"
        table.add_row(var, desc, set_status)
    console.print(table)
    console.print()


# ── Tag ───────────────────────────────────────────────────────────────────────

@tag_app.command("add")
def tag_add(
    name: str = typer.Argument(..., help="Nome da tag, ex: 'prova-revalida'"),
    color: str = typer.Option("#2979E0", "--color", help="Cor hex, ex: '#FF5733'"),
):
    """Cria uma nova tag."""
    db = get_session()
    existing = db.query(Tag).filter_by(name=name).first()
    if existing:
        rprint(f"[yellow]Tag '{name}' já existe.[/yellow]")
        return
    db.add(Tag(name=name, color=color))
    db.commit()
    rprint(f"[green]✓ Tag '{name}' criada.[/green]")


@tag_app.command("list")
def tag_list():
    """Lista todas as tags."""
    db = get_session()
    tags = db.query(Tag).order_by(Tag.name).all()
    if not tags:
        rprint("[yellow]Nenhuma tag cadastrada.[/yellow]")
        return
    table = Table(show_lines=True)
    table.add_column("ID", width=4, no_wrap=True)
    table.add_column("Nome", min_width=20, no_wrap=True)
    table.add_column("Cor", no_wrap=True)
    table.add_column("Tópicos", justify="right", no_wrap=True)
    for t in tags:
        table.add_row(str(t.id), t.name, t.color, str(len(t.topics)))
    console.print(table)


@tag_app.command("attach")
def tag_attach(
    tag: str = typer.Argument(..., help="Nome da tag"),
    topic: str = typer.Option(..., "--topic", "-t"),
    subject: str = typer.Option(..., "--subject", "-s"),
):
    """Associa uma tag a um tópico."""
    db = get_session()
    tag_obj = db.query(Tag).filter_by(name=tag).first()
    if not tag_obj:
        rprint(f"[red]Tag '{tag}' não encontrada. Crie com: medstudies tag add '{tag}'[/red]")
        raise typer.Exit(1)
    subj = db.query(Subject).filter_by(name=subject).first()
    if not subj:
        rprint(f"[red]Matéria '{subject}' não encontrada.[/red]")
        raise typer.Exit(1)
    t = db.query(Topic).filter_by(name=topic, subject_id=subj.id).first()
    if not t:
        rprint(f"[red]Tópico '{topic}' não encontrado.[/red]")
        raise typer.Exit(1)
    if tag_obj in t.tags:
        rprint(f"[yellow]Tópico '{topic}' já tem tag '{tag}'.[/yellow]")
        return
    t.tags.append(tag_obj)
    db.commit()
    rprint(f"[green]✓ Tag '{tag}' associada a '{topic}'.[/green]")


@tag_app.command("detach")
def tag_detach(
    tag: str = typer.Argument(..., help="Nome da tag"),
    topic: str = typer.Option(..., "--topic", "-t"),
    subject: str = typer.Option(..., "--subject", "-s"),
):
    """Remove associação de tag com tópico."""
    db = get_session()
    tag_obj = db.query(Tag).filter_by(name=tag).first()
    if not tag_obj:
        rprint(f"[red]Tag '{tag}' não encontrada.[/red]")
        raise typer.Exit(1)
    subj = db.query(Subject).filter_by(name=subject).first()
    t = db.query(Topic).filter_by(name=topic, subject_id=subj.id).first() if subj else None
    if not t:
        rprint(f"[red]Tópico não encontrado.[/red]")
        raise typer.Exit(1)
    if tag_obj not in t.tags:
        rprint(f"[yellow]Tópico '{topic}' não tem tag '{tag}'.[/yellow]")
        return
    t.tags.remove(tag_obj)
    db.commit()
    rprint(f"[green]✓ Tag '{tag}' removida de '{topic}'.[/green]")


@tag_app.command("topics")
def tag_topics(
    tag: str = typer.Argument(..., help="Nome da tag"),
):
    """Lista todos os tópicos com uma tag."""
    db = get_session()
    tag_obj = db.query(Tag).filter_by(name=tag).first()
    if not tag_obj:
        rprint(f"[red]Tag '{tag}' não encontrada.[/red]")
        raise typer.Exit(1)
    topics = tag_obj.topics
    if not topics:
        rprint(f"[yellow]Nenhum tópico com tag '{tag}'.[/yellow]")
        return
    table = Table(title=f"Tópicos com tag '{tag}'", show_lines=True)
    table.add_column("Matéria", min_width=16, no_wrap=True)
    table.add_column("Tópico", min_width=24, no_wrap=True)
    for t in topics:
        table.add_row(t.subject.name, t.name)
    console.print(table)


# ── Flashcard ─────────────────────────────────────────────────────────────────

@fc_app.command("add")
def fc_add(
    topic: str = typer.Argument(..., help="Nome do tópico"),
    subject: str = typer.Option(..., "--subject", "-s"),
    question: str = typer.Option(..., "--question", "-q", help="Pergunta"),
    answer: str = typer.Option(..., "--answer", "-a", help="Resposta"),
):
    """Adiciona um flashcard a um tópico."""
    from medstudies.persistence.models import FlashCard
    db = get_session()
    subj = db.query(Subject).filter_by(name=subject).first()
    if not subj:
        rprint(f"[red]Matéria '{subject}' não encontrada.[/red]")
        raise typer.Exit(1)
    t = db.query(Topic).filter_by(name=topic, subject_id=subj.id).first()
    if not t:
        rprint(f"[red]Tópico '{topic}' não encontrado.[/red]")
        raise typer.Exit(1)
    fc = FlashCard(topic_id=t.id, question=question, answer=answer)
    db.add(fc)
    db.commit()
    rprint(f"[green]✓ Flashcard adicionado a '{topic}'.[/green]")


@fc_app.command("list")
def fc_list(
    topic: str = typer.Option(None, "--topic", "-t"),
    subject: str = typer.Option(None, "--subject", "-s"),
):
    """Lista flashcards, opcionalmente filtrados por tópico."""
    from medstudies.persistence.models import FlashCard
    db = get_session()
    q = db.query(FlashCard).join(Topic).join(Subject)
    if subject:
        q = q.filter(Subject.name == subject)
    if topic:
        q = q.filter(Topic.name == topic)
    cards = q.all()

    if not cards:
        rprint("[yellow]Nenhum flashcard encontrado.[/yellow]")
        return

    table = Table(title=f"Flashcards ({len(cards)})", show_lines=True)
    table.add_column("ID", width=4, no_wrap=True)
    table.add_column("Tópico", min_width=20, no_wrap=True)
    table.add_column("Pergunta", min_width=30)
    table.add_column("Próx. revisão", no_wrap=True)
    table.add_column("EF", justify="right", no_wrap=True)

    for fc in cards:
        next_r = fc.next_review.strftime("%Y-%m-%d") if fc.next_review else "—"
        table.add_row(
            str(fc.id),
            f"{fc.topic.subject.name} › {fc.topic.name}",
            fc.question,
            next_r,
            f"{fc.ease_factor:.2f}",
        )
    console.print(table)


@fc_app.command("due")
def fc_due():
    """Lista flashcards com revisão vencida (para estudar agora)."""
    from medstudies.persistence.models import FlashCard
    db = get_session()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cards = (
        db.query(FlashCard)
        .filter((FlashCard.next_review == None) | (FlashCard.next_review <= now))
        .join(Topic).join(Subject)
        .all()
    )

    if not cards:
        rprint("[green]Nenhum flashcard vencido. Ótimo![/green]")
        return

    rprint(f"\n[bold]{len(cards)} flashcard(s) para revisar:[/bold]\n")

    for fc in cards:
        console.rule(f"#{fc.id} — {fc.topic.subject.name} › {fc.topic.name}")
        console.print(f"[bold cyan]P:[/bold cyan] {fc.question}\n")
        input("  [Enter para ver resposta]")
        console.print(f"[bold green]R:[/bold green] {fc.answer}\n")

        grade = typer.prompt(
            "  Qualidade (0=errei/5=fácil)",
            default=3,
        )
        grade = max(0, min(5, int(grade)))

        from medstudies.engine.sm2 import _sm2_step
        new_ef, new_interval, new_reps = _sm2_step(
            fc.ease_factor, fc.interval_days, fc.repetitions, grade
        )
        fc.ease_factor = new_ef
        fc.interval_days = new_interval
        fc.repetitions = new_reps
        fc.times_reviewed += 1
        fc.last_reviewed = now
        fc.next_review = now + timedelta(days=new_interval)

    db.commit()
    rprint(f"\n[green]✓ {len(cards)} flashcard(s) revisado(s).[/green]")


# ── Chat Agent ────────────────────────────────────────────────────────────────

@app.command("chat")
def chat_cmd(
    model: str = typer.Option(None, "--model", "-m", help="Modelo Claude (padrão: claude-haiku-4-5)"),
    api_key: str = typer.Option(None, "--api-key", envvar="ANTHROPIC_API_KEY", help="Anthropic API key"),
):
    """Inicia sessão de chat com o agente de estudos MedBot."""
    import os
    from medstudies.agent.chat import MedStudiesAgent, MODEL

    if model:
        os.environ["MEDSTUDIES_CHAT_MODEL"] = model

    if not api_key and not os.getenv("ANTHROPIC_API_KEY"):
        rprint("[red]API key não configurada.[/red]")
        rprint("  Defina ANTHROPIC_API_KEY ou use --api-key")
        raise typer.Exit(1)

    db = get_session()
    agent = MedStudiesAgent(db, api_key=api_key)

    console.print(f"\n[bold blue]MedBot[/bold blue] [dim]({os.getenv('MEDSTUDIES_CHAT_MODEL', MODEL)})[/dim]")
    console.print("[dim]Digite sua pergunta. 'sair' para encerrar, 'reset' para nova sessão.[/dim]\n")

    while True:
        try:
            user_input = input("Você: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Encerrando MedBot.[/dim]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("sair", "exit", "quit"):
            console.print("[dim]Até logo![/dim]")
            break
        if user_input.lower() == "reset":
            agent.reset()
            console.print("[dim]Sessão reiniciada.[/dim]\n")
            continue

        try:
            with console.status("[dim]MedBot pensando...[/dim]"):
                response = agent.chat(user_input)
            console.print(f"\n[bold green]MedBot:[/bold green] {response}\n")
        except Exception as e:
            rprint(f"[red]Erro: {e}[/red]")


if __name__ == "__main__":
    app()
