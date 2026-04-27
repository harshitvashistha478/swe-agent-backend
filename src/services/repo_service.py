"""
Repo service — builds LLM-ready context from a cloned repository on disk.
"""
from __future__ import annotations

import os
import logging

logger = logging.getLogger(__name__)

# Directories that bloat the tree without adding useful context
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", "target", ".idea", ".vscode",
    "coverage", ".pytest_cache", ".mypy_cache", "htmlcov",
}

# Key config / manifest files worth reading in full
_KEY_FILES = [
    "README.md", "README.rst", "README.txt",
    "package.json", "package-lock.json",
    "requirements.txt", "requirements.in", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "Makefile", "docker-compose.yml", "Dockerfile",
    ".env.example", "tsconfig.json", ".eslintrc.json",
]

_MAX_TREE_ENTRIES = 200
_README_CHARS     = 4_000
_FILE_CHARS       = 1_500   # per key config file
_MAX_CONTEXT_CHARS = 12_000  # hard cap sent to the LLM


def _build_tree(repo_path: str) -> str:
    """Return a text directory tree, skipping noise folders, max 3 levels deep."""
    lines: list[str] = []
    count = 0

    for root, dirs, files in os.walk(repo_path):
        # Compute depth relative to repo root
        rel = os.path.relpath(root, repo_path)
        depth = 0 if rel == "." else rel.count(os.sep) + 1

        if depth > 3:
            dirs.clear()
            continue

        # Prune noisy directories in-place so os.walk skips them
        dirs[:] = sorted(
            d for d in dirs
            if d not in _SKIP_DIRS and not d.startswith(".")
        )

        indent = "  " * depth
        folder_name = os.path.basename(root) if root != repo_path else "."
        if root != repo_path:
            lines.append(f"{indent}{folder_name}/")

        for fname in sorted(files)[:30]:          # at most 30 files per dir
            lines.append(f"{'  ' * (depth + 1)}{fname}")
            count += 1
            if count >= _MAX_TREE_ENTRIES:
                lines.append("  ... (truncated)")
                return "\n".join(lines)

    return "\n".join(lines)


def _read_file_safe(path: str, max_chars: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(max_chars)
    except Exception as exc:
        logger.debug("Could not read %s: %s", path, exc)
        return ""


def build_repo_context(repo_path: str) -> str:
    """
    Assemble a multi-section text context from a cloned repo for use as
    an LLM system-prompt payload.

    Sections (in order):
      1. Directory tree
      2. README (first found)
      3. Key config / manifest files
    """
    parts: list[str] = []

    # ── 1. Tree ──────────────────────────────────────────
    tree = _build_tree(repo_path)
    parts.append(f"## Directory Structure\n```\n{tree}\n```")

    # ── 2. README ────────────────────────────────────────
    readme_found = False
    for fname in ("README.md", "README.rst", "README.txt", "README"):
        rpath = os.path.join(repo_path, fname)
        if os.path.isfile(rpath):
            content = _read_file_safe(rpath, _README_CHARS)
            if content:
                parts.append(f"## {fname}\n{content}")
                readme_found = True
            break
    if not readme_found:
        parts.append("## README\n_(No README found in repository root)_")

    # ── 3. Key config files ──────────────────────────────
    for fname in _KEY_FILES:
        if fname.startswith("README"):
            continue                          # already handled above
        fpath = os.path.join(repo_path, fname)
        if os.path.isfile(fpath):
            content = _read_file_safe(fpath, _FILE_CHARS)
            if content:
                parts.append(f"## {fname}\n```\n{content}\n```")

    # ── Final assembly with hard cap ─────────────────────
    full = "\n\n".join(parts)
    if len(full) > _MAX_CONTEXT_CHARS:
        full = full[:_MAX_CONTEXT_CHARS] + "\n\n_(context truncated)_"

    return full
