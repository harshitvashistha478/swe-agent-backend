"""
Helper utilities for the analysis pipeline.
Pulls enriched context from the Neo4j graph that is already built
by build_repo_graph() so we never re-parse from scratch.
"""
from __future__ import annotations
import os
import logging
from src.db.neo4j_session import get_session
from src.services.graph_service import _repo_id, _file_id, query_intrafile

logger = logging.getLogger(__name__)


def get_all_files_for_repo(user_id: str, repo_name: str) -> list[dict]:
    """
    Return every File node for this repo — rel_path, language, description.
    Already stored in Neo4j from the graph-build step, so zero disk I/O.
    """
    repo_id = _repo_id(user_id, repo_name)
    cypher = """
    MATCH (f:File {repo_id: $repo_id})
    RETURN f.rel_path AS rel_path, f.language AS language,
           f.description AS description
    ORDER BY f.rel_path
    """
    with get_session() as s:
        return [dict(row) for row in s.run(cypher, repo_id=repo_id)]


def get_symbols_for_file(user_id: str, repo_name: str, rel_path: str) -> list[dict]:
    """All Symbol nodes (functions/classes) defined in a file."""
    repo_id  = _repo_id(user_id, repo_name)
    file_id  = _file_id(repo_id, rel_path)
    cypher = """
    MATCH (s:Symbol {file_id: $file_id})
    RETURN s.name AS name, s.kind AS kind, s.line AS line
    ORDER BY s.line
    """
    with get_session() as s:
        return [dict(row) for row in s.run(cypher, file_id=file_id)]


def get_callers_for_file(user_id: str, repo_name: str, rel_path: str) -> list[dict]:
    """Which other files import this file? (INTERFILE edges pointing IN)"""
    repo_id = _repo_id(user_id, repo_name)
    file_id = _file_id(repo_id, rel_path)
    cypher = """
    MATCH (a:File {repo_id: $repo_id})-[:INTERFILE]->(b:File {file_id: $file_id})
    RETURN a.rel_path AS importer
    """
    with get_session() as s:
        return [dict(row) for row in s.run(cypher, repo_id=repo_id, file_id=file_id)]


def find_dead_code(user_id: str, repo_name: str) -> list[dict]:
    """
    Symbols with no callers and not an exported route handler.
    These are strong dead-code candidates.
    """
    repo_id = _repo_id(user_id, repo_name)
    cypher = """
    MATCH (s:Symbol {repo_id: $repo_id})
    WHERE NOT ()-[:INTRAFILE]->(s)
      AND NOT s.name STARTS WITH 'test_'
      AND NOT s.name STARTS WITH '_'
    MATCH (f:File {file_id: s.file_id})
    RETURN s.name AS name, s.kind AS kind, f.rel_path AS file_path
    """
    with get_session() as s:
        return [dict(row) for row in s.run(cypher, repo_id=repo_id)]


def find_deeply_coupled_files(user_id: str, repo_name: str, threshold: int = 5) -> list[dict]:
    """
    Files imported by many others — high coupling, high blast radius if changed.
    """
    repo_id = _repo_id(user_id, repo_name)
    cypher = """
    MATCH (a:File {repo_id: $repo_id})-[:INTERFILE]->(b:File {repo_id: $repo_id})
    WITH b, count(a) AS importers
    WHERE importers >= $threshold
    RETURN b.rel_path AS file_path, importers
    ORDER BY importers DESC
    """
    with get_session() as s:
        return [dict(row) for row in s.run(cypher, repo_id=repo_id, threshold=threshold)]


def find_circular_imports(user_id: str, repo_name: str) -> list[dict]:
    """Detect circular import chains (length 2 only — extend path depth for more)."""
    repo_id = _repo_id(user_id, repo_name)
    cypher = """
    MATCH (a:File {repo_id: $repo_id})-[:INTERFILE]->(b:File {repo_id: $repo_id})
          -[:INTERFILE]->(a)
    RETURN a.rel_path AS file_a, b.rel_path AS file_b
    """
    with get_session() as s:
        return [dict(row) for row in s.run(cypher, repo_id=repo_id)]

