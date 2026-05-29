import hashlib
import json
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib import request

from .redaction import redact_text


BUDDY_SCHEMA_VERSION = "2026-05-26"
DEFAULT_QUANDORA_WEB_URL = "https://app.quandora.ai"
BUDDY_DOWNLOAD_URL = "https://app.quandora.ai/download/buddy"
BUDDY_RELEASE_MANIFEST_URL = "https://app.quandora.ai/.well-known/quandora-buddy/releases.json"
REQUIRED_BUDDY_VERSION = "0.1.0"
BUDDY_CONNECT_GUIDANCE = "Open Quandora Buddy, choose Connect Quandora, and complete the official Quandora connection flow."
BUDDY_EVENT_TYPES = {
    "factor.connected",
    "factor.selecting",
    "factor.thinking",
    "factor.writing",
    "factor.validating",
    "factor.casting",
    "factor.submitted",
    "factor.waiting",
    "factor.result",
    "factor.failed",
}
SENSITIVE_KEY_RE = re.compile(
    r"(api[_ -]?key|agent[_ -]?key|token|secret|authorization|password|plugin[_ -]?source|source[_ -]?code)",
    re.IGNORECASE,
)
PATH_KEY_RE = re.compile(r"(workspace|workspace_path|plugin_path|path)$", re.IGNORECASE)
INTERNAL_KEY_RE = re.compile(r"^(job_id|job_ids|workflow_job_id)$", re.IGNORECASE)


def validate_buddy_event_type(event_type: str) -> None:
    if event_type not in BUDDY_EVENT_TYPES:
        allowed = ", ".join(sorted(BUDDY_EVENT_TYPES))
        raise ValueError(f"Unsupported Buddy event type {event_type!r}. Allowed types: {allowed}")


def emit_buddy_event(
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    run: Mapping[str, Any] | None = None,
    source_agent: str = "codex",
    workspace: str | Path | None = None,
    home: str | Path | None = None,
    timeout: float = 0.2,
    opener: Any = None,
) -> dict[str, bool]:
    try:
        validate_buddy_event_type(event_type)
        discovery = _read_buddy_discovery(home)
        if discovery is None:
            return {"ok": True, "emitted": False}
        event = build_buddy_event(
            event_type,
            payload or {},
            run=run,
            source_agent=source_agent,
            workspace=workspace,
        )
        data = json.dumps(event, separators=(",", ":"), sort_keys=True).encode("utf-8")
        req = request.Request(
            f"http://127.0.0.1:{discovery['port']}/events",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", "X-Quandora-Buddy-Token": discovery["token"]},
        )
        active_opener = opener or request.build_opener()
        with active_opener.open(req, timeout=timeout) as response:
            status = getattr(response, "status", getattr(response, "code", 0))
        return {"ok": True, "emitted": 200 <= int(status) < 300}
    except Exception:
        return {"ok": True, "emitted": False}


def get_buddy_credential(
    *,
    home: str | Path | None = None,
    timeout: float = 0.3,
    opener: Any = None,
) -> dict[str, str] | None:
    try:
        discovery = _read_buddy_discovery(home)
        if discovery is None:
            return None
        status = _request_buddy_json(
            "GET",
            discovery,
            "/credential/status",
            timeout=timeout,
            opener=opener,
        )
        if not isinstance(status, Mapping) or not status.get("ok") or not status.get("connected"):
            return None
        token_payload = _request_buddy_json(
            "POST",
            discovery,
            "/credential/token",
            timeout=timeout,
            opener=opener,
        )
        if not isinstance(token_payload, Mapping) or not token_payload.get("ok"):
            return None
        credential = token_payload.get("credential")
        if not isinstance(credential, Mapping):
            return None
        if credential.get("type") != "agent_api_key":
            return None
        value = credential.get("value")
        if not isinstance(value, str) or not value.startswith("vt_"):
            return None
        base_url = token_payload.get("base_url") or status.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            return None
        return {"base_url": base_url.rstrip("/"), "api_key": value}
    except Exception:
        return None


def run_buddy_doctor(
    *,
    min_version: str = REQUIRED_BUDDY_VERSION,
    timeout: float = 5.0,
    which: Any = shutil.which,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    cli_path = which("quandora-buddy")
    guidance = buddy_install_guidance()
    if not cli_path:
        return {
            "ok": False,
            "ready": False,
            "status": "missing_cli",
            "required_version": min_version,
            "guidance": guidance,
        }

    command = [cli_path, "doctor", "--json", "--min-version", min_version]
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return {
            "ok": False,
            "ready": False,
            "status": "unknown_error",
            "required_version": min_version,
            "buddy_cli": cli_path,
            "error": redact_text(str(exc)),
            "guidance": guidance,
        }

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "ready": False,
            "status": "unknown_error",
            "required_version": min_version,
            "buddy_cli": cli_path,
            "exit_code": completed.returncode,
            "output": redact_text(f"{stdout}\n{stderr}".strip()),
            "guidance": guidance,
        }

    status = _map_buddy_doctor_status(payload, completed.returncode)
    ready = status == "ready"
    result: dict[str, Any] = {
        "ok": ready,
        "ready": ready,
        "status": status,
        "required_version": min_version,
        "buddy_cli": cli_path,
        "exit_code": completed.returncode,
        "doctor": _redact_json_values(payload),
        "guidance": guidance,
    }
    if status != "ready" and stderr.strip():
        result["stderr"] = redact_text(stderr.strip())
    return result


def buddy_install_guidance() -> dict[str, str]:
    return {
        "message": "Quandora Buddy is an optional desktop animation companion.",
        "download_url": BUDDY_DOWNLOAD_URL,
        "connect_guidance": "Install Buddy only when you want desktop fishing animation or local event display.",
    }


