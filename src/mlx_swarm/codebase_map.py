"""Bounded workspace graph for the New task skill map."""
# @lat: [[UI]]

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAP_SCHEMA_VERSION = 2
MAX_MAP_NODES = 300
MAX_MAP_DEPTH = 6
MAX_FOCUS_PATHS = 32
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
SKIP_FEATURE_SLUGS = {"index", "lat"}
LAT_TAG_RE = re.compile(r"@lat:\s*(.+)$", re.MULTILINE)
WIKI_LINK_RE = re.compile(
    r"\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|[^\]]*)?\]\]"
)
SOURCE_SUFFIXES = {
    ".cjs",
    ".go",
    ".js",
    ".md",
    ".mjs",
    ".py",
    ".rs",
    ".ts",
    ".tsx",
}


class CodebaseMapError(RuntimeError):
    """Raised when a workspace cannot be mapped."""


def build_codebase_map(workspace_root: Path) -> dict[str, Any]:
    """Return a feature graph when lat.md exists, else a package graph."""
    try:
        root = workspace_root.expanduser().resolve()
    except OSError as exc:
        raise CodebaseMapError(str(exc)) from exc
    if not root.is_dir():
        raise CodebaseMapError(f"Workspace folder not found: {root}")
    lat_dir = _lat_dir(root)
    if lat_dir is not None:
        return _payload(root, *_build_feature_graph(root, lat_dir), mode="features")
    return _payload(root, *_build_package_graph(root), mode="packages")


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
    preferred = [node for node in nodes if node.get("kind") in {"feature", "package"}]
    if preferred:
        nodes = preferred[:max_nodes]
    elif len(nodes) > max_nodes:
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
        if edge.get("kind") == "implements":
            continue
        lines.append(f"    {_mermaid_id(source)} --> {_mermaid_id(target)}")
    lines.append("```")
    return "\n".join(lines) + "\n"


def _payload(
    root: Path,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, str]],
    truncated: bool,
    *,
    mode: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": MAP_SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "workspaceRoot": str(root),
        "mode": mode,
        "truncated": truncated,
        "nodes": nodes,
        "edges": edges,
    }


def _build_feature_graph(
    root: Path,
    lat_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], bool]:
    features: dict[str, dict[str, Any]] = {}
    related: set[tuple[str, str]] = set()
    for page in sorted(lat_dir.glob("*.md"), key=lambda item: item.name.lower()):
        slug = _slug(page.stem)
        if slug in SKIP_FEATURE_SLUGS:
            continue
        text = _read_text(page)
        rel = _relative(page, root)
        if rel is None:
            continue
        feature_id = f"feature:{slug}"
        features[slug] = {
            "id": feature_id,
            "path": rel,
            "name": _heading(text) or page.stem.replace("-", " ").title(),
            "kind": "feature",
            "summary": _summary(text),
            "parentId": None,
            "fileCount": 0,
            "sourcePaths": [rel],
        }
        for target in _wiki_targets(text):
            if _looks_like_path(target):
                source = _existing_relative(root, target)
                if source:
                    features[slug]["sourcePaths"].append(source)
                continue
            other = _slug(target)
            if other in SKIP_FEATURE_SLUGS or other == slug:
                continue
            related.add((slug, other))

    for path, slugs in _scan_lat_tags(root):
        for slug in slugs:
            if slug not in features:
                continue
            features[slug]["sourcePaths"].append(path)

    nodes: list[dict[str, Any]] = []
    for slug, node in features.items():
        node["sourcePaths"] = _bound_paths(node["sourcePaths"])
        node["fileCount"] = max(0, len(node["sourcePaths"]) - 1)
        nodes.append(node)
        if len(nodes) >= MAX_MAP_NODES:
            return nodes, _related_edges(features, related), True

    return nodes, _related_edges(features, related), False


def _related_edges(
    features: dict[str, dict[str, Any]],
    related: set[tuple[str, str]],
) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source_slug, target_slug in sorted(related):
        if source_slug not in features or target_slug not in features:
            continue
        pair = (features[source_slug]["id"], features[target_slug]["id"])
        if pair in seen:
            continue
        seen.add(pair)
        edges.append({"source": pair[0], "target": pair[1], "kind": "related"})
    return edges


