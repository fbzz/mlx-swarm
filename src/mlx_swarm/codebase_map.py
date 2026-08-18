"""Bounded workspace graph for the New task skill map."""
# @lat: [[UI]]

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAP_SCHEMA_VERSION = 1
MAX_MAP_NODES = 300
MAX_MAP_DEPTH = 6
SMALL_DIR_FILES = 8
MERMAID_MAX_NODES = 80
SKIP_DIR_NAMES = {
    ".claude",
    ".git",
    ".mlx-swarm",
    ".mypy_cache",
    ".pytest_cache",
    ".swarm",
    ".tox",
    ".venv",
    "build",
    "dist",
    "htmlcov",
    "models",
    "node_modules",
    "venv",
    "__pycache__",
}
SKIP_FILE_NAMES = {".DS_Store"}


class CodebaseMapError(RuntimeError):
    """Raised when a workspace cannot be mapped."""


def build_codebase_map(workspace_root: Path) -> dict[str, Any]:
    """Return a parent/child graph of directories under workspace_root."""
    try:
        root = workspace_root.expanduser().resolve()
    except OSError as exc:
        raise CodebaseMapError(str(exc)) from exc
    if not root.is_dir():
        raise CodebaseMapError(f"Workspace folder not found: {root}")

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    truncated = False
    root_id = "."
    nodes.append(
        {
            "id": root_id,
            "path": root_id,
            "name": root.name,
            "kind": "dir",
            "parentId": None,
            "fileCount": 0,
        }
    )

    def add_node(
        *,
        path: str,
        name: str,
        kind: str,
        parent_id: str,
        file_count: int = 0,
    ) -> bool:
        nonlocal truncated
        if len(nodes) >= MAX_MAP_NODES:
            truncated = True
            return False
        nodes.append(
            {
                "id": path,
                "path": path,
                "name": name,
                "kind": kind,
                "parentId": parent_id,
                "fileCount": file_count,
            }
        )
        edges.append(
            {"source": parent_id, "target": path, "kind": "contains"}
        )
        return True

    def walk(directory: Path, parent_id: str, depth: int) -> None:
        nonlocal truncated
        if truncated or depth >= MAX_MAP_DEPTH:
            if depth >= MAX_MAP_DEPTH:
                truncated = True
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            return
        files: list[Path] = []
        dirs: list[Path] = []
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if _skip_dir(entry.name):
                    continue
                dirs.append(entry)
            elif entry.is_file():
                if entry.name in SKIP_FILE_NAMES or entry.name.startswith("."):
                    continue
                files.append(entry)
        by_id = {node["id"]: node for node in nodes}
        parent = by_id[parent_id]
        include_files = len(files) <= SMALL_DIR_FILES
        parent["fileCount"] = 0 if include_files else len(files)
        if parent_id != root_id and (directory / "__init__.py").is_file():
            parent["kind"] = "package"
        if include_files:
            for file_path in files:
                rel = _relative(file_path, root)
                if rel is None:
                    continue
                if not add_node(
                    path=rel,
                    name=file_path.name,
                    kind="file",
                    parent_id=parent_id,
                ):
                    return
        for child in dirs:
            rel = _relative(child, root)
            if rel is None:
                continue
            if not add_node(
                path=rel,
                name=child.name,
                kind="dir",
                parent_id=parent_id,
            ):
                return
            walk(child, rel, depth + 1)

    walk(root, root_id, 0)
    return {
        "schemaVersion": MAP_SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "workspaceRoot": str(root),
        "truncated": truncated,
        "nodes": nodes,
        "edges": edges,
    }


def persist_codebase_map(project_root: Path, payload: dict[str, Any]) -> Path:
    """Write the map beside the project store as codebase-map.json."""
    from .app_storage import project_store_dir

    path = project_store_dir(project_root) / "codebase-map.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def render_mermaid(
    payload: dict[str, Any],
    *,
    max_nodes: int = MERMAID_MAX_NODES,
) -> str:
    """Return a fenced mermaid flowchart for host-chat display."""
    nodes = list(payload.get("nodes") or [])
    if len(nodes) > max_nodes:
        ranked = sorted(
            nodes,
            key=lambda node: (
                str(node.get("path") or node.get("id") or "").count("/"),
                0 if node.get("kind") != "file" else 1,
                str(node.get("path") or ""),
            ),
        )
        nodes = ranked[:max_nodes]
    allowed = {str(node.get("id") or "") for node in nodes}
    lines = ["```mermaid", "flowchart LR"]
    for node in nodes:
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        label = str(node.get("name") or node_id).replace('"', "'")
        drawn = _mermaid_id(node_id)
        if node.get("kind") == "file":
            lines.append(f'    {drawn}["{label}"]')
        else:
            lines.append(f'    {drawn}(["{label}"])')
    for edge in payload.get("edges") or []:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source not in allowed or target not in allowed:
            continue
        lines.append(f"    {_mermaid_id(source)} --> {_mermaid_id(target)}")
    lines.append("```")
    return "\n".join(lines) + "\n"


def _mermaid_id(path: str) -> str:
    cleaned = "".join(character if character.isalnum() else "_" for character in path)
    return f"n_{cleaned}" if cleaned else "n_root"


def _relative(path: Path, root: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    return relative.as_posix()


def _skip_dir(name: str) -> bool:
    return name in SKIP_DIR_NAMES or name.endswith(".egg-info")
