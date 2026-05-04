# Medical Study Agent Hub — Setup

## 1. Install Python (if not yet installed)

Download from https://www.python.org/downloads/ — choose **Python 3.11+**.
During install, check **"Add Python to PATH"**.

Or use `winget`:
```
winget install Python.Python.3.13
```

Restart your terminal after installing.

## 2. Install the package

```bash
cd C:\Users\Ygor\projetos\medstudies
pip install -e .
```

This installs all dependencies (SQLAlchemy, Typer, Rich) and registers the `medstudies` CLI command.

## 3. Initialize the database

```bash
medstudies init
```

## 4. Seed demo data (optional but recommended for first run)

```bash
python scripts/seed_demo.py
```

This creates subjects, topics, and imports example question results from `data/imports/example_questions.csv`.

## 5. Generate your first daily plan

```bash
medstudies plan generate
```

---

## Full CLI Reference

```bash
# Subjects
medstudies subject add "Cardiologia" --weight 2.0
medstudies subject add "Pneumologia" --weight 1.5

# Topics
medstudies topic add "Insuficiência Cardíaca" --subject "Cardiologia" --anki-deck "Cardio::IC"
medstudies topic add "FA e Flutter" --subject "Cardiologia"
medstudies topic list

# Record question results
medstudies question add "Insuficiência Cardíaca" --subject "Cardiologia" --wrong --source "Medcof Mock 4"
medstudies question add "FA e Flutter" --subject "Cardiologia" --correct

# Record a study session
medstudies session add "Insuficiência Cardíaca" --subject "Cardiologia" --type review --duration 45

# Import mock-exam CSV
medstudies import data/imports/example_questions.csv

# Anki integration (requires Anki open + AnkiConnect add-on)
medstudies anki decks          # list your decks
medstudies anki sync           # pull card stats for all linked topics

# Generate daily plan
medstudies plan generate
medstudies plan generate --max 10   # more topics

# Show existing plan
medstudies plan show

# Reports
medstudies report weak             # top 10 weak topics
medstudies report weak --top 5 --min-questions 5
```

---

## Anki Integration Setup

1. Open Anki desktop
2. Install the **AnkiConnect** add-on: `Tools → Add-ons → Get Add-ons → Code: 2055492159`
3. Restart Anki
4. When adding a topic, pass `--anki-deck "YourDeckName"` to link it
5. Run `medstudies anki sync` — card stats (ease, lapses, due cards) are now part of the scoring

---

## CSV Import Format

```csv
topic_name,subject_name,source,answered_at,correct,notes
Insuficiência Cardíaca,Cardiologia,Medcof Mock 1,2025-03-01T10:00:00,false,confundi com edema pulmonar
FA e Flutter,Cardiologia,Medcof Mock 1,2025-03-01T11:00:00,true,
```

Fields:
| Field | Required | Notes |
|---|---|---|
| `topic_name` | yes | created automatically if missing |
| `subject_name` | yes | created automatically if missing |
| `source` | no | e.g. "Medcof Mock 3" |
| `answered_at` | no | ISO 8601 datetime |
| `correct` | yes | `true` / `false` |
| `notes` | no | free text |

---

## Priority Score Formula

```
priority_score =
    0.30 * error_rate                      # % wrong answers
  + 0.25 * days_since_last_review (norm.)  # recency urgency
  + 0.15 * error_volume (norm.)            # raw number of errors
  + 0.20 * subject_exam_weight (norm.)     # importance in target exam
  + 0.10 * anki_difficulty_signal          # ease factor + lapses + due ratio
```

Action assigned per topic:
- **REINFORCE** → Anki due > 5 or lapses > 5 (memory failure signal)
- **PRACTICE** → error rate > 45% with ≥ 3 questions answered
- **REVIEW** → everything else (stale or low-data topic)

---

## Extending the System

To add a new data source (e.g. Notion, PDF parser):

1. Create `medstudies/ingestion/notion_adapter.py`
2. Subclass `BaseIngestionAdapter` from `medstudies.ingestion.base`
3. Implement `source_name` and `ingest()` — return an `IngestResult`
4. Wire it up in `medstudies/interface/cli.py` with a new command

The domain models and scoring engine need no changes.

---

## Database

SQLite file is at `data/medstudies.db` by default.
Override with the env variable: `MEDSTUDIES_DB=path/to/custom.db`
