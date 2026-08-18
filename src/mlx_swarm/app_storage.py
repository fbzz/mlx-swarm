"""Project-local `.mlx-swarm` storage and a thin recent-folder catalog."""
# @lat: [[Config#Desktop application storage]]

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import SwarmConfig, load_config

APP_STORAGE_VERSION = 1
CATALOG_VERSION = 1
PROJECT_STORE_DIR = ".mlx-swarm"
UI_STATE_VERSION = 1
TERMINAL_SPLIT_DEFAULT = 0.5
TERMINAL_SPLIT_MIN = 0.25
TERMINAL_SPLIT_MAX = 0.75
MAX_FOCUS_PATHS = 32
MAX_FOCUS_PATH_CHARS = 512


def default_app_root() -> Path:
    """Return the per-user directory for recent-folder paths only."""
    override = os.environ.get("MLX_SWARM_APP_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys_platform() == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "mlx-swarm"
        ).resolve()
    return (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        / "mlx-swarm"
    ).expanduser().resolve()


def sys_platform() -> str:
    """Isolate platform lookup for deterministic tests."""
    import sys

    return sys.platform


def workspace_id(workspace_root: Path) -> str:
    """Return a stable, non-reversible identifier for a workspace path."""
    canonical = str(workspace_root.expanduser().resolve()).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:20]


def project_store_dir(project_root: Path) -> Path:
    """Return `<project>/.mlx-swarm`."""
    return project_root.expanduser().resolve() / PROJECT_STORE_DIR


def project_root_for_config(config: SwarmConfig) -> Path:
    """Return the folder the operator opened, not the `.mlx-swarm` config dir."""
    parent = config.source.parent.resolve()
    if parent.name == PROJECT_STORE_DIR:
        return parent.parent
    return parent


def workspace_root_for_config(config: SwarmConfig) -> Path:
    """Resolve the Git workspace, falling back to the opened project folder."""
    project_root = project_root_for_config(config)
    if config.workspace is not None:
        from .workspace import WorkspaceError, discover_git_root

        try:
            return discover_git_root(project_root)
        except WorkspaceError:
            return project_root
    return project_root


def legacy_global_runs(
    workspace_root: Path,
    *,
    app_root: Path | None = None,
) -> Path:
    """Historical Application Support run tree used only for one-shot migrate."""
    return (
        (app_root or default_app_root())
        / "workspaces"
        / workspace_id(workspace_root)
        / "runs"
    )


def config_for_app(
    config: SwarmConfig,
    *,
    app_root: Path | None = None,
) -> SwarmConfig:
    """Keep artifacts relative to the config file; do not project into app support."""
    del app_root
    return config


def clamp_terminal_split(value: Any) -> float:
    """Keep the New task terminal pane between 25% and 75% of the surface."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return TERMINAL_SPLIT_DEFAULT
    parsed = float(value)
    if parsed != parsed:
        return TERMINAL_SPLIT_DEFAULT
    return min(TERMINAL_SPLIT_MAX, max(TERMINAL_SPLIT_MIN, parsed))


def normalize_focus_paths(value: Any) -> list[str]:
    """Return workspace-relative focus paths, or raise ValueError."""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("focusPaths must be an array of strings.")
    if len(value) > MAX_FOCUS_PATHS:
        raise ValueError(f"focusPaths cannot exceed {MAX_FOCUS_PATHS} entries.")
    paths: list[str] = []
    for item in value:
        stripped = item.strip().replace("\\", "/")
        if not stripped:
            continue
        if len(stripped) > MAX_FOCUS_PATH_CHARS:
            raise ValueError("focusPaths entry is too long.")
        candidate = Path(stripped)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("focusPaths must stay inside the workspace.")
        paths.append(candidate.as_posix())
    return paths


def load_ui_state(project_root: Path) -> dict[str, Any]:
    """Load per-project UI preferences from `.mlx-swarm/ui-state.json`."""
    path = project_store_dir(project_root) / "ui-state.json"
    default = {
        "schemaVersion": UI_STATE_VERSION,
        "terminalVisible": True,
        "terminalSplit": TERMINAL_SPLIT_DEFAULT,
        "focusPaths": [],
    }
    if not path.is_file():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    if not isinstance(value, dict):
        return default
    visible = value.get("terminalVisible", True)
    try:
        focus_paths = normalize_focus_paths(value.get("focusPaths", []))
    except ValueError:
        focus_paths = []
    return {
        "schemaVersion": UI_STATE_VERSION,
        "terminalVisible": visible is not False,
        "terminalSplit": clamp_terminal_split(value.get("terminalSplit")),
        "focusPaths": focus_paths,
    }


def save_ui_state(
    project_root: Path,
    *,
    terminal_visible: bool | None = None,
    terminal_split: float | None = None,
    focus_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Persist per-project UI preferences next to the project store."""
    current = load_ui_state(project_root)
    if terminal_visible is not None:
        current["terminalVisible"] = bool(terminal_visible)
    if terminal_split is not None:
        current["terminalSplit"] = clamp_terminal_split(terminal_split)
    if focus_paths is not None:
        current["focusPaths"] = normalize_focus_paths(focus_paths)
    value = {
        "schemaVersion": UI_STATE_VERSION,
        "terminalVisible": current["terminalVisible"],
        "terminalSplit": current["terminalSplit"],
        "focusPaths": list(current["focusPaths"]),
    }
    _atomic_json(project_store_dir(project_root) / "ui-state.json", value)
    return value


