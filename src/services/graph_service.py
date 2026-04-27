"""
Graph Knowledge Base builder for RepoMind.

Schema
------
Nodes
  (:Repository  {repo_id, repo_name, user_id})
  (:File        {file_id, rel_path, language, repo_id})
  (:Symbol      {symbol_id, name, kind, line, file_id, repo_id})
                 kind = "function" | "class" | "method"

Relationships
  (:File)   -[:BELONGS_TO]->  (:Repository)
  (:Symbol) -[:DEFINED_IN]->  (:File)

  (:Symbol) -[:INTRAFILE {type:"calls", line}]->  (:Symbol)
    Meaning : function A *calls* function B — both live in the *same* file.
    Use     : understand internal logic and call flow within a module.

  (:File)   -[:INTERFILE {type:"imports", symbols:[...], line}]->  (:File)
    Meaning : file A *imports* something from file B.
    Use     : understand the module dependency graph across the codebase.

Supported languages
  Python  — full (AST-based: symbols + calls + imports, relative import resolution)
  JS/TS   — partial (regex-based: symbols + imports only)
"""
from __future__ import annotations

import ast
import logging
import os
import re
from dataclasses import dataclass, field

from src.db.neo4j_session import get_session

logger = logging.getLogger(__name__)

# ── Language detection ────────────────────────────────────────────────────────

_PYTHON_EXTS = {".py"}
_JS_EXTS     = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_SKIP_DIRS   = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", "target", ".mypy_cache", ".pytest_cache",
}

_MAX_FILE_BYTES = 300_000   # skip files > 300 KB (generated code, etc.)
_MAX_FILES      = 500       # cap total files per repo


def _language(ext: str) -> str | None:
    if ext in _PYTHON_EXTS:
        return "python"
    if ext in _JS_EXTS:
        return "javascript"
    return None


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class SymbolInfo:
    name: str
    kind: str        # "function" | "class" | "method"
    line: int


@dataclass
class ImportInfo:
    module: str      # raw module string from the source
    symbols: list[str]
    is_relative: bool
    level: int       # Python relative import level (0 = absolute)
    line: int
    resolved_rel_path: str | None = None   # filled in after resolution


@dataclass
class CallInfo:
    caller: str      # symbol name of the caller
    callee: str      # symbol name of the callee
    line: int


@dataclass
class FileParseResult:
    rel_path: str
    language: str
    symbols: list[SymbolInfo] = field(default_factory=list)
    imports: list[ImportInfo] = field(default_factory=list)
    calls:   list[CallInfo]   = field(default_factory=list)


# ── Python parser ─────────────────────────────────────────────────────────────

class _PythonVisitor(ast.NodeVisitor):
    """Walk a Python AST and extract symbols, imports, and intrafile calls."""

    def __init__(self):
        self.symbols: list[SymbolInfo] = []
        self.imports: list[ImportInfo] = []
        self.calls:   list[CallInfo]   = []
        self._defined: set[str] = set()   # names defined in this file
        self._scope_stack: list[str] = [] # current enclosing function names

    # ── Symbols ────────────────────────────────────

    def _enter_func(self, node: ast.FunctionDef):
        parent_name = self._scope_stack[-1] if self._scope_stack else None
        kind = "method" if parent_name and not isinstance(
            ast.parse("class X:\n pass").body[0], ast.ClassDef
        ) else "function"
        # Simplification: if we're inside a ClassDef mark as method
        kind = "method" if self._in_class else "function"
        self.symbols.append(SymbolInfo(name=node.name, kind=kind, line=node.lineno))
        self._defined.add(node.name)
        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_FunctionDef(self, node):      self._enter_func(node)
    def visit_AsyncFunctionDef(self, node): self._enter_func(node)

    _in_class = False

    def visit_ClassDef(self, node):
        self.symbols.append(SymbolInfo(name=node.name, kind="class", line=node.lineno))
        self._defined.add(node.name)
        prev = self._in_class
        self._in_class = True
        self.generic_visit(node)
        self._in_class = prev

    # ── Imports ────────────────────────────────────

    def visit_Import(self, node):
        for alias in node.names:
            self.imports.append(ImportInfo(
                module=alias.name, symbols=[],
                is_relative=False, level=0, line=node.lineno,
            ))

    def visit_ImportFrom(self, node):
        if node.module:
            self.imports.append(ImportInfo(
                module=node.module,
                symbols=[a.name for a in node.names if a.name != "*"],
                is_relative=(node.level or 0) > 0,
                level=node.level or 0,
                line=node.lineno,
            ))

    # ── Intrafile calls ────────────────────────────

    def visit_Call(self, node):
        if self._scope_stack:
            caller = self._scope_stack[-1]
            callee = None
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr
            if callee and callee in self._defined and callee != caller:
                self.calls.append(CallInfo(caller=caller, callee=callee, line=node.lineno))
        self.generic_visit(node)


