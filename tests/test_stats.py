"""Tests for aggregate session statistics."""
# @lat: [[Tests]]

from __future__ import annotations

import json
from pathlib import Path

from mlx_swarm.stats import collect_stats


def _write_session(
    root: Path,
    plan_id: str,
    session_id: str,
    tasks: dict,
    *,
    status: str = "completed",
    batches: list | None = None,
) -> None:
    session_dir = root / plan_id / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text(
        json.dumps({
            "sessionId": session_id,
            "planId": plan_id,
            "status": status,
            "tasks": tasks,
            "batches": batches or [],
        }),
        encoding="utf-8",
    )


def test_collect_stats_aggregates_sessions(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        "plan-a",
        "s1",
        {
            "one": {
                "status": "completed",
                "executionMode": "local-agent",
                "generationAttempts": [{"gatePassed": True}],
                "repairAttempts": 0,
            },
            "two": {
                "status": "completed",
                "executionMode": "deterministic-edit",
                "generationAttempts": [],
                "repairAttempts": 0,
            },
        },
        batches=[{"statistics": {"generationTokens": 90, "generationCalls": 1}}],
    )
    _write_session(
        tmp_path,
        "plan-b",
        "s2",
        {
            "three": {
                "status": "failed",
                "executionMode": "local-agent",
                "generationAttempts": [{"gatePassed": False}],
                "repairAttempts": 1,
                "escalatedMaxTokens": 2048,
            },
            "four": {
                "status": "blocked",
                "executionMode": "local-agent",
                "generationAttempts": [],
                "repairAttempts": 0,
            },
        },
        status="failed",
    )
    (tmp_path / "plan-c" / "s3").mkdir(parents=True)
    (tmp_path / "plan-c" / "s3" / "session.json").write_text("{bad")

    stats = collect_stats(tmp_path)

    assert stats["sessions"] == 2
    assert stats["unreadableSessions"] == 1
    assert stats["tasks"] == 4
    assert stats["taskStatus"]["blocked"] == 1
    assert stats["blockedShare"] == 0.25
    assert stats["executionModes"] == {
        "deterministic-edit": 1,
        "local-agent": 3,
    }
    assert stats["firstPassGate"] == {
        "passed": 1,
        "failed": 1,
        "rate": 0.5,
    }
    assert stats["repairAttemptsUsed"] == 1
    assert stats["escalatedTasks"] == 1
    assert stats["localGenerationTokens"] == 90
    assert stats["localGenerationCalls"] == 1


def test_collect_stats_empty_dir(tmp_path: Path) -> None:
    stats = collect_stats(tmp_path)
    assert stats["sessions"] == 0
    assert stats["blockedShare"] is None
    assert stats["firstPassGate"]["rate"] is None
