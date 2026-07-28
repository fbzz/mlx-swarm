"""Reproducible paired economics evaluation for MLX Swarm."""
# @lat: [[economics-evaluation]]

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import platform
import random
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .commander import (
    CommanderError,
    CommanderStore,
    canonical_json_sha256,
)
from .contracts import SwarmConfig, load_config
from .session import Session, _run_id
from .workspace import (
    WorkspaceError,
    discover_git_root,
    final_workspace_diff,
    load_artifact,
    load_workspace_snapshot,
    prepare_worktree,
    submit_artifact_decision,
)


EVALUATION_SCHEMA_VERSION = 1
PROFILE_SCHEMA_VERSION = 1
SUITE_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
DEFAULT_EVALUATIONS_DIR = ".swarm/evaluations"
DEFAULT_PUBLIC_RESULTS_DIR = "benchmarks/results"
README_START = "<!-- BEGIN MLX-SWARM-ECONOMICS -->"
README_END = "<!-- END MLX-SWARM-ECONOMICS -->"
MAX_LOG_BYTES = 1_000_000
BENCHMARK_BUILD_JOBS = 4
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,95}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_INFO_VALUE = re.compile(
    r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"(.*)"$'
)
_DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_SHELL_META = ("&&", "||", ">", "<", "|", ";", "$(", "`")
_SAFE_BENCHMARK_COMMANDS = {
    "pip",
    "pip3",
    "py.test",
    "pytest",
    "python",
    "python3",
    "touch",
    "tox",
}
_NON_PRODUCTION_PREFIXES = (
    ".github/",
    "doc/",
    "docs/",
    "test/",
    "tests/",
    "testing/",
)
_NON_PRODUCTION_NAMES = {
    "changelog",
    "changelog.md",
    "license",
    "license.md",
    "manifest.in",
    "pyproject.toml",
    "readme",
    "readme.md",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "tox.ini",
}


class EvaluationError(RuntimeError):
    """Raised when evaluation evidence or execution is invalid."""


@dataclass(frozen=True)
class FrontierSettings:
    command: str
    model: str
    reasoning_effort: str
    arm_timeout_seconds: int
    planning_timeout_seconds: int
    local_timeout_seconds: int
    review_timeout_seconds: int


@dataclass(frozen=True)
class SelectionSettings:
    pilot_size: int
    measured_size: int
    min_projects: int
    max_per_project: int
    max_changed_files: int
    max_changed_lines: int
    max_context_characters: int
    minimum_python: tuple[int, int]
    projects: tuple[str, ...]


@dataclass(frozen=True)
class StorageSettings:
    max_bytes: int
    min_free_bytes: int


@dataclass(frozen=True)
class ContainerSettings:
    image: str
    digest: str
    platform: str


@dataclass(frozen=True)
class EvaluationProfile:
    source: Path
    profile_id: str
    benchmark_repository: str
    benchmark_revision: str
    seed: int
    selection: SelectionSettings
    storage: StorageSettings
    container: ContainerSettings
    frontier: FrontierSettings
    python_bootstrap: tuple[str, ...]
    dependency_roots: dict[str, tuple[str, ...]]
    dependency_pins: dict[str, tuple[str, ...]]
    max_repair: int


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float
    timed_out: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_evaluation_profile(path: Path) -> EvaluationProfile:
    """Load a strict, pinned economics evaluation profile."""
    raw = _read_json(path)
    _exact_keys(
        raw,
        "profile",
        {
            "schemaVersion",
            "profileId",
            "benchmark",
            "seed",
            "selection",
            "storage",
            "container",
            "frontier",
            "pythonBootstrap",
            "dependencyRoots",
            "dependencyPins",
            "local",
        },
    )
    if _integer(raw["schemaVersion"], "profile.schemaVersion", 1, 100) != 1:
        raise EvaluationError("Unsupported evaluation profile schema version.")
    benchmark = _object(raw["benchmark"], "profile.benchmark")
    _exact_keys(
        benchmark,
        "profile.benchmark",
        {"repository", "revision"},
    )
    revision = _text(benchmark["revision"], "profile.benchmark.revision")
    if _GIT_SHA.fullmatch(revision) is None:
        raise EvaluationError(
            "profile.benchmark.revision must be a pinned 40-character Git SHA."
        )
    selection_raw = _object(raw["selection"], "profile.selection")
    _exact_keys(
        selection_raw,
        "profile.selection",
        {
            "pilotSize",
            "measuredSize",
            "minProjects",
            "maxPerProject",
            "maxChangedFiles",
            "maxChangedLines",
            "maxContextCharacters",
            "minimumPython",
            "projects",
        },
    )
    minimum_python_text = _text(
        selection_raw["minimumPython"],
        "profile.selection.minimumPython",
    )
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)", minimum_python_text)
    if match is None:
        raise EvaluationError(
            "profile.selection.minimumPython must be major.minor."
        )
    projects = _unique_text_array(
        selection_raw["projects"],
        "profile.selection.projects",
        minimum=1,
        maximum=32,
    )
    selection = SelectionSettings(
        pilot_size=_integer(
            selection_raw["pilotSize"],
            "profile.selection.pilotSize",
            1,
            100,
        ),
        measured_size=_integer(
            selection_raw["measuredSize"],
            "profile.selection.measuredSize",
            1,
            500,
        ),
        min_projects=_integer(
            selection_raw["minProjects"],
            "profile.selection.minProjects",
            1,
            32,
        ),
        max_per_project=_integer(
            selection_raw["maxPerProject"],
            "profile.selection.maxPerProject",
            1,
            100,
        ),
        max_changed_files=_integer(
            selection_raw["maxChangedFiles"],
            "profile.selection.maxChangedFiles",
            1,
            100,
        ),
        max_changed_lines=_integer(
            selection_raw["maxChangedLines"],
            "profile.selection.maxChangedLines",
            1,
            100_000,
        ),
        max_context_characters=_integer(
            selection_raw["maxContextCharacters"],
            "profile.selection.maxContextCharacters",
            1_024,
            500_000,
        ),
        minimum_python=(int(match.group(1)), int(match.group(2))),
        projects=projects,
    )
    if selection.min_projects > len(selection.projects):
        raise EvaluationError(
            "profile.selection.minProjects exceeds the project allowlist."
        )
    if (
        selection.measured_size
        > selection.max_per_project * len(selection.projects)
    ):
        raise EvaluationError(
            "Measured size cannot fit within maxPerProject and projects."
        )
    if (
        selection.measured_size
        > selection.max_per_project * selection.min_projects
        and selection.min_projects == len(selection.projects)
    ):
        raise EvaluationError(
            "Measured selection cannot satisfy its project ceiling."
        )
    storage_raw = _object(raw["storage"], "profile.storage")
    _exact_keys(
        storage_raw,
        "profile.storage",
        {"maxBytes", "minFreeBytes"},
    )
    storage = StorageSettings(
        max_bytes=_integer(
            storage_raw["maxBytes"],
            "profile.storage.maxBytes",
            1,
            10**15,
        ),
        min_free_bytes=_integer(
            storage_raw["minFreeBytes"],
            "profile.storage.minFreeBytes",
            1,
            10**15,
        ),
    )
    container_raw = _object(raw["container"], "profile.container")
    _exact_keys(
        container_raw,
        "profile.container",
        {"image", "digest", "platform"},
    )
    container_digest = _text(
        container_raw["digest"],
        "profile.container.digest",
    )
    if _IMAGE_DIGEST.fullmatch(container_digest) is None:
        raise EvaluationError(
            "profile.container.digest must be a sha256 image digest."
        )
    container = ContainerSettings(
        image=_text(container_raw["image"], "profile.container.image"),
        digest=container_digest,
        platform=_enum(
            container_raw["platform"],
            "profile.container.platform",
            {"linux/amd64", "linux/arm64"},
        ),
    )
    frontier_raw = _object(raw["frontier"], "profile.frontier")
    _exact_keys(
        frontier_raw,
        "profile.frontier",
        {
            "command",
            "model",
            "reasoningEffort",
            "armTimeoutSeconds",
            "planningTimeoutSeconds",
            "localTimeoutSeconds",
            "reviewTimeoutSeconds",
        },
    )
    frontier = FrontierSettings(
        command=_text(frontier_raw["command"], "profile.frontier.command"),
        model=_text(frontier_raw["model"], "profile.frontier.model"),
        reasoning_effort=_enum(
            frontier_raw["reasoningEffort"],
            "profile.frontier.reasoningEffort",
            {"low", "medium", "high", "xhigh", "max", "ultra"},
        ),
        arm_timeout_seconds=_integer(
            frontier_raw["armTimeoutSeconds"],
            "profile.frontier.armTimeoutSeconds",
            60,
            86_400,
        ),
        planning_timeout_seconds=_integer(
            frontier_raw["planningTimeoutSeconds"],
            "profile.frontier.planningTimeoutSeconds",
            30,
            86_400,
        ),
        local_timeout_seconds=_integer(
            frontier_raw["localTimeoutSeconds"],
            "profile.frontier.localTimeoutSeconds",
            30,
            86_400,
        ),
        review_timeout_seconds=_integer(
            frontier_raw["reviewTimeoutSeconds"],
            "profile.frontier.reviewTimeoutSeconds",
            30,
            86_400,
        ),
    )
    if (
        frontier.planning_timeout_seconds
        + frontier.local_timeout_seconds
        + frontier.review_timeout_seconds
        != frontier.arm_timeout_seconds
    ):
        raise EvaluationError(
            "Swarm phase timeouts must sum to armTimeoutSeconds."
        )
    local_raw = _object(raw["local"], "profile.local")
    _exact_keys(local_raw, "profile.local", {"maxRepair"})
    python_bootstrap = _unique_text_array(
        raw["pythonBootstrap"],
        "profile.pythonBootstrap",
        minimum=3,
        maximum=16,
    )
    for requirement in python_bootstrap:
        if not is_exact_requirement(requirement):
            raise EvaluationError(
                "profile.pythonBootstrap entries must use exact == pins."
            )
    dependency_roots_raw = _object(
        raw["dependencyRoots"],
        "profile.dependencyRoots",
    )
    if set(dependency_roots_raw) != set(selection.projects):
        raise EvaluationError(
            "profile.dependencyRoots keys must exactly match selection.projects."
        )
    dependency_roots: dict[str, tuple[str, ...]] = {}
    for project in selection.projects:
        roots = _unique_text_array(
            dependency_roots_raw[project],
            f"profile.dependencyRoots.{project}",
            minimum=0,
            maximum=64,
        )
        if any(
            re.fullmatch(r"[A-Za-z0-9_.-]+", root) is None
            for root in roots
        ):
            raise EvaluationError(
                f"profile.dependencyRoots.{project} contains an invalid package."
            )
        dependency_roots[project] = roots
    dependency_pins_raw = _object(
        raw["dependencyPins"],
        "profile.dependencyPins",
    )
    if set(dependency_pins_raw) != set(selection.projects):
        raise EvaluationError(
            "profile.dependencyPins keys must exactly match selection.projects."
        )
    dependency_pins: dict[str, tuple[str, ...]] = {}
    for project in selection.projects:
        pins = _unique_text_array(
            dependency_pins_raw[project],
            f"profile.dependencyPins.{project}",
            maximum=128,
        )
        if any(not is_exact_requirement(pin) for pin in pins):
            raise EvaluationError(
                f"profile.dependencyPins.{project} entries must use exact == pins."
            )
        dependency_pins[project] = pins
    profile_id = _identifier(raw["profileId"], "profile.profileId")
    return EvaluationProfile(
        source=path.resolve(),
        profile_id=profile_id,
        benchmark_repository=_text(
            benchmark["repository"],
            "profile.benchmark.repository",
        ),
        benchmark_revision=revision,
        seed=_integer(raw["seed"], "profile.seed", 0, 2**31 - 1),
        selection=selection,
        storage=storage,
        container=container,
        frontier=frontier,
        python_bootstrap=python_bootstrap,
        dependency_roots=dependency_roots,
        dependency_pins=dependency_pins,
        max_repair=_integer(
            local_raw["maxRepair"],
            "profile.local.maxRepair",
            0,
            5,
        ),
    )


def profile_payload(profile: EvaluationProfile) -> dict[str, Any]:
    return {
        "schemaVersion": PROFILE_SCHEMA_VERSION,
        "profileId": profile.profile_id,
        "benchmark": {
            "repository": profile.benchmark_repository,
            "revision": profile.benchmark_revision,
        },
        "seed": profile.seed,
        "selection": {
            "pilotSize": profile.selection.pilot_size,
            "measuredSize": profile.selection.measured_size,
            "minProjects": profile.selection.min_projects,
            "maxPerProject": profile.selection.max_per_project,
            "maxChangedFiles": profile.selection.max_changed_files,
            "maxChangedLines": profile.selection.max_changed_lines,
            "maxContextCharacters": profile.selection.max_context_characters,
            "minimumPython": (
                f"{profile.selection.minimum_python[0]}."
                f"{profile.selection.minimum_python[1]}"
            ),
            "projects": list(profile.selection.projects),
        },
        "storage": {
            "maxBytes": profile.storage.max_bytes,
            "minFreeBytes": profile.storage.min_free_bytes,
        },
        "container": {
            "image": profile.container.image,
            "digest": profile.container.digest,
            "platform": profile.container.platform,
        },
        "frontier": {
            "command": profile.frontier.command,
            "model": profile.frontier.model,
            "reasoningEffort": profile.frontier.reasoning_effort,
            "armTimeoutSeconds": profile.frontier.arm_timeout_seconds,
            "planningTimeoutSeconds": (
                profile.frontier.planning_timeout_seconds
            ),
            "localTimeoutSeconds": profile.frontier.local_timeout_seconds,
            "reviewTimeoutSeconds": profile.frontier.review_timeout_seconds,
        },
        "pythonBootstrap": list(profile.python_bootstrap),
        "dependencyRoots": {
            project: list(profile.dependency_roots[project])
            for project in profile.selection.projects
        },
        "dependencyPins": {
            project: list(profile.dependency_pins[project])
            for project in profile.selection.projects
        },
        "local": {"maxRepair": profile.max_repair},
    }