def _parse_python(abs_path: str, rel_path: str, repo_path: str) -> FileParseResult:
    result = FileParseResult(rel_path=rel_path, language="python")
    try:
        src = open(abs_path, encoding="utf-8", errors="ignore").read()
        tree = ast.parse(src, filename=abs_path)
        visitor = _PythonVisitor()
        visitor.visit(tree)
        result.symbols = visitor.symbols
        result.calls   = visitor.calls

        # Resolve imports to actual file paths
        for imp in visitor.imports:
            imp.resolved_rel_path = _resolve_python_import(
                rel_path, imp.module, imp.level, repo_path
            )
        result.imports = visitor.imports

    except SyntaxError as e:
        logger.debug("Python SyntaxError in %s: %s", rel_path, e)
    except Exception as e:
        logger.debug("Failed to parse %s: %s", rel_path, e)
    return result


def _resolve_python_import(
    importing_rel: str, module: str, level: int, repo_path: str
) -> str | None:
    """
    Try to map a Python import statement to an actual file within the repo.
    Returns the relative path if found, None if it's an external package.
    """
    if level > 0:
        # Relative import: go `level` directories up from the importing file
        base = os.path.dirname(os.path.join(repo_path, importing_rel))
        for _ in range(level - 1):
            base = os.path.dirname(base)
        module_path = os.path.join(base, module.replace(".", os.sep))
    else:
        module_path = os.path.join(repo_path, module.replace(".", os.sep))

    for candidate in [
        f"{module_path}.py",
        os.path.join(module_path, "__init__.py"),
    ]:
        if os.path.isfile(candidate):
            return os.path.relpath(candidate, repo_path).replace(os.sep, "/")
    return None


# ── JS / TS parser (regex-based) ──────────────────────────────────────────────

_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[\w*{},\s]+\s+from\s+)?|require\s*\(\s*)['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_JS_FUNC_RE = re.compile(
    r"""(?:^|\s)(?:function\s+([\w$]+)|const\s+([\w$]+)\s*=\s*(?:async\s+)?(?:\(|[\w$]+\s*=>))""",
    re.MULTILINE,
)


def _parse_js(abs_path: str, rel_path: str, repo_path: str) -> FileParseResult:
    result = FileParseResult(rel_path=rel_path, language="javascript")
    try:
        src = open(abs_path, encoding="utf-8", errors="ignore").read()

        # Symbols
        for m in _JS_FUNC_RE.finditer(src):
            name = m.group(1) or m.group(2)
            if name:
                line = src[: m.start()].count("\n") + 1
                result.symbols.append(SymbolInfo(name=name, kind="function", line=line))

        # Imports → resolve relative paths only
        for m in _JS_IMPORT_RE.finditer(src):
            spec = m.group(1)
            is_rel = spec.startswith(".")
            line = src[: m.start()].count("\n") + 1
            resolved = _resolve_js_import(rel_path, spec, repo_path) if is_rel else None
            result.imports.append(ImportInfo(
                module=spec, symbols=[], is_relative=is_rel,
                level=0, line=line, resolved_rel_path=resolved,
            ))

    except Exception as e:
        logger.debug("Failed to parse JS/TS %s: %s", rel_path, e)
    return result


def _resolve_js_import(importing_rel: str, spec: str, repo_path: str) -> str | None:
    base = os.path.dirname(os.path.join(repo_path, importing_rel))
    candidate = os.path.normpath(os.path.join(base, spec))

    extensions = ["", ".js", ".jsx", ".ts", ".tsx", "/index.js",
                  "/index.jsx", "/index.ts", "/index.tsx"]
    for ext in extensions:
        full = candidate + ext
        if os.path.isfile(full):
            return os.path.relpath(full, repo_path).replace(os.sep, "/")
    return None


# ── Repo walker ───────────────────────────────────────────────────────────────

def _collect_files(repo_path: str) -> list[tuple[str, str]]:
    """Walk repo and return (abs_path, rel_path) for supported source files."""
    results = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if _language(ext) is None:
                continue
            abs_path = os.path.join(root, fname)
            if os.path.getsize(abs_path) > _MAX_FILE_BYTES:
                continue
            rel_path = os.path.relpath(abs_path, repo_path).replace(os.sep, "/")
            results.append((abs_path, rel_path))
            if len(results) >= _MAX_FILES:
                logger.warning("Repo hit _MAX_FILES cap (%d) — truncating walk", _MAX_FILES)
                return results
    return results


