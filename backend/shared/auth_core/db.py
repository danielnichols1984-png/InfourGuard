from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from shared.auth_core.config import settings

engine = create_engine(settings.DATABASE_URL, future=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create auth_core's own tables. Call once at host app startup."""
    Base.metadata.create_all(bind=engine)
