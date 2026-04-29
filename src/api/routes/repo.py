import logging
import os

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from src.services.graph_service import build_graph_context
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
    Answer a question about a cloned repository using a local Ollama model.

    Context pipeline
    ----------------
    1. build_repo_context  — directory tree + README + key config files
       (gives the LLM a high-level map of the project)
    2. build_graph_context — embeds the question, runs a Neo4j vector search,
       retrieves the top-5 most relevant files by description similarity,
       includes their full source + call-graph edges
       (gives the LLM precise, question-specific code context)
    3. Conversation history is prepended so the model can answer follow-ups.
    """
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
            detail="Repository files not found on disk at expected path.",
        )

    # ── Build context ─────────────────────────────────────────────────────────
    overview_context = build_repo_context(repo_path)
    # Pass repo_path so the graph service can read full file content from disk
    retrieved_context = build_graph_context(
        user_id, req.repo_name, req.message, repo_path
    )
    logger.debug(
        "Context built for %s | overview=%d chars | retrieved=%d chars",
        req.repo_name, len(overview_context), len(retrieved_context),
    )

    # ── System prompt ─────────────────────────────────────────────────────────
    no_retrieved = retrieved_context == "No relevant graph data found."

    system_prompt = f"""You are RepoMind, an expert AI code assistant specialising in codebase analysis.

====================
📁 REPOSITORY OVERVIEW
====================
High-level directory structure, README, and key config files.
Use this to understand the project's purpose and layout.

{overview_context}

====================
🔍 RETRIEVED FILE CONTEXT
====================
{"These files were selected by semantic similarity to the user's question. " +
 "They include full source code and call-graph edges — prioritise this for code-level answers."
 if not no_retrieved else
 "No semantically relevant files were found. The graph index may still be building — rely on the overview above."}

{retrieved_context}

====================
INSTRUCTIONS
====================
- Ground every answer in the context provided above.
- Reference specific file paths, function names, and line numbers where relevant.
- If the answer cannot be determined from the context, say so explicitly — do NOT hallucinate.
- Keep answers concise and developer-friendly.
"""

    messages = [SystemMessage(content=system_prompt)]

    for msg in req.history:
        if msg.role == "user":
            messages.append(HumanMessage(content=msg.content))
        else:
            messages.append(AIMessage(content=msg.content))

    messages.append(HumanMessage(content=req.message))

    # ── Call local Ollama ─────────────────────────────────────────────────────
    try:
        llm = ChatOllama(
            model=settings.OLLAMA_CHAT_MODEL,
            base_url=settings.OLLAMA_BASE_URL,
            temperature=0.3,
            num_predict=2048,
        )
        response = llm.invoke(messages)
    except Exception as exc:
        logger.exception("Ollama LLM call failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Ollama call failed: {exc}. "
                f"Make sure Ollama is running at {settings.OLLAMA_BASE_URL} "
                f"and the model '{settings.OLLAMA_CHAT_MODEL}' is pulled."
            ),
        )

    return ChatResponse(answer=response.content)
