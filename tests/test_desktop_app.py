"""Tests for project-local `.mlx-swarm` storage and immutable review projections."""
# @lat: [[Tests#Desktop app]]

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mlx_swarm.app_storage import (
    PROJECT_STORE_DIR,
    config_for_app,
    discover_app_config,
    ensure_project_store,
    load_catalog,
    load_ui_state,
    project_root_for_config,
    register_workspace,
    save_ui_state,
    workspace_id,
)
from mlx_swarm.contracts import load_config
from mlx_swarm.review import load_attempts, parse_unified_diff


def _config(tmp_path: Path, artifacts: str = ".swarm/runs") -> Path:
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}", encoding="utf-8")
    path = tmp_path / "swarm.json"
    path.write_text(
        json.dumps({
            "schemaVersion": 1,
            "model": {
                "repository": "local/test",
                "localPath": str(model),
            },
            "batch": {},
            "artifacts": artifacts,
        }),
        encoding="utf-8",
    )
    return path


def test_app_config_keeps_artifacts_beside_the_config(tmp_path: Path) -> None:
    store = tmp_path / PROJECT_STORE_DIR
    store.mkdir()
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}", encoding="utf-8")
    path = store / "swarm.json"
    path.write_text(
        json.dumps({
            "schemaVersion": 1,
            "model": {
                "repository": "local/test",
                "localPath": str(model),
            },
            "batch": {},
            "artifacts": "runs",
        }),
        encoding="utf-8",
    )
    config = load_config(path)

    adapted = config_for_app(config, app_root=tmp_path / "Application Support")

    assert adapted.artifacts_dir == (store / "runs").resolve()
    assert project_root_for_config(adapted) == tmp_path.resolve()


def test_absolute_artifacts_override_project_store(tmp_path: Path) -> None:
    explicit = tmp_path / "custom-runs"
    config = load_config(_config(tmp_path, str(explicit)))

    assert config_for_app(
        config,
        app_root=tmp_path / "global",
    ).artifacts_dir == explicit


def test_ensure_project_store_migrates_legacy_runs_and_config(
    tmp_path: Path,
) -> None:
    _config(tmp_path)
    legacy_runs = tmp_path / ".swarm" / "runs" / "plan" / "session"
    legacy_runs.mkdir(parents=True)
    (legacy_runs / "session.json").write_text("{}", encoding="utf-8")
    app_root = tmp_path / "app"

    config_path = ensure_project_store(tmp_path, app_root=app_root)
    config = load_config(config_path)

    assert config_path == tmp_path / PROJECT_STORE_DIR / "swarm.json"
    assert config.artifacts_dir == (
        tmp_path / PROJECT_STORE_DIR / "runs"
    ).resolve()
    assert (
        tmp_path / PROJECT_STORE_DIR / "runs" / "plan" / "session" / "session.json"
    ).is_file()
    assert discover_app_config(tmp_path / "src", app_root=app_root) == config_path


def test_ensure_project_store_copies_legacy_global_cache(
    tmp_path: Path,
) -> None:
    global_runs = (
        tmp_path / "app" / "workspaces" / workspace_id(tmp_path) / "runs" / "old"
    )
    global_runs.mkdir(parents=True)
    (global_runs / "session.json").write_text("{}", encoding="utf-8")

    ensure_project_store(tmp_path, app_root=tmp_path / "app")

    assert (
        tmp_path / PROJECT_STORE_DIR / "runs" / "old" / "session.json"
    ).is_file()


def test_catalog_registers_recent_folder_without_moving_runs(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "app"
    config = load_config(ensure_project_store(tmp_path, app_root=app_root))

    metadata = register_workspace(config, app_root=app_root)
    catalog = load_catalog(app_root)

    assert catalog["workspaces"] == [metadata]
    assert metadata["configPath"] == str(
        (tmp_path / PROJECT_STORE_DIR / "swarm.json").resolve()
    )
    assert not (app_root / "workspaces").exists()


def test_ui_state_round_trips_terminal_visibility(tmp_path: Path) -> None:
    ensure_project_store(tmp_path)

    assert load_ui_state(tmp_path)["terminalVisible"] is True
    assert load_ui_state(tmp_path)["terminalSplit"] == 0.5
    save_ui_state(tmp_path, terminal_visible=False)
    assert load_ui_state(tmp_path)["terminalVisible"] is False
    assert load_ui_state(tmp_path)["terminalSplit"] == 0.5
    save_ui_state(tmp_path, terminal_split=0.2)
    assert load_ui_state(tmp_path)["terminalSplit"] == 0.25
    save_ui_state(tmp_path, terminal_split=0.6)
    assert load_ui_state(tmp_path)["terminalVisible"] is False
    assert load_ui_state(tmp_path)["terminalSplit"] == 0.6
    assert load_ui_state(tmp_path)["focusPaths"] == []
    save_ui_state(tmp_path, focus_paths=["src/pkg"])
    assert load_ui_state(tmp_path)["focusPaths"] == ["src/pkg"]


def test_parse_unified_diff_exposes_file_hunks_and_counts() -> None:
    parsed = parse_unified_diff(
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-before\n"
        "+after\n"
        " same\n"
    )

    assert parsed["summary"] == {"files": 1, "additions": 1, "deletions": 1}
    assert parsed["files"][0]["hunks"][0]["lines"][0] == {
        "type": "delete",
        "oldLine": 1,
        "newLine": None,
        "text": "before",
    }


def test_attempt_loader_requires_confinement_and_matching_digests(
    tmp_path: Path,
) -> None:
    session = tmp_path / "session"
    attempt = session / "attempts" / "edit" / "attempt-001.json"
    attempt.parent.mkdir(parents=True)
    prompt = "Implement the exact edit."
    output = '{"edits":[]}'
    record = {
        "attempt": 1,
        "phase": "generation",
        "prompt": prompt,
        "output": output,
        "normalizedOutput": output,
    }
    attempt.write_text(json.dumps(record), encoding="utf-8")
    state = {
        "generationAttempts": [{
            "path": "attempts/edit/attempt-001.json",
            "promptSha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "outputSha256": hashlib.sha256(output.encode()).hexdigest(),
        }],
        "reasoningAttempts": [{
            "path": "../../outside.json",
            "promptSha256": "0" * 64,
            "outputSha256": "0" * 64,
        }],
    }

    payload = load_attempts(session, "edit", state)

    assert len(payload["attempts"]) == 1
    assert payload["attempts"][0]["prompt"] == prompt
