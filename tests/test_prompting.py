"""Tests for prompt composition and dependency injection."""
# @lat: [[Tests#Prompting]]

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from mlx_swarm.contracts import (
    ContextSource,
    GatePattern,
    OutputGate,
    Plan,
    TaskContext,
    TaskDef,
)
from mlx_swarm.prompting import compose_prompt, compose_repair_prompt


def _mock_session(outputs: dict[str, str]) -> MagicMock:
    session = MagicMock()
    session.get_task_output = lambda tid: outputs.get(tid)
    return session


def test_compose_prompt_no_context() -> None:
    task = TaskDef(id="t1", role="general", prompt="Do something")
    prompt = compose_prompt(None, task)
    assert "Do something" in prompt


def test_compose_prompt_with_context() -> None:
    context = TaskContext(
        objective="Build a module",
        authoritative_sources=(
            ContextSource(label="Spec", content="def foo(): pass", origin="inline", sha256="abc"),
        ),
        constraints=("Must be pure Python",),
        rejection_criteria=("No external deps",),
        output_protocol="Return code only.",
    )
    task = TaskDef(id="t1", role="implementation", prompt="Implement foo.")
    prompt = compose_prompt(context, task)
    assert "AUTHORITY" in prompt
    assert "Build a module" in prompt
    assert "Spec" in prompt
    assert "def foo(): pass" in prompt
    assert "Must be pure Python" in prompt
    assert "No external deps" in prompt
    assert "Implement foo." in prompt
    assert "Return code only." in prompt


def test_compose_prompt_with_dependency() -> None:
    task = TaskDef(id="t2", role="test", prompt="Test it", depends_on=("t1",))
    session = _mock_session({"t1": "def foo(): pass"})
    prompt = compose_prompt(None, task, session=session)
    assert "DEPENDENCY OUTPUT: t1" in prompt
    assert "def foo(): pass" in prompt


def test_compose_prompt_dependency_not_completed() -> None:
    task = TaskDef(id="t2", role="test", prompt="Test it", depends_on=("t1",))
    session = _mock_session({})
    prompt = compose_prompt(None, task, session=session)
    assert "DEPENDENCY: t1" in prompt
    assert "not been completed" in prompt


def test_compose_repair_prompt() -> None:
    original = "## ROLE-SPECIFIC TASK\nImplement foo."
    feedback = "Your previous output was REJECTED for the following reasons:\n- Missing def"
    previous = "x = 1"
    prompt = compose_repair_prompt(original, feedback, previous)
    assert "Implement foo." in prompt
    assert "REJECTED" in prompt
    assert "x = 1" in prompt
    assert "Return ONLY the corrected output" in prompt


def test_repair_prompt_encodes_fenced_output_without_nested_fences() -> None:
    previous = "```diff\ndiff --git a/a.py b/a.py\n```"
    prompt = compose_repair_prompt("ORIGINAL", "corrupt patch", previous)
    assert json.dumps(previous) in prompt
    assert "## YOUR PREVIOUS OUTPUT (REJECTED)\n```" not in prompt
    assert "untrusted data" in prompt


def test_compose_prompt_worker_identity() -> None:
    task = TaskDef(id="my-task", role="review", prompt="Review code")
    prompt = compose_prompt(None, task)
    assert "my-task" in prompt
    assert "review" in prompt


def test_task_output_protocol_overrides_shared_protocol() -> None:
    context = TaskContext(
        objective="Review code",
        output_protocol="Return Python only.",
    )
    task = TaskDef(
        id="review",
        role="review",
        prompt="Review it",
        output_protocol="Return JSON only.",
    )
    prompt = compose_prompt(context, task)
    assert "Return JSON only." in prompt
    assert "Return Python only." not in prompt


def test_edit_manifest_worker_prompt_preserves_diff_approval_boundary() -> None:
    task = TaskDef(
        id="edit",
        role="implementation",
        prompt="Change the value.",
        artifact_type="patch",
        allowed_paths=("src/value.py",),
        verification=("unit",),
        worker_output_protocol="edit-manifest-v1",
    )
    session = _mock_session({})
    session.plan.workspace_execution = True
    prompt = compose_prompt(None, task, session=session)
    assert "Worker output protocol: edit-manifest-v1" in prompt
    assert '"edits"' in prompt
    assert "runtime will materialize and validate the unified diff" in prompt
    assert "do not return a Git diff" in prompt
