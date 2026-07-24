"""Shared pytest fixtures. Unit tests use in-memory SQLite for the LOCAL tables
(patient_outreach, messages); shared-table access via repo.py is faked per-test."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
