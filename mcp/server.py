#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Mapping


MCP_ROOT = Path(__file__).resolve().parent
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

from factor_mining_agent_lib import connect
from factor_mining_agent_lib.api import ApiClient, ApiError
from factor_mining_agent_lib.buddy import (
    BUDDY_DOWNLOAD_URL,
    emit_buddy_event,
    failure_payload,
    result_payload_from_wait_result,
    validate_buddy_event_type,
)
from factor_mining_agent_lib.config import HOME_ENV
from factor_mining_agent_lib.metadata import parse_plugin_metadata
from factor_mining_agent_lib.redaction import redact_text
from factor_mining_agent_lib.run_state import RunState, load_run_state, save_run_state
from factor_mining_agent_lib.workflow import is_workflow_terminal, summarize_factor_card, terminal_outcome


PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "quandora-factor-mining"
SERVER_VERSION = "0.3.0"
MISSING_CREDENTIAL_MESSAGE = (
    "No Quandora local-agent credential is connected. Run quandora_connect to authorize this plugin through "
    "Quandora Local Agent Connect. Buddy is optional and only provides the desktop fishing animation companion. "
    f"You can download Buddy separately from {BUDDY_DOWNLOAD_URL}."
)
TASK_PAYLOAD_REQUIRED_FIELDS = {
    "task_id",
    "title",
    "category",
    "description",
    "allowed_data",
    "fwd_period",
}


class McpServerError(RuntimeError):
    pass


class MissingCredentialError(McpServerError):
    pass


class ToolInputError(McpServerError):
    pass


TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "quandora_connect",
        "description": "Authorize this local Factor Mining plugin through Quandora Local Agent Connect.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "web_connect_url": {"type": "string"},
                "credential_backend_base_url": {"type": "string"},
                "orchestrator_base_url": {"type": "string"},
                "label": {"type": "string"},
                "replace_existing": {"type": "boolean"},
                "timeout_seconds": {"type": "number", "minimum": 1},
                "open_browser": {"type": "boolean"},
                "home": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_connect_wait",
        "description": "Wait for a pending Quandora Local Agent Connect authorization to finish and store the delegated credential.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "connect_handle": {"type": "string"},
                "timeout_seconds": {"type": "number", "minimum": 1},
                "home": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_connect_pending",
        "description": "List pending Quandora Local Agent Connect authorizations that can be completed or cancelled in this agent session.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_connect_cancel",
        "description": "Cancel a pending Quandora Local Agent Connect authorization and close its loopback callback server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "connect_handle": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_connect_status",
        "description": "Report plugin-local Local Agent Connect status and optional Buddy animation companion availability.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "live_check": {"type": "boolean"},
                "home": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_disconnect",
        "description": "Revoke and remove the plugin-local Local Agent Connect credential.",
        "inputSchema": {
            "type": "object",
            "properties": {"home": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_status",
        "description": "Validate the plugin-local delegated external-agent credential.",
        "inputSchema": {
            "type": "object",
            "properties": {"home": {"type": "string"}, "live_check": {"type": "boolean"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_list_public_tasks",
        "description": "List open public Factor Mining tasks through plugin-local Local Agent Connect authentication.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "status": {"type": "string"},
                "home": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_create_task_session",
        "description": "Create a task-backed Factor Mining session from a published task id.",
        "inputSchema": {
            "type": "object",
            "required": ["task_id"],
            "properties": {
                "task_id": {"type": "string"},
                "client_run_id": {"type": "string"},
                "home": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_create_custom_session",
        "description": "Create a task-backed session from a custom factor idea and explicit task payload.",
        "inputSchema": {
            "type": "object",
            "required": ["idea", "task_payload"],
            "properties": {
                "idea": {"type": "string"},
                "task_payload": {"type": "object"},
                "client_run_id": {"type": "string"},
                "home": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_parse_plugin_metadata",
        "description": "Parse plugin.py metadata statically without importing or executing generated code.",
        "inputSchema": {
            "type": "object",
            "required": ["plugin_path"],
            "properties": {"plugin_path": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_request_dedup_context",
        "description": "Request similar-factor context for a draft description and formula.",
        "inputSchema": {
            "type": "object",
            "required": ["session_id", "description", "formula"],
            "properties": {
                "session_id": {"type": "string"},
                "description": {"type": "string"},
                "formula": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "home": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_upload_backtest_wait",
        "description": "Parse metadata, upload plugin.py, submit a backtest, wait for terminal state, and fetch the default factor card.",
        "inputSchema": {
            "type": "object",
            "required": ["session_id", "plugin_path"],
            "properties": {
                "session_id": {"type": "string"},
                "plugin_path": {"type": "string"},
                "client_run_id": {"type": "string"},
                "parent_client_run_id": {"type": "string"},
                "position_mode": {"type": "string"},
                "fwd_period": {"type": "integer"},
                "decision_summary": {"type": "string"},
                "wait": {"type": "boolean"},
                "poll_interval": {"type": "number"},
                "timeout": {"type": "number"},
                "artifact_name": {"type": "string"},
                "output_dir": {"type": "string"},
                "home": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_resume_run",
        "description": "Resume a persisted Factor Mining run by client_run_id, optionally waiting for terminal result.",
        "inputSchema": {
            "type": "object",
            "required": ["client_run_id"],
            "properties": {
                "client_run_id": {"type": "string"},
                "wait": {"type": "boolean"},
                "poll_interval": {"type": "number"},
                "timeout": {"type": "number"},
                "artifact_name": {"type": "string"},
                "output_dir": {"type": "string"},
                "home": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "quandora_emit_buddy_event",
        "description": "Emit a sanitized, best-effort local Buddy event without credentials or generated source.",
        "inputSchema": {
            "type": "object",
            "required": ["event_type"],
            "properties": {
                "event_type": {"type": "string"},
                "payload": {"type": "object"},
                "run": {"type": "object"},
                "source_agent": {"type": "string"},
                "workspace": {"type": "string"},
                "home": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
)


def list_tool_names() -> list[str]:
    return [tool["name"] for tool in TOOL_DEFINITIONS]


def call_tool(name: str, arguments: Mapping[str, Any] | None = None, *, opener: Any = None, env: Mapping[str, str] | None = None) -> Any:
    args = dict(arguments or {})
    if name == "quandora_connect":
        return _connect(args, opener=opener, env=env)
    if name == "quandora_connect_wait":
        return _connect_wait(args, opener=opener, env=env)
    if name == "quandora_connect_pending":
        return _connect_pending()
    if name == "quandora_connect_cancel":
        return _connect_cancel(args)
    if name == "quandora_connect_status":
        return _connect_status(args, opener=opener, env=env)
    if name == "quandora_disconnect":
        return _disconnect(args, opener=opener, env=env)
    if name == "quandora_status":
        return _status(args, opener=opener, env=env)
    if name == "quandora_list_public_tasks":
        return _list_public_tasks(args, opener=opener, env=env)
    if name == "quandora_create_task_session":
        return _create_task_session(args, opener=opener, env=env)
    if name == "quandora_create_custom_session":
        return _create_custom_session(args, opener=opener, env=env)
    if name == "quandora_parse_plugin_metadata":
        return _parse_plugin_metadata(args)
    if name == "quandora_request_dedup_context":
        return _request_dedup_context(args, opener=opener, env=env)
    if name == "quandora_upload_backtest_wait":
        return _upload_backtest_wait(args, opener=opener, env=env)
    if name == "quandora_resume_run":
        return _resume_run(args, opener=opener, env=env)
    if name == "quandora_emit_buddy_event":
        return _emit_buddy_event(args, env=env)
    raise ToolInputError(f"Unknown Quandora MCP tool: {name}")


def _status(args: Mapping[str, Any], *, opener: Any, env: Mapping[str, str] | None) -> dict[str, Any]:
    status_args = dict(args)
    status_args["live_check"] = args.get("live_check", True)
    return _connect_status(status_args, opener=opener, env=env)


def _connect(args: Mapping[str, Any], *, opener: Any, env: Mapping[str, str] | None) -> dict[str, Any]:
    active_env = env if env is not None else os.environ
    result = connect.connect_local_agent(
        web_connect_url=str(args.get("web_connect_url") or active_env.get("QUANDORA_CONNECT_WEB_URL") or connect.DEFAULT_WEB_CONNECT_URL),
        credential_backend_base_url=_optional_string(args, "credential_backend_base_url")
        or active_env.get("QUANDORA_CREDENTIAL_BACKEND_BASE_URL"),
        orchestrator_base_url=str(
            args.get("orchestrator_base_url")
            or active_env.get("QUANDORA_ORCHESTRATOR_BASE_URL")
            or connect.DEFAULT_BASE_URL
        ),
        label=_optional_string(args, "label"),
        replace_existing=bool(args.get("replace_existing", True)),
        timeout_seconds=float(args.get("timeout_seconds") or connect.DEFAULT_TIMEOUT_SECONDS),
        open_browser=bool(args.get("open_browser", True)),
        home=_configured_home(args, env),
        opener=opener,
        client={"name": "quandora-factor-mining-plugin", "adapter": "codex", "version": SERVER_VERSION},
    )
    return _redact_payload(result)


def _connect_wait(args: Mapping[str, Any], *, opener: Any, env: Mapping[str, str] | None) -> dict[str, Any]:
    result = connect.wait_for_pending_connect(
        connect_handle=_optional_string(args, "connect_handle"),
        timeout_seconds=float(args.get("timeout_seconds") or connect.DEFAULT_TIMEOUT_SECONDS),
        home=_configured_home(args, env),
        opener=opener,
    )
    return _redact_payload(result)


def _connect_pending() -> dict[str, Any]:
    return _redact_payload({"ok": True, "pending": connect.pending_connect_status()})


def _connect_cancel(args: Mapping[str, Any]) -> dict[str, Any]:
    return _redact_payload(connect.cancel_pending_connect_request(_optional_string(args, "connect_handle")))


def _connect_status(args: Mapping[str, Any], *, opener: Any, env: Mapping[str, str] | None) -> dict[str, Any]:
    home = _configured_home(args, env)
    buddy = _buddy_runtime_status(home)
    try:
        record = connect.load_connected_credential(home)
    except Exception as exc:
        return {
            "ok": True,
            "connected": False,
            "credential": None,
            "buddy": buddy,
            "message": str(exc),
        }

    safe_record = connect.safe_credential_record(record)
    result: dict[str, Any] = {
        "ok": True,
        "connected": True,
        "redacted": safe_record["credential"]["redacted"],
        "capabilities": safe_record.get("capabilities", []),
        "identity": safe_record.get("identity", {}),
        "base_url": safe_record.get("base_url"),
        "connected_at": safe_record.get("connected_at"),
        "buddy": buddy,
    }
    if bool(args.get("live_check", False)):
        client = ApiClient(str(record["base_url"]), str(record["credential"]["value"]), opener=opener)
        result["agent_status"] = _redact_payload(client.agent_status())
    return result


def _disconnect(args: Mapping[str, Any], *, opener: Any, env: Mapping[str, str] | None) -> dict[str, Any]:
    return _redact_payload(connect.disconnect_local_agent(home=_configured_home(args, env), opener=opener))


def _list_public_tasks(args: Mapping[str, Any], *, opener: Any, env: Mapping[str, str] | None) -> Any:
    _credential, client = _client_from_local_auth(args, opener=opener, env=env)
    limit = int(args.get("limit") or 20)
    status = args.get("status") if args.get("status") is not None else "open"
    return _redact_payload(client.list_tasks(limit=limit, status=str(status) if status else None))


def _create_task_session(args: Mapping[str, Any], *, opener: Any, env: Mapping[str, str] | None) -> Any:
    task_id = _required_string(args, "task_id")
    client_run_id = _optional_string(args, "client_run_id")
    _credential, client = _client_from_local_auth(args, opener=opener, env=env)
    response = client.create_session(task_id=task_id, client_run_id=client_run_id)
    if client_run_id:
        save_run_state(
            RunState(client_run_id=client_run_id, session_id=_session_id(response)),
            home=_configured_home(args, env),
        )
    return _redact_payload(response)


def _create_custom_session(args: Mapping[str, Any], *, opener: Any, env: Mapping[str, str] | None) -> Any:
    idea = _required_string(args, "idea")
    task_payload = args.get("task_payload")
    if not isinstance(task_payload, Mapping):
        raise ToolInputError("task_payload must be a JSON object")
    _validate_task_payload(task_payload)
    client_run_id = _optional_string(args, "client_run_id")
    _credential, client = _client_from_local_auth(args, opener=opener, env=env)
    response = client.create_session(
        idea=idea,
        task_payload=task_payload,
        client_run_id=client_run_id,
    )
    if client_run_id:
        save_run_state(
            RunState(client_run_id=client_run_id, session_id=_session_id(response)),
            home=_configured_home(args, env),
        )
    return _redact_payload(response)


def _parse_plugin_metadata(args: Mapping[str, Any]) -> dict[str, Any]:
    return parse_plugin_metadata(_required_string(args, "plugin_path")).to_dict()


def _request_dedup_context(args: Mapping[str, Any], *, opener: Any, env: Mapping[str, str] | None) -> Any:
    _credential, client = _client_from_local_auth(args, opener=opener, env=env)
    return _redact_payload(
        client.dedup_context(
            session_id=_required_string(args, "session_id"),
            description=_required_string(args, "description"),
            formula=_required_string(args, "formula"),
            limit=int(args.get("limit") or 8),
        )
    )


def _upload_backtest_wait(args: Mapping[str, Any], *, opener: Any, env: Mapping[str, str] | None) -> Any:
    _credential, client = _client_from_local_auth(args, opener=opener, env=env)
    home = _configured_home(args, env)
    session_id = _required_string(args, "session_id")
    plugin_path = Path(_required_string(args, "plugin_path"))
    client_run_id = _optional_string(args, "client_run_id") or f"fm-{uuid.uuid4().hex}"
    artifact_name = _optional_string(args, "artifact_name") or "default_factor_card.json"
    output_dir = _optional_string(args, "output_dir")
    if output_dir:
        _validate_artifact_name(artifact_name)

    emit_buddy_event(
        "factor.validating",
        {"file": "plugin.py"},
        run={"client_run_id": client_run_id, "session_id": session_id},
        workspace=plugin_path.parent,
        home=home,
    )
    metadata = parse_plugin_metadata(plugin_path)
    try:
        upload_response = client.upload_plugin(
            session_id=session_id,
            plugin_path=plugin_path,
            metadata=metadata,
            client_run_id=client_run_id,
            parent_client_run_id=_optional_string(args, "parent_client_run_id"),
            fwd_period=int(args.get("fwd_period") or 7),
            decision_summary=_optional_string(args, "decision_summary"),
        )
        plugin_id = _plugin_id(upload_response)
        if not plugin_id:
            raise McpServerError("Upload response did not include plugin_id")
        emit_buddy_event(
            "factor.casting",
            {"stage": "plugin_uploaded"},
            run={"client_run_id": client_run_id, "session_id": session_id},
            workspace=plugin_path.parent,
            home=home,
        )
        backtest_response = client.submit_backtest(
            session_id,
            plugin_id,
            position_mode=str(args.get("position_mode") or "both"),
            client_run_id=client_run_id,
        )
        state = RunState(
            client_run_id=client_run_id,
            session_id=session_id,
            plugin_id=plugin_id,
            job_ids=_job_ids(backtest_response),
            plugin_path=str(plugin_path),
            workflow_stage="submitted",
            artifact_paths={},
        )
        save_run_state(state, home=home)
        emit_buddy_event(
            "factor.submitted",
            {"status": "submitted"},
            run={"client_run_id": client_run_id, "session_id": session_id},
            workspace=plugin_path.parent,
            home=home,
        )
        emit_buddy_event(
            "factor.waiting",
            {"submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            run={"client_run_id": client_run_id, "session_id": session_id},
            workspace=plugin_path.parent,
            home=home,
        )
        if args.get("wait", True) is False:
            return _redact_payload(
                {
                    "client_run_id": client_run_id,
                    "upload": upload_response,
                    "backtest": backtest_response,
                    "run_state": state.to_dict(),
                }
            )
        return _redact_payload(
            _run_wait_flow(
                client=client,
                state=state,
                artifact_name=artifact_name,
                output_dir=output_dir,
                poll_interval=float(args.get("poll_interval") or 10.0),
                timeout=float(args.get("timeout") or 900.0),
                home=home,
            )
        )
    except Exception as exc:
        emit_buddy_event(
            "factor.failed",
            failure_payload("upload-backtest", str(exc), recoverable=True),
            run={"client_run_id": client_run_id, "session_id": session_id},
            workspace=plugin_path.parent,
            home=home,
        )
        raise


def _resume_run(args: Mapping[str, Any], *, opener: Any, env: Mapping[str, str] | None) -> Any:
    _credential, client = _client_from_local_auth(args, opener=opener, env=env)
    home = _configured_home(args, env)
    state = load_run_state(_required_string(args, "client_run_id"), home=home)
    artifact_name = _optional_string(args, "artifact_name") or "default_factor_card.json"
    output_dir = _optional_string(args, "output_dir")
    if output_dir:
        _validate_artifact_name(artifact_name)
    if args.get("wait") is True:
        return _redact_payload(
            _run_wait_flow(
                client=client,
                state=state,
                artifact_name=artifact_name,
                output_dir=output_dir,
                poll_interval=float(args.get("poll_interval") or 10.0),
                timeout=float(args.get("timeout") or 900.0),
                home=home,
            )
        )

    workflow = client.workflow(state.session_id) if state.session_id else None
    jobs = [client.job(job_id) for job_id in state.job_ids]
    card, artifact = _fetch_optional_artifact(client, state.job_ids, artifact_name, output_dir)
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
    result = {
        **outcome,
        "run_state": state.to_dict(),
        "workflow": workflow,
        "jobs": jobs,
        "artifact": artifact,
        "summary": summary,
    }
    if outcome.get("terminal_status"):
        run = {"client_run_id": state.client_run_id, "session_id": state.session_id}
        if outcome.get("ok"):
            emit_buddy_event("factor.result", result_payload_from_wait_result(result), run=run, home=home)
        else:
            emit_buddy_event(
                "factor.failed",
                failure_payload("resume", f"Backtest finished with terminal status {outcome.get('status')}", recoverable=True),
                run=run,
                home=home,
            )
    return _redact_payload(result)


def _emit_buddy_event(args: Mapping[str, Any], *, env: Mapping[str, str] | None) -> dict[str, bool]:
    event_type = _required_string(args, "event_type")
    validate_buddy_event_type(event_type)
    payload = args.get("payload") if isinstance(args.get("payload"), Mapping) else {}
    run = args.get("run") if isinstance(args.get("run"), Mapping) else {}
    return emit_buddy_event(
        event_type,
        _redact_payload(payload),
        run=_redact_payload(run),
        source_agent=str(args.get("source_agent") or "codex"),
        workspace=args.get("workspace"),
        home=_configured_home(args, env),
    )


def _client_from_local_auth(
    args: Mapping[str, Any],
    *,
    opener: Any,
    env: Mapping[str, str] | None,
    verify_live: bool = True,
) -> tuple[dict[str, Any], ApiClient]:
    try:
        credential = connect.load_connected_credential(home=_configured_home(args, env))
    except Exception as exc:
        raise MissingCredentialError(MISSING_CREDENTIAL_MESSAGE) from exc
    client = ApiClient(str(credential["base_url"]), str(credential["credential"]["value"]), opener=opener)
    if verify_live:
        client.agent_status()
    return credential, client


def _buddy_runtime_status(home: str | None) -> dict[str, Any]:
    root = Path(home).expanduser() if home is not None else Path.home()
    port_file = root / ".quandora-buddy" / "port.json"
    try:
        payload = json.loads(port_file.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False, "role": "optional_animation_companion"}
    return {
        "available": isinstance(payload, Mapping) and isinstance(payload.get("port"), int),
        "role": "optional_animation_companion",
        "credential_provider": "compatibility_only",
    }


def _configured_home(args: Mapping[str, Any], env: Mapping[str, str] | None) -> str | None:
    if args.get("home"):
        return str(args["home"])
    active_env = env if env is not None else os.environ
    return active_env.get(HOME_ENV)


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
            raise McpServerError(
                "Backtest timed out before completion. "
                f"Use quandora_resume_run with client_run_id {state.client_run_id}. "
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
    if isinstance(card, dict) and isinstance(card.get("fish"), dict):
        summary["fish"] = dict(card["fish"])
    if artifact["status"] == "unavailable":
        summary["artifact_status"] = "unavailable"
        if artifact.get("errors"):
            summary["artifact_errors"] = artifact["errors"]
    outcome = terminal_outcome(latest_workflow or {}, latest_jobs)
    result = {
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
    run = {"client_run_id": state.client_run_id, "session_id": state.session_id}
    if result["ok"]:
        emit_buddy_event("factor.result", result_payload_from_wait_result(result), run=run, home=home)
    else:
        emit_buddy_event(
            "factor.failed",
            failure_payload("backtest", f"Backtest finished with terminal status {result['status']}", recoverable=True),
            run=run,
            home=home,
        )
    return result


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


def _save_json_artifact(output_dir: str | None, name: str, payload: Any) -> str | None:
    if not output_dir:
        return None
    _validate_artifact_name(name)
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    artifact_path = path / name
    artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(artifact_path)


def _validate_artifact_name(name: str) -> None:
    if not name or name in {".", ".."}:
        raise ToolInputError("artifact name must be a single file name")
    path = Path(name)
    if path.is_absolute() or "/" in name or "\\" in name or ".." in path.parts:
        raise ToolInputError("artifact name must be a single file name")


def _validate_task_payload(payload: Mapping[str, Any]) -> None:
    missing = sorted(field for field in TASK_PAYLOAD_REQUIRED_FIELDS if field not in payload)
    allowed_data = payload.get("allowed_data")
    if not isinstance(allowed_data, list) or not all(str(item).strip() for item in allowed_data):
        missing.append("allowed_data")
    if missing:
        fields = ", ".join(dict.fromkeys(missing))
        raise ToolInputError(f"task_payload is missing required fields: {fields}")


def _compact_workflow(workflow: Any) -> dict[str, Any]:
    if not isinstance(workflow, dict):
        return {}
    return {
        key: workflow.get(key)
        for key in ("stage", "status", "stage_label", "next_action", "progress")
        if key in workflow
    }


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


def _required_string(args: Mapping[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{key} is required")
    return value.strip()


def _optional_string(args: Mapping[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{key} must be a non-empty string when provided")
    return value.strip()


def _redact_payload(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if key_str.lower() in {"api_key", "authorization", "token", "credential", "secret", "password"}:
                continue
            redacted[key_str] = _redact_payload(item)
        return redacted
    return value


def handle_request(message: Mapping[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized":
        return None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        elif method == "tools/list":
            result = {"tools": list(TOOL_DEFINITIONS)}
        elif method == "tools/call":
            params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
            tool_name = params.get("name")
            if not isinstance(tool_name, str):
                raise ToolInputError("tools/call requires params.name")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), Mapping) else {}
            result = _mcp_tool_result(call_tool(tool_name, arguments))
        elif method == "ping":
            result = {}
        else:
            raise ToolInputError(f"Unsupported MCP method: {method}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except Exception as exc:
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": request_id, "result": _mcp_tool_error(exc)}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32000, "message": redact_text(str(exc))},
        }


def _mcp_tool_result(payload: Any) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(_redact_payload(payload), separators=(",", ":"), sort_keys=True),
            }
        ]
    }


def _mcp_tool_error(exc: Exception) -> dict[str, Any]:
    return {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": json.dumps({"ok": False, "error": redact_text(str(exc))}, separators=(",", ":"), sort_keys=True),
            }
        ],
    }


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    first = stream.readline()
    if not first:
        return None
    if first.startswith(b"Content-Length:"):
        length = int(first.split(b":", 1)[1].strip())
        while True:
            line = stream.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        raw = stream.read(length)
        return json.loads(raw.decode("utf-8"))
    return json.loads(first.decode("utf-8"))


def write_message(stream: BinaryIO, message: Mapping[str, Any]) -> None:
    body = json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
    stream.write(body + b"\n")
    stream.flush()


def run_stdio() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        message = read_message(stdin)
        if message is None:
            return 0
        response = handle_request(message)
        if response is not None:
            write_message(stdout, response)


if __name__ == "__main__":
    raise SystemExit(run_stdio())