# ── Function-level helpers for bottom-up analysis ─────────────────────────────

def get_all_symbols_for_repo(user_id: str, repo_name: str) -> list[dict]:
    """
    Return every Symbol node for this repo with its file path and line range.
    Used as the input set for topological sorting.

    Each dict: {symbol_id, name, kind, line, end_line, file_id, rel_path, language, file_description}
    """
    repo_id = _repo_id(user_id, repo_name)
    cypher = """
    MATCH (s:Symbol {repo_id: $repo_id})-[:DEFINED_IN]->(f:File {repo_id: $repo_id})
    RETURN s.symbol_id   AS symbol_id,
           s.name        AS name,
           s.kind        AS kind,
           s.line        AS line,
           s.end_line    AS end_line,
           s.file_id     AS file_id,
           f.rel_path    AS rel_path,
           f.language    AS language,
           f.description AS file_description
    ORDER BY f.rel_path, s.line
    """
    with get_session() as s:
        return [dict(row) for row in s.run(cypher, repo_id=repo_id)]


def get_all_call_edges(user_id: str, repo_name: str) -> list[tuple[str, str]]:
    """
    Return all INTRAFILE call edges as (caller_symbol_id, callee_symbol_id) tuples.
    These are the edges used to build the topological ordering.
    """
    repo_id = _repo_id(user_id, repo_name)
    cypher = """
    MATCH (a:Symbol {repo_id: $repo_id})-[:INTRAFILE]->(b:Symbol {repo_id: $repo_id})
    RETURN a.symbol_id AS caller_id, b.symbol_id AS callee_id
    """
    with get_session() as s:
        return [(row["caller_id"], row["callee_id"]) for row in s.run(cypher, repo_id=repo_id)]


def topological_sort_functions(
    symbols: list[dict],
    call_edges: list[tuple[str, str]],
) -> list[dict]:
    """
    Return symbols in bottom-up order: leaf functions first (those that call
    nothing inside the codebase), then their callers, up to top-level
    orchestrators.

    By the time a function is yielded, all of its callees have already been
    yielded — so analysis results can safely be passed as context upward.

    Cycles are detected and broken: any function still unprocessed after the
    main Kahn pass is appended at the end (they'll be analysed without callee
    context, which is the best we can do).

    Algorithm: Kahn's algorithm on the *reversed* call graph.
      - In the reversed graph an edge goes callee → caller.
      - A node's in-degree in the reversed graph = number of its callees.
      - Nodes with in-degree 0 are leaves (call nothing) — process first.
      - When a leaf is processed, decrement the in-degree of every function
        that calls it; once a caller's count hits 0 all its callees are done.
    """
    from collections import defaultdict, deque

    all_ids = {s["symbol_id"] for s in symbols}
    id_to_sym = {s["symbol_id"]: s for s in symbols}

    # callee_count[sid] = how many of sid's callees are still unprocessed
    callee_count: dict[str, int] = defaultdict(int)
    # callers_of[sid] = list of symbol_ids that directly call sid
    callers_of: dict[str, list[str]] = defaultdict(list)

    for caller_id, callee_id in call_edges:
        if caller_id in all_ids and callee_id in all_ids:
            callee_count[caller_id] += 1
            callers_of[callee_id].append(caller_id)

    # Seed: functions that call nothing within the repo (leaves)
    queue: deque[str] = deque(
        sid for sid in all_ids if callee_count[sid] == 0
    )

    result: list[dict] = []
    processed: set[str] = set()

    while queue:
        sid = queue.popleft()
        if sid in processed:
            continue
        processed.add(sid)
        result.append(id_to_sym[sid])

        # Every caller of this symbol now has one fewer unprocessed callee
        for caller_id in callers_of[sid]:
            callee_count[caller_id] -= 1
            if callee_count[caller_id] == 0 and caller_id not in processed:
                queue.append(caller_id)

    # Append any remaining symbols that are part of a cycle
    for sid, sym in id_to_sym.items():
        if sid not in processed:
            logger.debug("Cycle detected — appending %s without callee context", sym["name"])
            result.append(sym)

    return result


