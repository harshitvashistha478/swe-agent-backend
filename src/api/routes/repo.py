from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.schemas.repo import RepoImportRequest
from src.api.deps import get_db, get_current_user
from src.tasks.repo_tasks import clone_repo_task, extract_repo_name
from src.worker.celery_app import celery_app

router = APIRouter(prefix="/repo", tags=["repo"])


from src.models.repo_job import RepoJob

@router.post("/import")
def import_repo(
    req: RepoImportRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user)
):
    try:
        repo_name = extract_repo_name(req.repo_url)

        task = clone_repo_task.delay(
            repo_url=req.repo_url,
            visibility=req.visibility,
            access_token=req.access_token,
            user_id=user_id
        )

        # store job in DB
        job = RepoJob(
            id=task.id,
            repo_name=repo_name,
            user_id=str(user_id),
            status="PENDING"
        )
        db.add(job)
        db.commit()

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "message": "Repository import started",
        "repo_name": repo_name,
        "task_id": task.id
    }


@router.get("/status/{task_id}")
def get_status(task_id: str, db: Session = Depends(get_db)):
    job = db.query(RepoJob).filter(RepoJob.id == task_id).first()

    if not job:
        raise HTTPException(status_code=404, detail="Task not found")

    return {
        "task_id": job.id,
        "repo_name": job.repo_name,
        "status": job.status
    }
