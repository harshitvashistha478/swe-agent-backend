import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.api.deps import get_current_user, get_db
from src.models.repo_job import RepoJob
from src.schemas.repo import RepoImportRequest
from src.tasks.repo_tasks import clone_repo_task, extract_repo_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repo", tags=["repo"])


@router.post("/import", status_code=status.HTTP_202_ACCEPTED)
def import_repo(
    req: RepoImportRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    try:
        repo_name = extract_repo_name(str(req.repo_url))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    task = clone_repo_task.delay(
        repo_url=str(req.repo_url),
        visibility=req.visibility,
        access_token=req.access_token,
        user_id=user_id,
    )

    job = RepoJob(
        id=task.id,
        repo_name=repo_name,
        user_id=str(user_id),
        status="PENDING",
    )
    db.add(job)
    db.commit()

    logger.info("Repo import queued | task=%s user=%s repo=%s", task.id, user_id, repo_name)

    return {
        "message": "Repository import started",
        "repo_name": repo_name,
        "task_id": task.id,
    }


@router.get("/status/{task_id}")
def get_status(
    task_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    job = db.query(RepoJob).filter(RepoJob.id == task_id).first()

    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    if job.user_id != str(user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    return {
        "task_id": job.id,
        "repo_name": job.repo_name,
        "status": job.status,
    }
