import argparse
import getpass
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, TextIO

from .api import ApiClient, ApiError, validate_agent_status
from .config import AgentConfig, DEFAULT_BASE_URL, HOME_ENV, load_config, save_config
from .metadata import parse_plugin_metadata
from .redaction import redact_secret, redact_text
from .run_state import RunState, load_run_state, save_run_state
from .workflow import is_workflow_terminal, summarize_factor_card, terminal_outcome


TASK_PAYLOAD_REQUIRED_FIELDS = {
    "task_id",
    "title",
    "category",
    "description",
    "allowed_data",
    "fwd_period",
}


def _print_json(stdout: TextIO, payload: Any) -> None:
    stdout.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    stdout.write("\n")


def _configured_home(args: argparse.Namespace, env: Mapping[str, str]) -> str | None:
    return getattr(args, "home", None) or env.get(HOME_ENV)


def _read_api_key(args: argparse.Namespace, env: Mapping[str, str], stdin: TextIO, stderr: TextIO) -> str:
    key = env.get("FACTOR_MINING_AGENT_API_KEY")
    if key:
        return key.strip()
    if getattr(args, "api_key_stdin", False):
        return stdin.readline().strip()
    return getpass.getpass("Factor Mining Agent API Key: ", stream=stderr).strip()


def _require_saved_external_agent_status(config: AgentConfig) -> None:
    try:
        validate_agent_status(config.agent_status)
    except Exception as exc:
        raise RuntimeError(
            "Stored Factor Mining config is not verified as an external_agent delegated key. "
            "Run setup again with a Factor Mining Agent API Key, not a frontend user key."
        ) from exc


def _client_from_config(
    args: argparse.Namespace,
    env: Mapping[str, str],
    opener: Any = None,
    *,
    verify_saved: bool = True,
    verify_live: bool = True,
) -> tuple[AgentConfig, ApiClient]:
    config = load_config(home=_configured_home(args, env))
    client = ApiClient(config.base_url, config.api_key, opener=opener)
    if verify_saved:
        _require_saved_external_agent_status(config)
    if verify_live:
        client.agent_status()
    return config, client


def _job_ids(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("job_ids"), list):
        return [str(job_id) for job_id in payload["job_ids"]]
    jobs = payload.get("jobs")
    if isinstance(jobs, list):
        ids = []
        for job in jobs:
            if isinstance(job, dict) and job.get("job_id"):
                ids.append(str(job["job_id"]))
            elif isinstance(job, str):
                ids.append(job)
        return ids
    if payload.get("job_id"):
        return [str(payload["job_id"])]
    return []


def _session_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("session_id", "id"):
            if payload.get(key):
                return str(payload[key])
    return None


def _plugin_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("plugin_id", "id"):
            if payload.get(key):
                return str(payload[key])
    return None


def _validate_artifact_name(name: str) -> None:
    if not name or name in {".", ".."}:
        raise ValueError("artifact name must be a single file name")
    path = Path(name)
    if path.is_absolute() or "/" in name or "\\" in name or ".." in path.parts:
        raise ValueError("artifact name must be a single file name")


def _save_json_artifact(output_dir: str | None, name: str, payload: Any) -> str | None:
    if not output_dir:
        return None
    _validate_artifact_name(name)
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    artifact_path = path / name
    artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(artifact_path)


def _fetch_optional_artifact(
    client: ApiClient,
    job_ids: list[str],
    artifact_name: str,
    output_dir: str | None = None,
) -> tuple[Any | None, dict[str, Any]]:
    if output_dir:
        _validate_artifact_name(artifact_name)
    errors = []
    for job_id in job_ids:
        try:
            card = client.artifact(job_id, artifact_name)
            artifact: dict[str, Any] = {
                "name": artifact_name,
                "job_id": job_id,
                "status": "available",
            }
            saved_path = _save_json_artifact(output_dir, artifact_name, card)
            if saved_path:
                artifact["path"] = saved_path
            return card, artifact
        except ApiError as exc:
            if exc.status not in (404, 410):
                raise
            errors.append(
                {
                    "job_id": job_id,
                    "name": artifact_name,
                    "status": exc.status,
                    "message": "Artifact is unavailable.",
                }
            )
    artifact = {"name": artifact_name, "status": "unavailable"}
    if errors:
        artifact["errors"] = errors
    return None, artifact


