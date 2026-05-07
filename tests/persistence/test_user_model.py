from medstudies.persistence.models import User, Base
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def test_user_model_columns():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(id="550e8400-e29b-41d4-a716-446655440000", email="test@example.com")
        db.add(user)
        db.commit()
        loaded = db.get(User, "550e8400-e29b-41d4-a716-446655440000")
        assert loaded.email == "test@example.com"
        assert loaded.plan_tier == "free"
        assert loaded.display_name is None
        assert loaded.created_at is not None


from medstudies.persistence.models import Subject, Topic, Question, FlashCard, StudySession, AnkiSnapshot, TopicReview, Tag, DailyPlan, EditorialTopic, LibraryItem


def test_domain_models_have_user_id():
    """All domain models must have a user_id column."""
    models = [Subject, Topic, Question, FlashCard, StudySession,
              AnkiSnapshot, TopicReview, Tag, DailyPlan, EditorialTopic, LibraryItem]
    for model in models:
        assert hasattr(model, "user_id"), f"{model.__name__} missing user_id"
        col = model.user_id.property.columns[0]
        assert col.nullable is False, f"{model.__name__}.user_id must be NOT NULL"
        assert col.index is True, f"{model.__name__}.user_id must be indexed"
