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
    assert payload["truncated"] is False
    assert "src" in paths
    assert "src/pkg" in paths
    assert kinds["src/pkg"] == "package"
    assert "src/pkg/mod.py" in paths
    assert "node_modules" not in paths
    assert ".git" not in paths
    assert ".mlx-swarm" not in paths
    assert ".swarm" not in paths
    assert "vendor" in paths
    assert not any(path.startswith("vendor/") for path in paths)
    vendor = next(node for node in payload["nodes"] if node["path"] == "vendor")
    assert vendor["fileCount"] == 12
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
    assert 'n_src_main_py["main.py"]' in diagram
    assert "n_src --> n_src_main_py" in diagram
    assert diagram.rstrip().endswith("```")
