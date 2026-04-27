import logging
import os

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from src.api.deps import get_current_user, get_db
from src.core.config import settings
from src.models.repo_job import RepoJob
from src.schemas.repo import ChatRequest, ChatResponse, RepoImportRequest, RepoListItem
from src.services.repo_service import build_repo_context
from src.tasks.repo_tasks import clone_repo_task, extract_repo_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repo", tags=["repo"])


# ── Import ────────────────────────────────────────────────────────────────────

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


# ── Status ────────────────────────────────────────────────────────────────────

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


# ── List repos for current user ───────────────────────────────────────────────

@router.get("/list", response_model=list[RepoListItem])
def list_repos(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """Return all repo import jobs for the authenticated user, newest first."""
    jobs = (
        db.query(RepoJob)
        .filter(RepoJob.user_id == str(user_id))
        .order_by(RepoJob.id.desc())
        .all()
    )
    return [
        RepoListItem(task_id=j.id, repo_name=j.repo_name, status=j.status)
        for j in jobs
    ]


# ── Chat with a repo ──────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatResponse)
def chat_with_repo(
    req: ChatRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """
    Answer a question about a cloned repository using Groq LLM.
    Supports multi-turn conversation via the `history` field.
    """
    if not settings.GROQ_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GROQ_API_KEY is not configured on the server.",
        )

    # Verify the repo belongs to this user and is fully cloned
    job = (
        db.query(RepoJob)
        .filter(
            RepoJob.repo_name == req.repo_name,
            RepoJob.user_id == str(user_id),
            RepoJob.status == "DONE",
        )
        .first()
    )
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found or not yet fully cloned for this user.",
        )

    # Build the path and verify it exists on disk
    repo_path = os.path.join(settings.REPOS_BASE_PATH, str(user_id), req.repo_name)
    if not os.path.isdir(repo_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Repository files not found on disk at expected path.",
        )

    # Build repo context
    context = build_repo_context(repo_path)
    logger.debug("Built context for %s (%d chars)", req.repo_name, len(context))

    # Construct message list for the LLM
    system_prompt = (
        "You are RepoMind, an expert AI code assistant. "
        "A developer has imported a Git repository and is asking you questions about it. "
        "You have been given the following repository context (directory tree, README, and key config files).\n\n"
        f"REPOSITORY: {req.repo_name}\n\n"
        f"{context}\n\n"
        "Answer questions clearly and concisely. "
        "Use markdown formatting — code blocks for file paths, commands, and code snippets. "
        "If asked for the tree structure, display it from the Directory Structure section above."
    )

    messages = [SystemMessage(content=system_prompt)]

    for msg in req.history:
        if msg.role == "user":
            messages.append(HumanMessage(content=msg.content))
        else:
            messages.append(AIMessage(content=msg.content))

    messages.append(HumanMessage(content=req.message))

    # Call Groq
    try:
        llm = ChatGroq(
            model="llama-3.3-70b-versatile",
            groq_api_key=settings.GROQ_API_KEY,
            temperature=0.3,
            max_tokens=1024,
        )
        response = llm.invoke(messages)
    except Exception as exc:
        logger.exception("Groq LLM call failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM call failed: {exc}",
        )

    return ChatResponse(answer=response.content)
