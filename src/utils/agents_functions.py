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