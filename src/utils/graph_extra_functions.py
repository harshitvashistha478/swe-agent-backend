import re

from src.db.neo4j_session import get_session


def extract_file_from_question(question: str) -> str | None:
    match = re.search(r"[\w/]+\.py", question)
    return match.group(0) if match else None


def query_symbols_in_file(user_id: str, repo_name: str, file_rel_path: str):
    from src.services.graph_service import _file_id, _repo_id
    repo_id = _repo_id(user_id, repo_name)
    file_id = _file_id(repo_id, file_rel_path)

    cypher = """
    MATCH (s:Symbol {file_id: $file_id})
    RETURN s.name AS name, s.kind AS kind, s.line AS line
    ORDER BY s.line
    """

    with get_session() as s:
        return [dict(row) for row in s.run(cypher, file_id=file_id)]