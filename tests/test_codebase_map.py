"""Tests for the bounded New task codebase graph."""
# @lat: [[Tests#UI]]

from __future__ import annotations

from pathlib import Path

from mlx_swarm.codebase_map import (
    MAX_MAP_NODES,
    build_codebase_map,
    persist_codebase_map,
    render_mermaid,
)


def test_codebase_map_skips_generated_dirs_and_collapses_large_folders(
    tmp_path: Path,
) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "node_modules" / "left-pad").mkdir(parents=True)
    (tmp_path / "node_modules" / "left-pad" / "index.js").write_text(
        "module.exports = 1\n",
        encoding="utf-8",
    )
    (tmp_path / ".git").mkdir()
    (tmp_path / ".mlx-swarm").mkdir()
    (tmp_path / ".swarm").mkdir()
    (tmp_path / ".swarm" / "runs").mkdir()
    bulky = tmp_path / "vendor"
    bulky.mkdir()
    for index in range(12):
        (bulky / f"file-{index}.txt").write_text("n\n", encoding="utf-8")

    payload = build_codebase_map(tmp_path)
    paths = {node["path"] for node in payload["nodes"]}
    kinds = {node["path"]: node["kind"] for node in payload["nodes"]}

    assert payload["workspaceRoot"] == str(tmp_path.resolve())
    assert payload["mode"] == "packages"
    assert payload["truncated"] is False
    assert "src" in paths
    assert "src/pkg" in paths
    assert kinds["src/pkg"] == "package"
    assert "src/pkg/mod.py" not in paths
    assert "node_modules" not in paths
    assert ".git" not in paths
    assert ".mlx-swarm" not in paths
    assert ".swarm" not in paths
    assert "vendor" in paths
    assert not any(path.startswith("vendor/") for path in paths)
    vendor = next(node for node in payload["nodes"] if node["path"] == "vendor")
    assert vendor["fileCount"] == 12
    assert vendor["sourcePaths"] == ["vendor"]
    assert any(
        edge["source"] == "src" and edge["target"] == "src/pkg"
        for edge in payload["edges"]
    )


def test_codebase_map_caps_nodes_and_persists(
    tmp_path: Path,
) -> None:
    for index in range(MAX_MAP_NODES + 20):
        folder = tmp_path / f"d{index}"
        folder.mkdir()
        (folder / "a.txt").write_text("x\n", encoding="utf-8")

    payload = build_codebase_map(tmp_path)
    assert payload["truncated"] is True
    assert len(payload["nodes"]) <= MAX_MAP_NODES
    path = persist_codebase_map(tmp_path, payload)
    assert path == tmp_path / ".mlx-swarm" / "codebase-map.json"
    assert path.is_file()


def test_codebase_map_mermaid_shows_parent_child_edges(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    diagram = render_mermaid(build_codebase_map(tmp_path))
    assert diagram.startswith("```mermaid\nflowchart LR\n")
    assert 'n_src(["src"])' in diagram
    assert "n_src_main_py" not in diagram
    assert "n_. --> n_src" in diagram or "n__ --> n_src" in diagram
    assert diagram.rstrip().endswith("```")


def test_codebase_map_uses_lat_features_not_source_files(
    tmp_path: Path,
) -> None:
    lat = tmp_path / "lat.md"
    lat.mkdir()
    (lat / "index.md").write_text("# Index\n\n- [[Commander]]\n", encoding="utf-8")
    (lat / "commander.md").write_text(
        "# Commander\n\nFrontier planning.\n\nSee [[Plans]] and [[src/app.py]].\n",
        encoding="utf-8",
    )
    (lat / "plans.md").write_text("# Plans\n\nTask DAG.\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "# @lat: [[Commander]]\nprint(1)\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "other.py").write_text("print(2)\n", encoding="utf-8")

    payload = build_codebase_map(tmp_path)
    by_name = {node["name"]: node for node in payload["nodes"]}
    kinds = {node["kind"] for node in payload["nodes"]}

    assert payload["mode"] == "features"
    assert kinds == {"feature"}
    assert "Commander" in by_name
    assert "Plans" in by_name
    assert "Index" not in by_name
    assert by_name["Commander"]["summary"].startswith("Frontier planning")
    assert by_name["Commander"]["sourcePaths"] == [
        "lat.md/commander.md",
        "src/app.py",
    ]
    assert "src/other.py" not in by_name["Commander"]["sourcePaths"]
    assert any(
        edge["source"] == "feature:commander"
        and edge["target"] == "feature:plans"
        and edge["kind"] == "related"
        for edge in payload["edges"]
    )
    diagram = render_mermaid(payload)
    assert 'n_feature_commander(["Commander"])' in diagram
    assert "src/app.py" not in diagram
    assert "n_feature_commander --> n_feature_plans" in diagram