def _load_json_object(text: str, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _validate_task_payload(payload: Mapping[str, Any]) -> None:
    missing = sorted(field for field in TASK_PAYLOAD_REQUIRED_FIELDS if field not in payload)
    allowed_data = payload.get("allowed_data")
    if not isinstance(allowed_data, list) or not all(str(item).strip() for item in allowed_data):
        missing.append("allowed_data")
    if missing:
        fields = ", ".join(dict.fromkeys(missing))
        raise ValueError(f"task_payload is missing required fields: {fields}")


def _load_task_payload(args: argparse.Namespace) -> dict[str, Any] | None:
    text = getattr(args, "task_payload_json", None)
    path = getattr(args, "task_payload_file", None)
    if text and path:
        raise ValueError("Provide only one of --task-payload-json or --task-payload-file")
    if path:
        text = Path(path).read_text(encoding="utf-8")
    if not text:
        return None
    payload = _load_json_object(text, label="task_payload")
    _validate_task_payload(payload)
    return payload


def _compact_workflow(workflow: Any) -> dict[str, Any]:
    if not isinstance(workflow, dict):
        return {}
    return {
        key: workflow.get(key)
        for key in ("stage", "status", "stage_label", "next_action", "progress")
        if key in workflow
    }


def _run_wait_flow(
    *,
    client: ApiClient,
    state: RunState,
    artifact_name: str,
    output_dir: str | None,
    poll_interval: float,
    timeout: float,
    home: str | None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest_workflow: Any = None
    latest_jobs: list[dict[str, Any]] = []

    while True:
        latest_workflow = client.workflow(state.session_id) if state.session_id else {}
        latest_jobs = [client.job(job_id) for job_id in state.job_ids]
        stage = latest_workflow.get("stage") if isinstance(latest_workflow, dict) else None
        save_run_state(
            RunState(
                client_run_id=state.client_run_id,
                session_id=state.session_id,
                plugin_id=state.plugin_id,
                job_ids=state.job_ids,
                plugin_path=state.plugin_path,
                workflow_stage=stage,
                artifact_paths=state.artifact_paths,
            ),
            home=home,
        )
        if is_workflow_terminal(latest_workflow or {}, latest_jobs):
            break
        if time.monotonic() >= deadline:
            latest = {
                "workflow": _compact_workflow(latest_workflow),
                "jobs": [
                    {
                        "job_id": job.get("job_id") or job.get("id"),
                        "status": job.get("status"),
                        "position_mode": job.get("position_mode"),
                    }
                    for job in latest_jobs
                ],
            }
            raise RuntimeError(
                "Backtest timed out before completion. "
                f"Resume command: python3 scripts/factor_api.py resume --client-run-id {state.client_run_id} --wait. "
                f"Latest state: {json.dumps(latest, separators=(',', ':'), sort_keys=True)}"
            )
        if poll_interval > 0:
            time.sleep(poll_interval)

    card, artifact = _fetch_optional_artifact(client, state.job_ids, artifact_name, output_dir)
    artifact_paths = dict(state.artifact_paths)
    if artifact.get("path"):
        artifact_paths[str(artifact_name)] = str(artifact["path"])
        save_run_state(
            RunState(
                client_run_id=state.client_run_id,
                session_id=state.session_id,
                plugin_id=state.plugin_id,
                job_ids=state.job_ids,
                plugin_path=state.plugin_path,
                workflow_stage=stage,
                artifact_paths=artifact_paths,
            ),
            home=home,
        )
    summary = summarize_factor_card(card or {}, jobs=latest_jobs)
    if artifact["status"] == "unavailable":
        summary["artifact_status"] = "unavailable"
        if artifact.get("errors"):
            summary["artifact_errors"] = artifact["errors"]
    outcome = terminal_outcome(latest_workflow or {}, latest_jobs)
    return {
        **outcome,
        "client_run_id": state.client_run_id,
        "session_id": state.session_id,
        "plugin_id": state.plugin_id,
        "job_ids": state.job_ids,
        "workflow": _compact_workflow(latest_workflow),
        "jobs": latest_jobs,
        "artifact": artifact,
        "summary": summary,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factor_mining_agent", description="Factor Mining agent workflow helper")
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--home", help="Use an alternate Factor Mining agent state directory")
    subcommands = parser.add_subparsers(dest="command", required=True)

    setup = subcommands.add_parser("setup", parents=[parent], help="Configure Factor Mining Agent API access")
    setup.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Factor Mining API URL (default: production, {DEFAULT_BASE_URL})",
    )
    setup.add_argument("--api-key-stdin", action="store_true", help="Read the Agent API Key from stdin")

    subcommands.add_parser("status", parents=[parent], help="Verify Factor Mining Agent API access")

    metadata = subcommands.add_parser("metadata", help="Statically inspect plugin.py metadata")
    metadata.add_argument("--plugin-path", required=True, help="Path to plugin.py")

    tasks = subcommands.add_parser("tasks", parents=[parent], help="List available Factor Mining tasks")
    tasks.add_argument("--limit", type=int, default=20, help="Maximum number of tasks to return")
    tasks.add_argument("--status", default="open", help="Task status filter")

    create_session = subcommands.add_parser("create-session", parents=[parent], help="Create a research session")
    create_session.add_argument("--idea", help="Custom factor idea for free-mode research")
    create_session.add_argument("--task-id", help="Factor Mining task id for worker-mode research")
    create_session.add_argument("--task-payload-json", help="Direct task_payload JSON for custom sessions")
    create_session.add_argument("--task-payload-file", help="Path to direct task_payload JSON")
    create_session.add_argument("--client-run-id", help="Stable local run id for idempotency and resume")

    dedup = subcommands.add_parser("dedup-context", parents=[parent], help="Request factor deduplication context")
    dedup.add_argument("--session-id", required=True, help="Factor Mining session id")
    dedup.add_argument("--description", required=True, help="Draft factor description")
    dedup.add_argument("--formula", required=True, help="Draft factor formula")
    dedup.add_argument("--limit", type=int, default=8, help="Maximum similar factors to return")

    upload = subcommands.add_parser("upload-backtest", parents=[parent], help="Upload plugin.py and submit a backtest")
    upload.add_argument("--session-id", required=True, help="Factor Mining session id")
    upload.add_argument("--plugin-path", required=True, help="Path to plugin.py")
    upload.add_argument("--client-run-id", help="Stable local run id for idempotency and resume")
    upload.add_argument("--parent-client-run-id", help="Optional parent run id for retry lineage")
    upload.add_argument("--position-mode", default="both", help="Requested position mode")
    upload.add_argument("--fwd-period", type=int, default=7, help="Forward-return horizon, default 7")
    upload.add_argument("--decision-summary", help="Short local-agent rationale for this submission")
    upload.add_argument("--wait", action="store_true", help="Wait for terminal workflow and job results")
    upload.add_argument("--poll-interval", type=float, default=10.0, help="Seconds between polls when --wait is used")
    upload.add_argument("--timeout", type=float, default=900.0, help="Maximum seconds to wait for terminal results")
    upload.add_argument("--artifact-name", default="default_factor_card.json", help="Artifact to fetch after completion")
    upload.add_argument("--output-dir", help="Optional directory for fetched JSON artifacts")

    workflow = subcommands.add_parser("workflow", parents=[parent], help="Fetch workflow state")
    workflow.add_argument("--session-id", required=True, help="Factor Mining session id")

    job = subcommands.add_parser("job", parents=[parent], help="Fetch job state")
    job.add_argument("--job-id", required=True, help="Factor Mining job id")

    artifact = subcommands.add_parser("artifact", parents=[parent], help="Fetch a job artifact")
    artifact.add_argument("--job-id", required=True, help="Factor Mining job id")
    artifact.add_argument("--name", default="default_factor_card.json", help="Artifact name")
    artifact.add_argument("--output-dir", help="Optional directory for fetched JSON artifacts")

    resume = subcommands.add_parser("resume", parents=[parent], help="Resume a persisted run")
    resume.add_argument("--client-run-id", required=True, help="Stable local run id")
    resume.add_argument("--artifact-name", default="default_factor_card.json", help="Artifact to fetch when available")
    resume.add_argument("--wait", action="store_true", help="Wait for terminal workflow and job results")
    resume.add_argument("--poll-interval", type=float, default=10.0, help="Seconds between polls when --wait is used")
    resume.add_argument("--timeout", type=float, default=900.0, help="Maximum seconds to wait for terminal results")
    resume.add_argument("--output-dir", help="Optional directory for fetched JSON artifacts")

    return parser


def run(args: argparse.Namespace, *, env: Mapping[str, str], stdin: TextIO, stdout: TextIO, stderr: TextIO, opener: Any) -> int:
    if args.command == "setup":
        key = _read_api_key(args, env, stdin, stderr)
        if not key:
            raise RuntimeError("A Factor Mining agent API key is required")
        base_url = args.base_url.rstrip("/")
        client = ApiClient(base_url, key, opener=opener)
        health = client.health()
        status = dict(client.agent_status())
        status["health"] = health
        path = save_config(
            AgentConfig(base_url=base_url, api_key=key, agent_status=status),
            home=_configured_home(args, env),
        )
        _print_json(
            stdout,
            {
                "ok": True,
                "config_path": str(path),
                "base_url": base_url,
                "api_key": redact_secret(key),
                "agent_status": status,
            },
        )
        return 0

    if args.command == "metadata":
        _print_json(stdout, parse_plugin_metadata(args.plugin_path).to_dict())
        return 0

    if args.command == "status":
        config, client = _client_from_config(args, env, opener=opener, verify_saved=False, verify_live=False)
        payload = {"health": client.health(), "agent_status": client.agent_status()}
        payload["api_key"] = redact_secret(config.api_key)
        _print_json(stdout, payload)
        return 0

    config, client = _client_from_config(args, env, opener=opener)

    if args.command == "tasks":
        _print_json(stdout, client.list_tasks(limit=args.limit, status=args.status))
        return 0

    if args.command == "create-session":
        task_payload = _load_task_payload(args)
        if args.idea and task_payload is None:
            raise ValueError(
                "Custom sessions require direct task_payload. "
                "Provide --task-payload-file or --task-payload-json before upload."
            )
        response = client.create_session(
            idea=args.idea,
            task_id=args.task_id,
            task_payload=task_payload,
            client_run_id=args.client_run_id,
        )
        if args.client_run_id:
            save_run_state(
                RunState(client_run_id=args.client_run_id, session_id=_session_id(response)),
                home=_configured_home(args, env),
            )
        _print_json(stdout, response)
        return 0

    if args.command == "dedup-context":
        _print_json(
            stdout,
            client.dedup_context(
                session_id=args.session_id,
                description=args.description,
                formula=args.formula,
                limit=args.limit,
            ),
        )
        return 0

    if args.command == "upload-backtest":
        if args.output_dir:
            _validate_artifact_name(args.artifact_name)
        client_run_id = args.client_run_id or f"fm-{uuid.uuid4().hex}"
        plugin_path = Path(args.plugin_path)
        metadata = parse_plugin_metadata(plugin_path)
        upload_response = client.upload_plugin(
            session_id=args.session_id,
            plugin_path=plugin_path,
            metadata=metadata,
            client_run_id=client_run_id,
            parent_client_run_id=args.parent_client_run_id,
            fwd_period=args.fwd_period,
            decision_summary=args.decision_summary,
        )
        plugin_id = _plugin_id(upload_response)
        if not plugin_id:
            raise RuntimeError("Upload response did not include plugin_id")
        backtest_response = client.submit_backtest(
            args.session_id,
            plugin_id,
            position_mode=args.position_mode,
            client_run_id=client_run_id,
        )
        state = RunState(
            client_run_id=client_run_id,
            session_id=args.session_id,
            plugin_id=plugin_id,
            job_ids=_job_ids(backtest_response),
            plugin_path=str(plugin_path),
            workflow_stage="submitted",
            artifact_paths={},
        )
        save_run_state(state, home=_configured_home(args, env))
        if args.wait:
            _print_json(
                stdout,
                _run_wait_flow(
                    client=client,
                    state=state,
                    artifact_name=args.artifact_name,
                    output_dir=args.output_dir,
                    poll_interval=args.poll_interval,
                    timeout=args.timeout,
                    home=_configured_home(args, env),
                ),
            )
        else:
            _print_json(
                stdout,
                {
                    "client_run_id": client_run_id,
                    "upload": upload_response,
                    "backtest": backtest_response,
                    "run_state": state.to_dict(),
                },
            )
        return 0

    if args.command == "workflow":
        _print_json(stdout, client.workflow(args.session_id))
        return 0

    if args.command == "job":
        _print_json(stdout, client.job(args.job_id))
        return 0

    if args.command == "artifact":
        if args.output_dir:
            _validate_artifact_name(args.name)
        artifact = client.artifact(args.job_id, args.name)
        if args.output_dir:
            artifact_path = _save_json_artifact(args.output_dir, args.name, artifact)
            _print_json(stdout, {"artifact": artifact, "artifact_path": str(artifact_path)})
        else:
            _print_json(stdout, artifact)
        return 0

    if args.command == "resume":
        state = load_run_state(args.client_run_id, home=_configured_home(args, env))
        if args.output_dir:
            _validate_artifact_name(args.artifact_name)
        if args.wait:
            _print_json(
                stdout,
                _run_wait_flow(
                    client=client,
                    state=state,
                    artifact_name=args.artifact_name,
                    output_dir=args.output_dir,
                    poll_interval=args.poll_interval,
                    timeout=args.timeout,
                    home=_configured_home(args, env),
                ),
            )
            return 0
        workflow = client.workflow(state.session_id) if state.session_id else None
        jobs = [client.job(job_id) for job_id in state.job_ids]
        card = None
        card, artifact = _fetch_optional_artifact(client, state.job_ids, args.artifact_name, args.output_dir)
        summary = summarize_factor_card(card or {}, jobs=jobs)
        if artifact["status"] == "unavailable":
            summary["artifact_status"] = "unavailable"
            if artifact.get("errors"):
                summary["artifact_errors"] = artifact["errors"]
        outcome = (
            terminal_outcome(workflow or {}, jobs)
            if is_workflow_terminal(workflow or {}, jobs)
            else {"ok": False, "status": "running", "terminal_status": None}
        )
        _print_json(
            stdout,
            {
                **outcome,
                "run_state": state.to_dict(),
                "workflow": workflow,
                "jobs": jobs,
                "artifact": artifact,
                "summary": summary,
            },
        )
        return 0

    raise RuntimeError(f"Unknown command: {args.command}")


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    opener: Any = None,
) -> int:
    env = env if env is not None else os.environ
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    known_secret = env.get("FACTOR_MINING_AGENT_API_KEY")
    try:
        return run(args, env=env, stdin=stdin, stdout=stdout, stderr=stderr, opener=opener)
    except Exception as exc:
        stderr.write(redact_text(f"{exc}\n", extra_secrets=[known_secret] if known_secret else None))
        return 1
