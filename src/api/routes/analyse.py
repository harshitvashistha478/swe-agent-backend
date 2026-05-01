"""
Analysis endpoints.

POST /repo/analyse/{repo_name}         — trigger analysis, returns run_id
GET  /repo/analyse/{repo_name}/status  — poll run status + summary
GET  /repo/analyse/{repo_name}/issues  — paginated issues list
GET  /repo/analyse/{repo_name}/issues?severity=critical — filter by severity
GET  /repo/analyse/{repo_name}/issues?file=src/foo.py   — filter by file
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from src.api.deps import get_current_user, get_db
from src.models.analysis import AnalysisIssue, AnalysisRun
from src.models.repo_job import RepoJob
from src.tasks.analysis_tasks import run_analysis_task

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/repo/analyse", tags=["analyse"])


def _require_done_repo(repo_name: str, user_id: str, db: Session) -> RepoJob:
    """Reuse your existing pattern from graph.py."""
    job = (
        db.query(RepoJob)
        .filter(
            RepoJob.repo_name == repo_name,
            RepoJob.user_id   == str(user_id),
            RepoJob.status    == "DONE",
        )
        .first()
    )
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found or not yet fully cloned.",
        )
    return job


@router.post("/{repo_name}", status_code=status.HTTP_202_ACCEPTED)
def trigger_analysis(
    repo_name: str,
    db:      Session = Depends(get_db),
    user_id: str     = Depends(get_current_user),
):
    """Queue a full analysis run for a cloned + graph-indexed repo."""
    _require_done_repo(repo_name, user_id, db)

    # Cancel any existing in-progress run for this repo+user
    existing = (
        db.query(AnalysisRun)
        .filter(
            AnalysisRun.repo_name == repo_name,
            AnalysisRun.user_id   == str(user_id),
            AnalysisRun.status.in_(["PENDING", "RUNNING"]),
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Analysis already in progress (run_id={existing.id})",
        )

    run = AnalysisRun(repo_name=repo_name, user_id=str(user_id), status="PENDING")
    db.add(run)
    db.commit()
    db.refresh(run)

    run_analysis_task.delay(run_id=run.id, user_id=str(user_id), repo_name=repo_name)
    logger.info("Analysis queued | run=%s user=%s repo=%s", run.id, user_id, repo_name)

    return {"message": "Analysis started", "run_id": run.id}


@router.get("/{repo_name}/status")
def get_analysis_status(
    repo_name: str,
    db:      Session = Depends(get_db),
    user_id: str     = Depends(get_current_user),
):
    """Poll the latest analysis run for this repo."""
    run = (
        db.query(AnalysisRun)
        .filter(
            AnalysisRun.repo_name == repo_name,
            AnalysisRun.user_id   == str(user_id),
        )
        .order_by(AnalysisRun.started_at.desc())
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="No analysis found for this repo")

    return {
        "run_id":       run.id,
        "status":       run.status,
        "summary":      run.summary,
        "started_at":   run.started_at,
        "completed_at": run.completed_at,
    }


@router.get("/{repo_name}/issues")
def get_analysis_issues(
    repo_name: str,
    severity: str | None = Query(default=None),
    file:     str | None = Query(default=None),
    pass_type: str | None = Query(default=None),
    limit:    int         = Query(default=100, le=500),
    offset:   int         = Query(default=0),
    db:      Session = Depends(get_db),
    user_id: str     = Depends(get_current_user),
):
    """
    Return paginated issues for the latest completed analysis run.
    Filterable by severity, file path, and pass type.
    """
    run = (
        db.query(AnalysisRun)
        .filter(
            AnalysisRun.repo_name == repo_name,
            AnalysisRun.user_id   == str(user_id),
            AnalysisRun.status    == "DONE",
        )
        .order_by(AnalysisRun.started_at.desc())
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="No completed analysis found")

    q = db.query(AnalysisIssue).filter(AnalysisIssue.run_id == run.id)
    if severity:
        q = q.filter(AnalysisIssue.severity == severity)
    if file:
        q = q.filter(AnalysisIssue.file_path == file)
    if pass_type:
        q = q.filter(AnalysisIssue.pass_type == pass_type)

    total  = q.count()
    issues = q.order_by(AnalysisIssue.severity).offset(offset).limit(limit).all()

    return {
        "run_id": run.id,
        "total":  total,
        "issues": [
            {
                "id":           i.id,
                "file_path":    i.file_path,
                "symbol_name":  i.symbol_name,
                "pass_type":    i.pass_type,
                "severity":     i.severity,
                "issue_type":   i.issue_type,
                "description":  i.description,
                "suggested_fix": i.suggested_fix,
                "line_number":  i.line_number,
                "is_resolved":  i.is_resolved,
            }
            for i in issues
        ],
    }