# ── Neo4j writes ─────────────────────────────────────────────────────────────

_MERGE_REPO = """
MERGE (r:Repository {repo_id: $repo_id})
SET r.repo_name = $repo_name, r.user_id = $user_id
"""

_MERGE_FILE = """
MERGE (f:File {file_id: $file_id})
SET f.rel_path = $rel_path, f.language = $language, f.repo_id = $repo_id
WITH f
MATCH (r:Repository {repo_id: $repo_id})
MERGE (f)-[:BELONGS_TO]->(r)
"""

_MERGE_SYMBOL = """
MERGE (s:Symbol {symbol_id: $symbol_id})
SET s.name = $name, s.kind = $kind, s.line = $line,
    s.file_id = $file_id, s.repo_id = $repo_id
WITH s
MATCH (f:File {file_id: $file_id})
MERGE (s)-[:DEFINED_IN]->(f)
"""

_MERGE_INTRAFILE = """
MATCH (a:Symbol {symbol_id: $caller_id})
MATCH (b:Symbol {symbol_id: $callee_id})
MERGE (a)-[r:INTRAFILE]->(b)
SET r.type = 'calls', r.line = $line
"""

_MERGE_INTERFILE = """
MATCH (a:File {file_id: $src_id})
MATCH (b:File {file_id: $dst_id})
MERGE (a)-[r:INTERFILE]->(b)
SET r.type = 'imports', r.symbols = $symbols, r.line = $line
"""

_DELETE_REPO_GRAPH = """
MATCH (n {repo_id: $repo_id})
DETACH DELETE n
"""


def _repo_id(user_id: str, repo_name: str) -> str:
    return f"{user_id}/{repo_name}"

def _file_id(repo_id: str, rel_path: str) -> str:
    return f"{repo_id}/{rel_path}"

def _symbol_id(file_id: str, name: str) -> str:
    return f"{file_id}::{name}"


# ── Public API ────────────────────────────────────────────────────────────────

def build_repo_graph(repo_path: str, user_id: str, repo_name: str) -> dict:
    """
    Parse the cloned repository and write the full graph KB to Neo4j.
    Returns a summary dict with counts.

    Graph written:
      - Repository node
      - File nodes  (one per source file)
      - Symbol nodes (functions / classes)
      - INTRAFILE edges (function → function calls, same file)
      - INTERFILE edges (file → file imports)
    """
    repo_id = _repo_id(user_id, repo_name)
    logger.info("Building graph KB | repo=%s", repo_id)

    # 1. Clear any stale graph for this repo
    with get_session() as s:
        s.run(_DELETE_REPO_GRAPH, repo_id=repo_id)

    # 2. Create Repository node
    with get_session() as s:
        s.run(_MERGE_REPO, repo_id=repo_id, repo_name=repo_name, user_id=user_id)

    # 3. Collect + parse all source files
    files = _collect_files(repo_path)
    parse_results: list[FileParseResult] = []

    for abs_path, rel_path in files:
        ext = os.path.splitext(rel_path)[1].lower()
        lang = _language(ext)
        if lang == "python":
            pr = _parse_python(abs_path, rel_path, repo_path)
        else:
            pr = _parse_js(abs_path, rel_path, repo_path)
        parse_results.append(pr)

    # 4. Write File nodes
    with get_session() as s:
        for pr in parse_results:
            fid = _file_id(repo_id, pr.rel_path)
            s.run(_MERGE_FILE,
                  file_id=fid, rel_path=pr.rel_path,
                  language=pr.language, repo_id=repo_id)

    # 5. Write Symbol nodes
    symbol_count = 0
    with get_session() as s:
        for pr in parse_results:
            fid = _file_id(repo_id, pr.rel_path)
            for sym in pr.symbols:
                sid = _symbol_id(fid, sym.name)
                s.run(_MERGE_SYMBOL,
                      symbol_id=sid, name=sym.name, kind=sym.kind,
                      line=sym.line, file_id=fid, repo_id=repo_id)
                symbol_count += 1

    # 6. Write INTRAFILE edges (function → function calls within same file)
    intrafile_count = 0
    with get_session() as s:
        for pr in parse_results:
            fid = _file_id(repo_id, pr.rel_path)
            defined = {sym.name for sym in pr.symbols}
            for call in pr.calls:
                if call.caller in defined and call.callee in defined:
                    s.run(_MERGE_INTRAFILE,
                          caller_id=_symbol_id(fid, call.caller),
                          callee_id=_symbol_id(fid, call.callee),
                          line=call.line)
                    intrafile_count += 1

    # 7. Write INTERFILE edges (file → file imports)
    interfile_count = 0
    known_file_ids = {_file_id(repo_id, pr.rel_path) for pr in parse_results}
    with get_session() as s:
        for pr in parse_results:
            src_fid = _file_id(repo_id, pr.rel_path)
            for imp in pr.imports:
                if not imp.resolved_rel_path:
                    continue   # external package — skip
                dst_fid = _file_id(repo_id, imp.resolved_rel_path)
                if dst_fid not in known_file_ids:
                    continue   # target file not in our parse set
                s.run(_MERGE_INTERFILE,
                      src_id=src_fid, dst_id=dst_fid,
                      symbols=imp.symbols, line=imp.line)
                interfile_count += 1

    summary = {
        "repo_id":   repo_id,
        "files":     len(parse_results),
        "symbols":   symbol_count,
        "intrafile": intrafile_count,
        "interfile": interfile_count,
    }
    logger.info("Graph KB built | %s", summary)
    return summary


