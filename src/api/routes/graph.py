"""
Graph Knowledge Base query endpoints.

GET /repo/graph/{repo_name}/interfile   — file-to-file import edges
GET /repo/graph/{repo_name}/intrafile   — function-to-function call edges (whole repo)
GET /repo/graph/{repo_name}/intrafile?file=src/main.py  — for a single file
GET /repo/graph/{repo_name}/callers/{symbol}  — who calls a function?
GET /repo/graph/{repo_name}/dependents?file=src/core/config.py  — who imports a file?
POST /repo/graph/{repo_name}/rebuild    — re-trigger graph build
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from src.api.deps import get_current_user, get_db
from src.models.repo_job import RepoJob
from src.services.graph_service import (
    query_file_dependents,
    query_interfile,
    query_intrafile,
    query_symbol_callers,
)
from src.tasks.graph_tasks import build_graph_task

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repo/graph", tags=["graph"])


def _verify_repo_done(repo_name: str, user_id: str, db: Session) -> RepoJob:
    """Ensure the repo exists, belongs to this user, and is fully cloned."""
    job = (
        db.query(RepoJob)
        .filter(
            RepoJob.repo_name == repo_name,
            RepoJob.user_id == str(user_id),
            RepoJob.status == "DONE",
        )
        .first()
    )
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found or not yet fully cloned.",
        )
    return job


@router.get("/{repo_name}/interfile")
def get_interfile(
    repo_name: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """
    Return all INTERFILE edges: which file imports which other file.
    Shape: [{source, target, symbols, line}]
    """
    _verify_repo_done(repo_name, user_id, db)
    try:
        edges = query_interfile(str(user_id), repo_name)
        return {"repo": repo_name, "type": "interfile", "count": len(edges), "edges": edges}
    except Exception as exc:
        logger.exception("interfile query failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/{repo_name}/intrafile")
def get_intrafile(
    repo_name: str,
    file: str | None = Query(default=None, description="Filter to a single file (relative path)"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """
    Return all INTRAFILE edges: which function calls which other function.
    Optionally filter to one file with ?file=src/foo.py
    Shape: [{caller, callee, caller_file, line}]
    """
    _verify_repo_done(repo_name, user_id, db)
    try:
        edges = query_intrafile(str(user_id), repo_name, file_rel_path=file)
        return {"repo": repo_name, "type": "intrafile", "file": file, "count": len(edges), "edges": edges}
    except Exception as exc:
        logger.exception("intrafile query failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/{repo_name}/callers/{symbol_name}")
def get_callers(
    repo_name: str,
    symbol_name: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """
    Return all functions that call a given symbol anywhere in the repo.
    Shape: [{caller, file}]
    """
    _verify_repo_done(repo_name, user_id, db)
    try:
        callers = query_symbol_callers(str(user_id), repo_name, symbol_name)
        return {"symbol": symbol_name, "count": len(callers), "callers": callers}
    except Exception as exc:
        logger.exception("callers query failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/{repo_name}/dependents")
def get_dependents(
    repo_name: str,
    file: str = Query(..., description="Relative path of the file to inspect"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """
    Return all files that import a given file.
    Shape: [{importer}]
    """
    _verify_repo_done(repo_name, user_id, db)
    try:
        dependents = query_file_dependents(str(user_id), repo_name, file)
        return {"file": file, "count": len(dependents), "dependents": dependents}
    except Exception as exc:
        logger.exception("dependents query failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/{repo_name}/rebuild", status_code=status.HTTP_202_ACCEPTED)
def rebuild_graph(
    repo_name: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """Re-trigger graph KB build for an already-cloned repository."""
    _verify_repo_done(repo_name, user_id, db)
    task = build_graph_task.delay(user_id=str(user_id), repo_name=repo_name)
    return {"message": "Graph rebuild queued", "task_id": task.id}
