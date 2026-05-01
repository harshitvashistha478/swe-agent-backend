"""
Analysis service — multi-pass Ollama analysis using the existing Neo4j graph.

Passes
------
  security    → auth, injection, secrets, missing validation
  performance → N+1 patterns, blocking I/O, redundant work
  quality     → dead code, error handling, complexity, types

Each pass sends only the relevant code + graph context to the model
and forces structured JSON output so results are always parseable.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Generator

from src.core.config import settings
from src.utils.agents_functions import (
    find_circular_imports,
    find_dead_code,
    find_deeply_coupled_files,
    get_all_files_for_repo,
    get_callers_for_file,
    get_symbols_for_file,
)

logger = logging.getLogger(__name__)

# ── Ollama client (raw requests, same pattern as graph_service) ───────────────

def _ollama_chat(prompt: str, model: str | None = None) -> str:
    """
    Send a prompt to Ollama and return the response text.
    Forces JSON mode via the format param so the model always outputs parseable JSON.
    Falls back gracefully if Ollama is unreachable.
    """
    import requests
    model = model or settings.OLLAMA_CHAT_MODEL
    try:
        resp = requests.post(
            f"{settings.OLLAMA_BASE_URL}/api/generate",
            json={"model": model, "prompt": prompt, "format": "json", "stream": False},
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception as exc:
        logger.warning("Ollama call failed for model %s: %s", model, exc)
        return ""


# ── Prompt templates ──────────────────────────────────────────────────────────

_SECURITY_PROMPT = """You are a senior backend security engineer.
Analyse the code below for security vulnerabilities only.

FILE: {file_path}
SYMBOLS DEFINED: {symbols}
IMPORTED BY: {imported_by}

CODE:
{code}
Look for: SQL/NoSQL injection, hardcoded secrets, missing input validation,
broken auth/authz, insecure deserialization, path traversal, missing rate limiting.

Respond ONLY in this exact JSON (no markdown, no extra text):
{{
  "issues": [
    {{
      "type": "<issue_type>",
      "severity": "critical|high|medium|low",
      "line": <int_or_null>,
      "description": "<what is wrong>",
      "fix": "<concrete fix>",
      "why": "<security impact>"
    }}
  ]
}}
If no issues found return {{"issues": []}}
"""

_PERFORMANCE_PROMPT = """You are a senior backend performance engineer.
Analyse the code below for performance problems only.

FILE: {file_path}
SYMBOLS DEFINED: {symbols}
CALL GRAPH (who calls who in this file): {call_graph}

CODE:
{code}
Look for: N+1 database queries, synchronous blocking I/O in async context,
missing caching, repeated expensive computation, unneeded DB columns fetched,
missing pagination on list queries.

Respond ONLY in this exact JSON (no markdown, no extra text):
{{
  "issues": [
    {{
      "type": "<issue_type>",
      "severity": "critical|high|medium|low",
      "line": <int_or_null>,
      "description": "<what is wrong>",
      "fix": "<concrete fix>",
      "why": "<performance impact>"
    }}
  ]
}}
If no issues found return {{"issues": []}}
"""

_QUALITY_PROMPT = """You are a senior backend engineer doing a code quality review.
Analyse the code below for quality issues only.

FILE: {file_path}
SYMBOLS DEFINED: {symbols}

CODE:
{code}
Look for: missing error handling, overly complex functions (>50 lines),
missing type hints, hardcoded magic values, functions doing too many things,
missing docstrings on public functions, inconsistent naming.

