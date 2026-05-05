"""
Analysis service — function-level, bottom-up Ollama analysis using the Neo4j graph.

Strategy
--------
1.  Pull every Symbol (function/class/method) for the repo from Neo4j,
    together with its file path and line range.
2.  Build the intrafile call graph and topologically sort it so that
    leaf functions (those that call nothing within the codebase) come first
    and top-level orchestrators come last.
3.  Analyse each function in that order.  By the time we analyse a caller,
    all of its callees are already done — their summaries are passed into the
    prompt so the LLM has real behavioural context, not just names.
4.  After all functions are done, run the cross-file architectural checks
    (circular imports, high coupling, dead code) exactly as before.

Context window discipline
-------------------------
- Function source is extracted by line range (never the whole file).
- Source is capped at MAX_FUNC_CHARS chars, cut at a line boundary.
- Callee summaries are compact dicts — name + one-sentence description +
  issue count — not full source.
- The total prompt stays well within a 4 096-token local model window.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Generator

import httpx

from src.core.config import settings
from src.utils.agents_functions import (
    store_function_description,
    find_circular_imports,
    find_dead_code,
    find_deeply_coupled_files,
    get_all_call_edges,
    get_all_symbols_for_repo,
    get_callee_summaries,
    topological_sort_functions,
)

logger = logging.getLogger(__name__)

# Shared HTTP client — connection-pooled, single timeout config
_http = httpx.Client(timeout=120)

# Maximum characters of function source sent to the LLM.
# Cut at a line boundary so the model never sees half a statement.
MAX_FUNC_CHARS = 4_000


# ── Ollama client ─────────────────────────────────────────────────────────────

def _ollama_chat(prompt: str, model: str | None = None) -> str:
    """
    Send a prompt to Ollama and return the response text.
    Forces JSON mode so the model always outputs parseable JSON.
    Returns empty string on failure (caller handles the fallback).
    """
    model = model or settings.OLLAMA_CHAT_MODEL
    try:
        resp = _http.post(
            f"{settings.OLLAMA_BASE_URL}/api/generate",
            json={"model": model, "prompt": prompt, "format": "json", "stream": False},
        )
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception as exc:
        logger.warning("Ollama call failed (model=%s): %s", model, exc)
        return ""


# ── JSON extraction ───────────────────────────────────────────────────────────

def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("Could not parse JSON from model output: %.200s", raw)
        return {}


# ── Function source extraction ────────────────────────────────────────────────

def _extract_source(abs_path: str, start_line: int, end_line: int) -> str:
    """
    Read lines [start_line, end_line] (1-indexed, inclusive) from a file.
    Returns empty string if the file cannot be read or the range is invalid.
    """
    try:
        with open(abs_path, encoding="utf-8", errors="ignore") as fh:
            all_lines = fh.readlines()
    except Exception:
        return ""

    # Clamp to actual file length
    start = max(0, start_line - 1)
    end   = min(len(all_lines), end_line)
    return "".join(all_lines[start:end])


def _truncate_at_line_boundary(code: str, max_chars: int = MAX_FUNC_CHARS) -> tuple[str, bool]:
    """
    Truncate `code` to at most `max_chars` characters, always cutting at
    a newline so the model never sees a broken statement.

    Returns (truncated_code, was_truncated).
    """
    if len(code) <= max_chars:
        return code, False
    cut = code[:max_chars].rfind("\n")
    if cut <= 0:
        cut = max_chars
    return code[:cut] + "\n# ... [truncated — function too large for context window]", True


# ── Prompt template ───────────────────────────────────────────────────────────

_FUNCTION_PROMPT = """\
You are a senior engineer performing a combined security, performance, and \
code-quality review of a single function.

FILE:     {file_path}
FILE PURPOSE: {file_description}
FUNCTION: {func_name}  ({kind}, line {start_line})

FUNCTIONS THIS ONE CALLS (already analysed):
{callee_summaries}

SOURCE:
{source}

Tasks:
1. Write a ONE-sentence description of what this function does.
2. List every security, performance, and code-quality issue you can find.
   Only report real issues — do not invent problems that aren't there.

