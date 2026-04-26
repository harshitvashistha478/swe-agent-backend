from sqlalchemy import Column, String
from src.db.base import Base

class RepoJob(Base):
    __tablename__ = "repo_jobs"

    id = Column(String, primary_key=True, index=True)  # celery task_id
    repo_name = Column(String)
    user_id = Column(String)
    status = Column(String)  # PENDING, CLONING, DONE, FAILED