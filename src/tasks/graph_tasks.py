"""
Celery task: build the graph Knowledge Base for a cloned repository.
Runs after clone_repo_task completes successfully.
"""
import logging

from src.db.session import SessionLocal
from src.models.repo_job import RepoJob
from src.services.graph_service import build_repo_graph
from src.worker.celery_app import celery_app
from src.core.config import settings
import os

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=2, name="tasks.build_graph")
def build_graph_task(self, user_id: str, repo_name: str):
    """
    Parse the cloned repo and populate the Neo4j graph KB.
    Separate from the clone task so a graph failure doesn't affect
    the clone status shown to the user.
    """
    repo_path = os.path.join(settings.REPOS_BASE_PATH, str(user_id), repo_name)

    if not os.path.isdir(repo_path):
        logger.error(
            "Graph build skipped — repo path not found | user=%s repo=%s path=%s",
            user_id, repo_name, repo_path,
        )
        return {"status": "skipped", "reason": "repo path not found"}

    try:
        logger.info("Graph KB build started | user=%s repo=%s", user_id, repo_name)
        summary = build_repo_graph(repo_path, str(user_id), repo_name)
        logger.info("Graph KB build finished | %s", summary)
        return {"status": "done", **summary}

    except Exception as exc:
        logger.error(
            "Graph KB build failed | user=%s repo=%s: %s",
            user_id, repo_name, exc, exc_info=True,
        )
        raise self.retry(exc=exc, countdown=30)
