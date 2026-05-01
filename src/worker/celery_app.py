from celery import Celery
from src.core.config import settings
from dotenv import load_dotenv

load_dotenv()

celery_app = Celery(
    "repomind",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["src.tasks.repo_tasks", "src.tasks.graph_tasks", "src.tasks.analysis_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)