def clear_repo_graph(user_id: str, repo_name: str) -> None:
    """Delete all Neo4j nodes and edges for a specific repo."""
    repo_id = _repo_id(user_id, repo_name)
    with get_session() as s:
        s.run(_DELETE_REPO_GRAPH, repo_id=repo_id)
    logger.info("Graph KB cleared | repo=%s", repo_id)


def query_interfile(user_id: str, repo_name: str) -> list[dict]:
    """
    Return all INTERFILE (file→file import) edges for a repo.
    Each dict: {source, target, symbols, line}
    """
    repo_id = _repo_id(user_id, repo_name)
    cypher = """
    MATCH (a:File {repo_id: $repo_id})-[r:INTERFILE]->(b:File {repo_id: $repo_id})
    RETURN a.rel_path AS source, b.rel_path AS target,
           r.symbols AS symbols, r.line AS line
    ORDER BY a.rel_path, b.rel_path
    """
    with get_session() as s:
        return [dict(row) for row in s.run(cypher, repo_id=repo_id)]


def query_intrafile(user_id: str, repo_name: str, file_rel_path: str | None = None) -> list[dict]:
    """
    Return all INTRAFILE (function→function call) edges for a repo.
    Optionally filter to a single file.
    Each dict: {caller, callee, caller_file, line}
    """
    repo_id = _repo_id(user_id, repo_name)
    if file_rel_path:
        file_id = _file_id(repo_id, file_rel_path)
        cypher = """
        MATCH (a:Symbol {file_id: $file_id})-[r:INTRAFILE]->(b:Symbol {file_id: $file_id})
        RETURN a.name AS caller, b.name AS callee,
               a.file_id AS caller_file, r.line AS line
        ORDER BY r.line
        """
        params = {"file_id": file_id}
    else:
        cypher = """
        MATCH (a:Symbol {repo_id: $repo_id})-[r:INTRAFILE]->(b:Symbol {repo_id: $repo_id})
        MATCH (f:File {file_id: a.file_id})
        RETURN a.name AS caller, b.name AS callee,
               f.rel_path AS caller_file, r.line AS line
        ORDER BY f.rel_path, r.line
        """
        params = {"repo_id": repo_id}
    with get_session() as s:
        return [dict(row) for row in s.run(cypher, **params)]


def query_symbol_callers(user_id: str, repo_name: str, symbol_name: str) -> list[dict]:
    """Who calls a given function anywhere in the repo?"""
    repo_id = _repo_id(user_id, repo_name)
    cypher = """
    MATCH (a:Symbol {repo_id: $repo_id})-[:INTRAFILE]->(b:Symbol {name: $name, repo_id: $repo_id})
    MATCH (fa:File {file_id: a.file_id})
    RETURN a.name AS caller, fa.rel_path AS file
    ORDER BY fa.rel_path
    """
    with get_session() as s:
        return [dict(row) for row in s.run(cypher, repo_id=repo_id, name=symbol_name)]


def query_file_dependents(user_id: str, repo_name: str, rel_path: str) -> list[dict]:
    """Which files import a given file?"""
    repo_id = _repo_id(user_id, repo_name)
    file_id = _file_id(repo_id, rel_path)
    cypher = """
    MATCH (a:File {repo_id: $repo_id})-[:INTERFILE]->(b:File {file_id: $file_id})
    RETURN a.rel_path AS importer
    ORDER BY a.rel_path
    """
    with get_session() as s:
        return [dict(row) for row in s.run(cypher, repo_id=repo_id, file_id=file_id)]
