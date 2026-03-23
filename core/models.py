from __future__ import annotations
import datetime
import pathlib
from sqlalchemy import Column, Integer, String, Float, DateTime, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

class Job(Base):
    __tablename__ = "jobs"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    external_id = Column(String, unique=True, index=True, nullable=False)
    source      = Column(String, nullable=False)   # "greenhouse" | "lever"
    company     = Column(String, nullable=False)
    title       = Column(String, nullable=False)
    location    = Column(String, default="")
    url         = Column(String, default="")
    description = Column(Text,   default="")
    score       = Column(Float,  default=0.0)
    status      = Column(String, default="new")    # new | approved | rejected | applied
    fetched_at  = Column(DateTime, default=datetime.datetime.utcnow)
    applied_at  = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "external_id": self.external_id,
            "source":      self.source,
            "company":     self.company,
            "title":       self.title,
            "location":    self.location,
            "url":         self.url,
            "score":       self.score,
            "status":      self.status,
            "fetched_at":  self.fetched_at.isoformat() if self.fetched_at else None,
            "applied_at":  self.applied_at.isoformat() if self.applied_at else None,
        }


_DB_PATH = pathlib.Path(__file__).resolve().parents[1] / "jobs.db"


def get_engine(db_url: str | None = None):
    url = db_url or f"sqlite:///{_DB_PATH}"
    return create_engine(url, connect_args={"check_same_thread": False})


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)