Respond ONLY with this exact JSON (no markdown, no extra text):
{{
  "description": "<one sentence>",
  "issues": [
    {{
      "pass_type": "security|performance|quality",
      "type": "<short label>",
      "severity": "critical|high|medium|low",
      "line": <int or null>,
      "description": "<what is wrong>",
      "fix": "<concrete fix>",
      "why": "<impact>"
    }}
  ]
}}
If no issues found return {{"description": "<...>", "issues": []}}
"""


def _format_callee_summaries(summaries: list[dict]) -> str:
    if not summaries:
        return "  (none — leaf function)"
    lines = []
    for s in summaries:
        sev_str = (
            f"  ⚠ {s['issue_count']} issue(s): {', '.join(s['severities'])}"
            if s["issue_count"] else ""
        )
        lines.append(f"  • {s['name']}: {s['description']}{sev_str}")
    return "\n".join(lines)


# ── Per-function analysis ─────────────────────────────────────────────────────

def _analyse_function(
    symbol:         dict,          # row from get_all_symbols_for_repo
    repo_path:      str,
    analysis_cache: dict[str, dict],   # symbol_id → {description, issues}
) -> dict:
    """
    Analyse a single function/class.  Returns a result dict that is stored
    in analysis_cache and also yielded to the Celery task for DB persistence.

    result dict keys:
      symbol_id, file_path, symbol_name, pass_type (always "function"),
      description, issues (list of issue dicts)
    """
    rel_path   = symbol["rel_path"]
    abs_path   = os.path.join(repo_path, rel_path.replace("/", os.sep))
    start_line = symbol.get("line") or 1
    end_line   = symbol.get("end_line") or start_line
    file_id    = symbol["file_id"]

    # -- Extract source -------------------------------------------------------
    raw_source = _extract_source(abs_path, start_line, end_line)
    if not raw_source.strip():
        return {"symbol_id": symbol["symbol_id"], "file_path": rel_path,
                "symbol_name": symbol["name"], "description": "", "issues": []}

    source, truncated = _truncate_at_line_boundary(raw_source)
    if truncated:
        logger.debug("Truncated large function %s in %s", symbol["name"], rel_path)

    # -- Callee summaries (already in cache because we go bottom-up) ----------
    callee_data = get_callee_summaries(
        symbol_name=symbol["name"],
        file_id=file_id,
        repo_id=symbol["file_id"].rsplit("/", 2)[0] + "/" + symbol["file_id"].rsplit("/", 2)[1]
            if symbol["file_id"].count("/") >= 2 else symbol["file_id"],
        analysis_cache=analysis_cache,
    )

    # -- Build and send prompt ------------------------------------------------
    prompt = _FUNCTION_PROMPT.format(
        file_path        = rel_path,
        file_description = symbol.get("file_description") or "—",
        func_name        = symbol["name"],
        kind             = symbol.get("kind", "function"),
        start_line       = start_line,
        callee_summaries = _format_callee_summaries(callee_data),
        source           = source,
    )

    raw    = _ollama_chat(prompt)
    parsed = _extract_json(raw)

    description = parsed.get("description", "")
    issues      = parsed.get("issues", [])

    # Stamp each issue with its origin
    for issue in issues:
        issue["file_path"]   = rel_path
        issue["symbol_name"] = symbol["name"]
        issue["pass_type"]   = issue.get("pass_type", "quality")

    result = {
        "symbol_id":   symbol["symbol_id"],
        "file_path":   rel_path,
        "symbol_name": symbol["name"],
        "description": description,
        "issues":      issues,
    }

    # Store in cache so callers can reference this analysis
    analysis_cache[symbol["symbol_id"]] = result
    # Persist description to Neo4j so the insights endpoint can read it
    store_function_description(symbol["symbol_id"], description)
    return result


# ── Cross-file (architectural) issues ─────────────────────────────────────────

def _cross_file_issues(user_id: str, repo_name: str) -> list[dict]:
    """
    Find architectural issues that span multiple files using the Neo4j graph.
    These cannot be found by looking at individual functions.
    """
    issues: list[dict] = []

    for c in find_circular_imports(user_id, repo_name):
        issues.append({
            "file_path":     c["file_a"],
            "symbol_name":   None,
            "pass_type":     "quality",
            "severity":      "high",
            "type":          "circular_import",
            "description":   f"{c['file_a']} and {c['file_b']} import each other",
            "fix":           "Extract shared code to a third module to break the cycle",
            "why":           "Circular imports cause import errors and make testing hard",
        })

    for c in find_deeply_coupled_files(user_id, repo_name, threshold=5):
        issues.append({
            "file_path":     c["file_path"],
            "symbol_name":   None,
            "pass_type":     "quality",
            "severity":      "medium",
            "type":          "high_coupling",
            "description":   f"Imported by {c['importers']} files — high blast radius",
            "fix":           "Consider an interface layer or dependency injection",
            "why":           "A change here breaks many dependents simultaneously",
        })

    # Fixed dead-code query: exclude common entry-point patterns
    _ENTRY_POINT_NAMES = {"main", "setup", "teardown", "run", "create_app", "app"}
    for d in find_dead_code(user_id, repo_name):
        if d["name"] in _ENTRY_POINT_NAMES or d["kind"] == "class":
            continue
        issues.append({
            "file_path":     d["file_path"],
            "symbol_name":   d["name"],
            "pass_type":     "quality",
            "severity":      "low",
            "type":          "dead_code",
            "description":   f"{d['kind']} '{d['name']}' is never called internally",
            "fix":           "Remove or add tests / usages for this symbol",
            "why":           "Dead code increases maintenance burden and causes confusion",
        })

    return issues


# ── Main entry point ──────────────────────────────────────────────────────────

def run_analysis(
    user_id:   str,
    repo_name: str,
    repo_path: str,
) -> Generator[dict, None, None]:
    """
    Run the full bottom-up function-level analysis pipeline.

    Yields progress dicts so the Celery task can persist results
    incrementally and the SSE endpoint can stream updates to the frontend.

    Yield shape:
      {
        "stage":    "function_analysis" | "cross_file" | "done" | "error",
        "file":     str,          # rel_path of the file being processed
        "function": str,          # name of the function just analysed
        "progress": int,          # 0-100
        "issues":   list[dict],   # issues found in this batch
        "description": str,       # LLM description for this function
      }
    """
    # ── Load all symbols and sort them bottom-up ──────────────────────────────
    all_symbols  = get_all_symbols_for_repo(user_id, repo_name)
    call_edges   = get_all_call_edges(user_id, repo_name)
    sorted_syms  = topological_sort_functions(all_symbols, call_edges)

    # Filter to Python only for now (extend to JS when ready)
    py_symbols = [s for s in sorted_syms if s.get("language") == "python"]
    total      = len(py_symbols)

    if total == 0:
        yield {
            "stage": "error", "file": "", "function": "", "progress": 0,
            "issues": [], "description": "",
            "message": "No Python symbols found. Has the graph been built?",
        }
        return

    logger.info(
        "Bottom-up analysis | repo=%s/%s | %d functions in topological order",
        user_id, repo_name, total,
    )

    # Shared cache: symbol_id → {description, issues}
    # Populated as each function is analysed; passed to callers later.
    analysis_cache: dict[str, dict] = {}

    # ── Phase 1: per-function analysis (bottom-up) ────────────────────────────
    for idx, symbol in enumerate(py_symbols, start=1):
        result   = _analyse_function(symbol, repo_path, analysis_cache)
        progress = int((idx / total) * 85)   # reserve last 15 % for cross-file

        yield {
            "stage":       "function_analysis",
            "file":        result["file_path"],
            "function":    result["symbol_name"],
            "progress":    progress,
            "issues":      result["issues"],
            "description": result["description"],
        }

    # ── Phase 2: cross-file / architectural issues ────────────────────────────
    yield {"stage": "cross_file", "file": "", "function": "", "progress": 87,
           "issues": [], "description": ""}

    arch_issues = _cross_file_issues(user_id, repo_name)

    # ── Phase 3: summary ──────────────────────────────────────────────────────
    all_issues = [
        issue
        for result in analysis_cache.values()
        for issue in result.get("issues", [])
    ] + arch_issues

    counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for issue in all_issues:
        sev = issue.get("severity", "low")
        counts[sev] = counts.get(sev, 0) + 1

    yield {
        "stage":       "done",
        "file":        "",
        "function":    "",
        "progress":    100,
        "issues":      arch_issues,
        "description": "",
        "summary":     counts,
    }
