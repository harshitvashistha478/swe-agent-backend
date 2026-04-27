import logging
import os
import stat
import subprocess
import tempfile
from urllib.parse import urlparse

from src.core.config import settings
from src.db.session import SessionLocal
from src.models.repo_job import RepoJob
from src.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}


def extract_repo_name(repo_url: str) -> str:
    """
    Extract and sanitise the repository path from a URL.
    Raises ValueError for disallowed hosts or path-traversal attempts.
    """
    parsed = urlparse(str(repo_url))

    if parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(
            f"Unsupported host '{parsed.hostname}'. "
            f"Allowed: {', '.join(sorted(ALLOWED_HOSTS))}"
        )

    raw = parsed.path.strip("/").removesuffix(".git")
    parts = raw.replace("\\", "/").split("/")

    safe_parts = []
    for part in parts:
        if not part or part in (".", "..") or "\x00" in part:
            raise ValueError(f"Invalid path component in repository URL: {part!r}")
        safe_parts.append(part)

    if not safe_parts:
        raise ValueError("Could not extract a valid repository name from URL")

    return "/".join(safe_parts)


def _clone_public(repo_url: str, clone_path: str) -> None:
    result = subprocess.run(
        ["git", "clone", "--", repo_url, clone_path],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")


def _clone_private(repo_url: str, clone_path: str, token: str) -> None:
    """
    Clone a private repo using a GIT_ASKPASS helper so the token is never
    visible in the process list (ps aux) or shell history.
    """
    askpass_script = (
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *sername*) echo 'x-access-token' ;;\n"
        "  *assword*) printf '%s' \"$_GIT_TOKEN\" ;;\n"
        "esac\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        askpass_path = os.path.join(tmpdir, "git-askpass.sh")
        with open(askpass_path, "w") as f:
            f.write(askpass_script)
        os.chmod(askpass_path, stat.S_IRWXU)  # 0700

        clone_env = {
            **os.environ,
            "GIT_ASKPASS": askpass_path,
            "_GIT_TOKEN": token,
            "GIT_TERMINAL_PROMPT": "0",
        }
        result = subprocess.run(
            ["git", "clone", "--", repo_url, clone_path],
            capture_output=True,
            text=True,
            env=clone_env,
        )

    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")


@celery_app.task(bind=True, max_retries=3)
def clone_repo_task(
    self,
    repo_url: str,
    visibility: str,
    access_token: str | None,
    user_id: str,
):
    db = SessionLocal()
    job = None  # initialised before try so except can always reference it

    try:
        repo_name = extract_repo_name(repo_url)

        job = db.query(RepoJob).filter(RepoJob.id == self.request.id).first()
        if job:
            job.status = "CLONING"
            db.commit()

        if visibility == "private" and not access_token:
            raise ValueError("Access token is required for private repositories")

        clone_path = os.path.join(settings.REPOS_BASE_PATH, str(user_id), repo_name)

        if os.path.exists(clone_path):
            raise ValueError(f"Repository '{repo_name}' is already cloned for this user")

        os.makedirs(os.path.dirname(clone_path), exist_ok=True)

        logger.info(
            "Cloning repo | task=%s user=%s repo=%s visibility=%s",
            self.request.id, user_id, repo_name, visibility,
        )

        if access_token:
            _clone_private(str(repo_url), clone_path, access_token)
        else:
            _clone_public(str(repo_url), clone_path)

        if job:
            job.status = "DONE"
            db.commit()

        logger.info("Clone complete | task=%s repo=%s", self.request.id, repo_name)

        # ── Kick off graph KB build as a follow-up task ──────────────────────
        # Import here to avoid circular import at module load time
        from src.tasks.graph_tasks import build_graph_task
        build_graph_task.delay(user_id=str(user_id), repo_name=repo_name)
        logger.info("Graph KB task queued | user=%s repo=%s", user_id, repo_name)

        return {"status": "completed", "repo": repo_name}

    except ValueError as exc:
        # Permanent input error — mark failed, do NOT retry
        logger.warning("Clone task input error | task=%s: %s", self.request.id, exc)
        if job:
            job.status = "FAILED"
            db.commit()
        raise

    except Exception as exc:
        logger.error(
            "Clone task transient error | task=%s: %s",
            self.request.id, exc, exc_info=True,
        )
        if job:
            job.status = "FAILED"
            db.commit()
        raise self.retry(exc=exc, countdown=5)

    finally:
        db.close()