def get_callee_summaries(
    symbol_name: str,
    file_id: str,
    repo_id: str,
    analysis_cache: dict[str, dict],
) -> list[dict]:
    """
    Given a function being analysed, look up the already-computed analysis
    summaries for all functions it calls.

    analysis_cache maps symbol_id → {"description": str, "issues": list[dict]}

    Returns a list of dicts: {name, description, issue_count, severities}
    so the LLM prompt gets a compact but informative callee summary.
    """
    cypher = """
    MATCH (a:Symbol {name: $name, file_id: $file_id})-[:INTRAFILE]->(b:Symbol)
    RETURN b.symbol_id AS callee_id, b.name AS callee_name
    """
    summaries = []
    with get_session() as s:
        rows = s.run(cypher, name=symbol_name, file_id=file_id)
        for row in rows:
            cached = analysis_cache.get(row["callee_id"])
            if cached:
                issues = cached.get("issues", [])
                severities = list({i.get("severity", "low") for i in issues})
                summaries.append({
                    "name":        row["callee_name"],
                    "description": cached.get("description", ""),
                    "issue_count": len(issues),
                    "severities":  severities,
                })
            else:
                summaries.append({
                    "name":        row["callee_name"],
                    "description": "(not yet analysed)",
                    "issue_count": 0,
                    "severities":  [],
                })
    return summaries


def store_function_description(symbol_id: str, description: str) -> None:
    """Write the LLM-generated description back onto the Symbol node in Neo4j."""
    if not description:
        return
    cypher = """
    MATCH (s:Symbol {symbol_id: $symbol_id})
    SET s.description = $description
    """
    with get_session() as s:
        s.run(cypher, symbol_id=symbol_id, description=description)


def get_insights_for_repo(user_id: str, repo_name: str) -> dict:
    """
    Return all symbols for this repo grouped by file, including:
      - LLM description (stored on Symbol node after analysis)
      - Direct callers and callees (by name)
      - Line range

    Used by the /analyse/insights endpoint to join with Postgres issue data.
    """
    repo_id = _repo_id(user_id, repo_name)

    # ── All symbols with their file info ─────────────────────────────────────
    sym_cypher = """
    MATCH (s:Symbol {repo_id: $repo_id})-[:DEFINED_IN]->(f:File {repo_id: $repo_id})
    RETURN s.symbol_id   AS symbol_id,
           s.name        AS name,
           s.kind        AS kind,
           s.line        AS line,
           s.end_line    AS end_line,
           s.description AS description,
           f.rel_path    AS rel_path,
           f.language    AS language,
           f.description AS file_description
    ORDER BY f.rel_path, s.line
    """

    # ── All intrafile call edges ──────────────────────────────────────────────
    edge_cypher = """
    MATCH (a:Symbol {repo_id: $repo_id})-[:INTRAFILE]->(b:Symbol {repo_id: $repo_id})
    RETURN a.symbol_id AS caller_id, a.name AS caller_name,
           b.symbol_id AS callee_id, b.name AS callee_name
    """

    with get_session() as s:
        symbols = [dict(row) for row in s.run(sym_cypher, repo_id=repo_id)]
        edges   = [dict(row) for row in s.run(edge_cypher, repo_id=repo_id)]

    # Build caller/callee lookup maps
    callees_of: dict[str, list[str]] = {}   # symbol_id → [callee_name, ...]
    callers_of: dict[str, list[str]] = {}   # symbol_id → [caller_name, ...]
    for e in edges:
        callees_of.setdefault(e["caller_id"], []).append(e["callee_name"])
        callers_of.setdefault(e["callee_id"], []).append(e["caller_name"])

    # Attach to symbols
    for sym in symbols:
        sid = sym["symbol_id"]
        sym["callees"] = callees_of.get(sid, [])
        sym["callers"] = callers_of.get(sid, [])

    return {"symbols": symbols}
