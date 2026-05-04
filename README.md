# MedStudies — Study Agent Hub

> Intelligent study planner for Brazilian medical residency exams. Tracks your performance across Medcof mock exams, Anki flashcards, and manual sessions — then generates a personalized daily study plan powered by spaced repetition (SM-2).

![Python](https://img.shields.io/badge/Python-3.11+-blue)
![Tests](https://img.shields.io/badge/tests-36%20passing-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Daily study plan** — SM-2 + error-rate weighted priority scoring across all topics
- **Weekly scheduler** — distributes topics across morning (PRACTICE) and afternoon (REVIEW) blocks
- **Mock exam import** — CSV/JSON import from Medcof or any structured exam source
- **Anki integration** — sync card stats via AnkiConnect
- **Flashcards** — built-in SM-2 flashcard system with `flashcard due` review session
- **Reports** — error trend, subject ranking, weak topic analysis
- **Tags** — filter study plans by tag (e.g. `enare`, `prova-sp`)
- **PubMed news feed** — latest medical literature on the dashboard
- **Gamification** — XP, levels, streaks, achievements
- **Dark mode** — automatic via system preference

## Stack

```
Python 3.11+ · FastAPI · SQLAlchemy (SQLite) · Typer CLI · Rich · Chart.js
```

## Architecture

```
Ingestion          Domain             Engine              Interface
─────────          ──────             ──────              ─────────
CSV/JSON  ──┐      Subject            TopicScorer         FastAPI dashboard
AnkiConnect─┤  →   Topic       →      SM2Engine      →    Typer CLI
Medcof     ─┘      Question           DailyPlanBuilder    REST API
                   FlashCard          WeeklyPlanBuilder
```

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/yourusername/medstudies
cd medstudies
pip install -e .

# 2. Initialize database
medstudies init

# 3. Start dashboard
uvicorn medstudies.interface.api:app --reload
# Open http://localhost:8000
```

**Windows — start with one click:**
```powershell
.\scripts\start.ps1
```

**Windows — register as startup service:**
```powershell
# Run as Administrator (once)
.\scripts\install-startup.ps1
```

## CLI Commands

```bash
# Study plan
medstudies plan generate              # today's plan
medstudies plan generate --tag enare  # filtered by tag
medstudies plan week                  # 7-day schedule

# Import data
medstudies mock run simulado.csv --source "Medcof Mock 1" --date 2026-04-27

# Reports
medstudies report weak                # worst topics by error rate
medstudies report subject             # performance by subject
medstudies report trend               # weekly error trend

# Tags (filter plans by exam)
medstudies tag add "enare" --color "#E8362A"
medstudies tag attach "enare" --topic "DPOC" --subject "Pneumologia"
medstudies plan generate --tag "enare"

# Flashcards (SM-2)
medstudies flashcard add "DPOC" --subject "Pneumologia" \
  --question "Estadiamento GOLD" --answer "I-IV por FEV1"
medstudies flashcard due              # interactive review session

# Anki sync (requires Anki open with AnkiConnect)
medstudies anki sync

# Notifications (requires env vars)
medstudies notify config              # show required env vars
medstudies notify send                # send today's plan via email/Telegram
```

## CSV Import Format

```csv
subject_name,topic_name,correct,notes
Cardiologia,Insuficiência Cardíaca,false,Confundi critérios
Pneumologia,DPOC,true,
Clínica Médica,Sepse,false,SOFA score errado
```

Download template from the dashboard onboarding wizard or:
```bash
curl http://localhost:8000/api/export/template.csv -o template.csv
```

## Environment Variables

```bash
# Notifications (optional)
MEDSTUDIES_EMAIL_FROM=seuemail@gmail.com
MEDSTUDIES_EMAIL_PASSWORD=app-password
MEDSTUDIES_TG_TOKEN=bot-token
MEDSTUDIES_TG_CHAT_ID=chat-id

# Medcof scraper (optional)
MEDCOF_EMAIL=email@medcof.com.br
MEDCOF_PASSWORD=senha

# AI chat agent (optional)
ANTHROPIC_API_KEY=sk-ant-...
```

## Development

```bash
pip install -e ".[dev]"
pytest                    # run 36 tests
uvicorn medstudies.interface.api:app --reload  # dashboard with hot reload
```

## Exam Templates

Pre-configured subject weights for major Brazilian residency exams:

| Exam | Profile |
|------|---------|
| ENARE | Clínica Médica 3.0 · Cirurgia 1.5 · GO 1.5 · Pediatria 1.5 |
| SUS-SP (VUNESP) | Clínica Médica 2.5 · Preventiva 2.0 · Cirurgia 1.5 |
| HC-FMUSP / FAMERP | Clínica Médica 2.5 · Cirurgia 2.0 · Neurologia 1.0 |

Apply via dashboard onboarding or: `POST /api/edital/apply?template_id=enare`

---

Built by a physician, for physicians. Designed to replace scattered spreadsheets and disconnected tools with a single intelligent hub.