Respond ONLY in this exact JSON (no markdown, no extra text):
{{
  "issues": [
    {{
      "type": "<issue_type>",
      "severity": "critical|high|medium|low",
      "line": <int_or_null>,
      "description": "<what is wrong>",
      "fix": "<concrete fix>",
      "why": "<quality impact>"
    }}
  ]
}}
If no issues found return {{"issues": []}}
"""

PASSES = [
    ("security",    _SECURITY_PROMPT),
    ("performance", _PERFORMANCE_PROMPT),
    ("quality",     _QUALITY_PROMPT),
]


# ── JSON extraction (handles models that wrap with markdown fences) ────────────

def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    # Strip ```json ... ``` fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("Could not parse JSON from model output: %s", raw[:200])
        return {"issues": []}


# ── Per-file analysis ─────────────────────────────────────────────────────────

def _analyse_file(
    file_info: dict,
    user_id: str,
    repo_name: str,
    repo_path: str,
) -> list[dict]:
    """Run all three passes on a single file. Returns a flat list of issue dicts."""
    rel_path = file_info["rel_path"]
    abs_path = os.path.join(repo_path, rel_path.replace("/", os.sep))

    # Read source code — skip if unreadable
    try:
        with open(abs_path, encoding="utf-8", errors="ignore") as fh:
            code = fh.read()
    except Exception:
        logger.debug("Skipping unreadable file: %s", rel_path)
        return []

    if not code.strip():
        return []

    # Gather graph context (already in Neo4j from the build step)
    symbols   = get_symbols_for_file(user_id, repo_name, rel_path)
    importers = get_callers_for_file(user_id, repo_name, rel_path)

    sym_names  = [f"{s['kind']} {s['name']} (line {s['line']})" for s in symbols]
    importer_names = [i["importer"] for i in importers]

    all_issues: list[dict] = []

    for pass_name, template in PASSES:
        prompt = template.format(
            file_path   = rel_path,
            symbols     = ", ".join(sym_names) or "none",
            imported_by = ", ".join(importer_names) or "none",
            call_graph  = ", ".join(
                f"{s['name']} → ?" for s in symbols
            ),           # intrafile calls are implicit via symbol list for now
            code        = code[:6000],  # stay inside context window
        )

        raw    = _ollama_chat(prompt)
        parsed = _extract_json(raw)

        for issue in parsed.get("issues", []):
            issue["file_path"]   = rel_path
            issue["symbol_name"] = None
            issue["pass_type"]   = pass_name
            all_issues.append(issue)

    return all_issues


# ── Cross-file (architectural) issues ─────────────────────────────────────────

def _cross_file_issues(user_id: str, repo_name: str) -> list[dict]:
    """
    Find architectural issues that span multiple files using the Neo4j graph.
    These can't be found by looking at a single file.
    """
    issues: list[dict] = []

    # Circular imports
    circles = find_circular_imports(user_id, repo_name)
    for c in circles:
        issues.append({
            "file_path":     c["file_a"],
            "symbol_name":   None,
            "pass_type":     "quality",
            "severity":      "high",
            "issue_type":    "circular_import",
            "description":   f"{c['file_a']} and {c['file_b']} import each other",
            "suggested_fix": "Extract shared code to a third module to break the cycle",
            "line_number":   None,
        })

    # Highly coupled files
    coupled = find_deeply_coupled_files(user_id, repo_name, threshold=5)
    for c in coupled:
        issues.append({
            "file_path":     c["file_path"],
            "symbol_name":   None,
            "pass_type":     "quality",
            "severity":      "medium",
            "issue_type":    "high_coupling",
            "description":   f"Imported by {c['importers']} files — high blast radius",
            "suggested_fix": "Consider an interface layer or dependency injection",
            "line_number":   None,
        })

    # Dead code
    dead = find_dead_code(user_id, repo_name)
    for d in dead:
        issues.append({
            "file_path":     d["file_path"],
            "symbol_name":   d["name"],
            "pass_type":     "quality",
            "severity":      "low",
            "issue_type":    "dead_code",
            "description":   f"{d['kind']} '{d['name']}' is never called",
            "suggested_fix": "Remove or add tests/usages for this symbol",
            "line_number":   None,
        })

    return issues


# ── Main entry point ──────────────────────────────────────────────────────────

def run_analysis(
    user_id: str,
    repo_name: str,
    repo_path: str,
) -> Generator[dict, None, None]:
    """
    Run the full analysis pipeline. Yields progress dicts so the Celery task
    can track progress and the SSE endpoint can stream updates to the frontend.

    Yields: {"stage": str, "file": str, "progress": int, "issues": list}
    """
    files = get_all_files_for_repo(user_id, repo_name)
    total = len(files)

    if total == 0:
        yield {"stage": "error", "file": "", "progress": 0,
               "issues": [], "message": "No files found. Has the graph been built?"}
        return

    all_issues: list[dict] = []

    # ── Phase 1: per-file analysis ────────────────────────────────────────────
    for idx, file_info in enumerate(files, start=1):
        rel_path = file_info["rel_path"]
        # Only analyse Python for now; extend to JS when ready
        if file_info.get("language") != "python":
            continue

        issues = _analyse_file(file_info, user_id, repo_name, repo_path)
        all_issues.extend(issues)

        progress = int((idx / total) * 85)   # reserve last 15% for cross-file
        yield {
            "stage":    "file_analysis",
            "file":     rel_path,
            "progress": progress,
            "issues":   issues,
        }

    # ── Phase 2: cross-file / architectural issues ────────────────────────────
    yield {"stage": "cross_file", "file": "", "progress": 87, "issues": []}
    arch_issues = _cross_file_issues(user_id, repo_name)
    all_issues.extend(arch_issues)

    # ── Phase 3: summary ──────────────────────────────────────────────────────
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for issue in all_issues:
        sev = issue.get("severity", "low")
        counts[sev] = counts.get(sev, 0) + 1

    yield {
        "stage":    "done",
        "file":     "",
        "progress": 100,
        "issues":   arch_issues,
        "summary":  counts,
    }