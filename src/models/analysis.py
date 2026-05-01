import uuid
from sqlalchemy import Column, String, Text, Integer, Boolean, DateTime, func
from sqlalchemy.dialects.postgresql import UUID, JSONB
from src.db.base import Base

class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id       = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    repo_name = Column(String, nullable=False)
    user_id  = Column(String, nullable=False)
    status   = Column(String, default="PENDING")  # PENDING|RUNNING|DONE|FAILED
    summary  = Column(JSONB)                        # {critical:N, high:N, ...}
    started_at   = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))

class AnalysisIssue(Base):
    __tablename__ = "analysis_issues"

    id            = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id        = Column(String, nullable=False, index=True)
    file_path     = Column(Text)
    symbol_name   = Column(String)
    pass_type     = Column(String)   # security | performance | quality
    severity      = Column(String)   # critical | high | medium | low
    issue_type    = Column(String)
    description   = Column(Text)
    suggested_fix = Column(Text)
    line_number   = Column(Integer)
    is_resolved   = Column(Boolean, default=False)