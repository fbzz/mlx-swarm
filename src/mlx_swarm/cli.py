"""CLI entrypoint for MLX Swarm."""
# @lat: [[Architecture]]

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .commander import (
    CommanderError,
    CommanderStore,
    canonical_json_sha256,
)
from .contracts import ContractError, load_config, load_plan
from .evaluation import (
    DEFAULT_PUBLIC_RESULTS_DIR,
    EvaluationError,
    EvaluationRunner,
    EvaluationStore,
    load_evaluation_profile,
    preliminary_evaluation_profile,
    profile_payload,
    update_readme_economics,
)
from .executor import execute_plan
from .session import Session, _run_id, _utc_now
from .skill_install import SkillInstallError, install_bundled_skill
from .workspace import (
    WorkspaceError,
    cleanup_worktree,
    execution_preview,
    load_artifact,
    load_workspace_snapshot,
    prepare_worktree,
    submit_artifact_decision,
    workspace_readiness,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mlx-swarm",
        description=(
            "Run bounded DAGs of local MLX agents with deterministic gates "
            "and frontier-final review."
        ),
    )
    parser.add_argument(
        "--config",
        required=False,
        type=Path,
        help="Path to MLX Swarm config JSON.",
    )
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
    run_parser.add_argument(
        "--approve-plan-digest",
        default=None,
        help="Required canonical plan SHA-256 for a new workspace run.",
    )
    run_parser.add_argument(
        "--approve-execution-digest",
        default=None,
        help="Required displayed execution SHA-256 for a new workspace run.",
    )

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

    artifact_parser = sub.add_parser(
        "artifact",
        help="Inspect or decide a typed workspace artifact.",
    )
    artifact_sub = artifact_parser.add_subparsers(
        dest="artifact_command",
        required=True,
    )
    artifact_show = artifact_sub.add_parser("show")
    artifact_show.add_argument("session_dir", type=Path)
    artifact_show.add_argument("task_id")
    for command in ("apply", "reject", "verify"):
        decision_parser = artifact_sub.add_parser(command)
        decision_parser.add_argument("session_dir", type=Path)
        decision_parser.add_argument("task_id")
        decision_parser.add_argument("--digest", required=True)
        if command == "reject":
            decision_parser.add_argument("--reason", default=None)

    workspace_parser = sub.add_parser(
        "workspace",
        help="Inspect or clean an isolated session worktree.",
    )
    workspace_sub = workspace_parser.add_subparsers(
        dest="workspace_command",
        required=True,
    )
    workspace_preview_parser = workspace_sub.add_parser("preview")
    workspace_preview_parser.add_argument("plan", type=Path)
    workspace_status_parser = workspace_sub.add_parser("status")
    workspace_status_parser.add_argument("session_dir", type=Path)
    workspace_cleanup_parser = workspace_sub.add_parser("cleanup")
    workspace_cleanup_parser.add_argument("session_dir", type=Path)

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

    commander_parser = sub.add_parser(
        "commander",
        help="Manage frontier planning and final-review handoffs.",
    )
    commander_sub = commander_parser.add_subparsers(
        dest="commander_command",
        required=True,
    )

    create_parser = commander_sub.add_parser(
        "create",
        help="Create a frontier planning request.",
    )
    create_parser.add_argument("--objective", required=True)
    create_parser.add_argument(
        "--constraint",
        action="append",
        default=[],
        help="Operator constraint; repeat for multiple values.",
    )
    create_parser.add_argument("--revision-of", default=None)

    commander_sub.add_parser("list", help="List commander requests.")
    show_parser = commander_sub.add_parser(
        "show",
        help="Show a commander request and its plan ledger.",
    )
    show_parser.add_argument("request_id")

    claim_plan_parser = commander_sub.add_parser(
        "claim-plan",
        help="Atomically claim one planning response slot.",
    )
    claim_plan_parser.add_argument("request_id")
    claim_plan_parser.add_argument("--adapter", default="codex-skill")

    release_plan_parser = commander_sub.add_parser(
        "release-plan",
        help="Release a planning claim before a response is recorded.",
    )
    release_plan_parser.add_argument("request_id")
    release_plan_parser.add_argument("--claim-id", required=True)

    import_plan_parser = commander_sub.add_parser(
        "import-plan",
        help="Import and seal one frontier plan response.",
    )
    import_plan_parser.add_argument("request_id")
    import_plan_parser.add_argument("response", type=Path)
    import_plan_parser.add_argument("--claim-id", required=True)
    _add_frontier_receipt_options(import_plan_parser)

    claim_review_parser = commander_sub.add_parser(
        "claim-review",
        help="Atomically claim one final-review response slot.",
    )
    claim_review_parser.add_argument("session_dir", type=Path)
    claim_review_parser.add_argument("--adapter", default="codex-skill")

    release_review_parser = commander_sub.add_parser(
        "release-review",
        help="Release a review claim before a response is recorded.",
    )
    release_review_parser.add_argument("session_dir", type=Path)
    release_review_parser.add_argument("--claim-id", required=True)

    import_review_parser = commander_sub.add_parser(
        "import-review",
        help="Import and seal one frontier review response.",
    )
    import_review_parser.add_argument("session_dir", type=Path)
    import_review_parser.add_argument("response", type=Path)
    import_review_parser.add_argument("--claim-id", required=True)
    _add_frontier_receipt_options(import_review_parser)

    review_status_parser = commander_sub.add_parser(
        "review-status",
        help="Inspect a session's review ledger.",
    )
    review_status_parser.add_argument("session_dir", type=Path)

    evaluation_parser = sub.add_parser(
        "eval",
        help="Prepare, run, inspect, and publish paired economics studies.",
    )
    evaluation_sub = evaluation_parser.add_subparsers(
        dest="evaluation_command",
        required=True,
    )
    evaluation_prepare = evaluation_sub.add_parser(
        "prepare",
        help="Freeze a pinned BugsInPy pilot and measured suite.",
    )
    evaluation_prepare.add_argument("profile", type=Path)
    evaluation_prepare.add_argument(
        "--preliminary",
        action="store_true",
        help="Prepare 2 calibration and 6 measured cases (one per project).",
    )
    evaluation_prepare.add_argument(
        "--resume",
        dest="resume_evaluation_id",
        default=None,
        help=(
            "Resume an interrupted, unsealed preparation by evaluation ID. "
            "Completed case runtimes are reused."
        ),
    )
    evaluation_run = evaluation_sub.add_parser(
        "run",
        help="Run or resume one paired evaluation phase.",
    )
    evaluation_run.add_argument("evaluation_id")
    evaluation_run.add_argument(
        "--phase",
        required=True,
        choices=("pilot", "measured"),
    )
    evaluation_run.add_argument(
        "--profile",
        type=Path,
        required=True,
        help="Original profile; its digest is checked against the snapshot.",
    )
    evaluation_run.add_argument(
        "--preliminary",
        action="store_true",
        help="Use the derived 2+6 profile frozen by prepare --preliminary.",
    )
    evaluation_status = evaluation_sub.add_parser(
        "status",
        help="Inspect an evaluation ledger and paired progress.",
    )
    evaluation_status.add_argument("evaluation_id")
    evaluation_report = evaluation_sub.add_parser(
        "report",
        help="Export sanitized evidence and update the README tables.",
    )
    evaluation_report.add_argument("evaluation_id")
    evaluation_report.add_argument(
        "--export",
        type=Path,
        default=None,
        help="Empty export directory (defaults under benchmarks/results).",
    )
    evaluation_report.add_argument(
        "--readme",
        type=Path,
        default=None,
        help="README to update (defaults beside the config).",
    )
    evaluation_report.add_argument(
        "--check",
        action="store_true",
        help="Verify the generated README block without changing it.",
    )
    evaluation_report.add_argument(
        "--preliminary",
        action="store_true",
        help=(
            "Export a deterministic 2-calibration / 6-measured partial study "
            "without enabling the 30-pair claim gate."
        ),
    )

    skill_parser = sub.add_parser(
        "skill",
        help="Manage the bundled Codex orchestration skill.",
    )
    skill_sub = skill_parser.add_subparsers(
        dest="skill_command",
        required=True,
    )
    install_parser = skill_sub.add_parser(
        "install",
        help="Install the bundled mlx-swarm-commander skill.",
    )
    install_parser.add_argument(
        "--skills-dir",
        type=Path,
        default=None,
        help="Override the Codex skills directory.",
    )
    install_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing non-symlinked skill.",
    )

    return parser


