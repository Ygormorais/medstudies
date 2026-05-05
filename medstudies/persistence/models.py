"""
SQLAlchemy ORM models. Topic is the central entity — everything links to it.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import (
    Column, Index, Integer, String, Float, DateTime, ForeignKey,
    Boolean, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Subject(Base):
    """Top-level grouping, e.g. Cardiology, Pediatrics."""
    __tablename__ = "subjects"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    exam_weight = Column(Float, default=1.0)  # relative importance in target exam
    created_at = Column(DateTime, default=datetime.utcnow)

    topics = relationship("Topic", back_populates="subject", cascade="all, delete-orphan")


class Topic(Base):
    """
    Core entity. Hierarchical (parent_id for sub-topics).
    Links to external systems via anki_deck and external_ref.
    """
    __tablename__ = "topics"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    subject_id = Column(Integer, ForeignKey("subjects.id"), nullable=False)
    parent_id = Column(Integer, ForeignKey("topics.id"), nullable=True)

    is_favorite = Column(Boolean, default=False)             # starred for special attention

    # External references — we don't duplicate data, we link
    anki_deck = Column(String(200), nullable=True)          # AnkiConnect deck name
    anki_tags = Column(String(500), nullable=True)          # comma-separated Anki tags
    notability_notebook = Column(String(300), nullable=True) # ex: "Cardio/Insuficiência Cardíaca"
    external_ref = Column(String(200), nullable=True)       # future: Notion page id, etc.
    study_notes = Column(Text, nullable=True)               # free-form study notes / mnemonics

    created_at = Column(DateTime, default=datetime.utcnow)

    subject = relationship("Subject", back_populates="topics")
    parent = relationship("Topic", remote_side="Topic.id", back_populates="children")
    children = relationship("Topic", back_populates="parent")

    questions = relationship("Question", back_populates="topic", cascade="all, delete-orphan")
    sessions = relationship("StudySession", back_populates="topic", cascade="all, delete-orphan")
    anki_snapshots = relationship("AnkiSnapshot", back_populates="topic", cascade="all, delete-orphan")
    flashcards = relationship("FlashCard", back_populates="topic", cascade="all, delete-orphan")
    tags = relationship("Tag", secondary="topic_tags", back_populates="topics")

    __table_args__ = (
        UniqueConstraint("name", "subject_id", name="uq_topic_name_subject"),
        Index("ix_topics_subject_id", "subject_id"),
        Index("ix_topics_parent_id", "parent_id"),
        Index("ix_topics_is_favorite", "is_favorite"),
    )


class Question(Base):
    """A practice question answered by the student."""
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True)
    topic_id = Column(Integer, ForeignKey("topics.id"), nullable=False)
    source = Column(String(100), nullable=True)   # e.g. "Medcof 2024 Mock #3"
    answered_at = Column(DateTime, default=datetime.utcnow)
    correct = Column(Boolean, nullable=False)
    notes = Column(Text, nullable=True)
    difficulty = Column(String(20), default="medio", nullable=True)  # facil | medio | dificil
    statement = Column(Text, nullable=True)       # full question text / stem

    topic = relationship("Topic", back_populates="questions")

    __table_args__ = (
        Index("ix_questions_topic_id", "topic_id"),
        Index("ix_questions_answered_at", "answered_at"),
        Index("ix_questions_correct", "correct"),
        Index("ix_questions_topic_correct", "topic_id", "correct"),
    )


class StudySession(Base):
    """Records a manual or detected study session."""
    __tablename__ = "study_sessions"

    id = Column(Integer, primary_key=True)
    topic_id = Column(Integer, ForeignKey("topics.id"), nullable=False)
    session_type = Column(String(50), default="review")  # review | practice | lecture
    started_at = Column(DateTime, default=datetime.utcnow)
    duration_minutes = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)

    topic = relationship("Topic", back_populates="sessions")

    __table_args__ = (
        Index("ix_study_sessions_topic_id", "topic_id"),
        Index("ix_study_sessions_started_at", "started_at"),
    )


class AnkiSnapshot(Base):
    """
    Snapshot of Anki card stats for a topic/deck, pulled via AnkiConnect.
    We store aggregates, not individual cards, to avoid duplication.
    """
    __tablename__ = "anki_snapshots"

    id = Column(Integer, primary_key=True)
    topic_id = Column(Integer, ForeignKey("topics.id"), nullable=False)
    synced_at = Column(DateTime, default=datetime.utcnow)

    deck_name = Column(String(200), nullable=False)
    total_cards = Column(Integer, default=0)
    due_cards = Column(Integer, default=0)
    avg_ease = Column(Float, nullable=True)      # average ease factor (2500 = normal)
    avg_interval = Column(Float, nullable=True)  # average interval in days
    total_lapses = Column(Integer, default=0)    # total times cards were failed

    topic = relationship("Topic", back_populates="anki_snapshots")


class TopicReview(Base):
    """
    SM-2 spaced repetition state per topic.
    Updated every time the student answers questions for that topic.
    """
    __tablename__ = "topic_reviews"

    id = Column(Integer, primary_key=True)
    topic_id = Column(Integer, ForeignKey("topics.id"), nullable=False, unique=True)
    ease_factor = Column(Float, default=2.5)       # EF starts at 2.5
    interval_days = Column(Float, default=1.0)     # next interval in days
    repetitions = Column(Integer, default=0)       # consecutive correct reviews
    next_review = Column(DateTime, nullable=True)  # when to review next
    last_reviewed = Column(DateTime, nullable=True)

    topic = relationship("Topic")

    __table_args__ = (
        Index("ix_topic_reviews_next_review", "next_review"),
        Index("ix_topic_reviews_topic_id", "topic_id"),
    )


class FlashCard(Base):
    """A Q&A flashcard linked to a topic, with SM-2 state."""
    __tablename__ = "flashcards"

    id = Column(Integer, primary_key=True)
    topic_id = Column(Integer, ForeignKey("topics.id"), nullable=False)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    hint = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    times_reviewed = Column(Integer, default=0)
    last_reviewed = Column(DateTime, nullable=True)
    # SM-2 fields
    ease_factor = Column(Float, default=2.5)
    interval_days = Column(Float, default=1.0)
    repetitions = Column(Integer, default=0)
    next_review = Column(DateTime, nullable=True)

    topic = relationship("Topic", back_populates="flashcards")

    __table_args__ = (
        Index("ix_flashcards_topic_id", "topic_id"),
        Index("ix_flashcards_next_review", "next_review"),
    )


# ── Tags ──────────────────────────────────────────────────────────────────────

from sqlalchemy import Table
topic_tags = Table(
    "topic_tags", Base.metadata,
    Column("topic_id", Integer, ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id",   Integer, ForeignKey("tags.id",   ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    """User-defined label that can be applied to multiple topics."""
    __tablename__ = "tags"

    id    = Column(Integer, primary_key=True)
    name  = Column(String(80), unique=True, nullable=False)
    color = Column(String(7), default="#2979E0")  # hex colour for the chip

    topics = relationship("Topic", secondary=topic_tags, back_populates="tags")


class DailyPlan(Base):
    """Persisted daily plan generated by the decision engine."""
    __tablename__ = "daily_plans"

    id = Column(Integer, primary_key=True)
    generated_at = Column(DateTime, default=datetime.utcnow)
    plan_date = Column(String(10), nullable=False)  # YYYY-MM-DD
    plan_json = Column(Text, nullable=False)         # serialized plan items

    __table_args__ = (
        Index("ix_daily_plans_plan_date", "plan_date"),
    )
