"""
Neo4j driver — singleton + context manager.

Usage:
    from src.db.neo4j_session import get_session

    with get_session() as session:
        session.run("MATCH (n) RETURN count(n)")
"""
import logging
from contextlib import contextmanager

from neo4j import GraphDatabase
from src.core.config import settings

logger = logging.getLogger(__name__)

_driver = None


def get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )
        logger.info("Neo4j driver connected → %s", settings.NEO4J_URI)
    return _driver


def close_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None
        logger.info("Neo4j driver closed")


@contextmanager
def get_session(database: str = "neo4j"):
    """Yield a Neo4j session, always closing it on exit."""
    with get_driver().session(database=database) as session:
        yield session


def setup_indexes() -> None:
    """
    Create uniqueness constraints and indexes.
    Safe to call on every startup — Neo4j ignores constraints that already exist.
    """
    constraints = [
        "CREATE CONSTRAINT repo_id IF NOT EXISTS FOR (r:Repository) REQUIRE r.repo_id IS UNIQUE",
        "CREATE CONSTRAINT file_id IF NOT EXISTS FOR (f:File) REQUIRE f.file_id IS UNIQUE",
        "CREATE CONSTRAINT symbol_id IF NOT EXISTS FOR (s:Symbol) REQUIRE s.symbol_id IS UNIQUE",
    ]
    indexes = [
        "CREATE INDEX file_repo_idx IF NOT EXISTS FOR (f:File) ON (f.repo_id)",
        "CREATE INDEX symbol_repo_idx IF NOT EXISTS FOR (s:Symbol) ON (s.repo_id)",
        "CREATE INDEX symbol_file_idx IF NOT EXISTS FOR (s:Symbol) ON (s.file_id)",
    ]
    with get_session() as session:
        for stmt in constraints + indexes:
            session.run(stmt)
    logger.info("Neo4j constraints and indexes ensured")