def ensure_project_store(
    folder: Path,
    *,
    app_root: Path | None = None,
) -> Path:
    """Create or migrate `<folder>/.mlx-swarm` and return its swarm.json path."""
    project_root = folder.expanduser().resolve()
    if not project_root.is_dir():
        raise FileNotFoundError(f"Project folder not found: {project_root}")
    store = project_store_dir(project_root)
    store.mkdir(parents=True, exist_ok=True)
    config_path = store / "swarm.json"
    if not config_path.is_file():
        legacy = project_root / "swarm.json"
        if legacy.is_file():
            _migrate_legacy_config(legacy, config_path)
        else:
            _atomic_json(config_path, _starter_config(project_root))
    _migrate_runs(project_root, app_root=app_root)
    return config_path


def open_project_config(
    folder: Path,
    *,
    app_root: Path | None = None,
) -> SwarmConfig:
    """Load the project store for an opened folder after migrate/create."""
    return load_config(ensure_project_store(folder, app_root=app_root))


def register_workspace(
    config: SwarmConfig,
    *,
    app_root: Path | None = None,
) -> dict[str, Any]:
    """Record the opened folder path for Open Recent. Run data stays in-project."""
    root = (app_root or default_app_root()).resolve()
    workspace_root = workspace_root_for_config(config)
    identifier = workspace_id(workspace_root)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    meta = {
        "schemaVersion": APP_STORAGE_VERSION,
        "workspaceId": identifier,
        "name": workspace_root.name,
        "workspaceRoot": str(workspace_root),
        "configPath": str(config.source.resolve()),
        "artifactsDir": str(config.artifacts_dir.resolve()),
        "lastOpenedAt": now,
    }
    catalog_path = root / "catalog.json"
    catalog = _read_catalog(catalog_path)
    workspaces = {
        item["workspaceId"]: item
        for item in catalog.get("workspaces", [])
        if isinstance(item, dict) and isinstance(item.get("workspaceId"), str)
    }
    workspaces[identifier] = meta
    value = {
        "schemaVersion": CATALOG_VERSION,
        "updatedAt": now,
        "workspaces": sorted(
            workspaces.values(),
            key=lambda item: item.get("lastOpenedAt", ""),
            reverse=True,
        ),
    }
    _atomic_json(catalog_path, value)
    return meta


def load_catalog(app_root: Path | None = None) -> dict[str, Any]:
    """Load the recent-folder catalog without scanning unrelated folders."""
    root = (app_root or default_app_root()).resolve()
    return _read_catalog(root / "catalog.json")


def discover_app_config(
    start: Path | None = None,
    *,
    app_root: Path | None = None,
) -> Path | None:
    """Find `.mlx-swarm/swarm.json`, then a legacy swarm.json, then the catalog."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        nested = directory / PROJECT_STORE_DIR / "swarm.json"
        if nested.is_file():
            return nested
        legacy = directory / "swarm.json"
        if legacy.is_file():
            return legacy
    for item in load_catalog(app_root).get("workspaces", []):
        if not isinstance(item, dict):
            continue
        candidate = item.get("configPath")
        if isinstance(candidate, str) and Path(candidate).is_file():
            return Path(candidate).resolve()
    return None


def _starter_config(project_root: Path) -> dict[str, Any]:
    write_roots: list[str] = []
    for child in sorted(project_root.iterdir(), key=lambda path: path.name):
        if child.name.startswith("."):
            continue
        if child.is_dir() or child.suffix.lower() in {".md", ".toml"}:
            write_roots.append(child.name)
        if len(write_roots) >= 32:
            break
    if not write_roots:
        write_roots = ["."]
    return {
        "schemaVersion": 2,
        "model": {
            "repository": "mlx-community/Qwen3.6-35B-A3B-4bit",
            "revision": "",
        },
        "batch": {
            "maxWorkers": 2,
            "prefillStepSize": 1024,
            "maxPromptCharacters": 80000,
            "maxBatchPromptTokens": 49152,
        },
        "artifacts": "runs",
        "workspace": {
            "writeRoots": write_roots,
            "verificationProfiles": {},
        },
        "enableThinking": False,
        "reasoningEffort": "low",
        "seed": 20260727,
    }


def _migrate_legacy_config(legacy: Path, destination: Path) -> None:
    try:
        raw = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    artifacts = raw.get("artifacts", "runs")
    if isinstance(artifacts, str) and not Path(artifacts).expanduser().is_absolute():
        raw["artifacts"] = "runs"
    _atomic_json(destination, raw)


def _migrate_runs(
    project_root: Path,
    *,
    app_root: Path | None = None,
) -> None:
    destination = project_store_dir(project_root) / "runs"
    if _has_entries(destination):
        return
    sources = [
        project_root / ".swarm" / "runs",
        legacy_global_runs(project_root, app_root=app_root),
    ]
    for source in sources:
        if not source.is_dir() or not _has_entries(source):
            continue
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return


def _has_entries(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir())


def _read_catalog(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schemaVersion": CATALOG_VERSION, "workspaces": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schemaVersion": CATALOG_VERSION, "workspaces": []}
    if not isinstance(value, dict) or value.get("schemaVersion") != CATALOG_VERSION:
        return {"schemaVersion": CATALOG_VERSION, "workspaces": []}
    if not isinstance(value.get("workspaces"), list):
        value["workspaces"] = []
    return value


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
