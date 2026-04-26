import os
import subprocess
from urllib.parse import urlparse
from src.worker.celery_app import celery_app
from src.db.session import SessionLocal
from src.models.repo_job import RepoJob


def extract_repo_name(repo_url: str) -> str:
    path = urlparse(repo_url).path
    return path.strip("/").replace(".git", "")


def build_clone_url(repo_url: str, token: str | None):
    if token:
        return repo_url.replace("https://", f"https://{token}@")
    return repo_url


@celery_app.task(bind=True, max_retries=3)
def clone_repo_task(self, repo_url: str, visibility: str, access_token: str | None, user_id: str):
    db = SessionLocal()
    try:
        repo_name = extract_repo_name(repo_url)

        # update status → CLONING
        job = db.query(RepoJob).filter(RepoJob.id == self.request.id).first()
        if job:
            job.status = "CLONING"
            db.commit()

        if visibility == "private" and not access_token:
            raise ValueError("Access token required for private repo")

        clone_url = build_clone_url(repo_url, access_token)
        clone_path = os.path.join("repos", str(user_id), repo_name)
        os.makedirs(os.path.dirname(clone_path), exist_ok=True)

        subprocess.run(["git", "clone", clone_url, clone_path], check=True)

        # update status → DONE
        if job:
            job.status = "DONE"
            db.commit()

        return {"status": "completed", "repo": repo_name}

    except Exception as e:
        if job:
            job.status = "FAILED"
            db.commit()
        raise self.retry(exc=e, countdown=5)

    finally:
        db.close()
