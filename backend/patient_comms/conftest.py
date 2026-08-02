"""Shared pytest fixtures. Unit tests use in-memory SQLite for the LOCAL tables
(patient_outreach, messages); shared-table access via repo.py is faked per-test."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base


@pytest.fixture(autouse=True)
def _responder_off_by_default(monkeypatch):
    # Keep the suite hermetic. The conversational responder defaults ON in
    # production; tests must not make live LLM calls. Tests that exercise the
    # responder set RESPONDER=on themselves (their setenv wins over this,
    # since they share this test's monkeypatch instance).
    monkeypatch.setenv("RESPONDER", "off")


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