def build_buddy_event(
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    run: Mapping[str, Any] | None = None,
    source_agent: str = "codex",
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    validate_buddy_event_type(event_type)
    return {
        "schema_version": BUDDY_SCHEMA_VERSION,
        "event_id": f"evt_{uuid.uuid4().hex}",
        "occurred_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": {
            "agent": _safe_short_string(source_agent) or "codex",
            "adapter": "factor-mining-plugin",
            "workspace": _workspace_label(workspace),
        },
        "run": _sanitize_run(run or {}),
        "type": event_type,
        "payload": _sanitize_payload(dict(payload or {})),
    }


def result_payload_from_wait_result(result: Mapping[str, Any]) -> dict[str, Any]:
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else {}
    payload: dict[str, Any] = {
        "status": result.get("status"),
        "terminal_status": result.get("terminal_status"),
        "factor_name": summary.get("factor_name"),
        "metrics": summary.get("metrics") if isinstance(summary.get("metrics"), Mapping) else {},
    }
    fish = summary.get("fish") or result.get("fish")
    if isinstance(fish, Mapping):
        payload["fish"] = dict(fish)
    for key in ("result_url", "web_url"):
        if result.get(key):
            payload[key] = result[key]
        elif summary.get(key):
            payload[key] = summary[key]
    if summary.get("artifact_status"):
        payload["artifact_status"] = summary["artifact_status"]
    return payload


def failure_payload(stage: str, message: str, *, recoverable: bool = True) -> dict[str, Any]:
    return {
        "stage": _safe_short_string(stage) or "workflow",
        "message": redact_text(_safe_short_string(message, max_length=240) or "Workflow stopped before a usable result."),
        "recoverable": bool(recoverable),
    }


def _read_buddy_discovery(home: str | Path | None) -> dict[str, Any] | None:
    root = Path(home).expanduser() if home is not None else Path.home()
    port_file = root / ".quandora-buddy" / "port.json"
    try:
        payload = json.loads(port_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    port = payload.get("port") if isinstance(payload, Mapping) else None
    token = payload.get("token") if isinstance(payload, Mapping) else None
    if isinstance(port, int) and 0 < port < 65536 and isinstance(token, str) and len(token) >= 32:
        return {"port": port, "token": token}
    return None


def _request_buddy_json(
    method: str,
    discovery: Mapping[str, Any],
    path: str,
    *,
    timeout: float,
    opener: Any = None,
) -> Any:
    req = request.Request(
        f"http://127.0.0.1:{discovery['port']}{path}",
        method=method,
        headers={"X-Quandora-Buddy-Token": str(discovery["token"])},
    )
    active_opener = opener or request.build_opener()
    with active_opener.open(req, timeout=timeout) as response:
        raw = response.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _map_buddy_doctor_status(payload: Any, returncode: int) -> str:
    if not isinstance(payload, Mapping):
        return "unknown_error"
    if payload.get("ready") is True or payload.get("status") == "ready":
        return "ready"
    reason = str(payload.get("reason") or "")
    checks = payload.get("checks") if isinstance(payload.get("checks"), Mapping) else {}
    version = checks.get("version") if isinstance(checks.get("version"), Mapping) else {}
    credential = checks.get("credential") if isinstance(checks.get("credential"), Mapping) else {}
    if reason == "version_too_old" or version.get("ok") is False:
        return "outdated"
    if reason in {"runtime_discovery_missing", "runtime_not_running", "discovery_token_rejected"}:
        return "runtime_not_running"
    if reason in {"credential_missing", "not_connected"} or credential.get("connected") is False:
        return "disconnected"
    if returncode != 0:
        return "unknown_error"
    return "unknown_error"


def _redact_json_values(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_json_values(item) for item in value]
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if key_str == "value":
                clean[key_str] = "redacted"
            else:
                clean[key_str] = _redact_json_values(item)
        return clean
    return value


def _sanitize_run(run: Mapping[str, Any]) -> dict[str, str]:
    allowed = ("client_run_id", "session_id", "task_id")
    return {
        key: value
        for key in allowed
        if (value := _safe_short_string(run.get(key), max_length=160)) is not None
    }


def _sanitize_payload(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _safe_short_string(value)
    if isinstance(value, list):
        return [_sanitize_payload(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in list(value.items())[:40]:
            key_str = str(key)
            if SENSITIVE_KEY_RE.search(key_str) or PATH_KEY_RE.search(key_str) or INTERNAL_KEY_RE.search(key_str):
                continue
            clean_item = _sanitize_payload(item, depth=depth + 1)
            if clean_item is not None:
                clean[key_str] = clean_item
        return clean
    return None


def _safe_short_string(value: Any, *, max_length: int = 500) -> str | None:
    if value is None:
        return None
    text = redact_text(str(value))
    if _looks_like_path(text) or _looks_like_factor_source(text):
        return "redacted"
    if len(text) > max_length:
        return f"{text[:max_length]}..."
    return text


def _workspace_label(workspace: str | Path | None) -> str:
    if workspace is None:
        return "redacted"
    text = str(workspace)
    if _looks_like_path(text):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        return f"workspace:{digest}"
    return _safe_short_string(text, max_length=120) or "redacted"


def _looks_like_path(value: str) -> bool:
    return (
        value.startswith("/")
        or bool(re.search(r"/(Users|home|workspace|tmp|var|private)/", value))
        or bool(re.match(r"^[A-Za-z]:\\", value))
        or "/Desktop/" in value
        or "\\Desktop\\" in value
    )


def _looks_like_factor_source(value: str) -> bool:
    lower = value.lower()
    return "\n" in value and (
        "def predict" in lower
        or "class factor" in lower
        or "import pandas" in lower
        or "factor_sections" in lower
    )
