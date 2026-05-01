"""
Celery task: run multi-pass code analysis on a cloned + graph-indexed repo.
Triggered by POST /repo/analyse/{repo_name}
"""
import logging
import os
from datetime import datetime, timezone

from src.core.config import settings
from src.db.session import SessionLocal
from src.models.analysis import AnalysisIssue, AnalysisRun
from src.services.agent_service import run_analysis
from src.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=1, name="tasks.run_analysis")
def run_analysis_task(self, run_id: str, user_id: str, repo_name: str):
    """
    Execute the full analysis pipeline and persist results to Postgres.
    Updates AnalysisRun.status throughout so the frontend can poll /analyse/status.
    """
    repo_path = os.path.join(settings.REPOS_BASE_PATH, str(user_id), repo_name)
    db = SessionLocal()

    try:
        # Mark as running
        run = db.query(AnalysisRun).filter(AnalysisRun.id == run_id).first()
        if not run:
            logger.error("AnalysisRun %s not found", run_id)
            return

        run.status = "RUNNING"
        db.commit()

        logger.info("Analysis started | run=%s user=%s repo=%s", run_id, user_id, repo_name)

        total_summary = {"critical": 0, "high": 0, "medium": 0, "low": 0}

        # Generator yields progress dicts — we persist issues in each batch
        for update in run_analysis(user_id, repo_name, repo_path):
            issues = update.get("issues", [])

            for issue in issues:
                db.add(AnalysisIssue(
                    run_id        = run_id,
                    file_path     = issue.get("file_path"),
                    symbol_name   = issue.get("symbol_name"),
                    pass_type     = issue.get("pass_type"),
                    severity      = issue.get("severity", "low"),
                    issue_type    = issue.get("type") or issue.get("issue_type"),
                    description   = issue.get("description"),
                    suggested_fix = issue.get("fix") or issue.get("suggested_fix"),
                    line_number   = issue.get("line"),
                ))
            db.commit()

            if update.get("summary"):
                total_summary = update["summary"]

        run.status       = "DONE"
        run.summary      = total_summary
        run.completed_at = datetime.now(timezone.utc)
        db.commit()

        logger.info("Analysis done | run=%s summary=%s", run_id, total_summary)
        return {"status": "done", "run_id": run_id, **total_summary}

    except Exception as exc:
        logger.error("Analysis failed | run=%s: %s", run_id, exc, exc_info=True)
        run = db.query(AnalysisRun).filter(AnalysisRun.id == run_id).first()
        if run:
            run.status = "FAILED"
            db.commit()
        raise self.retry(exc=exc, countdown=10)

    finally:
        db.close()