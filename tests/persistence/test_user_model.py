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