def read_text_portable(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise EvaluationError(f"Benchmark metadata is not supported text: {path}")


def parse_info_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in read_text_portable(path).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _INFO_VALUE.fullmatch(line)
        if match is None:
            raise EvaluationError(f"Unsupported benchmark info line: {line}")
        if match.group(1) in result:
            raise EvaluationError(
                f"Duplicate benchmark info field: {match.group(1)}"
            )
        result[match.group(1)] = match.group(2)
    return result


def parse_benchmark_commands(path: Path) -> list[list[str]]:
    """Parse a deliberately narrow command subset without invoking a shell."""
    commands: list[list[str]] = []
    if not path.is_file():
        return commands
    for raw_line in read_text_portable(path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if any(marker in line for marker in _SHELL_META):
            raise EvaluationError(
                f"Benchmark command requires unsupported shell syntax: {line}"
            )
        try:
            argv = shlex.split(line)
        except ValueError as exc:
            raise EvaluationError(
                f"Benchmark command is malformed: {line}"
            ) from exc
        if not argv or argv[0] not in _SAFE_BENCHMARK_COMMANDS:
            raise EvaluationError(
                f"Benchmark command is not allowlisted: {line}"
            )
        if len(argv) > 128 or any(len(value) > 8_192 for value in argv):
            raise EvaluationError("Benchmark command exceeds safety limits.")
        commands.append(argv)
    if path.name == "run_test.sh" and not commands:
        raise EvaluationError("Benchmark case has no verification commands.")
    return commands


def patch_metadata(patch: str) -> dict[str, Any]:
    if any(
        marker in patch
        for marker in (
            "GIT binary patch",
            "Binary files ",
            "Subproject commit ",
            "new file mode 160000",
            "old file mode 160000",
        )
    ):
        raise EvaluationError(
            "Reference patch contains binary or submodule content."
        )
    if any(
        line.startswith(("rename from ", "rename to ", "copy from ", "copy to "))
        for line in patch.splitlines()
    ):
        raise EvaluationError("Reference patch contains rename/copy metadata.")
    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        match = _DIFF_HEADER.fullmatch(line)
        if match is None or match.group(1) != match.group(2):
            raise EvaluationError("Reference patch contains rename/copy metadata.")
        path = match.group(1)
        if path.startswith("/") or ".." in Path(path).parts:
            raise EvaluationError("Reference patch path escapes the project.")
        paths.append(path)
    if not paths or len(paths) != len(set(paths)):
        raise EvaluationError(
            "Reference patch must contain unique unified diff paths."
        )
    changed_lines = sum(
        1
        for line in patch.splitlines()
        if (
            (line.startswith("+") or line.startswith("-"))
            and not line.startswith(("+++", "---"))
        )
    )
    return {
        "paths": paths,
        "changedFiles": len(paths),
        "changedLines": changed_lines,
        "sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
    }


def enumerate_bugsinpy_candidates(
    metadata_root: Path,
    profile: EvaluationProfile,
) -> list[dict[str, Any]]:
    """Enumerate statically eligible candidates without exposing patch text."""
    candidates: list[dict[str, Any]] = []
    projects_root = metadata_root / "projects"
    for project in profile.selection.projects:
        project_dir = projects_root / project
        project_info = parse_info_file(project_dir / "project.info")
        repository = project_info.get("github_url")
        if not repository or project_info.get("status") != "OK":
            continue
        bug_dirs = sorted(
            (project_dir / "bugs").glob("*"),
            key=lambda value: (
                int(value.name) if value.name.isdigit() else 2**31,
                value.name,
            ),
        )
        for bug_dir in bug_dirs:
            if not bug_dir.is_dir() or not bug_dir.name.isdigit():
                continue
            try:
                info = parse_info_file(bug_dir / "bug.info")
                patch = read_text_portable(bug_dir / "bug_patch.txt")
                patch_info = patch_metadata(patch)
                setup = parse_benchmark_commands(bug_dir / "setup.sh")
                verification = parse_benchmark_commands(
                    bug_dir / "run_test.sh"
                )
                requirements = read_text_portable(
                    bug_dir / "requirements.txt"
                )
                python_version = _python_version(info.get("python_version"))
                buggy_commit = _commitish(
                    info.get("buggy_commit_id"),
                    "buggy_commit_id",
                )
                fixed_commit = _commitish(
                    info.get("fixed_commit_id"),
                    "fixed_commit_id",
                )
                test_files = tuple(
                    value
                    for value in info.get("test_file", "").split(";")
                    if value
                )
                if not test_files:
                    raise EvaluationError("Benchmark case has no test files.")
                _safe_relative_paths(test_files, "test_file")
                if python_version < profile.selection.minimum_python:
                    continue
                if (
                    patch_info["changedFiles"]
                    > profile.selection.max_changed_files
                    or patch_info["changedLines"]
                    > profile.selection.max_changed_lines
                ):
                    continue
                if any(
                    _is_non_production_path(value)
                    for value in patch_info["paths"]
                ):
                    continue
                if any(
                    Path(value).suffix.lower()
                    in {".c", ".cc", ".cpp", ".h", ".hpp", ".pxd", ".pyx"}
                    for value in patch_info["paths"]
                ):
                    continue
                requirement_lines = _requirement_lines(
                    requirements,
                    project=project,
                )
                dependency_requirements(
                    requirement_lines,
                    project,
                    profile,
                )
            except (EvaluationError, OSError):
                continue
            candidates.append({
                "caseId": f"{project.lower()}-{bug_dir.name}",
                "project": project,
                "bugId": int(bug_dir.name),
                "repository": repository,
                "buggyCommit": buggy_commit,
                "fixedCommit": fixed_commit,
                "pythonVersion": (
                    f"{python_version[0]}.{python_version[1]}"
                ),
                "testFiles": list(test_files),
                "setupArgv": setup,
                "verificationArgv": verification,
                "requirements": requirement_lines,
                "reference": patch_info,
            })
    if not candidates:
        raise EvaluationError("No statically eligible BugsInPy cases found.")
    candidates.sort(key=lambda value: value["caseId"])
    _assign_patch_strata(candidates)
    return candidates


def select_cases(
    candidates: Sequence[dict[str, Any]],
    profile: EvaluationProfile,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select disjoint pilot/measured sets with deterministic quotas."""
    rng = random.Random(profile.seed)
    by_project: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_project.setdefault(candidate["project"], []).append(candidate)
    projects = [
        project
        for project in profile.selection.projects
        if by_project.get(project)
    ]
    if len(projects) < profile.selection.min_projects:
        raise EvaluationError(
            "Not enough projects have eligible benchmark cases."
        )
    capacities = {
        project: min(
            len(by_project[project]),
            profile.selection.max_per_project,
        )
        for project in projects
    }
    if sum(capacities.values()) < profile.selection.measured_size:
        raise EvaluationError(
            "Measured selection cannot fit within the available project caps."
        )
    if profile.selection.measured_size < profile.selection.min_projects:
        raise EvaluationError(
            "Measured size cannot cover the minimum project count."
        )
    included = projects[: min(len(projects), profile.selection.measured_size)]
    quotas = {project: (1 if project in included else 0) for project in projects}
    remaining = profile.selection.measured_size - sum(quotas.values())
    for project in projects:
        if remaining <= 0:
            break
        available = capacities[project] - quotas[project]
        granted = min(available, remaining)
        quotas[project] += granted
        remaining -= granted
    if remaining:
        raise EvaluationError("Measured selection exhausted its quotas.")
    if sum(value > 0 for value in quotas.values()) < profile.selection.min_projects:
        raise EvaluationError("Measured suite does not cover enough projects.")
    shuffled: dict[str, list[dict[str, Any]]] = {}
    for project in projects:
        values = list(by_project[project])
        rng.shuffle(values)
        values.sort(
            key=lambda value: (
                value["reference"]["stratum"],
                hashlib.sha256(
                    f"{profile.seed}:{value['caseId']}".encode()
                ).hexdigest(),
            )
        )
        shuffled[project] = values
    pilot: list[dict[str, Any]] = []
    pilot_projects = [
        project
        for project in projects
        if len(shuffled[project]) > quotas[project]
    ]
    pilot_projects.sort(
        key=lambda project: (
            any(
                any("build_ext" in arg for arg in command)
                for value in shuffled[project]
                for command in value["setupArgv"]
            ),
            projects.index(project),
        )
    )
    inexpensive = [
        project
        for project in pilot_projects
        if not any(
            any("build_ext" in arg for arg in command)
            for value in shuffled[project]
            for command in value["setupArgv"]
        )
    ]
    if sum(
        len(shuffled[project]) - quotas[project]
        for project in inexpensive
    ) >= profile.selection.pilot_size:
        pilot_projects = inexpensive
    project_index = 0
    while len(pilot) < profile.selection.pilot_size:
        if not pilot_projects:
            raise EvaluationError(
                "Pilot selection has no cases beyond measured reservations."
            )
        project = pilot_projects[project_index % len(pilot_projects)]
        project_index += 1
        values = shuffled[project]
        if len(values) <= quotas[project]:
            pilot_projects.remove(project)
            project_index = 0
            continue
        counts = {
            stratum: sum(
                value["reference"]["stratum"] == stratum
                for value in pilot
            )
            for stratum in ("small", "medium", "large")
        }
        preferred = min(
            ("small", "medium", "large"),
            key=lambda key: (counts[key], key),
        )
        selected_index = next(
            (
                index
                for index in range(len(values))
                if values[index]["reference"]["stratum"] == preferred
            ),
            0,
        )
        pilot.append(values.pop(selected_index))
    measured: list[dict[str, Any]] = []
    per_project: dict[str, int] = {project: 0 for project in projects}
    while len(measured) < profile.selection.measured_size:
        progress = False
        for project in projects:
            if len(measured) >= profile.selection.measured_size:
                break
            if (
                per_project[project] >= quotas[project]
            ):
                continue
            values = shuffled[project]
            if not values:
                continue
            # Prefer the currently least represented patch-size stratum.
            counts = {
                stratum: sum(
                    value["reference"]["stratum"] == stratum
                    for value in measured
                )
                for stratum in ("small", "medium", "large")
            }
            preferred = min(counts, key=lambda key: (counts[key], key))
            selected_index = next(
                (
                    index
                    for index, value in enumerate(values)
                    if value["reference"]["stratum"] == preferred
                ),
                0,
            )
            measured.append(values.pop(selected_index))
            per_project[project] += 1
            progress = True
        if not progress:
            raise EvaluationError("Measured selection exhausted its quotas.")
    if len({value["project"] for value in measured}) < profile.selection.min_projects:
        raise EvaluationError("Measured suite does not cover enough projects.")
    if len({value["caseId"] for value in pilot + measured}) != len(
        pilot + measured
    ):
        raise EvaluationError("Pilot and measured selections overlap.")
    _rebalance_strata(measured, candidates, pilot, projects, profile)
    return pilot, measured


def evaluation_case(
    candidate: dict[str, Any],
    phase: str,
) -> dict[str, Any]:
    if phase not in {"pilot", "measured"}:
        raise EvaluationError("Evaluation case phase is invalid.")
    return {
        **candidate,
        "phase": phase,
        "objective": (
            f"Repair BugsInPy case {candidate['caseId']} so every "
            "frozen verification command passes without modifying "
            "tests or benchmark evidence."
        ),
    }


def parse_codex_usage_jsonl(text: str) -> dict[str, Any]:
    """Aggregate exact usage from every Codex turn.completed JSONL event."""
    prompt_tokens = 0
    cached_input_tokens = 0
    completion_tokens = 0
    reasoning_tokens = 0
    total_tokens = 0
    turns = 0
    malformed = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        input_value = _usage_integer(
            usage,
            ("input_tokens", "prompt_tokens"),
        )
        output_value = _usage_integer(
            usage,
            ("output_tokens", "completion_tokens"),
        )
        cached_value = _usage_integer(
            usage,
            ("cached_input_tokens",),
            default=0,
        )
        reasoning_value = _usage_integer(
            usage,
            ("reasoning_output_tokens", "reasoning_tokens"),
            default=0,
        )
        total_value = _usage_integer(
            usage,
            ("total_tokens",),
            default=input_value + output_value,
        )
        prompt_tokens += input_value
        cached_input_tokens += cached_value
        completion_tokens += output_value
        reasoning_tokens += reasoning_value
        total_tokens += total_value
        turns += 1
    if turns == 0:
        return {
            "usageStatus": "unavailable",
            "turns": 0,
            "promptTokens": None,
            "cachedInputTokens": None,
            "completionTokens": None,
            "reasoningTokens": None,
            "totalTokens": None,
            "malformedLines": malformed,
        }
    return {
        "usageStatus": "reported",
        "turns": turns,
        "promptTokens": prompt_tokens,
        "cachedInputTokens": cached_input_tokens,
        "completionTokens": completion_tokens,
        "reasoningTokens": reasoning_tokens,
        "totalTokens": total_tokens,
        "malformedLines": malformed,
    }


def validate_arm_result(value: Any) -> dict[str, Any]:
    """Validate one immutable per-arm measurement."""
    result = _object(value, "armResult")
    _exact_keys(
        result,
        "armResult",
        {
            "schemaVersion",
            "caseId",
            "phase",
            "arm",
            "status",
            "completed",
            "score",
            "elapsedSeconds",
            "phaseSeconds",
            "frontierUsage",
            "localUsage",
            "repairs",
            "modelLoads",
            "reviewVerdict",
            "patch",
            "oracle",
            "recordedAt",
        },
    )
    if _integer(result["schemaVersion"], "armResult.schemaVersion", 1, 100) != 1:
        raise EvaluationError("Unsupported arm result schema version.")
    _identifier(result["caseId"], "armResult.caseId")
    _enum(result["phase"], "armResult.phase", {"pilot", "measured"})
    _enum(result["arm"], "armResult.arm", {"frontier-alone", "mlx-swarm"})
    _enum(
        result["status"],
        "armResult.status",
        {"completed", "failed", "timed_out", "invalid"},
    )
    if not isinstance(result["completed"], bool):
        raise EvaluationError("armResult.completed must be boolean.")
    _integer(result["score"], "armResult.score", 0, 1)
    _number(
        result["elapsedSeconds"],
        "armResult.elapsedSeconds",
        0,
        10**9,
    )
    phase_seconds = _object(
        result["phaseSeconds"],
        "armResult.phaseSeconds",
    )
    allowed_phase_names = (
        {"frontier", "oracle"}
        if result["arm"] == "frontier-alone"
        else {"planning", "local", "review", "oracle"}
    )
    unknown_phases = set(phase_seconds) - allowed_phase_names
    if unknown_phases:
        raise EvaluationError(
            "armResult.phaseSeconds contains unknown phases: "
            + ", ".join(sorted(unknown_phases))
        )
    for name, seconds in phase_seconds.items():
        _number(
            seconds,
            f"armResult.phaseSeconds.{name}",
            0,
            10**9,
        )
    _validate_frontier_usage(
        result["frontierUsage"],
        "armResult.frontierUsage",
    )
    _validate_local_usage(result["localUsage"], "armResult.localUsage")
    _integer(result["repairs"], "armResult.repairs", 0, 10**9)
    _integer(result["modelLoads"], "armResult.modelLoads", 0, 10**9)
    review = result["reviewVerdict"]
    if review is not None:
        _enum(
            review,
            "armResult.reviewVerdict",
            {"approved", "changes_requested", "rejected"},
        )
    patch = _object(result["patch"], "armResult.patch")
    _exact_keys(
        patch,
        "armResult.patch",
        {"sha256", "changedFiles"},
    )
    if patch["sha256"] is not None and (
        not isinstance(patch["sha256"], str)
        or _SHA256.fullmatch(patch["sha256"]) is None
    ):
        raise EvaluationError("armResult.patch.sha256 must be null or SHA-256.")
    _integer(patch["changedFiles"], "armResult.patch.changedFiles", 0, 100_000)
    oracle = _object(result["oracle"], "armResult.oracle")
    _exact_keys(
        oracle,
        "armResult.oracle",
        {"passed", "exitCode", "evidence"},
    )
    if not isinstance(oracle["passed"], bool):
        raise EvaluationError("armResult.oracle.passed must be boolean.")
    if int(result["score"]) != int(oracle["passed"]):
        raise EvaluationError(
            "armResult.score must equal the executable oracle result."
        )
    if oracle["exitCode"] is not None:
        _integer(
            oracle["exitCode"],
            "armResult.oracle.exitCode",
            -2**31,
            2**31 - 1,
        )
    _text(
        oracle["evidence"],
        "armResult.oracle.evidence",
        allow_empty=True,
        maximum=MAX_LOG_BYTES,
    )
    _text(result["recordedAt"], "armResult.recordedAt")
    if result["completed"] != (result["status"] == "completed"):
        raise EvaluationError(
            "armResult.completed must match status=completed."
        )
    if result["arm"] == "frontier-alone":
        if (
            any(result["localUsage"].values())
            or result["repairs"] != 0
            or result["modelLoads"] != 0
            or result["reviewVerdict"] is not None
        ):
            raise EvaluationError(
                "Frontier-alone results cannot contain local or review metrics."
            )
    return result


def aggregate_results(
    suite: dict[str, Any],
    arm_results: Sequence[dict[str, Any]],
    *,
    bootstrap_samples: int = 10_000,
) -> dict[str, Any]:
    """Build paired metrics without mixing frontier and local tokens."""
    measured_ids = [
        case["caseId"]
        for case in suite["cases"]
        if case["phase"] == "measured"
    ]
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for raw_result in arm_results:
        result = validate_arm_result(raw_result)
        key = (result["caseId"], result["arm"])
        if key in indexed:
            raise EvaluationError(
                f"Duplicate arm result for {result['caseId']} {result['arm']}."
            )
        indexed[key] = result
    rows: list[dict[str, Any]] = []
    for case_id in measured_ids:
        frontier = indexed.get((case_id, "frontier-alone"))
        swarm = indexed.get((case_id, "mlx-swarm"))
        if frontier is None or swarm is None:
            continue
        frontier_tokens = _reported_total(frontier["frontierUsage"])
        swarm_tokens = _reported_total(swarm["frontierUsage"])
        rows.append({
            "caseId": case_id,
            "project": next(
                case["project"]
                for case in suite["cases"]
                if case["caseId"] == case_id
            ),
            "frontier": frontier,
            "swarm": swarm,
            "frontierTokenDelta": (
                frontier_tokens - swarm_tokens
                if frontier_tokens is not None and swarm_tokens is not None
                else None
            ),
            "timeDeltaSeconds": (
                float(frontier["elapsedSeconds"])
                - float(swarm["elapsedSeconds"])
            ),
        })
    complete_pairs = len(rows)
    all_usage_valid = (
        complete_pairs == len(measured_ids)
        and all(row["frontierTokenDelta"] is not None for row in rows)
    )
    token_deltas = [
        int(row["frontierTokenDelta"])
        for row in rows
        if row["frontierTokenDelta"] is not None
    ]
    lower = None
    upper = None
    if token_deltas:
        lower, upper = bootstrap_mean_interval(
            token_deltas,
            seed=int(suite["seed"]),
            samples=bootstrap_samples,
        )
    frontier_metrics = _arm_aggregate(
        [row["frontier"] for row in rows],
        include_local=False,
    )
    swarm_metrics = _arm_aggregate(
        [row["swarm"] for row in rows],
        include_local=True,
    )
    accepted_both = [
        row
        for row in rows
        if row["frontier"]["score"] == 1 and row["swarm"]["score"] == 1
    ]
    accepted_both_deltas = [
        int(row["frontierTokenDelta"])
        for row in accepted_both
        if row["frontierTokenDelta"] is not None
    ]
    claim_established = bool(
        all_usage_valid
        and frontier_metrics["completed"] <= swarm_metrics["completed"]
        and frontier_metrics["score"] <= swarm_metrics["score"]
        and lower is not None
        and lower > 0
    )
    summary = {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "suiteId": suite["suiteId"],
        "measuredCases": len(measured_ids),
        "completePairs": complete_pairs,
        "frontierAlone": frontier_metrics,
        "mlxSwarm": swarm_metrics,
        "paired": {
            "frontierTokensSaved": sum(token_deltas) if token_deltas else None,
            "frontierTokensSavedMedian": (
                statistics.median(token_deltas) if token_deltas else None
            ),
            "frontierTokensSavedPercent": _percentage(
                sum(token_deltas),
                frontier_metrics["frontierTokens"],
            )
            if token_deltas
            else None,
            "bootstrap95": {
                "lower": lower,
                "upper": upper,
                "samples": bootstrap_samples,
            },
            "acceptedByBothCases": len(accepted_both),
            "acceptedByBothTokensSaved": (
                sum(accepted_both_deltas)
                if accepted_both_deltas
                else None
            ),
            "allUsageValid": all_usage_valid,
        },
        "claim": {
            "status": (
                "established"
                if claim_established
                else "tradeoff_measured"
                if complete_pairs == len(measured_ids)
                else "incomplete"
            ),
            "text": (
                "MLX Swarm saves frontier tokens without reducing completion "
                "or executable acceptance."
                if claim_established
                else "Token savings at completion and acceptance parity are "
                "not established by this study."
            ),
        },
        "rows": rows,
        "generatedAt": utc_now(),
    }
    return summary


def bootstrap_mean_interval(
    values: Sequence[int | float],
    *,
    seed: int,
    samples: int = 10_000,
) -> tuple[float, float]:
    if not values:
        raise EvaluationError("Bootstrap requires at least one value.")
    if samples < 100:
        raise EvaluationError("Bootstrap requires at least 100 samples.")
    rng = random.Random(seed)
    size = len(values)
    means = sorted(
        sum(values[rng.randrange(size)] for _ in range(size)) / size
        for _ in range(samples)
    )
    lower_index = max(0, math.floor(0.025 * (samples - 1)))
    upper_index = min(samples - 1, math.ceil(0.975 * (samples - 1)))
    return float(means[lower_index]), float(means[upper_index])


def render_readme_economics(summary: dict[str, Any]) -> str:
    """Render deterministic aggregate and per-case Markdown tables."""
    frontier = summary["frontierAlone"]
    swarm = summary["mlxSwarm"]
    paired = summary["paired"]
    lines = [
        "## Measured economics",
        "",
        f"**Study status:** `{summary['claim']['status']}` — "
        f"{summary['claim']['text']}",
        "",
    ]
    study = summary.get("study")
    if isinstance(study, dict):
        local_model = study.get("localModel") or {}
        hardware = study.get("hardware") or {}
        lines.extend([
            (
                f"Pinned protocol: `{study.get('benchmark', 'BugsInPy')}@"
                f"{study.get('benchmarkRevision', 'unknown')}` · "
                f"`{study.get('frontierModel', 'unknown')}` "
                f"({study.get('reasoningEffort', 'unknown')}) · local "
                f"`{local_model.get('repository', 'unknown')}@"
                f"{local_model.get('fingerprint', 'unknown')}` · "
                f"seed `{study.get('seed', 'unknown')}`."
            ),
            "",
            (
                f"Recorded `{study.get('recordedAt', 'unknown')}` on "
                f"`{hardware.get('machine', 'unknown')}` / "
                f"`{hardware.get('processor', 'unknown')}` with "
                f"{format_bytes(hardware.get('memoryBytes'))}. "
                f"MLX Swarm commit `{study.get('mlxSwarmCommit', 'unknown')}`; "
                f"Codex `{study.get('codexVersion', 'unknown')}`."
            ),
            "",
        ])
    preparation = summary.get("preparation")
    if isinstance(preparation, dict):
        lines.extend([
            (
                "One-time case preparation (excluded from task timing): "
                f"{format_duration(preparation.get('totalSeconds'))} across "
                f"{preparation.get('caseCount', 0)} cases."
            ),
            "",
        ])
    lines.extend([
        "Scores are binary executable-oracle results. Times are end-to-end "
        "wall time and exclude one-time benchmark preparation. Frontier and "
        "local tokens are intentionally separate. This pass@1 study is one "
        "suite on one machine; it does not establish monetary savings or "
        "generalize beyond the pinned protocol.",
        "",
        "| Metric | Frontier Alone | MLX Swarm | Delta |",
        "|---|---:|---:|---:|",
        (
            f"| Completed | {frontier['completed']}/{summary['measuredCases']} "
            f"({frontier['completionRate']:.1f}%) "
            f"| {swarm['completed']}/{summary['measuredCases']} "
            f"({swarm['completionRate']:.1f}%) "
            f"| {_signed(swarm['completed'] - frontier['completed'])} |"
        ),
        (
            f"| Score | {frontier['score']}/{summary['measuredCases']} "
            f"| {swarm['score']}/{summary['measuredCases']} "
            f"| {_signed(swarm['score'] - frontier['score'])} |"
        ),
        (
            f"| Median end-to-end time | "
            f"{format_duration(frontier['medianElapsedSeconds'])} "
            f"| {format_duration(swarm['medianElapsedSeconds'])} "
            f"| {_format_time_percentage(frontier['medianElapsedSeconds'], swarm['medianElapsedSeconds'])} |"
        ),
        (
            f"| Frontier tokens (total / median) "
            f"| {format_integer(frontier['frontierTokens'])} / "
            f"{format_integer(frontier['medianFrontierTokens'])} "
            f"| {format_integer(swarm['frontierTokens'])} / "
            f"{format_integer(swarm['medianFrontierTokens'])} "
            f"| {format_integer(paired['frontierTokensSaved'])} saved "
            f"({format_percentage(paired['frontierTokensSavedPercent'])}) |"
        ),
        (
            f"| Local tokens (total / median) | — "
            f"| {format_integer(swarm['localTokens'])} / "
            f"{format_integer(swarm['medianLocalTokens'])} "
            "| separate |"
        ),
        (
            f"| Repairs (total / median) | — "
            f"| {format_integer(swarm['repairs'])} / "
            f"{format_integer(swarm['medianRepairs'])} | — |"
        ),
        f"| Model loads | — | {format_integer(swarm['modelLoads'])} | — |",
        "",
        "| Task | Project | Frontier score | Frontier time | Frontier tokens "
        "| Swarm score | Swarm time | Swarm frontier tokens | Local tokens "
        "| Repairs | Loads | Review | Token delta | Time delta |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|",
    ])
    for row in summary["rows"]:
        frontier_result = row["frontier"]
        swarm_result = row["swarm"]
        lines.append(
            f"| [{row['caseId']}](benchmarks/results/{summary['suiteId']}/"
            f"cases/{row['caseId']}.json) | {row['project']} "
            f"| {frontier_result['score']} "
            f"| {format_duration(frontier_result['elapsedSeconds'])} "
            f"| {format_integer(_reported_total(frontier_result['frontierUsage']))} "
            f"| {swarm_result['score']} "
            f"| {format_duration(swarm_result['elapsedSeconds'])} "
            f"| {format_integer(_reported_total(swarm_result['frontierUsage']))} "
            f"| {format_integer(_local_total(swarm_result['localUsage']))} "
            f"| {swarm_result['repairs']} "
            f"| {swarm_result['modelLoads']} "
            f"| {swarm_result['reviewVerdict'] or 'not eligible'} "
            f"| {format_integer(row['frontierTokenDelta'])} "
            f"| {_signed_duration(row['timeDeltaSeconds'])} |"
        )
    lines.extend([
        "",
        f"Study: `{summary['suiteId']}` · paired cases: "
        f"{summary['completePairs']}/{summary['measuredCases']} · "
        f"95% bootstrap token-saving interval: "
        f"{_format_interval(paired['bootstrap95']['lower'], paired['bootstrap95']['upper'])}. "
        f"Accepted-by-both savings: "
        f"{format_integer(paired['acceptedByBothTokensSaved'])} tokens across "
        f"{paired['acceptedByBothCases']} cases.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def render_calibration_results(results: Sequence[dict[str, Any]]) -> str:
    lines = [
        "## Calibration evidence",
        "",
        "Calibration validates the harness and does not enter headline scores.",
        "",
        "| Task | Arm | Status | Score | Time | Frontier tokens |",
        "|---|---|---|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result['caseId']} | {result['arm']} | {result['status']} "
            f"| {result['score']} | {format_duration(result['elapsedSeconds'])} "
            f"| {format_integer(_reported_total(result['frontierUsage']))} |"
        )
    if not results:
        lines.append("| — | — | not available | — | — | — |")
    return "\n".join(lines).rstrip() + "\n"


def update_readme_economics(
    readme_path: Path,
    rendered: str,
    *,
    check: bool = False,
) -> bool:
    """Replace one generated README block, or verify it without writing."""
    text = readme_path.read_text(encoding="utf-8")
    block = f"{README_START}\n{rendered.rstrip()}\n{README_END}"
    if README_START in text or README_END in text:
        if text.count(README_START) != 1 or text.count(README_END) != 1:
            raise EvaluationError("README economics markers are malformed.")
        start = text.index(README_START)
        end = text.index(README_END) + len(README_END)
        updated = text[:start] + block + text[end:]
    else:
        updated = text.rstrip() + "\n\n" + block + "\n"
    changed = updated != text
    if check:
        if changed:
            raise EvaluationError("README economics table is out of date.")
        return False
    if changed:
        readme_path.write_text(updated, encoding="utf-8")
    return changed


class EvaluationStore:
    """Filesystem-backed immutable evaluation ledger."""

    def __init__(
        self,
        config: SwarmConfig,
        *,
        root: Path | None = None,
    ):
        self.config = config
        try:
            self.workspace_root = discover_git_root(config.source.parent)
        except WorkspaceError:
            self.workspace_root = config.source.parent.resolve()
        self.root = (
            root.resolve()
            if root is not None
            else (
                self.workspace_root / DEFAULT_EVALUATIONS_DIR
            ).resolve()
        )
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(
        self,
        profile: EvaluationProfile,
        *,
        clone: Callable[[EvaluationProfile, Path], Path] | None = None,
    ) -> dict[str, Any]:
        source = mlx_swarm_source_revision()
        if source["dirty"]:
            raise EvaluationError(
                "MLX Swarm source is dirty; commit the benchmark harness "
                "before freezing an evaluation."
            )
        container = inspect_container(profile)
        self._check_storage(profile)
        evaluation_id = (
            f"{profile.profile_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        ).lower()
        evaluation_dir = self.root / evaluation_id
        try:
            evaluation_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise EvaluationError(
                f"Evaluation already exists: {evaluation_id}"
            ) from exc
        _atomic_json(
            evaluation_dir / "profile.snapshot.json",
            profile_payload(profile),
        )
        clone_fn = clone or clone_benchmark_metadata
        metadata_root = clone_fn(profile, evaluation_dir / "benchmark")
        candidates = enumerate_bugsinpy_candidates(metadata_root, profile)
        resolve_case_commits(
            candidates,
            evaluation_dir / "cache" / "repositories",
        )
        runner = EvaluationRunner(self.config, self, profile)
        excluded: list[dict[str, Any]] = []
        while True:
            pilot, measured = select_cases(candidates, profile)
            cases = [
                evaluation_case(value, phase)
                for phase, values in (
                    ("pilot", pilot),
                    ("measured", measured),
                )
                for value in values
            ]
            failures: list[dict[str, Any]] = []
            for case in cases:
                try:
                    runtime_path = (
                        evaluation_dir
                        / "cases"
                        / case["caseId"]
                        / "runtime.json"
                    )
                    was_prepared = runtime_path.is_file()
                    runner.prepare_case(
                        evaluation_dir,
                        case,
                        retain_mirror=True,
                    )
                    if not was_prepared:
                        self._check_storage(profile)
                except Exception as exc:
                    failures.append({
                        "caseId": case["caseId"],
                        "reason": str(exc)[:8_000],
                    })
                    shutil.rmtree(
                        evaluation_dir / "cases" / case["caseId"],
                        ignore_errors=True,
                    )
            if not failures:
                break
            failed_ids = {value["caseId"] for value in failures}
            excluded.extend(failures)
            _atomic_json(
                evaluation_dir / "preparation-exclusions.json",
                {
                    "schemaVersion": 1,
                    "cases": excluded,
                    "recordedAt": utc_now(),
                },
            )
            candidates = [
                value
                for value in candidates
                if value["caseId"] not in failed_ids
            ]
        selected_ids = {case["caseId"] for case in cases}
        cases_root = evaluation_dir / "cases"
        if cases_root.is_dir():
            for case_dir in cases_root.iterdir():
                if (
                    case_dir.is_dir()
                    and case_dir.name not in selected_ids
                ):
                    shutil.rmtree(case_dir)
        self._check_storage(profile)
        _atomic_json(
            evaluation_dir / "preparation-exclusions.json",
            {
                "schemaVersion": 1,
                "cases": excluded,
                "recordedAt": utc_now(),
            },
        )
        source_after = mlx_swarm_source_revision()
        if source_after != source:
            raise EvaluationError(
                "MLX Swarm source changed while preparing the evaluation."
            )
        suite = {
            "schemaVersion": SUITE_SCHEMA_VERSION,
            "suiteId": evaluation_id,
            "profileId": profile.profile_id,
            "benchmark": {
                "name": "BugsInPy",
                "repository": profile.benchmark_repository,
                "revision": profile.benchmark_revision,
            },
            "seed": profile.seed,
            "createdAt": utc_now(),
            "cases": cases,
        }
        validate_suite(suite, profile)
        _atomic_json(evaluation_dir / "suite.json", suite)
        # Metadata can contain reference patches and mirrors contain future
        # commits. The frozen suite is sufficient to reproduce selection, so
        # remove both before any model process can inspect an arm workspace.
        remove_sensitive_preparation_sources(evaluation_dir)
        state = {
            "schemaVersion": EVALUATION_SCHEMA_VERSION,
            "evaluationId": evaluation_id,
            "status": "prepared",
            "pilotStatus": "pending",
            "measuredStatus": "locked",
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            "results": {},
        }
        _atomic_json(evaluation_dir / "evaluation.json", state)
        _atomic_json(
            evaluation_dir / "environment.json",
            environment_fingerprint(
                self.config,
                profile,
                container=container,
            ),
        )
        return self.detail(evaluation_id)

    def list(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return values
        for path in self.root.glob("*/evaluation.json"):
            try:
                state = _read_json(path)
            except (EvaluationError, OSError):
                continue
            values.append({
                "evaluationId": state.get("evaluationId"),
                "status": state.get("status"),
                "pilotStatus": state.get("pilotStatus"),
                "measuredStatus": state.get("measuredStatus"),
                "updatedAt": state.get("updatedAt"),
                "dir": str(path.parent),
            })
        values.sort(key=lambda value: value.get("updatedAt") or "", reverse=True)
        return values

    def detail(self, evaluation_id: str) -> dict[str, Any]:
        evaluation_dir = self._dir(evaluation_id)
        state = _read_json(evaluation_dir / "evaluation.json")
        suite = _read_json(evaluation_dir / "suite.json")
        summary = None
        if (evaluation_dir / "summary.json").is_file():
            summary = _read_json(evaluation_dir / "summary.json")
        results = self.load_results(evaluation_id)
        return {
            "evaluation": state,
            "suite": suite,
            "summary": summary,
            "results": results,
            "environment": _read_json(
                evaluation_dir / "environment.json"
            ),
        }

    def load_results(self, evaluation_id: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        results_root = self._dir(evaluation_id) / "results"
        if not results_root.is_dir():
            return results
        for path in sorted(results_root.glob("*/*.json")):
            try:
                results.append(validate_arm_result(_read_json(path)))
            except (EvaluationError, OSError):
                continue
        return results

    def record_result(
        self,
        evaluation_id: str,
        result: dict[str, Any],
    ) -> None:
        result = validate_arm_result(result)
        evaluation_dir = self._dir(evaluation_id)
        suite = _read_json(evaluation_dir / "suite.json")
        case_ids = {case["caseId"] for case in suite["cases"]}
        if result["caseId"] not in case_ids:
            raise EvaluationError("Arm result case is absent from the suite.")
        path = (
            evaluation_dir
            / "results"
            / result["caseId"]
            / f"{result['arm']}.json"
        )
        _exclusive_json(path, result)
        state_path = evaluation_dir / "evaluation.json"
        state = _read_json(state_path)
        state["updatedAt"] = utc_now()
        state.setdefault("results", {})[
            f"{result['caseId']}:{result['arm']}"
        ] = str(path.relative_to(evaluation_dir))
        _atomic_json(state_path, state)

    def finalize_phase(
        self,
        evaluation_id: str,
        phase: str,
    ) -> dict[str, Any]:
        if phase not in {"pilot", "measured"}:
            raise EvaluationError("Evaluation phase must be pilot or measured.")
        evaluation_dir = self._dir(evaluation_id)
        state_path = evaluation_dir / "evaluation.json"
        state = _read_json(state_path)
        suite = _read_json(evaluation_dir / "suite.json")
        expected = {
            (case["caseId"], arm)
            for case in suite["cases"]
            if case["phase"] == phase
            for arm in ("frontier-alone", "mlx-swarm")
        }
        observed = {
            (result["caseId"], result["arm"])
            for result in self.load_results(evaluation_id)
            if result["phase"] == phase
        }
        missing = sorted(expected - observed)
        if missing:
            raise EvaluationError(
                f"Evaluation phase is incomplete; missing {len(missing)} arm results."
            )
        if phase == "pilot":
            # Calibration unlocks measured work only when evidence itself is
            # valid. Pilot model scores never gate the measured phase.
            state["pilotStatus"] = "completed"
            state["measuredStatus"] = "pending"
            state["status"] = "pilot_completed"
        else:
            if state.get("pilotStatus") != "completed":
                raise EvaluationError(
                    "Measured phase remains locked until pilot completion."
                )
            state["measuredStatus"] = "completed"
            state["status"] = "completed"
        state["updatedAt"] = utc_now()
        _atomic_json(state_path, state)
        if phase == "measured":
            summary = aggregate_results(
                suite,
                self.load_results(evaluation_id),
            )
            summary["preparation"] = preparation_summary(
                evaluation_dir,
                suite,
            )
            summary["study"] = study_context(
                suite,
                _read_json(evaluation_dir / "environment.json"),
            )
            _atomic_json(evaluation_dir / "summary.json", summary)
        return self.detail(evaluation_id)

    def report(
        self,
        evaluation_id: str,
        export_dir: Path,
        *,
        check: bool = False,
    ) -> dict[str, Any]:
        detail = self.detail(evaluation_id)
        summary = detail["summary"]
        if summary is None:
            raise EvaluationError(
                "Measured phase must be complete before exporting a report."
            )
        export_dir = export_dir.resolve()
        if not check and export_dir.exists() and any(export_dir.iterdir()):
            raise EvaluationError("Report export directory must be empty.")
        sanitized_rows: list[dict[str, Any]] = []
        for row in summary["rows"]:
            sanitized = sanitize_public_row(row)
            sanitized_rows.append(sanitized)
        public_summary = {
            **summary,
            "rows": sanitized_rows,
        }
        public_study = {
            "schemaVersion": RESULT_SCHEMA_VERSION,
            "suite": sanitize_suite(detail["suite"]),
            "environment": sanitize_environment(detail["environment"]),
        }
        calibration = [
            _sanitize_arm(result)
            for result in detail["results"]
            if result["phase"] == "pilot"
        ]
        calibration.sort(key=lambda value: (value["caseId"], value["arm"]))
        readme_report = render_readme_economics(public_summary)
        detailed_report = (
            readme_report.rstrip()
            + "\n\n"
            + render_calibration_results(calibration)
        )
        if check:
            expected_paths = {
                Path("summary.json"),
                Path("study.json"),
                Path("calibration.json"),
                Path("report.md"),
                *{
                    Path("cases") / f"{row['caseId']}.json"
                    for row in sanitized_rows
                },
            }
            actual_paths = {
                path.relative_to(export_dir)
                for path in export_dir.rglob("*")
                if path.is_file()
            } if export_dir.is_dir() else set()
            if actual_paths != expected_paths:
                raise EvaluationError(
                    "Sanitized evaluation export is missing or out of date."
                )
            if (
                _read_json(export_dir / "summary.json") != public_summary
                or _read_json(export_dir / "study.json") != public_study
                or _read_json(export_dir / "calibration.json").get("results")
                != calibration
                or (export_dir / "report.md").read_text(encoding="utf-8")
                != detailed_report
            ):
                raise EvaluationError(
                    "Sanitized evaluation export is out of date."
                )
            for row in sanitized_rows:
                if _read_json(
                    export_dir / "cases" / f"{row['caseId']}.json"
                ) != row:
                    raise EvaluationError(
                        "Sanitized case evidence is out of date."
                    )
        else:
            export_dir.mkdir(parents=True, exist_ok=True)
            cases_dir = export_dir / "cases"
            cases_dir.mkdir()
            for row in sanitized_rows:
                _atomic_json(
                    cases_dir / f"{row['caseId']}.json",
                    row,
                )
            _atomic_json(export_dir / "summary.json", public_summary)
            _atomic_json(export_dir / "study.json", public_study)
            _atomic_json(
                export_dir / "calibration.json",
                {
                    "schemaVersion": RESULT_SCHEMA_VERSION,
                    "results": calibration,
                },
            )
            (export_dir / "report.md").write_text(
                detailed_report,
                encoding="utf-8",
            )
        return {
            "evaluationId": evaluation_id,
            "exportDir": str(export_dir),
            "summary": public_summary,
            "readmeMarkdown": readme_report,
        }

    def _dir(self, evaluation_id: str) -> Path:
        _identifier(evaluation_id, "evaluationId")
        path = (self.root / evaluation_id).resolve()
        if not _is_within(path, self.root) or not path.is_dir():
            raise EvaluationError(f"Evaluation not found: {evaluation_id}")
        return path

    def _check_storage(self, profile: EvaluationProfile) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(self.root)
        if usage.free < profile.storage.min_free_bytes:
            raise EvaluationError(
                "Evaluation storage reserve is not available: "
                f"{usage.free} free, {profile.storage.min_free_bytes} required."
            )
        current = directory_size(self.root)
        if current > profile.storage.max_bytes:
            raise EvaluationError(
                "Evaluation root already exceeds its configured storage ceiling."
            )


class EvaluationRunner:
    """Execute paired frontier-alone and MLX Swarm arms sequentially."""

    def __init__(
        self,
        config: SwarmConfig,
        store: EvaluationStore,
        profile: EvaluationProfile,
    ):
        self.config = config
        self.store = store
        self.profile = profile

    def run_phase(
        self,
        evaluation_id: str,
        phase: str,
    ) -> dict[str, Any]:
        if phase not in {"pilot", "measured"}:
            raise EvaluationError("Evaluation phase must be pilot or measured.")
        detail = self.store.detail(evaluation_id)
        current_source = mlx_swarm_source_revision()
        frozen_commit = detail["environment"].get("mlxSwarmCommit")
        if (
            current_source["dirty"]
            or current_source["commit"] != frozen_commit
        ):
            raise EvaluationError(
                "MLX Swarm source differs from the prepared evaluation."
            )
        current_container = inspect_container(self.profile)
        frozen_container = (
            detail["environment"].get("runtime", {}).get("container")
        )
        if current_container != frozen_container:
            raise EvaluationError(
                "Benchmark container differs from the prepared environment."
            )
        state = detail["evaluation"]
        if phase == "measured" and state.get("pilotStatus") != "completed":
            raise EvaluationError(
                "Measured work is locked until the six-case pilot completes."
            )
        evaluation_dir = self.store._dir(evaluation_id)
        suite = validate_suite(detail["suite"], self.profile)
        existing = {
            (result["caseId"], result["arm"])
            for result in detail["results"]
        }
        cases = [case for case in suite["cases"] if case["phase"] == phase]
        for case in cases:
            missing_arms = [
                arm
                for arm in ("frontier-alone", "mlx-swarm")
                if (case["caseId"], arm) not in existing
            ]
            if not missing_arms:
                continue
            with exclusive_case_lock(evaluation_dir, case["caseId"]):
                self.store._check_storage(self.profile)
                try:
                    runtime = self.prepare_case(evaluation_dir, case)
                except Exception as exc:
                    for arm in missing_arms:
                        self.store.record_result(
                            evaluation_id,
                            invalid_arm_result(
                                case,
                                arm,
                                f"Case preparation failed: {exc}",
                            ),
                        )
                    continue
                order = list(missing_arms)
                random.Random(
                    f"{suite['seed']}:{case['caseId']}"
                ).shuffle(order)
                for arm in order:
                    try:
                        result = (
                            self.run_frontier_alone(case, runtime)
                            if arm == "frontier-alone"
                            else self.run_mlx_swarm(case, runtime)
                        )
                        self.store._check_storage(self.profile)
                    except Exception as exc:
                        result = invalid_arm_result(
                            case,
                            arm,
                            f"Arm execution failed: {exc}",
                        )
                    self.store.record_result(evaluation_id, result)
        if phase == "pilot":
            pilot_results = [
                result
                for result in self.store.load_results(evaluation_id)
                if result["phase"] == "pilot"
            ]
            expected = len(cases) * 2
            if len(pilot_results) == expected and any(
                result["status"] == "invalid"
                or result["frontierUsage"]["usageStatus"] != "reported"
                for result in pilot_results
            ):
                raise EvaluationError(
                    "Pilot evidence is invalid; measured work remains locked."
                )
            validate_pilot_evidence(
                evaluation_dir,
                cases,
                pilot_results,
                self.profile,
                self.store,
            )
        return self.store.finalize_phase(evaluation_id, phase)

    def prepare_case(
        self,
        evaluation_dir: Path,
        case: dict[str, Any],
        *,
        retain_mirror: bool = False,
    ) -> dict[str, Any]:
        """Create a history-free case snapshot and prove its oracle."""
        started = time.perf_counter()
        case_root = evaluation_dir / "cases" / case["caseId"]
        runtime_path = case_root / "runtime.json"
        if runtime_path.is_file():
            runtime = _read_json(runtime_path)
            base = Path(runtime["baseSnapshot"])
            environment = Path(runtime["environment"])
            expected_dependency = dependency_environment_receipt(
                case,
                self.profile,
            )
            ready_path = environment / ".mlx-swarm-ready.json"
            ready = (
                _read_json(ready_path)
                if ready_path.is_file()
                else {}
            )
            if (
                base.is_dir()
                and environment.is_dir()
                and runtime.get("containerDigest")
                == self.profile.container.digest
                and runtime.get("dependencyContractSha256")
                == expected_dependency["sha256"]
                and ready.get("contract") == expected_dependency
            ):
                return runtime
            raise EvaluationError(
                "Prepared case runtime differs from the frozen profile."
            )
        case_root.mkdir(parents=True, exist_ok=True)
        for partial in ("base", "fixed-validation", "preflight"):
            partial_path = case_root / partial
            if partial_path.is_dir() and not partial_path.is_symlink():
                shutil.rmtree(partial_path)
            elif partial_path.exists() or partial_path.is_symlink():
                partial_path.unlink()
        (case_root / "verifier.json").unlink(missing_ok=True)
        mirror = (
            evaluation_dir
            / "cache"
            / "repositories"
            / f"{case['project']}.git"
        )
        if not mirror.is_dir():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            _run_checked(
                [
                    "git",
                    "clone",
                    "--mirror",
                    case["repository"],
                    str(mirror),
                ],
                cwd=mirror.parent,
                timeout=900,
            )
        else:
            _run_checked(
                ["git", "--git-dir", str(mirror), "fetch", "--prune", "origin"],
                cwd=mirror.parent,
                timeout=600,
            )
        for commit in (case["buggyCommit"], case["fixedCommit"]):
            result = run_command(
                ["git", "--git-dir", str(mirror), "cat-file", "-e", commit],
                cwd=mirror.parent,
                timeout=30,
            )
            if result.returncode != 0:
                raise EvaluationError(
                    f"Project mirror does not contain pinned commit {commit}."
                )
        work_root = case_root / "preflight"
        buggy_checkout = work_root / "buggy"
        fixed_checkout = work_root / "fixed"
        if work_root.exists():
            shutil.rmtree(work_root)
        work_root.mkdir(parents=True)
        _git_worktree_add(mirror, buggy_checkout, case["buggyCommit"])
        _git_worktree_add(mirror, fixed_checkout, case["fixedCommit"])
        try:
            copy_fixed_test_support(
                fixed_checkout,
                buggy_checkout,
                case["testFiles"],
            )
            base_snapshot = case_root / "base"
            if base_snapshot.exists():
                raise EvaluationError("Case base snapshot already exists.")
            validate_repository_symlinks(buggy_checkout)
            shutil.copytree(
                buggy_checkout,
                base_snapshot,
                ignore=shutil.ignore_patterns(".git"),
                symlinks=True,
            )
            environment = dependency_environment_path(
                evaluation_dir,
                case,
                self.profile,
            )
            self._prepare_environment(
                case,
                environment,
                evaluation_dir,
            )
            self._run_setup(
                case,
                base_snapshot,
                environment,
                evaluation_dir,
            )
            _remove_generated_state(base_snapshot)
            _init_snapshot_repository(base_snapshot)
            verifier_manifest = {
                "schemaVersion": 1,
                "caseId": case["caseId"],
                "evaluationRoot": str(evaluation_dir),
                "containerImage": self.profile.container.image,
                "containerDigest": self.profile.container.digest,
                "containerPlatform": self.profile.container.platform,
                "environment": str(environment),
                "commands": case["verificationArgv"],
                "timeoutSeconds": min(
                    self.profile.frontier.local_timeout_seconds,
                    1_800,
                ),
            }
            _atomic_json(case_root / "verifier.json", verifier_manifest)
            buggy_result = run_case_verifier(
                case_root / "verifier.json",
                base_snapshot,
            )
            fixed_runtime = case_root / "fixed-validation"
            validate_repository_symlinks(fixed_checkout)
            shutil.copytree(
                fixed_checkout,
                fixed_runtime,
                ignore=shutil.ignore_patterns(".git"),
                symlinks=True,
            )
            try:
                self._run_setup(
                    case,
                    fixed_runtime,
                    environment,
                    evaluation_dir,
                    cache_writes=False,
                )
                _remove_generated_state(fixed_runtime)
                fixed_result = run_case_verifier(
                    case_root / "verifier.json",
                    fixed_runtime,
                )
            finally:
                shutil.rmtree(fixed_runtime, ignore_errors=True)
            _atomic_json(
                case_root / "preflight-oracle.json",
                {
                    "schemaVersion": 1,
                    "caseId": case["caseId"],
                    "buggy": {
                        **buggy_result,
                        "evidence": buggy_result["evidence"][:MAX_LOG_BYTES],
                    },
                    "fixed": {
                        **fixed_result,
                        "evidence": fixed_result["evidence"][:MAX_LOG_BYTES],
                    },
                    "recordedAt": utc_now(),
                },
            )
            if buggy_result["passed"]:
                raise EvaluationError(
                    "Buggy snapshot unexpectedly passes the frozen oracle."
                )
            if not fixed_result["passed"]:
                raise EvaluationError(
                    "Fixed snapshot does not pass the frozen oracle."
                )
            failure_evidence = buggy_result["evidence"][:40_000]
            runtime = {
                "schemaVersion": 1,
                "caseId": case["caseId"],
                "evaluationRoot": str(evaluation_dir),
                "baseSnapshot": str(base_snapshot),
                "baseSha": _git_text(base_snapshot, ["rev-parse", "HEAD"]),
                "environment": str(environment),
                "containerDigest": self.profile.container.digest,
                "dependencyContractSha256": (
                    dependency_environment_receipt(
                        case,
                        self.profile,
                    )["sha256"]
                ),
                "verifierManifest": str(case_root / "verifier.json"),
                "failureEvidence": failure_evidence,
                "preparationSeconds": time.perf_counter() - started,
                "buggyOracleSeconds": buggy_result["elapsedSeconds"],
                "fixedOracleSeconds": fixed_result["elapsedSeconds"],
                "preparedAt": utc_now(),
            }
            _atomic_json(runtime_path, runtime)
            return runtime
        finally:
            _git_worktree_remove(mirror, buggy_checkout)
            _git_worktree_remove(mirror, fixed_checkout)
            if not retain_mirror:
                shutil.rmtree(mirror, ignore_errors=True)
            shutil.rmtree(work_root, ignore_errors=True)

    def _prepare_environment(
        self,
        case: dict[str, Any],
        environment: Path,
        evaluation_dir: Path,
    ) -> None:
        ready_path = environment / ".mlx-swarm-ready.json"
        expected = dependency_environment_receipt(case, self.profile)
        if ready_path.is_file():
            ready = _read_json(ready_path)
            if (
                ready.get("contract") == expected
                and isinstance(ready.get("resolvedLock"), list)
                and ready["resolvedLock"]
            ):
                return
        if environment.exists():
            shutil.rmtree(environment)
        environment.parent.mkdir(parents=True, exist_ok=True)
        container_environment = container_path(
            environment,
            evaluation_dir,
        )
        create_argv = docker_case_argv(
            self.profile,
            evaluation_dir,
            evaluation_dir,
            [
                "uv",
                "venv",
                "--python",
                case["pythonVersion"],
                container_environment,
            ],
            network="bridge",
        )
        _run_checked(
            create_argv,
            cwd=evaluation_dir,
            timeout=900,
        )
        _run_checked(
            docker_case_argv(
                self.profile,
                evaluation_dir,
                evaluation_dir,
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    f"{container_environment}/bin/python",
                    *self.profile.python_bootstrap,
                ],
                network="bridge",
            ),
            cwd=evaluation_dir,
            timeout=900,
        )
        requirements = case_dependency_constraints(case, self.profile)
        roots = dependency_requirements(
            requirements,
            case["project"],
            self.profile,
        )
        constraints_path = (
            environment.parent / f"{environment.name}.constraints.txt"
        )
        constraints_text = "\n".join(requirements) + "\n"
        constraints_path.write_text(constraints_text, encoding="utf-8")
        if roots:
            _run_checked(
                docker_case_argv(
                    self.profile,
                    evaluation_dir,
                    evaluation_dir,
                    [
                        "uv",
                        "pip",
                        "install",
                        "--python",
                        f"{container_environment}/bin/python",
                        "--constraint",
                        container_path(constraints_path, evaluation_dir),
                        *roots,
                    ],
                    network="bridge",
                ),
                cwd=evaluation_dir,
                timeout=2_700,
            )
        freeze = _run_checked(
            docker_case_argv(
                self.profile,
                evaluation_dir,
                evaluation_dir,
                [
                    "uv",
                    "pip",
                    "freeze",
                    "--python",
                    f"{container_environment}/bin/python",
                ],
                network="none",
            ),
            cwd=evaluation_dir,
            timeout=300,
        )
        resolved_lock = validate_resolved_dependencies(
            freeze.stdout,
            requirements,
            self.profile.python_bootstrap,
        )
        _atomic_json(
            ready_path,
            {
                "contract": expected,
                "resolvedLock": resolved_lock,
            },
        )

    def _run_setup(
        self,
        case: dict[str, Any],
        workspace: Path,
        environment: Path,
        evaluation_dir: Path,
        *,
        deadline: float | None = None,
        cache_writes: bool = True,
    ) -> None:
        for raw_argv in case["setupArgv"]:
            if deadline is not None and time.monotonic() >= deadline:
                raise EvaluationError("Arm deadline expired during setup.")
            if is_dependency_or_project_install(raw_argv):
                # Dependencies are frozen in the shared, digest-addressed
                # environment. Project imports resolve from the disposable
                # workspace and never mutate that environment.
                continue
            container_environment = Path(
                container_path(environment, evaluation_dir)
            )
            argv = rewrite_benchmark_argv(
                raw_argv,
                container_environment,
                for_setup=True,
                require_environment=False,
            )
            argv = normalize_setup_parallelism(argv)
            _run_checked(
                docker_case_argv(
                    self.profile,
                    evaluation_dir,
                    workspace,
                    argv,
                    network="none",
                    extra_env=(
                        ()
                        if cache_writes
                        else ("CCACHE_DISABLE=true",)
                    ),
                ),
                cwd=evaluation_dir,
                timeout=(
                    1_800
                    if deadline is None
                    else max(
                        1,
                        min(1_800, math.floor(deadline - time.monotonic())),
                    )
                ),
            )

    def _uv_environment(self, root: Path) -> dict[str, str]:
        # Retained for compatibility with callers that only need a sanitized
        # host environment. Benchmark dependencies never execute on the host.
        return {
            "PATH": os.environ.get("PATH", ""),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "UV_CACHE_DIR": str(root / "uv-cache"),
            "UV_PYTHON_INSTALL_DIR": str(root / "uv-python"),
        }

    def run_frontier_alone(
        self,
        case: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        arm_root = (
            Path(runtime["baseSnapshot"]).parent
            / "arms"
            / "frontier-alone"
        )
        repository = fresh_arm_repository(
            Path(runtime["baseSnapshot"]),
            arm_root / "repo",
        )
        base_sha = _git_text(repository, ["rev-parse", "HEAD"])
        task_packet = build_task_packet(case, runtime)
        evidence_root = arm_root / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=True)
        last_message = evidence_root / "last-message.txt"
        started = time.perf_counter()
        command = codex_command(
            self.profile,
            cwd=repository,
            sandbox="workspace-write",
            output_last_message=last_message,
        )
        codex_result = run_command(
            command,
            cwd=repository,
            timeout=self.profile.frontier.arm_timeout_seconds,
            env=frontier_environment(),
            input_text=frontier_alone_prompt(task_packet),
        )
        (evidence_root / "events.jsonl").write_text(
            codex_result.stdout,
            encoding="utf-8",
        )
        (evidence_root / "stderr.log").write_text(
            codex_result.stderr,
            encoding="utf-8",
        )
        usage = usage_with_phases([
            ("frontier-alone", parse_codex_usage_jsonl(codex_result.stdout))
        ])
        diff = _git_diff(repository, base_sha)
        patch = persist_candidate_patch(evidence_root, diff)
        structural_error = validate_candidate_diff(
            diff,
            case,
            repository,
        )
        remaining = self.profile.frontier.arm_timeout_seconds - (
            time.perf_counter() - started
        )
        if structural_error is None and diff and remaining > 0:
            oracle = self._score_candidate(
                case,
                runtime,
                diff,
                arm_root,
                timeout_seconds=remaining,
            )
        else:
            oracle = {
                "passed": False,
                "exitCode": None,
                "evidence": (
                    "Arm deadline expired before independent oracle."
                    if remaining <= 0
                    else structural_error or "No candidate patch produced."
                ),
                "timedOut": remaining <= 0,
            }
        elapsed = time.perf_counter() - started
        deadline_expired = bool(
            elapsed >= self.profile.frontier.arm_timeout_seconds
            or oracle.get("timedOut")
        )
        completed = bool(
            diff
            and structural_error is None
            and not codex_result.timed_out
            and codex_result.returncode == 0
            and not deadline_expired
        )
        status = (
            "timed_out"
            if codex_result.timed_out or deadline_expired
            else "completed"
            if completed
            else "failed"
        )
        return make_arm_result(
            case=case,
            arm="frontier-alone",
            status=status,
            completed=completed,
            score=1 if oracle["passed"] else 0,
            elapsed_seconds=elapsed,
            phase_seconds={
                "frontier": codex_result.elapsed_seconds,
                "oracle": oracle.get("elapsedSeconds", 0.0),
            },
            frontier_usage=usage,
            local_usage=empty_local_usage(),
            repairs=0,
            model_loads=0,
            review_verdict=None,
            patch=patch,
            oracle=oracle,
        )

    def run_mlx_swarm(
        self,
        case: dict[str, Any],
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        arm_root = (
            Path(runtime["baseSnapshot"]).parent
            / "arms"
            / "mlx-swarm"
        )
        repository = fresh_arm_repository(
            Path(runtime["baseSnapshot"]),
            arm_root / "repo",
        )
        base_sha = _git_text(repository, ["rev-parse", "HEAD"])
        config_path = repository / ".mlx-swarm-eval.json"
        artifacts_root = arm_root / "artifacts"
        write_evaluation_config(
            self.config,
            config_path,
            artifacts_root,
            Path(runtime["verifierManifest"]),
            repository,
        )
        eval_config = load_config(config_path)
        store = CommanderStore(eval_config)
        task_packet = build_task_packet(case, runtime)
        request = store.create_request(
            case["objective"],
            [
                "Modify production code only; never modify tests or benchmark evidence.",
                "Use the bugsinpy-acceptance verification profile for every mutating artifact.",
                "Return a schema-v2 typed workspace plan.",
                task_packet,
            ],
            request_id=f"eval-{case['caseId']}",
        )
        claim = store.claim_plan(
            request["request"]["requestId"],
            adapter="codex-cli-evaluation",
        )
        evidence_root = arm_root / "evidence"
        evidence_root.mkdir(parents=True, exist_ok=True)
        plan_response = evidence_root / "plan-response.json"
        started = time.perf_counter()
        deadline = started + self.profile.frontier.arm_timeout_seconds
        plan_codex = run_command(
            codex_command(
                self.profile,
                cwd=repository,
                sandbox="read-only",
                output_last_message=plan_response,
            ),
            cwd=repository,
            timeout=self.profile.frontier.planning_timeout_seconds,
            env=frontier_environment(),
            input_text=Path(claim["promptPath"]).read_text(encoding="utf-8"),
        )
        (evidence_root / "plan-events.jsonl").write_text(
            plan_codex.stdout,
            encoding="utf-8",
        )
        plan_usage = parse_codex_usage_jsonl(plan_codex.stdout)
        if (
            plan_codex.timed_out
            or plan_codex.returncode != 0
            or not plan_response.is_file()
        ):
            return make_arm_result(
                case=case,
                arm="mlx-swarm",
                status="timed_out" if plan_codex.timed_out else "failed",
                completed=False,
                score=0,
                elapsed_seconds=time.perf_counter() - started,
                phase_seconds={"planning": plan_codex.elapsed_seconds},
                frontier_usage=usage_with_phases([("planning", plan_usage)]),
                local_usage=empty_local_usage(),
                repairs=0,
                model_loads=0,
                review_verdict=None,
                patch={"sha256": None, "changedFiles": 0},
                oracle={
                    "passed": False,
                    "exitCode": None,
                    "evidence": "Frontier planning did not produce a valid response.",
                },
            )
        imported = store.import_plan(
            request["request"]["requestId"],
            plan_response,
            claim_id=claim["claimId"],
            adapter="codex-cli-evaluation",
            provider="openai-codex",
            model=self.profile.frontier.model,
            prompt_tokens=plan_usage.get("promptTokens"),
            completion_tokens=plan_usage.get("completionTokens"),
            total_tokens=plan_usage.get("totalTokens"),
        )
        plan_digest = imported["plan"]["digest"]
        execution = imported["executionPreview"]
        if not isinstance(execution, dict):
            raise EvaluationError("Frontier plan has no execution preview.")
        plan, plan_path, approval, receipt, request_state = store.approved_plan(
            request["request"]["requestId"],
            plan_digest,
            source="evaluation-harness",
            execution_digest=execution["executionDigest"],
        )
        run_id = _run_id()
        session_dir = artifacts_root / plan.plan_id / run_id
        snapshot = prepare_worktree(
            eval_config,
            plan,
            session_id=run_id,
            expected_execution_digest=execution["executionDigest"],
        )
        session = Session(
            session_dir,
            plan,
            session_id=run_id,
            launch_source="evaluation",
        )
        session.set_sources(
            config_source=config_path,
            plan_source=plan_path,
        )
        session.state["maxRepair"] = self.profile.max_repair
        session._save()
        session.attach_workspace(
            snapshot,
            execution_approval={
                "schemaVersion": 1,
                "planSha256": plan_digest,
                "executionDigest": execution["executionDigest"],
                "workspaceRoot": execution["workspaceRoot"],
                "baseSha": execution["baseSha"],
                "approvedAt": utc_now(),
                "source": "evaluation-harness",
            },
        )
        session.attach_commander(
            request_id=request["request"]["requestId"],
            approval=approval.to_json(),
            planning_receipt=receipt,
            revision_of=request_state.get("revisionOf"),
        )
        store.mark_launched(
            request["request"]["requestId"],
            approval,
            plan_id=plan.plan_id,
            session_id=run_id,
        )
        local_started = time.perf_counter()
        local_timeout = min(
            self.profile.frontier.local_timeout_seconds,
            max(1, math.floor(deadline - time.perf_counter())),
        )
        local_result = run_swarm_with_synthetic_operator(
            eval_config,
            plan_path,
            session_dir,
            self.profile.max_repair,
            timeout=local_timeout,
        )
        local_seconds = time.perf_counter() - local_started
        session = Session.load(session_dir, eval_config)
        local_usage = session.local_usage()
        repairs = sum(
            int(task.get("repairAttempts", 0))
            for task in session.state.get("tasks", {}).values()
        )
        workspace = load_workspace_snapshot(session_dir)
        diff, _ = final_workspace_diff(workspace)
        patch = persist_candidate_patch(evidence_root, diff)
        structural_error = validate_candidate_diff(diff, case, repository)
        review_usage = None
        review_verdict = None
        review_seconds = 0.0
        review_timed_out = False
        if (
            session.state.get("status") == "completed"
            and time.perf_counter() < deadline
        ):
            review_claim = store.claim_review(
                session_dir,
                adapter="codex-cli-evaluation",
            )
            review_response = evidence_root / "review-response.json"
            review_result = run_command(
                codex_command(
                    self.profile,
                    cwd=repository,
                    sandbox="read-only",
                    output_last_message=review_response,
                ),
                cwd=repository,
                timeout=min(
                    self.profile.frontier.review_timeout_seconds,
                    max(1, math.floor(deadline - time.perf_counter())),
                ),
                env=frontier_environment(),
                input_text=Path(review_claim["promptPath"]).read_text(
                    encoding="utf-8"
                ),
            )
            review_seconds = review_result.elapsed_seconds
            review_timed_out = review_result.timed_out
            (evidence_root / "review-events.jsonl").write_text(
                review_result.stdout,
                encoding="utf-8",
            )
            review_usage = parse_codex_usage_jsonl(review_result.stdout)
            if (
                not review_result.timed_out
                and review_result.returncode == 0
                and review_response.is_file()
            ):
                imported_review = store.import_review(
                    session_dir,
                    review_response,
                    claim_id=review_claim["claimId"],
                    adapter="codex-cli-evaluation",
                    provider="openai-codex",
                    model=self.profile.frontier.model,
                    prompt_tokens=review_usage.get("promptTokens"),
                    completion_tokens=review_usage.get("completionTokens"),
                    total_tokens=review_usage.get("totalTokens"),
                )
                review_verdict = imported_review["review"]["verdict"]
        remaining = deadline - time.perf_counter()
        if structural_error is None and diff and remaining > 0:
            oracle = self._score_candidate(
                case,
                runtime,
                diff,
                arm_root,
                timeout_seconds=remaining,
            )
        else:
            oracle = {
                "passed": False,
                "exitCode": None,
                "evidence": (
                    "Arm deadline expired before independent oracle."
                    if remaining <= 0
                    else structural_error or "No candidate patch produced."
                ),
                "timedOut": remaining <= 0,
            }
        phases = [("planning", plan_usage)]
        if review_usage is not None:
            phases.append(("review", review_usage))
        usage = usage_with_phases(phases)
        completed = bool(
            session.state.get("status") == "completed"
            and review_verdict is not None
            and not local_result.timed_out
            and not oracle.get("timedOut")
        )
        deadline_expired = (
            time.perf_counter() >= deadline or bool(oracle.get("timedOut"))
        )
        status = (
            "timed_out"
            if local_result.timed_out or review_timed_out or deadline_expired
            else "completed"
            if completed
            else "failed"
        )
        elapsed = time.perf_counter() - started
        return make_arm_result(
            case=case,
            arm="mlx-swarm",
            status=status,
            completed=completed,
            score=1 if oracle["passed"] else 0,
            elapsed_seconds=elapsed,
            phase_seconds={
                "planning": plan_codex.elapsed_seconds,
                "local": local_seconds,
                "review": review_seconds,
                "oracle": oracle.get("elapsedSeconds", 0.0),
            },
            frontier_usage=usage,
            local_usage=local_usage,
            repairs=repairs,
            model_loads=int(local_usage.get("modelLoads", 0)),
            review_verdict=review_verdict,
            patch=patch,
            oracle=oracle,
        )

    def _score_candidate(
        self,
        case: dict[str, Any],
        runtime: dict[str, Any],
        diff: str,
        arm_root: Path,
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        oracle_root = arm_root / "oracle"
        if oracle_root.exists():
            shutil.rmtree(oracle_root)
        repository = fresh_arm_repository(
            Path(runtime["baseSnapshot"]),
            oracle_root,
        )
        apply_result = run_command(
            ["git", "apply", "--index", "-"],
            cwd=repository,
            timeout=max(1, min(60, math.floor(deadline - time.monotonic()))),
            input_text=diff,
        )
        if apply_result.returncode != 0:
            return {
                "passed": False,
                "exitCode": apply_result.returncode,
                "evidence": (
                    "Independent oracle could not apply candidate patch: "
                    + (apply_result.stderr or apply_result.stdout)
                )[:MAX_LOG_BYTES],
                "elapsedSeconds": apply_result.elapsed_seconds,
            }
        return run_case_verifier(
            Path(runtime["verifierManifest"]),
            repository,
            timeout_seconds=max(0.0, deadline - time.monotonic()),
        )


@contextmanager
def exclusive_case_lock(
    evaluation_dir: Path,
    case_id: str,
) -> Iterable[None]:
    """Prevent concurrent preparation or execution of the same frozen case."""
    _identifier(case_id, "caseId")
    locks = evaluation_dir / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    path = locks / f"{case_id}.lock"
    token = f"{os.getpid()}:{time.time_ns()}"
    for attempt in range(2):
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            owner = ""
            try:
                owner = path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            pid_text = owner.partition(":")[0]
            if attempt == 0 and pid_text.isdigit() and not _pid_is_alive(
                int(pid_text)
            ):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise EvaluationError(
                f"Evaluation case is already locked: {case_id}"
            )
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            break
    else:  # pragma: no cover - loop always returns or raises
        raise EvaluationError(f"Could not lock evaluation case: {case_id}")
    try:
        yield
    finally:
        try:
            if path.read_text(encoding="utf-8").strip() == token:
                path.unlink()
        except FileNotFoundError:
            pass


def validate_pilot_evidence(
    evaluation_dir: Path,
    cases: Sequence[dict[str, Any]],
    results: Sequence[dict[str, Any]],
    profile: EvaluationProfile,
    store: EvaluationStore,
) -> None:
    """Prove the calibration gate before measured work can be unlocked."""
    if len(results) != len(cases) * 2:
        raise EvaluationError("Pilot serialization is incomplete.")
    for result in results:
        validate_arm_result(result)
        if result["frontierUsage"]["usageStatus"] != "reported":
            raise EvaluationError(
                "Pilot usage capture is incomplete; measured work is locked."
            )
    for case in cases:
        runtime_path = (
            evaluation_dir / "cases" / case["caseId"] / "runtime.json"
        )
        runtime = _read_json(runtime_path)
        base = Path(_text(runtime.get("baseSnapshot"), "runtime.baseSnapshot"))
        environment = Path(
            _text(runtime.get("environment"), "runtime.environment")
        )
        if not base.is_dir() or not environment.is_dir():
            raise EvaluationError("Pilot preparation evidence is incomplete.")
        if _git_text(base, ["remote"]):
            raise EvaluationError(
                "Pilot isolation failed: frozen workspace retains a remote."
            )
        if len(_git_text(base, ["rev-list", "--all"]).splitlines()) != 1:
            raise EvaluationError(
                "Pilot isolation failed: frozen workspace exposes history."
            )
        _number(
            runtime.get("preparationSeconds"),
            "runtime.preparationSeconds",
            0,
            10**9,
        )
    store._check_storage(profile)


def run_swarm_with_synthetic_operator(
    config: SwarmConfig,
    plan_path: Path,
    session_dir: Path,
    max_repair: int,
    *,
    timeout: int,
) -> CommandResult:
    argv = [
        sys.executable,
        "-m",
        "mlx_swarm.cli",
        "--config",
        str(config.source),
        "run",
        str(plan_path),
        "--session-dir",
        str(session_dir),
        "--max-repair",
        str(max_repair),
    ]
    started = time.perf_counter()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + timeout
    decided: set[str] = set()
    timed_out = False
    while process.poll() is None:
        if time.monotonic() >= deadline:
            timed_out = True
            try:
                os.killpg(process.pid, 15)
            except ProcessLookupError:
                pass
            break
        state_path = session_dir / "session.json"
        if state_path.is_file():
            try:
                state = _read_json(state_path)
            except EvaluationError:
                time.sleep(0.1)
                continue
            for task_id, task in state.get("tasks", {}).items():
                status = task.get("status")
                if status == "awaiting_approval" and task_id not in decided:
                    manifest, _ = load_artifact(session_dir, task_id)
                    submit_artifact_decision(
                        session_dir,
                        task_id,
                        action="apply",
                        artifact_sha256=manifest["sha256"],
                        source="evaluation-harness",
                    )
                    decided.add(task_id)
                elif (
                    status == "verification_failed"
                    and f"reject:{task_id}" not in decided
                ):
                    manifest, _ = load_artifact(session_dir, task_id)
                    submit_artifact_decision(
                        session_dir,
                        task_id,
                        action="reject",
                        artifact_sha256=manifest["sha256"],
                        source="evaluation-harness",
                        reason="Frozen evaluation verifier failed.",
                    )
                    decided.add(f"reject:{task_id}")
        time.sleep(0.1)
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
    return CommandResult(
        argv=tuple(argv),
        returncode=process.returncode,
        stdout=stdout[:MAX_LOG_BYTES].decode("utf-8", "replace"),
        stderr=stderr[:MAX_LOG_BYTES].decode("utf-8", "replace"),
        elapsed_seconds=time.perf_counter() - started,
        timed_out=timed_out,
    )


def run_case_verifier(
    manifest_path: Path,
    workspace: Path,
    *,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    manifest = _read_json(manifest_path)
    _exact_keys(
        manifest,
        "verifier",
        {
            "schemaVersion",
            "caseId",
            "evaluationRoot",
            "containerImage",
            "containerDigest",
            "containerPlatform",
            "environment",
            "commands",
            "timeoutSeconds",
        },
    )
    if _integer(manifest["schemaVersion"], "verifier.schemaVersion", 1, 1) != 1:
        raise EvaluationError("Unsupported verifier schema.")
    evaluation_root = Path(
        _text(manifest["evaluationRoot"], "verifier.evaluationRoot")
    ).resolve()
    if not evaluation_root.is_dir():
        raise EvaluationError("Verifier evaluation root is missing.")
    workspace = workspace.resolve()
    manifest_path = manifest_path.resolve()
    if (
        not _is_within(workspace, evaluation_root)
        or not _is_within(manifest_path, evaluation_root)
    ):
        raise EvaluationError(
            "Verifier paths must remain inside the frozen evaluation root."
        )
    container_image = _text(
        manifest["containerImage"],
        "verifier.containerImage",
    )
    container_digest = _text(
        manifest["containerDigest"],
        "verifier.containerDigest",
    )
    if _IMAGE_DIGEST.fullmatch(container_digest) is None:
        raise EvaluationError("Verifier container digest is invalid.")
    container_platform = _enum(
        manifest["containerPlatform"],
        "verifier.containerPlatform",
        {"linux/amd64", "linux/arm64"},
    )
    inspect_container_contract(
        container_image,
        container_digest,
        container_platform,
        cwd=evaluation_root,
    )
    environment = Path(
        _text(manifest["environment"], "verifier.environment")
    ).resolve()
    if (
        not environment.is_dir()
        or not _is_within(environment, evaluation_root)
    ):
        raise EvaluationError("Verifier environment is missing.")
    commands = _list(
        manifest["commands"],
        "verifier.commands",
        minimum=1,
        maximum=100,
    )
    timeout = _integer(
        manifest["timeoutSeconds"],
        "verifier.timeoutSeconds",
        1,
        3_600,
    )
    started = time.perf_counter()
    deadline = (
        time.monotonic() + timeout_seconds
        if timeout_seconds is not None
        else None
    )
    logs: list[str] = []
    exit_code = 0
    timed_out = False
    for index, raw_command in enumerate(commands):
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            exit_code = -1
            logs.append("Verifier deadline expired before the next command.")
            break
        argv = rewrite_benchmark_argv(
            _unique_or_repeated_text_array(
                raw_command,
                f"verifier.commands[{index}]",
                minimum=1,
                maximum=128,
            ),
            Path(container_path(environment, evaluation_root)),
            for_setup=False,
            require_environment=False,
        )
        result = run_command(
            docker_runtime_argv(
                image=container_image,
                platform_name=container_platform,
                evaluation_root=evaluation_root,
                cwd=workspace,
                argv=argv,
                network="none",
            ),
            cwd=evaluation_root,
            timeout=(
                timeout
                if deadline is None
                else max(
                    1,
                    min(timeout, math.floor(deadline - time.monotonic())),
                )
            ),
            max_output_bytes=MAX_LOG_BYTES,
        )
        logs.append(
            f"$ {' '.join(argv)}\n{result.stdout}{result.stderr}"
        )
        exit_code = result.returncode
        timed_out = result.timed_out
        if result.timed_out or result.returncode != 0:
            break
    evidence = "\n".join(logs)[-MAX_LOG_BYTES:]
    return {
        "passed": exit_code == 0 and not timed_out,
        "exitCode": exit_code,
        "evidence": evidence,
        "elapsedSeconds": time.perf_counter() - started,
        "timedOut": timed_out,
    }


def rewrite_benchmark_argv(
    raw_argv: Sequence[str],
    environment: Path,
    *,
    for_setup: bool,
    require_environment: bool = True,
) -> list[str]:
    if not raw_argv or raw_argv[0] not in _SAFE_BENCHMARK_COMMANDS:
        raise EvaluationError("Benchmark command is not allowlisted.")
    executable = raw_argv[0]
    rest = list(raw_argv[1:])
    python = environment / "bin" / "python"
    if require_environment and not python.is_file():
        raise EvaluationError("Benchmark virtual environment has no Python.")
    if executable in {"python", "python3"}:
        return [str(python), *rest]
    if executable in {"pytest", "py.test", "tox"}:
        module = "pytest" if executable in {"pytest", "py.test"} else "tox"
        return [str(python), "-m", module, *rest]
    if executable in {"pip", "pip3"}:
        if not for_setup or not rest or rest[0] != "install":
            raise EvaluationError(
                "Only allowlisted setup pip install commands are permitted."
            )
        return ["uv", "pip", *rest]
    return [executable, *rest]


def normalize_setup_parallelism(argv: Sequence[str]) -> list[str]:
    """Replace BugsInPy's ambiguous build_ext -j 0 with a fixed job count."""
    values = list(argv)
    if "setup.py" not in values or "build_ext" not in values:
        return values
    for flag in ("-j", "--parallel"):
        if flag not in values:
            continue
        index = values.index(flag) + 1
        if index < len(values) and values[index] == "0":
            values[index] = str(BENCHMARK_BUILD_JOBS)
    return values


def dependency_environment_receipt(
    case: dict[str, Any],
    profile: EvaluationProfile,
) -> dict[str, Any]:
    payload = {
        "schemaVersion": 1,
        "project": case["project"],
        "pythonVersion": case["pythonVersion"],
        "pythonBootstrap": list(profile.python_bootstrap),
        "requirements": case_dependency_constraints(case, profile),
        "dependencyRoots": list(
            profile.dependency_roots[case["project"]]
        ),
        "containerDigest": profile.container.digest,
        "containerPlatform": profile.container.platform,
    }
    return {
        **payload,
        "sha256": canonical_json_sha256(payload),
    }


def canonical_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def dependency_requirements(
    requirements: Sequence[str],
    project: str,
    profile: EvaluationProfile,
) -> list[str]:
    requirements = case_dependency_constraints(
        {"project": project, "requirements": list(requirements)},
        profile,
    )
    by_name: dict[str, str] = {}
    for requirement in requirements:
        match = re.match(r"^([A-Za-z0-9_.-]+)", requirement)
        if match is None:
            raise EvaluationError("Pinned dependency has no package name.")
        by_name[canonical_package_name(match.group(1))] = requirement
    selected: list[str] = []
    for root in profile.dependency_roots[project]:
        canonical = canonical_package_name(root)
        requirement = by_name.get(canonical)
        if requirement is None:
            raise EvaluationError(
                f"{project} dependency root is absent from the case freeze: {root}"
            )
        selected.append(requirement)
    return selected


def case_dependency_constraints(
    case: dict[str, Any],
    profile: EvaluationProfile,
) -> list[str]:
    values: dict[str, str] = {}
    order: list[str] = []
    for requirement in [
        *case["requirements"],
        *profile.dependency_pins[case["project"]],
    ]:
        match = re.match(r"^([A-Za-z0-9_.-]+)", requirement)
        if match is None:
            raise EvaluationError("Pinned dependency has no package name.")
        name = canonical_package_name(match.group(1))
        if name not in values:
            order.append(name)
        values[name] = requirement
    return [values[name] for name in order]


def validate_resolved_dependencies(
    freeze_output: str,
    constraints: Sequence[str],
    bootstrap: Sequence[str],
) -> list[str]:
    allowed: dict[str, set[str]] = {}
    for requirement in [*constraints, *bootstrap]:
        base = requirement.partition(";")[0].strip()
        match = re.fullmatch(
            r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==(.+)",
            base,
        )
        if match is None:
            raise EvaluationError("Dependency constraint is not exact.")
        allowed.setdefault(
            canonical_package_name(match.group(1)),
            set(),
        ).add(match.group(2))
    resolved: list[str] = []
    for raw_line in freeze_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==(.+)", line)
        if match is None:
            raise EvaluationError(
                f"Resolved dependency is not an exact registry pin: {line}"
            )
        name = canonical_package_name(match.group(1))
        if match.group(2) not in allowed.get(name, set()):
            raise EvaluationError(
                f"Resolved dependency escaped the frozen constraints: {line}"
            )
        resolved.append(line)
    if not resolved:
        raise EvaluationError("Resolved dependency lock is empty.")
    return sorted(resolved, key=str.lower)


def dependency_environment_path(
    evaluation_dir: Path,
    case: dict[str, Any],
    profile: EvaluationProfile,
) -> Path:
    receipt = dependency_environment_receipt(case, profile)
    project = _identifier(
        str(case["project"]).lower(),
        "case.project",
    )
    return (
        evaluation_dir
        / "cache"
        / "environments"
        / f"{project}-{receipt['sha256'][:16]}"
    )


def container_path(path: Path, evaluation_root: Path) -> str:
    root = evaluation_root.resolve()
    target = path.resolve()
    if not _is_within(target, root):
        raise EvaluationError(
            "Benchmark runtime path escapes the evaluation root."
        )
    relative = target.relative_to(root)
    return "/evaluation" if not relative.parts else f"/evaluation/{relative.as_posix()}"


def docker_runtime_argv(
    *,
    image: str,
    platform_name: str,
    evaluation_root: Path,
    cwd: Path,
    argv: Sequence[str],
    network: str,
    extra_env: Sequence[str] = (),
) -> list[str]:
    if network not in {"bridge", "none"}:
        raise EvaluationError("Unsupported benchmark container network mode.")
    if any(
        re.fullmatch(r"[A-Z][A-Z0-9_]*=[A-Za-z0-9._/: -]+", item) is None
        for item in extra_env
    ):
        raise EvaluationError("Invalid benchmark container environment value.")
    workdir = container_path(cwd, evaluation_root)
    container_name = (
        "mlx-swarm-eval-"
        + hashlib.sha256(
            f"{os.getpid()}:{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:20]
    )
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        "--label",
        "mlx-swarm.evaluation=true",
        "--platform",
        platform_name,
        "--network",
        network,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--volume",
        f"{evaluation_root.resolve()}:/evaluation",
        "--workdir",
        workdir,
        "--env",
        f"HOME={workdir}",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        f"PYTHONPATH={workdir}",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "NO_COLOR=1",
        "--env",
        "UV_CACHE_DIR=/tmp/uv-cache",
        "--env",
        "UV_PYTHON_INSTALL_DIR=/evaluation/cache/python",
        "--env",
        "CCACHE_DIR=/evaluation/cache/ccache",
        "--env",
        "CCACHE_MAXSIZE=2G",
        "--env",
        "CCACHE_BASEDIR=/evaluation",
        "--env",
        "CCACHE_NOHASHDIR=true",
        "--env",
        "CC=ccache gcc",
        "--env",
        "CXX=ccache g++",
        *[
            value
            for item in extra_env
            for value in ("--env", item)
        ],
        image,
        *argv,
    ]


def docker_case_argv(
    profile: EvaluationProfile,
    evaluation_root: Path,
    cwd: Path,
    argv: Sequence[str],
    *,
    network: str,
    extra_env: Sequence[str] = (),
) -> list[str]:
    return docker_runtime_argv(
        image=profile.container.image,
        platform_name=profile.container.platform,
        evaluation_root=evaluation_root,
        cwd=cwd,
        argv=argv,
        network=network,
        extra_env=extra_env,
    )


def is_dependency_or_project_install(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    if argv[0] in {"pip", "pip3"}:
        return True
    if argv[0] not in {"python", "python3"}:
        return False
    rest = list(argv[1:])
    if len(rest) >= 2 and rest[:2] == ["setup.py", "install"]:
        return True
    compact = "".join(rest)
    return compact.startswith("-mpipinstall")


def sanitized_case_environment(
    environment: Path,
    workspace: Path,
) -> dict[str, str]:
    path = f"{environment / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin"
    values = {
        "PATH": path,
        "HOME": str(workspace),
        "TMPDIR": tempfile.gettempdir(),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PYTHONPATH": str(workspace),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
    }
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        if name in os.environ:
            values[name] = os.environ[name]
    return values


def build_task_packet(
    case: dict[str, Any],
    runtime: dict[str, Any],
) -> str:
    return (
        f"CASE: {case['caseId']}\n"
        f"PROJECT: {case['project']}\n"
        f"OBJECTIVE: {case['objective']}\n"
        "BOUNDARY: Modify production code only. Do not modify tests, Git "
        "metadata, dependencies, or benchmark evidence.\n"
        "ACCEPTANCE COMMANDS (fixed argv, never a shell):\n"
        f"{json.dumps(case['verificationArgv'], sort_keys=True)}\n"
        "INITIAL FAILURE EVIDENCE:\n"
        f"{runtime['failureEvidence'][:40_000]}"
    )


def frontier_alone_prompt(task_packet: str) -> str:
    return (
        "You are the frontier-alone baseline in a paired code-repair study.\n"
        "Solve the task directly in the current disposable Git repository. "
        "Inspect files and run tests as needed. Modify production code only. "
        "Do not commit. Do not access the network or any path outside this "
        "repository. Finish only when the working tree contains your final "
        "candidate patch.\n\n"
        f"{task_packet}"
    )


def codex_command(
    profile: EvaluationProfile,
    *,
    cwd: Path,
    sandbox: str,
    output_last_message: Path,
) -> list[str]:
    return [
        profile.frontier.command,
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--model",
        profile.frontier.model,
        "--config",
        f'model_reasoning_effort="{profile.frontier.reasoning_effort}"',
        "--sandbox",
        sandbox,
        "--cd",
        str(cwd),
        "--output-last-message",
        str(output_last_message),
        "-",
    ]


def frontier_environment(
    case_environment: Path | None = None,
) -> dict[str, str]:
    allowed = (
        "CODEX_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_FILE",
        "TMPDIR",
    )
    values = {
        name: os.environ[name]
        for name in allowed
        if name in os.environ
    }
    if case_environment is not None:
        original_path = values.get("PATH", os.defpath)
        values["PATH"] = f"{case_environment / 'bin'}:{original_path}"
        values["VIRTUAL_ENV"] = str(case_environment)
    return values


def usage_with_phases(
    phases: Sequence[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    reported = all(
        usage.get("usageStatus") == "reported"
        for _, usage in phases
    )
    fields = (
        "promptTokens",
        "cachedInputTokens",
        "completionTokens",
        "reasoningTokens",
        "totalTokens",
    )
    return {
        "usageStatus": "reported" if reported else "unavailable",
        "turns": sum(int(usage.get("turns", 0)) for _, usage in phases),
        **{
            field: (
                sum(int(usage[field]) for _, usage in phases)
                if reported
                else None
            )
            for field in fields
        },
        "phases": [
            {"phase": name, **usage}
            for name, usage in phases
        ],
    }


def empty_local_usage() -> dict[str, int]:
    return {
        "promptTokens": 0,
        "generationTokens": 0,
        "generationCalls": 0,
        "modelLoads": 0,
    }


def make_arm_result(
    *,
    case: dict[str, Any],
    arm: str,
    status: str,
    completed: bool,
    score: int,
    elapsed_seconds: float,
    phase_seconds: dict[str, float],
    frontier_usage: dict[str, Any],
    local_usage: dict[str, int],
    repairs: int,
    model_loads: int,
    review_verdict: str | None,
    patch: dict[str, Any],
    oracle: dict[str, Any],
) -> dict[str, Any]:
    result = {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "caseId": case["caseId"],
        "phase": case["phase"],
        "arm": arm,
        "status": status,
        "completed": completed,
        "score": score,
        "elapsedSeconds": elapsed_seconds,
        "phaseSeconds": phase_seconds,
        "frontierUsage": frontier_usage,
        "localUsage": local_usage,
        "repairs": repairs,
        "modelLoads": model_loads,
        "reviewVerdict": review_verdict,
        "patch": patch,
        "oracle": {
            "passed": bool(oracle["passed"]),
            "exitCode": oracle.get("exitCode"),
            "evidence": str(oracle.get("evidence", ""))[:MAX_LOG_BYTES],
        },
        "recordedAt": utc_now(),
    }
    return validate_arm_result(result)


def invalid_arm_result(
    case: dict[str, Any],
    arm: str,
    evidence: str,
) -> dict[str, Any]:
    return make_arm_result(
        case=case,
        arm=arm,
        status="invalid",
        completed=False,
        score=0,
        elapsed_seconds=0.0,
        phase_seconds={},
        frontier_usage=usage_with_phases([
            (
                "unavailable",
                {
                    "usageStatus": "unavailable",
                    "turns": 0,
                    "promptTokens": None,
                    "cachedInputTokens": None,
                    "completionTokens": None,
                    "reasoningTokens": None,
                    "totalTokens": None,
                    "malformedLines": 0,
                },
            )
        ]),
        local_usage=empty_local_usage(),
        repairs=0,
        model_loads=0,
        review_verdict=None,
        patch={"sha256": None, "changedFiles": 0},
        oracle={"passed": False, "exitCode": None, "evidence": evidence},
    )


def persist_candidate_patch(
    evidence_root: Path,
    diff: str,
) -> dict[str, Any]:
    if not diff:
        return {"sha256": None, "changedFiles": 0}
    evidence_root.mkdir(parents=True, exist_ok=True)
    path = evidence_root / "candidate.diff"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != diff:
            raise EvaluationError("Candidate patch evidence is immutable.")
    else:
        path.write_text(diff, encoding="utf-8")
    metadata = patch_metadata(diff)
    return {
        "sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "changedFiles": metadata["changedFiles"],
    }


def validate_candidate_diff(
    diff: str,
    case: dict[str, Any],
    repository: Path,
) -> str | None:
    if not diff:
        return "Candidate patch is empty."
    try:
        metadata = patch_metadata(diff)
    except EvaluationError as exc:
        return str(exc)
    if any(_is_non_production_path(path) for path in metadata["paths"]):
        return "Candidate patch modifies tests or non-production evidence."
    if metadata["changedFiles"] > 20:
        return "Candidate patch changes more than 20 files."
    result = run_command(
        ["git", "apply", "--check", "-"],
        cwd=repository,
        timeout=60,
        input_text=diff,
    )
    if result.returncode != 0:
        # The diff was produced against this already-modified repository. Test
        # it against HEAD by reversing first; independent scoring performs the
        # authoritative clean-apply check.
        reverse = run_command(
            ["git", "apply", "--reverse", "--check", "-"],
            cwd=repository,
            timeout=60,
            input_text=diff,
        )
        if reverse.returncode != 0:
            return "Candidate patch is not a valid unified diff."
    del case
    return None


def write_evaluation_config(
    source: SwarmConfig,
    path: Path,
    artifacts_root: Path,
    verifier_manifest: Path,
    repository: Path,
) -> None:
    model: dict[str, Any] = {"repository": source.model.repository}
    if source.model.revision:
        model["revision"] = source.model.revision
    if source.model.local_path:
        model["localPath"] = source.model.local_path
    payload = {
        "schemaVersion": 2,
        "model": model,
        "batch": {
            "maxWorkers": source.batch.max_workers,
            "prefillStepSize": source.batch.prefill_step_size,
            "maxPromptCharacters": source.batch.max_prompt_characters,
        },
        "artifacts": str(artifacts_root.resolve()),
        "enableThinking": source.enable_thinking,
        "seed": source.seed,
        "workspace": {
            "writeRoots": evaluation_write_roots(repository),
            "verificationProfiles": {
                "bugsinpy-acceptance": {
                    "argv": [
                        sys.executable,
                        "-m",
                        "mlx_swarm.evaluation",
                        "verify-case",
                        str(verifier_manifest.resolve()),
                    ],
                    "cwd": ".",
                    "timeoutSeconds": 1_800,
                    "inheritEnv": [
                        "PATH",
                        "TMPDIR",
                        "LANG",
                        "LC_ALL",
                    ],
                    "environment": {},
                }
            },
        },
    }
    _atomic_json(path, payload)


def evaluation_write_roots(repository: Path) -> list[str]:
    roots: list[str] = []
    for child in sorted(repository.iterdir(), key=lambda value: value.name):
        name = child.name
        if (
            name == ".git"
            or name.startswith(".")
            or _is_non_production_path(name)
            or child.is_symlink()
        ):
            continue
        if child.is_dir() and name.lower() in {
            "benchmark",
            "benchmarks",
            "doc",
            "docs",
            "example",
            "examples",
            "test",
            "tests",
            "testing",
        }:
            continue
        roots.append(name)
    if not roots:
        raise EvaluationError(
            "Benchmark case exposes no production write roots."
        )
    return roots


def fresh_arm_repository(base: Path, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    validate_repository_symlinks(base)
    shutil.copytree(base, destination, symlinks=True)
    return destination


def copy_fixed_test_support(
    fixed_checkout: Path,
    buggy_checkout: Path,
    test_files: Sequence[str],
) -> None:
    """Copy designated fixed tests and newly introduced Python support files."""
    copied: set[Path] = set()
    copied_bytes = 0
    for relative_text in test_files:
        relative = Path(relative_text)
        source = fixed_checkout / relative
        if not source.is_file():
            raise EvaluationError(
                f"Fixed test file is missing: {relative_text}"
            )
        target = buggy_checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.add(relative)
        copied_bytes += source.stat().st_size
        if not any(
            part.lower() in {"test", "tests", "testing"}
            for part in relative.parent.parts
        ):
            continue
        support_root = source.parent
        for support in support_root.rglob("*.py"):
            support_relative = support.relative_to(fixed_checkout)
            support_target = buggy_checkout / support_relative
            if (
                support_relative in copied
                or (
                    support_target.exists()
                    and support.name not in {"__init__.py", "conftest.py"}
                )
            ):
                continue
            copied_bytes += support.stat().st_size
            if len(copied) >= 512 or copied_bytes > 10_000_000:
                raise EvaluationError(
                    "Fixed test support exceeds the preparation limit."
                )
            support_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(support, support_target)
            copied.add(support_relative)


def validate_repository_symlinks(repository: Path) -> None:
    root = repository.resolve()
    for path in repository.rglob("*"):
        if not path.is_symlink():
            continue
        target_text = os.readlink(path)
        target = Path(target_text)
        if target.is_absolute():
            raise EvaluationError(
                f"Benchmark repository contains an absolute symlink: {path}"
            )
        resolved = (path.parent / target).resolve(strict=False)
        if not _is_within(resolved, root):
            raise EvaluationError(
                f"Benchmark repository symlink escapes its root: {path}"
            )


def _init_snapshot_repository(path: Path) -> None:
    git_path = path / ".git"
    if git_path.exists():
        if git_path.is_dir():
            shutil.rmtree(git_path)
        else:
            git_path.unlink()
    _run_checked(["git", "init", "-q"], cwd=path, timeout=30)
    _run_checked(
        ["git", "config", "user.name", "MLX Swarm Evaluation"],
        cwd=path,
        timeout=30,
    )
    _run_checked(
        ["git", "config", "user.email", "evaluation@localhost"],
        cwd=path,
        timeout=30,
    )
    _run_checked(["git", "add", "-A"], cwd=path, timeout=60)
    _run_checked(
        ["git", "commit", "-q", "--no-verify", "-m", "frozen buggy snapshot"],
        cwd=path,
        timeout=120,
    )


def _remove_generated_state(path: Path) -> None:
    for name in (
        ".coverage",
        ".eggs",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
    ):
        for candidate in path.rglob(name):
            if candidate.is_dir() and not candidate.is_symlink():
                shutil.rmtree(candidate, ignore_errors=True)
            elif candidate.exists():
                candidate.unlink(missing_ok=True)
    for candidate in path.rglob("*.pyc"):
        candidate.unlink(missing_ok=True)
    for candidate in path.rglob("*.egg-info"):
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate, ignore_errors=True)
        elif candidate.exists():
            candidate.unlink(missing_ok=True)


def _git_worktree_add(mirror: Path, destination: Path, commit: str) -> None:
    _run_checked(
        [
            "git",
            "--git-dir",
            str(mirror),
            "worktree",
            "add",
            "--detach",
            str(destination),
            commit,
        ],
        cwd=mirror.parent,
        timeout=300,
    )


def _git_worktree_remove(mirror: Path, destination: Path) -> None:
    if not destination.exists():
        return
    run_command(
        [
            "git",
            "--git-dir",
            str(mirror),
            "worktree",
            "remove",
            "--force",
            str(destination),
        ],
        cwd=mirror.parent,
        timeout=120,
    )


def _git_text(cwd: Path, argv: Sequence[str]) -> str:
    return _run_checked(["git", *argv], cwd=cwd, timeout=60).stdout.strip()


def _git_diff(repository: Path, base_sha: str) -> str:
    return _run_checked(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            base_sha,
            "--",
        ],
        cwd=repository,
        timeout=60,
    ).stdout


def _venv_python(environment: Path) -> Path:
    candidate = environment / "bin" / "python"
    if not candidate.is_file():
        raise EvaluationError("Benchmark virtual environment has no Python.")
    return candidate


def validate_suite(
    value: Any,
    profile: EvaluationProfile | None = None,
) -> dict[str, Any]:
    suite = _object(value, "suite")
    _exact_keys(
        suite,
        "suite",
        {
            "schemaVersion",
            "suiteId",
            "profileId",
            "benchmark",
            "seed",
            "createdAt",
            "cases",
        },
    )
    if _integer(suite["schemaVersion"], "suite.schemaVersion", 1, 100) != 1:
        raise EvaluationError("Unsupported suite schema version.")
    _identifier(suite["suiteId"], "suite.suiteId")
    _identifier(suite["profileId"], "suite.profileId")
    _integer(suite["seed"], "suite.seed", 0, 2**31 - 1)
    _text(suite["createdAt"], "suite.createdAt")
    benchmark = _object(suite["benchmark"], "suite.benchmark")
    _exact_keys(
        benchmark,
        "suite.benchmark",
        {"name", "repository", "revision"},
    )
    _text(benchmark["name"], "suite.benchmark.name")
    _text(benchmark["repository"], "suite.benchmark.repository")
    if _GIT_SHA.fullmatch(
        _text(benchmark["revision"], "suite.benchmark.revision")
    ) is None:
        raise EvaluationError("suite benchmark revision is not pinned.")
    cases = _list(suite["cases"], "suite.cases", minimum=1, maximum=1_000)
    ids: set[str] = set()
    phase_counts = {"pilot": 0, "measured": 0}
    project_counts: dict[str, int] = {}
    for index, raw_case in enumerate(cases):
        case = _object(raw_case, f"suite.cases[{index}]")
        _exact_keys(
            case,
            f"suite.cases[{index}]",
            {
                "caseId",
                "project",
                "bugId",
                "repository",
                "buggyCommit",
                "fixedCommit",
                "pythonVersion",
                "testFiles",
                "setupArgv",
                "verificationArgv",
                "requirements",
                "reference",
                "phase",
                "objective",
            },
        )
        case_id = _identifier(case["caseId"], f"suite.cases[{index}].caseId")
        if case_id in ids:
            raise EvaluationError(f"Duplicate suite case: {case_id}")
        ids.add(case_id)
        _text(case["project"], f"suite.cases[{index}].project")
        _integer(case["bugId"], f"suite.cases[{index}].bugId", 1, 10**9)
        _text(case["repository"], f"suite.cases[{index}].repository")
        _commit(case["buggyCommit"], "suite case buggyCommit")
        _commit(case["fixedCommit"], "suite case fixedCommit")
        _python_version(case["pythonVersion"])
        _safe_relative_paths(
            _unique_text_array(
                case["testFiles"],
                f"suite.cases[{index}].testFiles",
                minimum=1,
                maximum=100,
            ),
            f"suite.cases[{index}].testFiles",
        )
        for command_field in ("setupArgv", "verificationArgv"):
            commands = _list(
                case[command_field],
                f"suite.cases[{index}].{command_field}",
                minimum=0 if command_field == "setupArgv" else 1,
                maximum=100,
            )
            for command_index, raw_command in enumerate(commands):
                command = _unique_or_repeated_text_array(
                    raw_command,
                    (
                        f"suite.cases[{index}].{command_field}"
                        f"[{command_index}]"
                    ),
                    minimum=1,
                    maximum=128,
                )
                if command[0] not in _SAFE_BENCHMARK_COMMANDS:
                    raise EvaluationError("Suite contains unsafe command.")
        _unique_or_repeated_text_array(
            case["requirements"],
            f"suite.cases[{index}].requirements",
            maximum=2_000,
        )
        reference = _object(
            case["reference"],
            f"suite.cases[{index}].reference",
        )
        _exact_keys(
            reference,
            f"suite.cases[{index}].reference",
            {"paths", "changedFiles", "changedLines", "sha256", "stratum"},
        )
        paths = _unique_text_array(
            reference["paths"],
            f"suite.cases[{index}].reference.paths",
            minimum=1,
            maximum=100,
        )
        _safe_relative_paths(paths, "reference paths")
        _integer(
            reference["changedFiles"],
            "reference.changedFiles",
            1,
            100,
        )
        _integer(
            reference["changedLines"],
            "reference.changedLines",
            1,
            100_000,
        )
        if _SHA256.fullmatch(
            _text(reference["sha256"], "reference.sha256")
        ) is None:
            raise EvaluationError("Reference digest is not SHA-256.")
        _enum(
            reference["stratum"],
            "reference.stratum",
            {"small", "medium", "large"},
        )
        phase = _enum(
            case["phase"],
            f"suite.cases[{index}].phase",
            {"pilot", "measured"},
        )
        phase_counts[phase] += 1
        if phase == "measured":
            project = case["project"]
            project_counts[project] = project_counts.get(project, 0) + 1
        _text(case["objective"], f"suite.cases[{index}].objective")
    if profile is not None:
        if phase_counts["pilot"] != profile.selection.pilot_size:
            raise EvaluationError("Suite pilot size differs from profile.")
        if phase_counts["measured"] != profile.selection.measured_size:
            raise EvaluationError("Suite measured size differs from profile.")
        if len(project_counts) < profile.selection.min_projects:
            raise EvaluationError("Suite covers too few measured projects.")
        if any(
            count > profile.selection.max_per_project
            for count in project_counts.values()
        ):
            raise EvaluationError("Suite exceeds measured project ceiling.")
    return suite


def clone_benchmark_metadata(
    profile: EvaluationProfile,
    destination: Path,
) -> Path:
    if destination.exists():
        raise EvaluationError("Benchmark destination already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run_checked([
        "git",
        "clone",
        "--filter=blob:none",
        "--no-checkout",
        profile.benchmark_repository,
        str(destination),
    ], cwd=destination.parent, timeout=600)
    _run_checked([
        "git",
        "checkout",
        "--detach",
        profile.benchmark_revision,
    ], cwd=destination, timeout=300)
    actual = _run_checked(
        ["git", "rev-parse", "HEAD"],
        cwd=destination,
        timeout=30,
    ).stdout.strip()
    if actual != profile.benchmark_revision:
        raise EvaluationError("Benchmark checkout does not match pinned revision.")
    return destination


def resolve_case_commits(
    cases: Sequence[dict[str, Any]],
    repositories_root: Path,
) -> None:
    """Resolve BugsInPy's abbreviated commits before freezing the suite."""
    repositories_root.mkdir(parents=True, exist_ok=True)
    by_project: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        by_project.setdefault(case["project"], []).append(case)
    for project, project_cases in sorted(by_project.items()):
        if all(
            len(case[field]) == 40
            for case in project_cases
            for field in ("buggyCommit", "fixedCommit")
        ):
            continue
        mirror = repositories_root / f"{project}.git"
        if not mirror.is_dir():
            _run_checked(
                [
                    "git",
                    "clone",
                    "--mirror",
                    project_cases[0]["repository"],
                    str(mirror),
                ],
                cwd=repositories_root,
                timeout=900,
            )
        for case in project_cases:
            for field in ("buggyCommit", "fixedCommit"):
                commitish = _commitish(case[field], field)
                result = _run_checked(
                    [
                        "git",
                        "--git-dir",
                        str(mirror),
                        "rev-parse",
                        f"{commitish}^{{commit}}",
                    ],
                    cwd=repositories_root,
                    timeout=30,
                )
                resolved = result.stdout.strip()
                case[field] = _commit(resolved, field)


def preparation_summary(
    evaluation_dir: Path,
    suite: dict[str, Any],
) -> dict[str, Any]:
    phases: dict[str, list[float]] = {"pilot": [], "measured": []}
    for case in suite["cases"]:
        runtime_path = (
            evaluation_dir / "cases" / case["caseId"] / "runtime.json"
        )
        if not runtime_path.is_file():
            continue
        runtime = _read_json(runtime_path)
        seconds = _number(
            runtime.get("preparationSeconds"),
            "runtime.preparationSeconds",
            0,
            10**9,
        )
        phases[case["phase"]].append(seconds)
    values = phases["pilot"] + phases["measured"]
    return {
        "caseCount": len(values),
        "totalSeconds": sum(values),
        "medianSeconds": statistics.median(values) if values else None,
        "pilotSeconds": sum(phases["pilot"]),
        "measuredSeconds": sum(phases["measured"]),
    }


def study_context(
    suite: dict[str, Any],
    environment: dict[str, Any],
) -> dict[str, Any]:
    return {
        "benchmark": suite["benchmark"]["name"],
        "benchmarkRevision": suite["benchmark"]["revision"],
        "seed": suite["seed"],
        "recordedAt": environment.get("recordedAt"),
        "frontierModel": environment.get("frontierModel"),
        "reasoningEffort": environment.get("reasoningEffort"),
        "codexVersion": environment.get("codexVersion"),
        "mlxSwarmCommit": environment.get("mlxSwarmCommit"),
        "localModel": environment.get("localModel"),
        "hardware": environment.get("hardware"),
        "runtime": environment.get("runtime"),
    }


def inspect_container(profile: EvaluationProfile) -> dict[str, Any]:
    return inspect_container_contract(
        profile.container.image,
        profile.container.digest,
        profile.container.platform,
        cwd=profile.source.parent,
    )


def inspect_container_contract(
    image: str,
    digest: str,
    platform_name: str,
    *,
    cwd: Path,
) -> dict[str, Any]:
    result = run_command(
        ["docker", "image", "inspect", image],
        cwd=cwd,
        timeout=30,
    )
    if result.timed_out or result.returncode != 0:
        raise EvaluationError(
            "Pinned benchmark container is unavailable: "
            + (result.stderr or result.stdout).strip()
        )
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise EvaluationError(
            "Docker returned malformed image metadata."
        ) from exc
    if not isinstance(values, list) or len(values) != 1:
        raise EvaluationError("Docker returned ambiguous image metadata.")
    value = _object(values[0], "container image metadata")
    actual = value.get("Id")
    if actual != digest:
        raise EvaluationError(
            "Benchmark container digest mismatch: "
            f"expected {digest}, got {actual}."
        )
    size = value.get("Size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise EvaluationError("Docker image size is unavailable.")
    inspected = {
        "image": image,
        "digest": actual,
        "architecture": _text(
            value.get("Architecture"),
            "container architecture",
        ),
        "os": _text(value.get("Os"), "container os"),
        "sizeBytes": size,
    }
    actual_platform = f"{inspected['os']}/{inspected['architecture']}"
    if actual_platform != platform_name:
        raise EvaluationError(
            "Benchmark container platform mismatch: "
            f"expected {platform_name}, got {actual_platform}."
        )
    inspected["platform"] = actual_platform
    return inspected


def environment_fingerprint(
    config: SwarmConfig,
    profile: EvaluationProfile,
    *,
    container: dict[str, Any],
) -> dict[str, Any]:
    codex_version = _best_effort_version([profile.frontier.command, "--version"])
    source = mlx_swarm_source_revision()
    model_path = None
    model_fingerprint = None
    try:
        from .backend import _resolve_model_path

        resolved = _resolve_model_path(config)
        model_path = str(resolved)
        model_fingerprint = fingerprint_model_directory(resolved)
    except Exception:
        pass
    memory_bytes = None
    try:
        memory_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        pass
    packages: dict[str, str | None] = {}
    for name in ("mlx", "mlx-lm", "huggingface-hub"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "schemaVersion": EVALUATION_SCHEMA_VERSION,
        "recordedAt": utc_now(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "codexVersion": codex_version,
        "frontierModel": profile.frontier.model,
        "reasoningEffort": profile.frontier.reasoning_effort,
        "mlxSwarmCommit": source["commit"],
        "mlxSwarmSourceDirty": source["dirty"],
        "hardware": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor() or "unknown",
            "memoryBytes": memory_bytes,
        },
        "runtime": {
            "python": sys.version.split()[0],
            "git": _best_effort_version(["git", "--version"]),
            "uv": _best_effort_version(["uv", "--version"]),
            "benchmarkBuildJobs": BENCHMARK_BUILD_JOBS,
            "packages": packages,
            "container": container,
        },
        "localModel": {
            "repository": config.model.repository,
            "revision": config.model.revision,
            "path": model_path,
            "fingerprint": model_fingerprint,
        },
        "profileSha256": canonical_json_sha256(profile_payload(profile)),
    }


def mlx_swarm_source_revision() -> dict[str, Any]:
    """Return the package source commit, never the evaluated target commit."""
    source_root = Path(__file__).resolve().parents[2]
    commit = _best_effort_output(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=source_root,
    )
    if not _GIT_SHA.fullmatch(commit):
        raise EvaluationError(
            "MLX Swarm source is not an identifiable Git revision."
        )
    status = run_command(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=source_root,
        timeout=30,
    )
    if status.returncode != 0 or status.timed_out:
        raise EvaluationError("Could not inspect MLX Swarm source state.")
    return {
        "root": str(source_root),
        "commit": commit,
        "dirty": bool(status.stdout.strip()),
    }


def fingerprint_model_directory(path: Path) -> str:
    records: list[dict[str, Any]] = []
    if path.is_dir():
        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            stat = child.stat()
            records.append({
                "path": str(child.relative_to(path)),
                "size": stat.st_size,
            })
    return canonical_json_sha256(records)


def sanitize_public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "caseId": row["caseId"],
        "project": row["project"],
        "frontier": _sanitize_arm(row["frontier"]),
        "swarm": _sanitize_arm(row["swarm"]),
        "frontierTokenDelta": row["frontierTokenDelta"],
        "timeDeltaSeconds": row["timeDeltaSeconds"],
    }


def remove_sensitive_preparation_sources(evaluation_dir: Path) -> None:
    """Remove upstream reference material before model execution."""
    root = evaluation_dir.resolve()
    for relative in (Path("benchmark"), Path("cache/repositories")):
        target = (root / relative).resolve()
        if not _is_within(target, root):
            raise EvaluationError("Preparation cleanup path escaped its root.")
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()


def sanitize_suite(suite: dict[str, Any]) -> dict[str, Any]:
    return {
        **suite,
        "cases": [
            {
                "caseId": case["caseId"],
                "project": case["project"],
                "bugId": case["bugId"],
                "repository": case["repository"],
                "buggyCommit": case["buggyCommit"],
                "pythonVersion": case["pythonVersion"],
                "testFiles": case["testFiles"],
                "reference": case["reference"],
                "phase": case["phase"],
                "objective": case["objective"],
            }
            for case in suite["cases"]
        ],
    }


def sanitize_environment(value: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(value)
    local_model = dict(sanitized.get("localModel", {}))
    local_model["path"] = None
    sanitized["localModel"] = local_model
    return sanitized


def directory_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    value = max(0, int(round(float(seconds))))
    minutes, remaining = divmod(value, 60)
    return f"{minutes:02d}:{remaining:02d}"


def format_integer(value: int | float | None) -> str:
    if value is None:
        return "—"
    return f"{int(round(value)):,}"


def format_percentage(value: int | float | None) -> str:
    if value is None:
        return "—"
    return f"{float(value):.1f}%"


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "unknown memory"
    gibibytes = float(value) / 1024**3
    return f"{gibibytes:.1f} GiB memory"


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    max_output_bytes: int = MAX_LOG_BYTES,
) -> CommandResult:
    """Run one bounded array command with no shell and group timeout."""
    if not argv or any(not isinstance(value, str) or not value for value in argv):
        raise EvaluationError("Subprocess argv must contain non-empty strings.")
    started = time.perf_counter()
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(
            input_text.encode("utf-8") if input_text is not None else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        _remove_timed_out_docker_container(argv)
        try:
            os.killpg(process.pid, 15)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
    return CommandResult(
        argv=tuple(argv),
        returncode=process.returncode,
        stdout=stdout[:max_output_bytes].decode("utf-8", "replace"),
        stderr=stderr[:max_output_bytes].decode("utf-8", "replace"),
        elapsed_seconds=time.perf_counter() - started,
        timed_out=timed_out,
    )


def _remove_timed_out_docker_container(argv: Sequence[str]) -> None:
    """Stop a named benchmark container when its attached CLI times out."""
    if list(argv[:2]) != ["docker", "run"] or "--name" not in argv:
        return
    name_index = list(argv).index("--name") + 1
    if name_index >= len(argv):
        return
    name = argv[name_index]
    if not re.fullmatch(r"mlx-swarm-eval-[0-9a-f]{20}", name):
        return
    try:
        subprocess.run(
            ["docker", "rm", "--force", name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_checked(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> CommandResult:
    result = run_command(argv, cwd=cwd, timeout=timeout, env=env)
    if result.timed_out:
        raise EvaluationError(f"Command timed out: {argv[0]}")
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise EvaluationError(
            f"Command failed ({result.returncode}): {' '.join(argv)}: {message}"
        )
    return result


def _arm_aggregate(
    results: Sequence[dict[str, Any]],
    *,
    include_local: bool,
) -> dict[str, Any]:
    frontier_totals = [
        total
        for total in (
            _reported_total(result["frontierUsage"])
            for result in results
        )
        if total is not None
    ]
    elapsed = [float(result["elapsedSeconds"]) for result in results]
    local_totals = [
        _local_total(result["localUsage"])
        for result in results
    ]
    return {
        "cases": len(results),
        "completed": sum(bool(result["completed"]) for result in results),
        "score": sum(int(result["score"]) for result in results),
        "completionRate": (
            sum(bool(result["completed"]) for result in results)
            / len(results)
            * 100.0
            if results
            else 0.0
        ),
        "scoreRate": (
            sum(int(result["score"]) for result in results)
            / len(results)
            * 100.0
            if results
            else 0.0
        ),
        "medianElapsedSeconds": (
            statistics.median(elapsed) if elapsed else None
        ),
        "frontierTokens": (
            sum(frontier_totals)
            if len(frontier_totals) == len(results)
            else None
        ),
        "medianFrontierTokens": (
            statistics.median(frontier_totals)
            if len(frontier_totals) == len(results) and frontier_totals
            else None
        ),
        "localTokens": sum(local_totals) if include_local else 0,
        "medianLocalTokens": (
            statistics.median(local_totals)
            if include_local and local_totals
            else 0
        ),
        "repairs": sum(int(result["repairs"]) for result in results),
        "medianRepairs": (
            statistics.median(int(result["repairs"]) for result in results)
            if results
            else 0
        ),
        "modelLoads": sum(int(result["modelLoads"]) for result in results),
    }


def _validate_frontier_usage(value: Any, name: str) -> None:
    usage = _object(value, name)
    _exact_keys(
        usage,
        name,
        {
            "usageStatus",
            "turns",
            "promptTokens",
            "cachedInputTokens",
            "completionTokens",
            "reasoningTokens",
            "totalTokens",
            "phases",
        },
    )
    status = _enum(
        usage["usageStatus"],
        f"{name}.usageStatus",
        {"reported", "unavailable"},
    )
    _integer(usage["turns"], f"{name}.turns", 0, 10**9)
    for field in (
        "promptTokens",
        "cachedInputTokens",
        "completionTokens",
        "reasoningTokens",
        "totalTokens",
    ):
        raw = usage[field]
        if status == "reported":
            _integer(raw, f"{name}.{field}", 0, 10**15)
        elif raw is not None:
            raise EvaluationError(
                f"{name}.{field} must be null when usage is unavailable."
            )
    phases = _list(usage["phases"], f"{name}.phases", maximum=10)
    phase_totals = {
        "turns": 0,
        "promptTokens": 0,
        "cachedInputTokens": 0,
        "completionTokens": 0,
        "reasoningTokens": 0,
        "totalTokens": 0,
    }
    all_reported = True
    for index, raw_phase in enumerate(phases):
        phase = _object(raw_phase, f"{name}.phases[{index}]")
        _exact_keys(
            phase,
            f"{name}.phases[{index}]",
            {
                "phase",
                "usageStatus",
                "turns",
                "promptTokens",
                "cachedInputTokens",
                "completionTokens",
                "reasoningTokens",
                "totalTokens",
                "malformedLines",
            },
        )
        _text(phase["phase"], f"{name}.phases[{index}].phase")
        phase_status = _enum(
            phase["usageStatus"],
            f"{name}.phases[{index}].usageStatus",
            {"reported", "unavailable"},
        )
        turns = _integer(
            phase["turns"],
            f"{name}.phases[{index}].turns",
            0,
            10**9,
        )
        _integer(
            phase["malformedLines"],
            f"{name}.phases[{index}].malformedLines",
            0,
            10**9,
        )
        phase_totals["turns"] += turns
        all_reported = all_reported and phase_status == "reported"
        for field in (
            "promptTokens",
            "cachedInputTokens",
            "completionTokens",
            "reasoningTokens",
            "totalTokens",
        ):
            raw = phase[field]
            if phase_status == "reported":
                phase_totals[field] += _integer(
                    raw,
                    f"{name}.phases[{index}].{field}",
                    0,
                    10**15,
                )
            elif raw is not None:
                raise EvaluationError(
                    f"{name}.phases[{index}].{field} must be null "
                    "when usage is unavailable."
                )
    if phases:
        if (status == "reported") != all_reported:
            raise EvaluationError(
                f"{name}.usageStatus does not match its phase receipts."
            )
        if usage["turns"] != phase_totals["turns"]:
            raise EvaluationError(f"{name}.turns does not equal phase totals.")
        if status == "reported":
            for field in (
                "promptTokens",
                "cachedInputTokens",
                "completionTokens",
                "reasoningTokens",
                "totalTokens",
            ):
                if usage[field] != phase_totals[field]:
                    raise EvaluationError(
                        f"{name}.{field} does not equal phase totals."
                    )


def _validate_local_usage(value: Any, name: str) -> None:
    usage = _object(value, name)
    _exact_keys(
        usage,
        name,
        {
            "promptTokens",
            "generationTokens",
            "generationCalls",
            "modelLoads",
        },
    )
    for field in usage:
        _integer(usage[field], f"{name}.{field}", 0, 10**15)


def _reported_total(usage: dict[str, Any]) -> int | None:
    if usage.get("usageStatus") != "reported":
        return None
    total = usage.get("totalTokens")
    return int(total) if isinstance(total, int) else None


def _local_total(usage: dict[str, Any]) -> int:
    return int(usage.get("promptTokens", 0)) + int(
        usage.get("generationTokens", 0)
    )


def _usage_integer(
    usage: dict[str, Any],
    keys: Sequence[str],
    *,
    default: int | None = None,
) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    if default is not None:
        return default
    raise EvaluationError(
        f"Codex usage is missing required field: {'/'.join(keys)}"
    )


def _assign_patch_strata(candidates: list[dict[str, Any]]) -> None:
    values = sorted(
        candidate["reference"]["changedLines"]
        for candidate in candidates
    )
    one_third = values[max(0, math.ceil(len(values) / 3) - 1)]
    two_thirds = values[max(0, math.ceil(2 * len(values) / 3) - 1)]
    for candidate in candidates:
        changed = candidate["reference"]["changedLines"]
        candidate["reference"]["stratum"] = (
            "small"
            if changed <= one_third
            else "medium"
            if changed <= two_thirds
            else "large"
        )


def _rebalance_strata(
    measured: list[dict[str, Any]],
    candidates: Sequence[dict[str, Any]],
    pilot: Sequence[dict[str, Any]],
    projects: Sequence[str],
    profile: EvaluationProfile,
) -> None:
    target_base, remainder = divmod(len(measured), 3)
    targets = {
        "small": target_base + (1 if remainder > 0 else 0),
        "medium": target_base + (1 if remainder > 1 else 0),
        "large": target_base,
    }
    selected_ids = {value["caseId"] for value in measured}
    excluded_ids = selected_ids | {value["caseId"] for value in pilot}
    project_counts = {
        project: sum(value["project"] == project for value in measured)
        for project in projects
    }
    for desired in ("small", "medium", "large"):
        while (
            sum(
                value["reference"]["stratum"] == desired
                for value in measured
            )
            < targets[desired]
        ):
            surplus = next(
                (
                    stratum
                    for stratum in ("large", "medium", "small")
                    if sum(
                        value["reference"]["stratum"] == stratum
                        for value in measured
                    )
                    > targets[stratum]
                ),
                None,
            )
            if surplus is None:
                break
            replacement_pair = None
            for current_index, current in enumerate(measured):
                if current["reference"]["stratum"] != surplus:
                    continue
                for candidate in candidates:
                    if (
                        candidate["caseId"] in excluded_ids
                        or candidate["project"] not in projects
                        or candidate["reference"]["stratum"] != desired
                    ):
                        continue
                    if candidate["project"] == current["project"]:
                        replacement_pair = (current_index, candidate)
                        break
                    if (
                        project_counts[candidate["project"]]
                        < profile.selection.max_per_project
                    ):
                        replacement_pair = (current_index, candidate)
                        break
                if replacement_pair is not None:
                    break
            if replacement_pair is None:
                raise EvaluationError(
                    "Unable to balance measured patch-size strata."
                )
            index, replacement = replacement_pair
            removed = measured[index]
            measured[index] = replacement
            excluded_ids.remove(removed["caseId"])
            excluded_ids.add(replacement["caseId"])
            project_counts[removed["project"]] -= 1
            project_counts[replacement["project"]] += 1


def _requirement_lines(
    text: str,
    *,
    project: str | None = None,
) -> list[str]:
    values: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if any(marker in line for marker in ("\x00", "\n", "\r")):
            raise EvaluationError("Requirement contains invalid characters.")
        if line.startswith("-e"):
            egg = re.search(r"#egg=([A-Za-z0-9_.-]+)", line)
            if (
                project is not None
                and egg is not None
                and egg.group(1).lower().replace("_", "-")
                == project.lower().replace("_", "-")
            ):
                # BugsInPy freezes the project itself in requirements. The
                # harness installs each isolated buggy/fixed workspace
                # explicitly instead of fetching that future-facing VCS URL.
                continue
            raise EvaluationError(
                "Editable dependency requirements are excluded."
            )
        if line.startswith(("--", "git+", "http:", "https:")):
            raise EvaluationError(
                "URL and option requirements are excluded."
            )
        match = re.match(r"^([A-Za-z0-9_.-]+)", line)
        if match is None or not is_exact_requirement(line):
            raise EvaluationError(
                "Benchmark dependencies must use exact == pins."
            )
        package = match.group(1).lower().replace("_", "-")
        canonical_project = (
            project.lower().replace("_", "-")
            if project is not None
            else None
        )
        if package == canonical_project:
            continue
        if package in {"pydivert", "pywin32"}:
            line = f"{line}; sys_platform == 'win32'"
        values.append(line)
    return values


def is_exact_requirement(value: str) -> bool:
    requirement = value.partition(";")[0].strip()
    return re.fullmatch(
        r"[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?==[^=\s]+",
        requirement,
    ) is not None


def _is_non_production_path(path: str) -> bool:
    lowered = path.lower()
    return (
        lowered.startswith(_NON_PRODUCTION_PREFIXES)
        or "/test" in lowered
        or Path(lowered).name in _NON_PRODUCTION_NAMES
        or Path(lowered).suffix in {".lock", ".toml", ".yaml", ".yml"}
    )


def _python_version(value: Any) -> tuple[int, int]:
    if not isinstance(value, str):
        raise EvaluationError("Benchmark python_version is missing.")
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)(?:\.[0-9]+)?", value)
    if match is None:
        raise EvaluationError(f"Unsupported Python version: {value}")
    return int(match.group(1)), int(match.group(2))


def _commit(value: Any, name: str) -> str:
    if not isinstance(value, str) or _GIT_SHA.fullmatch(value) is None:
        raise EvaluationError(f"{name} must be a 40-character Git SHA.")
    return value


def _commitish(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{7,40}", value) is None
    ):
        raise EvaluationError(
            f"{name} must be a 7- to 40-character hexadecimal Git commit."
        )
    return value


def _safe_relative_paths(values: Iterable[str], name: str) -> None:
    for value in values:
        path = Path(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or ".git" in path.parts
            or not value
        ):
            raise EvaluationError(f"{name} contains an unsafe path: {value}")


def _sanitize_arm(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": value["schemaVersion"],
        "caseId": value["caseId"],
        "phase": value["phase"],
        "arm": value["arm"],
        "status": value["status"],
        "completed": value["completed"],
        "score": value["score"],
        "elapsedSeconds": value["elapsedSeconds"],
        "phaseSeconds": value["phaseSeconds"],
        "frontierUsage": value["frontierUsage"],
        "localUsage": value["localUsage"],
        "repairs": value["repairs"],
        "modelLoads": value["modelLoads"],
        "reviewVerdict": value["reviewVerdict"],
        "patch": value["patch"],
        "oracle": {
            "passed": value["oracle"]["passed"],
            "exitCode": value["oracle"]["exitCode"],
            "evidence": _sanitize_evidence(
                value["oracle"]["evidence"][:4_000]
            ),
        },
        "recordedAt": value["recordedAt"],
    }


def _sanitize_evidence(value: str) -> str:
    sanitized = re.sub(
        r"(?<![A-Za-z0-9_.-])/(?:Users|private|home|tmp|var)/[^\s:'\"]+",
        "<local-path>",
        value,
    )
    return re.sub(
        r"(?i)\b[A-Z]:\\(?:Users|Temp)\\[^\s:'\"]+",
        "<local-path>",
        sanitized,
    )


def _percentage(numerator: int | float, denominator: int | float | None) -> float | None:
    if denominator in {None, 0}:
        return None
    return float(numerator) / float(denominator) * 100.0


def _signed(value: int | float) -> str:
    return f"{value:+g}"


def _signed_duration(seconds: int | float) -> str:
    prefix = "+" if seconds >= 0 else "-"
    return prefix + format_duration(abs(seconds))


def _format_time_delta(
    frontier_seconds: int | float | None,
    swarm_seconds: int | float | None,
) -> str:
    if frontier_seconds is None or swarm_seconds is None:
        return "—"
    return _signed_duration(float(frontier_seconds) - float(swarm_seconds))


def _format_time_percentage(
    frontier_seconds: int | float | None,
    swarm_seconds: int | float | None,
) -> str:
    if frontier_seconds in {None, 0} or swarm_seconds is None:
        return "—"
    delta = (
        float(swarm_seconds) - float(frontier_seconds)
    ) / float(frontier_seconds) * 100.0
    return f"{delta:+.1f}%"


def _format_interval(lower: float | None, upper: float | None) -> str:
    if lower is None or upper is None:
        return "unavailable"
    return f"{lower:,.1f} to {upper:,.1f} tokens"


def _best_effort_version(argv: Sequence[str]) -> str | None:
    return _best_effort_output(argv)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _best_effort_output(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
) -> str | None:
    try:
        result = run_command(
            argv,
            cwd=cwd or Path.cwd(),
            timeout=10,
        )
    except Exception:
        return None
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[-1] if output else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvaluationError(f"JSON file not found: {path}") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise EvaluationError(f"Invalid JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"JSON file must contain an object: {path}")
    return value


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _exclusive_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise EvaluationError(f"Evidence already exists: {path}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvaluationError(f"{name} must be an object.")
    return value


def _list(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 100_000,
) -> list[Any]:
    if not isinstance(value, list):
        raise EvaluationError(f"{name} must be an array.")
    if not minimum <= len(value) <= maximum:
        raise EvaluationError(
            f"{name} must contain {minimum} to {maximum} entries."
        )
    return value


def _exact_keys(
    value: dict[str, Any],
    name: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise EvaluationError(
            f"{name} is missing fields: {', '.join(sorted(missing))}."
        )
    if unknown:
        raise EvaluationError(
            f"{name} has unknown fields: {', '.join(sorted(unknown))}."
        )


def _text(
    value: Any,
    name: str,
    *,
    allow_empty: bool = False,
    maximum: int = 500_000,
) -> str:
    if not isinstance(value, str):
        raise EvaluationError(f"{name} must be a string.")
    if not allow_empty and not value.strip():
        raise EvaluationError(f"{name} must not be empty.")
    if len(value) > maximum:
        raise EvaluationError(f"{name} exceeds {maximum} characters.")
    return value


def _identifier(value: Any, name: str) -> str:
    text = _text(value, name)
    if _IDENTIFIER.fullmatch(text) is None:
        raise EvaluationError(f"{name} is not a safe identifier.")
    return text


def _integer(
    value: Any,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise EvaluationError(
            f"{name} must be an integer from {minimum} to {maximum}."
        )
    return value


def _number(
    value: Any,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise EvaluationError(
            f"{name} must be a finite number from {minimum} to {maximum}."
        )
    return float(value)


def _enum(value: Any, name: str, allowed: set[str]) -> str:
    text = _text(value, name)
    if text not in allowed:
        raise EvaluationError(
            f"{name} must be one of: {', '.join(sorted(allowed))}."
        )
    return text


def _unique_text_array(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 100_000,
) -> tuple[str, ...]:
    values = _unique_or_repeated_text_array(
        value,
        name,
        minimum=minimum,
        maximum=maximum,
    )
    if len(values) != len(set(values)):
        raise EvaluationError(f"{name} must not contain duplicates.")
    return tuple(values)


def _unique_or_repeated_text_array(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 100_000,
) -> list[str]:
    values = _list(value, name, minimum=minimum, maximum=maximum)
    return [
        _text(item, f"{name}[{index}]", maximum=8_192)
        for index, item in enumerate(values)
    ]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _module_main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m mlx_swarm.evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify-case")
    verify.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    if args.command == "verify-case":
        try:
            result = run_case_verifier(args.manifest.resolve(), Path.cwd())
        except EvaluationError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["passed"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(_module_main())
