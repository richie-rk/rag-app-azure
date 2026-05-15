"""SQLAlchemy engine and session factory.

All database access goes through this module, never raw SQL.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

_engine = None
_SessionLocal = None


def get_engine():
    """Return cached SQLAlchemy engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            pool_recycle=300,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return cached session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


def get_db() -> Session:
    """Yield a database session. Use as a dependency or context manager."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session  # type: ignore[misc]
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
