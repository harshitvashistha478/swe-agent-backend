"""
Analysis endpoints.

POST /repo/analyse?repo_name=owner/repo         — trigger analysis, returns run_id
GET  /repo/analyse/status?repo_name=owner/repo  — poll run status + summary
GET  /repo/analyse/issues?repo_name=owner/repo  — paginated issues list
GET  /repo/analyse/issues?repo_name=owner/repo&severity=critical — filter by severity
GET  /repo/analyse/issues?repo_name=owner/repo&file=src/foo.py   — filter by file

repo_name is passed as a query parameter (not a path segment) so that
repo names containing slashes (e.g. "owner/repo") do not cause 404s
due to URL path-segment encoding issues.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from src.api.deps import get_current_user, get_db
from src.models.analysis import AnalysisIssue, AnalysisRun
from src.models.repo_job import RepoJob
from src.tasks.analysis_tasks import run_analysis_task
from src.utils.agents_functions import get_insights_for_repo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/repo/analyse", tags=["analyse"])


def _require_done_repo(repo_name: str, user_id: str, db: Session) -> RepoJob:
    """Return the DONE RepoJob or raise 404."""
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


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def trigger_analysis(
    repo_name: str     = Query(..., description="Full repo name, e.g. owner/repo"),
    db:        Session = Depends(get_db),
    user_id:   str     = Depends(get_current_user),
):
    """Queue a full analysis run for a cloned + graph-indexed repo."""
    _require_done_repo(repo_name, user_id, db)

    # Reject if a run is already in progress
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


@router.get("/status")
def get_analysis_status(
    repo_name: str     = Query(..., description="Full repo name, e.g. owner/repo"),
    db:        Session = Depends(get_db),
    user_id:   str     = Depends(get_current_user),
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


@router.get("/issues")
def get_analysis_issues(
    repo_name:  str           = Query(..., description="Full repo name, e.g. owner/repo"),
    severity:   str | None    = Query(default=None),
    file:       str | None    = Query(default=None),
    pass_type:  str | None    = Query(default=None),
    limit:      int           = Query(default=100, le=500),
    offset:     int           = Query(default=0),
    db:         Session       = Depends(get_db),
    user_id:    str           = Depends(get_current_user),
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
                "id":            i.id,
                "file_path":     i.file_path,
                "symbol_name":   i.symbol_name,
                "pass_type":     i.pass_type,
                "severity":      i.severity,
                "issue_type":    i.issue_type,
                "description":   i.description,
                "suggested_fix": i.suggested_fix,
                "line_number":   i.line_number,
                "is_resolved":   i.is_resolved,
            }
            for i in issues
        ],
    }


@router.get("/insights")
def get_analysis_insights(
    repo_name: str     = Query(..., description="Full repo name, e.g. owner/repo"),
    db:        Session = Depends(get_db),
    user_id:   str     = Depends(get_current_user),
):
    """
    Return function-level metadata for the latest completed analysis run.

    Joins:
      - Neo4j: symbol descriptions, call graph (callers/callees per function)
      - Postgres: issues grouped by function (severity breakdown, full issue list)

    Response shape:
      {
        "run_id": str,
        "summary": { total_functions, functions_with_issues, clean_functions },
        "files": [
          {
            "rel_path": str,
            "file_description": str,
            "language": str,
            "issue_count": int,
            "max_severity": "critical|high|medium|low|none",
            "functions": [
              {
                "name": str, "kind": str, "line": int, "end_line": int,
                "description": str,
                "callers": [str], "callees": [str],
                "issue_count": int,
                "severity_breakdown": { critical, high, medium, low },
                "issues": [ { severity, issue_type, description, suggested_fix, line_number } ]
              }
            ]
          }
        ]
      }
    """
    # Require a completed run
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
        raise HTTPException(status_code=404, detail="No completed analysis found for this repo")

    # ── Pull all issues from Postgres grouped by (file_path, symbol_name) ───
    raw_issues = (
        db.query(AnalysisIssue)
        .filter(AnalysisIssue.run_id == run.id)
        .all()
    )
    # Index: (file_path, symbol_name) → list[issue_dict]
    issues_by_fn: dict[tuple, list] = {}
    for i in raw_issues:
        key = (i.file_path or "", i.symbol_name or "")
        issues_by_fn.setdefault(key, []).append({
            "severity":      i.severity,
            "issue_type":    i.issue_type,
            "pass_type":     i.pass_type,
            "description":   i.description,
            "suggested_fix": i.suggested_fix,
            "line_number":   i.line_number,
        })

    # ── Pull symbol graph data from Neo4j ────────────────────────────────────
    graph_data = get_insights_for_repo(str(user_id), repo_name)
    symbols    = graph_data["symbols"]

    # ── Merge and group by file ──────────────────────────────────────────────
    SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}

    files_map: dict[str, dict] = {}
    for sym in symbols:
        rp = sym["rel_path"]
        if rp not in files_map:
            files_map[rp] = {
                "rel_path":         rp,
                "file_description": sym.get("file_description") or "",
                "language":         sym.get("language") or "",
                "functions":        [],
            }

        fn_issues = issues_by_fn.get((rp, sym["name"]), [])
        sev_breakdown = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for iss in fn_issues:
            sev = iss.get("severity", "low")
            sev_breakdown[sev] = sev_breakdown.get(sev, 0) + 1

        files_map[rp]["functions"].append({
            "name":               sym["name"],
            "kind":               sym.get("kind", "function"),
            "line":               sym.get("line"),
            "end_line":           sym.get("end_line"),
            "description":        sym.get("description") or "",
            "callers":            sym.get("callers", []),
            "callees":            sym.get("callees", []),
            "issue_count":        len(fn_issues),
            "severity_breakdown": sev_breakdown,
            "issues":             fn_issues,
        })

    # Compute per-file aggregates
    files = []
    for rp, fdata in sorted(files_map.items()):
        total_issues = sum(fn["issue_count"] for fn in fdata["functions"])
        # max severity across all functions in this file
        all_sevs = [
            iss["severity"]
            for fn in fdata["functions"]
            for iss in fn["issues"]
        ]
        max_sev = min(all_sevs, key=lambda s: SEV_ORDER.get(s, 4)) if all_sevs else "none"
        fdata["issue_count"]  = total_issues
        fdata["max_severity"] = max_sev
        files.append(fdata)

    # Summary
    total_fns     = sum(len(f["functions"]) for f in files)
    fns_w_issues  = sum(
        1 for f in files for fn in f["functions"] if fn["issue_count"] > 0
    )

    return {
        "run_id":  run.id,
        "summary": {
            "total_functions":       total_fns,
            "functions_with_issues": fns_w_issues,
            "clean_functions":       total_fns - fns_w_issues,
        },
        "files": files,
    }
