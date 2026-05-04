"""
Seed the database with demo subjects + topics so you can immediately run:
    medstudies plan generate
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from medstudies.persistence.database import init_db, get_session
from medstudies.persistence.models import Subject, Topic
from medstudies.ingestion.csv_adapter import CSVAdapter

SUBJECTS = [
    ("Cardiologia",     2.0),
    ("Pneumologia",     1.5),
    ("Endocrinologia",  1.5),
    ("Clínica Médica",  2.0),
    ("Cirurgia Geral",  1.2),
]

TOPICS = {
    "Cardiologia": [
        ("Insuficiência Cardíaca", "Cardio::IC"),
        ("FA e Flutter",           "Cardio::Arritmias"),
        ("Hipertensão Arterial",   "Cardio::HAS"),
        ("Síndromes Coronarianas", "Cardio::SCA"),
    ],
    "Pneumologia": [
        ("Pneumonia Bacteriana", "Pneumo::Infecções"),
        ("DPOC",                 "Pneumo::DPOC"),
        ("Asma",                 "Pneumo::Asma"),
    ],
    "Endocrinologia": [
        ("DM Tipo 2",        "Endo::DM"),
        ("Hipotireoidismo",  "Endo::Tireoide"),
    ],
    "Clínica Médica": [
        ("Sepse",            "CM::Sepse"),
        ("Equilíbrio Ácido-Base", None),
    ],
    "Cirurgia Geral": [
        ("Abdome Agudo",    None),
        ("Hérnias",         None),
    ],
}


def main():
    init_db()
    db = get_session()

    for subj_name, weight in SUBJECTS:
        existing = db.query(Subject).filter_by(name=subj_name).first()
        if not existing:
            db.add(Subject(name=subj_name, exam_weight=weight))
    db.commit()

    for subj_name, topics in TOPICS.items():
        subj = db.query(Subject).filter_by(name=subj_name).first()
        for topic_name, anki_deck in topics:
            existing = db.query(Topic).filter_by(name=topic_name, subject_id=subj.id).first()
            if not existing:
                db.add(Topic(name=topic_name, subject_id=subj.id, anki_deck=anki_deck))
    db.commit()

    # Import example questions
    csv_path = os.path.join(os.path.dirname(__file__), "..", "data", "imports", "example_questions.csv")
    adapter = CSVAdapter(db)
    result = adapter.ingest(file_path=csv_path)
    print(f"Imported {result.records_created} question records. Errors: {result.errors}")

    print("Demo data seeded. Run: medstudies plan generate")


if __name__ == "__main__":
    main()
