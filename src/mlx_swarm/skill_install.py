"""Install the bundled MLX Swarm Commander Agent Skill."""
# @lat: [[Commander]]

from __future__ import annotations

import os
import shutil
import uuid
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

SKILL_NAME = "mlx-swarm-commander"
SUPPORTED_SKILL_HOSTS = {"claude", "codex"}
SKILL_ADAPTERS = {
    "claude": "claude-code-skill",
    "codex": "codex-skill",
}


class SkillInstallError(RuntimeError):
    """Raised when the bundled skill cannot be safely installed."""


def install_bundled_skill(
    *,
    host: str,
    skills_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Copy the validated bundled skill into a supported host directory."""
    normalized_host = host.strip().lower()
    if normalized_host not in SUPPORTED_SKILL_HOSTS:
        supported = ", ".join(sorted(SUPPORTED_SKILL_HOSTS))
        raise SkillInstallError(
            f"Unsupported skill host {host!r}; choose one of: {supported}."
        )
    destination_root = (
        skills_dir.expanduser()
        if skills_dir is not None
        else _default_skills_dir(normalized_host)
    ).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / SKILL_NAME
    if destination.parent != destination_root:
        raise SkillInstallError("Invalid Agent Skill destination.")
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
    _validate_skill_resource(resource, host=normalized_host)
    staging = destination_root / f".{SKILL_NAME}-{uuid.uuid4().hex}.tmp"
    try:
        staging.mkdir()
        _copy_resource_tree(
            resource,
            staging,
            include_openai_metadata=normalized_host == "codex",
        )
        _validate_skill(staging, host=normalized_host)
        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return destination


def _default_skills_dir(host: str) -> Path:
    if host == "claude":
        claude_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if claude_config_dir:
            return Path(claude_config_dir).expanduser() / "skills"
        return Path.home() / ".claude" / "skills"
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "skills"
    return Path.home() / ".codex" / "skills"


def _validate_skill(path: Path, *, host: str) -> None:
    skill_file = path / "SKILL.md"
    metadata_file = path / "agents" / "openai.yaml"
    if not skill_file.is_file():
        raise SkillInstallError("Bundled skill is missing SKILL.md.")
    if host == "codex" and not metadata_file.is_file():
        raise SkillInstallError(
            "Bundled skill is missing SKILL.md or agents/openai.yaml."
        )
    if host == "claude" and metadata_file.exists():
        raise SkillInstallError(
            "Claude skill installation contains Codex-only UI metadata."
        )
    content = skill_file.read_text(encoding="utf-8")
    if not content.startswith("---\n") or "\n---\n" not in content[4:]:
        raise SkillInstallError("Bundled skill frontmatter is invalid.")
    frontmatter = content.split("---", 2)[1]
    if f"name: {SKILL_NAME}" not in frontmatter:
        raise SkillInstallError("Bundled skill name does not match its folder.")
    if "description:" not in frontmatter:
        raise SkillInstallError("Bundled skill description is missing.")


def _validate_skill_resource(resource: Traversable, *, host: str) -> None:
    skill_file = resource.joinpath("SKILL.md")
    metadata_file = resource.joinpath("agents").joinpath("openai.yaml")
    if not skill_file.is_file():
        raise SkillInstallError("Bundled skill is missing SKILL.md.")
    if host == "codex" and not metadata_file.is_file():
        raise SkillInstallError(
            "Bundled skill is missing SKILL.md or agents/openai.yaml."
        )
    content = skill_file.read_text(encoding="utf-8")
    if f"name: {SKILL_NAME}" not in content:
        raise SkillInstallError("Bundled skill name does not match its folder.")


def _copy_resource_tree(
    source: Traversable,
    destination: Path,
    *,
    include_openai_metadata: bool,
) -> None:
    for entry in source.iterdir():
        if entry.name == "agents" and not include_openai_metadata:
            continue
        target = destination / entry.name
        if entry.is_dir():
            target.mkdir()
            _copy_resource_tree(
                entry,
                target,
                include_openai_metadata=include_openai_metadata,
            )
        elif entry.is_file():
            target.write_bytes(entry.read_bytes())
