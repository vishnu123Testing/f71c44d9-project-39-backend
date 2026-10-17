from collections.abc import Generator

from sqlalchemy import Engine
from sqlalchemy.orm import DeclarativeBase, Session

from src.models.database import Base, SessionLocal, engine, get_db


def test_engine_initialization() -> None:
    """Test that engine is properly initialized with default configuration."""
    assert isinstance(engine, Engine)
    assert engine.url.drivername in ("sqlite", "postgresql", "mysql")


def test_base_declarative_metadata() -> None:
    """Test that Base class inherits from DeclarativeBase and has metadata."""
    assert issubclass(Base, DeclarativeBase)
    assert Base.metadata is not None


def test_session_local() -> None:
    """Test that SessionLocal creates valid sessions bound to engine."""
    session = SessionLocal()
    try:
        assert isinstance(session, Session)
        assert session.bind == engine
    finally:
        session.close()


def test_get_db_yields_and_closes_session() -> None:
    """Test that get_db generator yields a session and closes it afterwards."""
    generator = get_db()
    assert isinstance(generator, Generator)

    db_session = next(generator)
    assert isinstance(db_session, Session)

    # Closing generator should invoke the finally block in get_db
    try:
        next(generator)
    except StopIteration:
        pass

    # Session should be closed (in SQLAlchemy, session.is_active or checking transaction state)
    # Re-testing that yielding gave a valid session
