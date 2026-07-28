"""Install the bundled MLX Swarm Commander skill for Codex."""
# @lat: [[Commander]]

from __future__ import annotations

import os
import shutil
import uuid
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

SKILL_NAME = "mlx-swarm-commander"


class SkillInstallError(RuntimeError):
    """Raised when the bundled skill cannot be safely installed."""


def install_bundled_skill(
    *,
    skills_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Copy the validated bundled skill into the Codex skills directory."""
    destination_root = (
        skills_dir.expanduser()
        if skills_dir is not None
        else _default_skills_dir()
    ).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = (destination_root / SKILL_NAME).resolve()
    if destination.parent != destination_root:
        raise SkillInstallError("Invalid Codex skill destination.")
    if destination.exists() and not force:
        raise SkillInstallError(
            f"Skill already exists at {destination}; pass --force to replace it."
        )
    if destination.is_symlink():
        raise SkillInstallError(
            "Refusing to replace a symlinked skill destination."
        )
    if destination.exists() and not destination.is_dir():
        raise SkillInstallError(
            "Refusing to replace a non-directory skill destination."
        )

    resource = files("mlx_swarm.bundled_skills").joinpath(SKILL_NAME)
    _validate_skill_resource(resource)
    staging = destination_root / f".{SKILL_NAME}-{uuid.uuid4().hex}.tmp"
    try:
        staging.mkdir()
        _copy_resource_tree(resource, staging)
        _validate_skill(staging)
        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def _default_skills_dir() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "skills"
    return Path.home() / ".codex" / "skills"


def _validate_skill(path: Path) -> None:
    skill_file = path / "SKILL.md"
    metadata_file = path / "agents" / "openai.yaml"
    if not skill_file.is_file() or not metadata_file.is_file():
        raise SkillInstallError(
            "Bundled skill is missing SKILL.md or agents/openai.yaml."
        )
    content = skill_file.read_text(encoding="utf-8")
    if not content.startswith("---\n") or "\n---\n" not in content[4:]:
        raise SkillInstallError("Bundled skill frontmatter is invalid.")
    frontmatter = content.split("---", 2)[1]
    if f"name: {SKILL_NAME}" not in frontmatter:
        raise SkillInstallError("Bundled skill name does not match its folder.")
    if "description:" not in frontmatter:
        raise SkillInstallError("Bundled skill description is missing.")


def _validate_skill_resource(resource: Traversable) -> None:
    skill_file = resource.joinpath("SKILL.md")
    metadata_file = resource.joinpath("agents").joinpath("openai.yaml")
    if not skill_file.is_file() or not metadata_file.is_file():
        raise SkillInstallError(
            "Bundled skill is missing SKILL.md or agents/openai.yaml."
        )
    content = skill_file.read_text(encoding="utf-8")
    if f"name: {SKILL_NAME}" not in content:
        raise SkillInstallError("Bundled skill name does not match its folder.")


def _copy_resource_tree(source: Traversable, destination: Path) -> None:
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_dir():
            target.mkdir()
            _copy_resource_tree(entry, target)
        elif entry.is_file():
            target.write_bytes(entry.read_bytes())