def _add_frontier_receipt_options(
    parser: argparse.ArgumentParser,
) -> None:
    parser.add_argument("--adapter", default=None)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--prompt-tokens", type=_non_negative_int, default=None)
    parser.add_argument(
        "--completion-tokens",
        type=_non_negative_int,
        default=None,
    )
    parser.add_argument("--total-tokens", type=_non_negative_int, default=None)


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
        if argv is None and Path(sys.argv[0]).name == "swarm":
            print(
                "Warning: the 'swarm' command is deprecated; use 'mlx-swarm'.",
                file=sys.stderr,
            )

        if args.command == "skill":
            if args.skill_command == "install":
                installed = install_bundled_skill(
                    skills_dir=args.skills_dir,
                    force=args.force,
                )
                _print({
                    "installed": True,
                    "skill": "mlx-swarm-commander",
                    "path": str(installed),
                })
                return 0

        if args.config is None:
            parser.error("--config is required for this command.")
        config = load_config(args.config)

        if args.command == "eval":
            evaluation_store = EvaluationStore(config)
            if args.evaluation_command == "prepare":
                profile = load_evaluation_profile(args.profile)
                if args.preliminary:
                    profile = preliminary_evaluation_profile(profile)
                if args.resume_evaluation_id is None:
                    detail = evaluation_store.prepare(profile)
                else:
                    detail = evaluation_store.prepare(
                        profile,
                        resume_evaluation_id=args.resume_evaluation_id,
                    )
                _print(detail)
                return 0
            if args.evaluation_command == "status":
                _print(evaluation_store.detail(args.evaluation_id))
                return 0
            if args.evaluation_command == "run":
                profile = load_evaluation_profile(args.profile)
                if args.preliminary:
                    profile = preliminary_evaluation_profile(profile)
                detail = evaluation_store.detail(args.evaluation_id)
                expected = detail["environment"].get("profileSha256")
                actual = canonical_json_sha256(profile_payload(profile))
                if expected != actual:
                    raise EvaluationError(
                        "Evaluation profile differs from the prepared snapshot."
                    )
                runner = EvaluationRunner(
                    config,
                    evaluation_store,
                    profile,
                )
                _print(
                    runner.run_phase(
                        args.evaluation_id,
                        args.phase,
                    )
                )
                return 0
            export_dir = (
                args.export
                if args.export is not None
                else (
                    evaluation_store.workspace_root
                    / DEFAULT_PUBLIC_RESULTS_DIR
                    / (
                        f"{args.evaluation_id}-preliminary-6"
                        if args.preliminary
                        else args.evaluation_id
                    )
                )
            )
            report = evaluation_store.report(
                args.evaluation_id,
                export_dir,
                check=args.check,
                preliminary=args.preliminary,
            )
            readme = (
                args.readme
                if args.readme is not None
                else evaluation_store.workspace_root / "README.md"
            )
            update_readme_economics(
                readme.resolve(),
                report["readmeMarkdown"],
                check=args.check,
            )
            _print({
                "evaluationId": args.evaluation_id,
                "reportId": report["reportId"],
                "exportDir": str(export_dir.resolve()),
                "readme": str(readme.resolve()),
                "checked": args.check,
            })
            return 0

        if args.command == "artifact":
            session = Session.load(args.session_dir, config)
            if args.task_id not in session.state.get("tasks", {}):
                raise WorkspaceError(f"Task not found: {args.task_id}")
            if args.artifact_command == "show":
                manifest, payload = load_artifact(
                    session.dir,
                    args.task_id,
                )
                _print({
                    "manifest": manifest,
                    "payload": payload,
                    "task": session.state["tasks"][args.task_id],
                })
                return 0
            task_status = session.get_task_status(args.task_id)
            allowed_statuses = {
                "apply": {"awaiting_approval"},
                "reject": {
                    "awaiting_approval",
                    "verification_failed",
                },
                "verify": {"verification_failed"},
            }
            if task_status not in allowed_statuses[args.artifact_command]:
                raise WorkspaceError(
                    f"Artifact action {args.artifact_command!r} is not "
                    f"available from {task_status!r}."
                )
            decision = submit_artifact_decision(
                session.dir,
                args.task_id,
                action=args.artifact_command,
                artifact_sha256=args.digest,
                source="cli",
                reason=getattr(args, "reason", None),
            )
            _print({
                "queued": True,
                "decision": decision,
                "resumeCommand": (
                    f"mlx-swarm --config {config.source} resume {session.dir}"
                ),
            })
            return 0

        if args.command == "workspace":
            if args.workspace_command == "preview":
                plan = load_plan(args.plan, config)
                _print({
                    "planDigest": canonical_json_sha256(plan.raw),
                    "execution": execution_preview(config, plan),
                })
                return 0
            session = Session.load(args.session_dir, config)
            snapshot = load_workspace_snapshot(session.dir)
            if args.workspace_command == "status":
                _print({
                    "workspace": snapshot,
                    "runStatus": session.state.get("status"),
                })
                return 0
            if session.state.get("status") not in {
                "completed",
                "partial",
                "failed",
            }:
                raise WorkspaceError(
                    "Only terminal-run worktrees can be cleaned up."
                )
            if snapshot.get("cleanedUp"):
                raise WorkspaceError(
                    "Session worktree was already cleaned up."
                )
            cleanup_worktree(snapshot)
            session.update_workspace(snapshot)
            _print({
                "cleanedUp": True,
                "branch": snapshot["branch"],
            })
            return 0

        if args.command == "commander":
            store = CommanderStore(config)
            if args.commander_command == "create":
                _print(
                    store.create_request(
                        args.objective,
                        args.constraint,
                        revision_of=args.revision_of,
                    )
                )
                return 0
            if args.commander_command == "list":
                _print({"requests": store.list_requests()})
                return 0
            if args.commander_command == "show":
                _print(store.request_detail(args.request_id))
                return 0
            if args.commander_command == "claim-plan":
                _print(
                    store.claim_plan(
                        args.request_id,
                        adapter=args.adapter,
                    )
                )
                return 0
            if args.commander_command == "release-plan":
                store.release_plan_claim(
                    args.request_id,
                    args.claim_id,
                )
                _print({"released": True, "phase": "plan"})
                return 0
            if args.commander_command == "import-plan":
                _print(
                    store.import_plan(
                        args.request_id,
                        args.response,
                        claim_id=args.claim_id,
                        **_receipt_options(args),
                    )
                )
                return 0
            if args.commander_command == "claim-review":
                _print(
                    store.claim_review(
                        args.session_dir,
                        adapter=args.adapter,
                    )
                )
                return 0
            if args.commander_command == "release-review":
                store.release_review_claim(
                    args.session_dir,
                    args.claim_id,
                )
                _print({"released": True, "phase": "review"})
                return 0
            if args.commander_command == "import-review":
                _print(
                    store.import_review(
                        args.session_dir,
                        args.response,
                        claim_id=args.claim_id,
                        **_receipt_options(args),
                    )
                )
                return 0
            if args.commander_command == "review-status":
                _print(store.review_detail(args.session_dir))
                return 0

        if args.command == "doctor":
            from .backend import _resolve_model_path

            workspace = workspace_readiness(config)
            try:
                model_path = _resolve_model_path(config)
                result = {
                    "ready": (
                        workspace.get("ready", True)
                        if workspace.get("enabled")
                        else True
                    ),
                    "model": {"path": str(model_path), "repository": config.model.repository},
                    "batch": {
                        "maxWorkers": config.batch.max_workers,
                        "prefillStepSize": config.batch.prefill_step_size,
                        "maxPromptCharacters": config.batch.max_prompt_characters,
                    },
                    "artifactsDir": str(config.artifacts_dir),
                    "workspace": workspace,
                }
            except Exception as exc:
                result = {
                    "ready": False,
                    "error": str(exc),
                    "workspace": workspace,
                }
            _print(result)
            return 0 if result.get("ready") else 1

        if args.command == "run":
            plan = load_plan(args.plan, config)
            session_dir = args.session_dir
            existing_session = (
                session_dir is not None
                and (session_dir / "session.json").is_file()
            )
            if plan.workspace_execution and not existing_session:
                plan_digest = canonical_json_sha256(plan.raw)
                if args.approve_plan_digest != plan_digest:
                    raise WorkspaceError(
                        "Workspace run requires the displayed canonical plan "
                        "digest via --approve-plan-digest."
                    )
                preview = execution_preview(config, plan)
                if (
                    args.approve_execution_digest
                    != preview["executionDigest"]
                ):
                    raise WorkspaceError(
                        "Workspace run requires the displayed execution "
                        "digest via --approve-execution-digest."
                    )
                run_id = (
                    session_dir.name
                    if session_dir is not None
                    else _run_id()
                )
                session_dir = (
                    session_dir
                    or config.artifacts_dir / plan.plan_id / run_id
                )
                snapshot = prepare_worktree(
                    config,
                    plan,
                    session_id=run_id,
                    expected_execution_digest=preview["executionDigest"],
                )
                prepared = Session(
                    session_dir,
                    plan,
                    session_id=run_id,
                    launch_source="cli",
                )
                prepared.set_sources(
                    config_source=config.source,
                    plan_source=plan.source,
                )
                prepared.state["maxRepair"] = args.max_repair
                prepared.attach_workspace(
                    snapshot,
                    execution_approval={
                        "schemaVersion": 1,
                        "planSha256": plan_digest,
                        "executionDigest": preview["executionDigest"],
                        "workspaceRoot": preview["workspaceRoot"],
                        "baseSha": preview["baseSha"],
                        "approvedAt": _utc_now(),
                        "source": "cli",
                    },
                )
            session = execute_plan(
                config,
                plan,
                session_dir=session_dir,
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
    except (
        CommanderError,
        EvaluationError,
        SkillInstallError,
        WorkspaceError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


def _receipt_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "adapter": args.adapter,
        "provider": args.provider,
        "model": args.model,
        "prompt_tokens": args.prompt_tokens,
        "completion_tokens": args.completion_tokens,
        "total_tokens": args.total_tokens,
    }


if __name__ == "__main__":
    sys.exit(main())
