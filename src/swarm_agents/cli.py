"""CLI entrypoint for the swarm agent framework."""
# @lat: [[Architecture]]

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .contracts import ContractError, load_config, load_plan
from .executor import execute_plan
from .session import Session


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarm",
        description=(
            "Run bounded DAGs of local MLX agents with deterministic gates "
            "and durable sessions."
        ),
    )
    parser.add_argument("--config", required=True, type=Path, help="Path to swarm config JSON.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Check config and model availability without loading Metal.")

    run_parser = sub.add_parser("run", help="Execute a plan.")
    run_parser.add_argument("plan", type=Path, help="Path to plan JSON.")
    run_parser.add_argument("--session-dir", type=Path, default=None, help="Override session directory.")
    run_parser.add_argument(
        "--max-repair",
        type=_non_negative_int,
        default=2,
        help="Global cap on repair attempts per task.",
    )
    run_parser.add_argument("--verbose", action="store_true", help="Print full statistics.")

    inspect_parser = sub.add_parser("inspect", help="Inspect a session.")
    inspect_parser.add_argument("session_dir", type=Path, help="Path to session directory.")
    inspect_parser.add_argument("--task", type=str, default=None, help="Inspect a specific task.")
    inspect_parser.add_argument("--output", action="store_true", help="Print task output.")

    resume_parser = sub.add_parser("resume", help="Resume a paused or partial session.")
    resume_parser.add_argument("session_dir", type=Path, help="Path to session directory.")
    resume_parser.add_argument(
        "--max-repair",
        type=_non_negative_int,
        default=2,
        help="Global cap on repair attempts per task.",
    )
    resume_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print final task results.",
    )

    sub.add_parser("list", help="List sessions in artifacts directory.")

    ui_parser = sub.add_parser(
        "ui",
        help="Launch the localhost swarm work cockpit.",
    )
    ui_parser.add_argument(
        "--plans-dir",
        type=Path,
        default=None,
        help="Approved plan directory (defaults to the config directory).",
    )
    ui_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Local bind host (127.0.0.1, localhost, or ::1).",
    )
    ui_parser.add_argument(
        "--port",
        type=_port,
        default=8765,
        help="Local port (default: 8765; use 0 to select a free port).",
    )
    ui_parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the cockpit in the default browser.",
    )

    return parser


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _port(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 65_535:
        raise argparse.ArgumentTypeError("must be between 0 and 65535")
    return parsed


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)

        if args.command == "doctor":
            from .backend import _resolve_model_path

            try:
                model_path = _resolve_model_path(config)
                result = {
                    "ready": True,
                    "model": {"path": str(model_path), "repository": config.model.repository},
                    "batch": {
                        "maxWorkers": config.batch.max_workers,
                        "prefillStepSize": config.batch.prefill_step_size,
                        "maxPromptCharacters": config.batch.max_prompt_characters,
                    },
                    "artifactsDir": str(config.artifacts_dir),
                }
            except Exception as exc:
                result = {"ready": False, "error": str(exc)}
            _print(result)
            return 0 if result.get("ready") else 1

        if args.command == "run":
            plan = load_plan(args.plan, config)
            session = execute_plan(
                config,
                plan,
                session_dir=args.session_dir,
                max_repair=args.max_repair,
            )
            summary = session.summary()
            if isinstance(session.state, dict):
                summary["frontierResult"] = session.state.get("frontierResult")
            if args.verbose:
                summary["tasks"] = session.export_results()["tasks"]
            _print(summary)
            return 0 if summary["status"] == "completed" else 1

        if args.command == "inspect":
            session = Session.load(args.session_dir, config)
            if args.task:
                task_state = session.state["tasks"].get(args.task)
                if not task_state:
                    print(f"Task not found: {args.task}", file=sys.stderr)
                    return 1
                if args.output:
                    print(task_state.get("normalizedOutput") or task_state.get("output") or "(no output)")
                else:
                    _print(task_state)
            else:
                _print(session.summary())
            return 0

        if args.command == "resume":
            session = Session.load(args.session_dir, config)
            plan = session.plan
            session = execute_plan(
                config,
                plan,
                session_dir=args.session_dir,
                max_repair=args.max_repair,
            )
            summary = session.summary()
            if isinstance(session.state, dict):
                summary["frontierResult"] = session.state.get("frontierResult")
            if args.verbose:
                summary["tasks"] = session.export_results()["tasks"]
            _print(summary)
            return 0 if summary["status"] == "completed" else 1

        if args.command == "list":
            sessions: list[dict[str, Any]] = []
            if config.artifacts_dir.is_dir():
                for plan_dir in sorted(config.artifacts_dir.iterdir()):
                    if not plan_dir.is_dir():
                        continue
                    for session_dir in sorted(plan_dir.iterdir()):
                        state_file = session_dir / "session.json"
                        if state_file.is_file():
                            state = json.loads(state_file.read_text())
                            sessions.append({
                                "sessionId": state.get("sessionId"),
                                "planId": state.get("planId"),
                                "status": state.get("status"),
                                "dir": str(session_dir),
                            })
            _print(sessions)
            return 0

        if args.command == "ui":
            from .ui import serve_ui

            plans_dir = (
                args.plans_dir
                if args.plans_dir is not None
                else config.source.parent
            )
            serve_ui(
                config,
                plans_dir,
                host=args.host,
                port=args.port,
                open_browser=not args.no_open,
            )
            return 0

    except ContractError as exc:
        parser.error(str(exc))
        return 2
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