def _build_package_graph(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], bool]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    truncated = False
    root_id = "."
    nodes.append(
        {
            "id": root_id,
            "path": root_id,
            "name": root.name,
            "kind": "package" if (root / "__init__.py").is_file() else "dir",
            "summary": "Opened workspace",
            "parentId": None,
            "fileCount": 0,
            "sourcePaths": [root_id],
        }
    )

    def add_dir(*, path: str, name: str, kind: str, parent_id: str) -> bool:
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
                "summary": f"{kind.title()} {path}",
                "parentId": parent_id,
                "fileCount": 0,
                "sourcePaths": [path],
            }
        )
        edges.append({"source": parent_id, "target": path, "kind": "contains"})
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
        files = 0
        dirs: list[Path] = []
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if _skip_dir(entry.name):
                    continue
                dirs.append(entry)
            elif entry.is_file() and entry.name not in SKIP_FILE_NAMES:
                files += 1
        by_id = {node["id"]: node for node in nodes}
        parent = by_id[parent_id]
        parent["fileCount"] = files
        if parent_id != root_id and (directory / "__init__.py").is_file():
            parent["kind"] = "package"
        for child in dirs:
            rel = _relative(child, root)
            if rel is None:
                continue
            kind = "package" if (child / "__init__.py").is_file() else "dir"
            if not add_dir(path=rel, name=child.name, kind=kind, parent_id=parent_id):
                return
            walk(child, rel, depth + 1)

    walk(root, root_id, 0)
    return nodes, edges, truncated


def _scan_lat_tags(root: Path) -> list[tuple[str, list[str]]]:
    found: list[tuple[str, list[str]]] = []

    def walk(directory: Path, depth: int) -> None:
        if depth >= MAX_MAP_DEPTH:
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if _skip_dir(entry.name) or entry.name == "lat.md":
                    continue
                walk(entry, depth + 1)
                continue
            if entry.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            rel = _relative(entry, root)
            if rel is None:
                continue
            slugs = _lat_slugs(_read_text(entry, limit=4096))
            if slugs:
                found.append((rel, slugs))

    walk(root, 0)
    return found


def _lat_dir(root: Path) -> Path | None:
    candidate = root / "lat.md"
    if candidate.is_dir():
        return candidate
    return None


def _wiki_targets(text: str) -> list[str]:
    return [match.group(1).strip() for match in WIKI_LINK_RE.finditer(text)]


def _lat_slugs(text: str) -> list[str]:
    slugs: list[str] = []
    for line in LAT_TAG_RE.findall(text):
        for target in _wiki_targets(line):
            if _looks_like_path(target):
                continue
            slug = _slug(target)
            if slug and slug not in SKIP_FEATURE_SLUGS:
                slugs.append(slug)
    return list(dict.fromkeys(slugs))


def _heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _summary(text: str) -> str:
    lines: list[str] = []
    started = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if started:
                break
            continue
        if stripped.startswith("#"):
            continue
        if stripped.startswith("```"):
            break
        started = True
        lines.append(WIKI_LINK_RE.sub(lambda match: match.group(1), stripped))
        if len(" ".join(lines)) >= 180:
            break
    summary = " ".join(lines).strip()
    if len(summary) > 180:
        return summary[:177].rstrip() + "..."
    return summary


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _looks_like_path(target: str) -> bool:
    lowered = target.lower()
    return "/" in target or lowered.endswith(tuple(SOURCE_SUFFIXES))


def _existing_relative(root: Path, target: str) -> str | None:
    candidate = (root / target).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.exists():
        return None
    return _relative(candidate, root)


def _bound_paths(paths: list[str], limit: int = MAX_FOCUS_PATHS) -> list[str]:
    unique = [path for path in dict.fromkeys(paths) if path]
    if len(unique) <= limit:
        return unique
    collapsed: list[str] = []
    for path in unique:
        parent = Path(path).parent.as_posix()
        collapsed.append(parent if parent != "." else path)
    return list(dict.fromkeys(collapsed))[:limit]


def _read_text(path: Path, limit: int | None = None) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit) if limit is not None else handle.read()
    except OSError:
        return ""


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
