from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib import parse, request
from urllib.error import HTTPError, URLError

from .api import ApiClient
from .config import DEFAULT_BASE_URL, agent_home, ensure_agent_home, _write_private_json
from .redaction import redact_obj, redact_text


DEFAULT_WEB_CONNECT_URL = "https://app.quandora.ai/local-agent/connect"
DEFAULT_TIMEOUT_SECONDS = 300
PENDING_CONNECT_TTL_SECONDS = 600
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PATH = "/callback"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
LOCAL_AGENT_CREDENTIAL_PREFIX = "vt_agent_"


class ConnectCallbackError(RuntimeError):
    pass


class ConnectExchangeError(RuntimeError):
    pass


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def redact_credential(value: str | None) -> str:
    if not value:
        return ""
    if value.startswith("vt_agent_") and len(value) > 13:
        return f"vt_agent_...{value[-4:]}"
    if value.startswith("vt_") and len(value) > 7:
        return f"vt_...{value[-4:]}"
    return f"***{value[-4:]}" if len(value) > 4 else "***"


def credential_path(home: str | Path | None = None) -> Path:
    return agent_home(home) / "local-agent-credential.json"


def credential_exists(home: str | Path | None = None) -> bool:
    return credential_path(home).exists()


def is_local_agent_connect_credential(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(LOCAL_AGENT_CREDENTIAL_PREFIX) and len(value) > len(LOCAL_AGENT_CREDENTIAL_PREFIX)


def load_connected_credential(home: str | Path | None = None) -> dict[str, Any]:
    path = credential_path(home)
    if not path.exists():
        raise ConnectExchangeError("No Quandora local-agent credential is connected. Run quandora_connect. Buddy is optional.")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    credential = payload.get("credential") if isinstance(payload, Mapping) else None
    value = credential.get("value") if isinstance(credential, Mapping) else None
    if not is_local_agent_connect_credential(value):
        raise ConnectExchangeError("Stored Quandora local-agent credential is invalid. Run quandora_connect again.")
    return dict(payload)


def save_connected_credential(
    exchange_response: Mapping[str, Any],
    *,
    home: str | Path | None = None,
    orchestrator_base_url: str = DEFAULT_BASE_URL,
    credential_backend_base_url: str,
) -> dict[str, Any]:
    credential = exchange_response.get("credential")
    if not isinstance(credential, Mapping):
        raise ConnectExchangeError("connect exchange response omitted credential")
    raw_value = credential.get("value")
    if not is_local_agent_connect_credential(raw_value):
        raise ConnectExchangeError("connect exchange response returned an invalid credential")
    credential_type = str(credential.get("type") or "agent_api_key")
    record = {
        "credential": {
            "type": credential_type,
            "value": raw_value,
            "redacted": redact_credential(raw_value),
        },
        "base_url": str(exchange_response.get("base_url") or orchestrator_base_url).rstrip("/"),
        "credential_backend_base_url": credential_backend_base_url.rstrip("/"),
        "identity": redact_obj(exchange_response.get("identity") or {}),
        "capabilities": list(exchange_response.get("capabilities") or []),
        "connected_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    root = ensure_agent_home(home)
    path = root / credential_path(home).name
    _write_private_json(path, record)
    return safe_credential_record(record)


def safe_credential_record(record: Mapping[str, Any]) -> dict[str, Any]:
    credential = record.get("credential") if isinstance(record.get("credential"), Mapping) else {}
    raw_value = credential.get("value") if isinstance(credential, Mapping) else None
    return {
        "credential": {
            "type": credential.get("type") if isinstance(credential, Mapping) else None,
            "redacted": credential.get("redacted") or redact_credential(raw_value if isinstance(raw_value, str) else None),
        },
        "base_url": record.get("base_url"),
        "credential_backend_base_url": record.get("credential_backend_base_url"),
        "identity": redact_obj(record.get("identity") or {}),
        "capabilities": list(record.get("capabilities") or []),
        "connected_at": record.get("connected_at"),
    }


def clear_connected_credential(home: str | Path | None = None) -> bool:
    path = credential_path(home)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


class ConnectCallbackGuard:
    def __init__(self, *, expected_state: str):
        if not expected_state:
            raise ValueError("expected_state is required")
        self.expected_state = expected_state
        self._handled = False

    def accept(self, callback_url: str) -> dict[str, str]:
        if self._handled:
            raise ConnectCallbackError("connect_callback_replayed")
        parsed = parse.urlsplit(str(callback_url))
        if parsed.scheme != "http" or parsed.path != CALLBACK_PATH or (parsed.hostname or "").lower() not in LOOPBACK_HOSTS:
            raise ConnectCallbackError("connect_callback_malformed")
        query = parse.parse_qs(parsed.query)
        state = _single_query_value(query, "state")
        if state != self.expected_state:
            raise ConnectCallbackError("connect_state_mismatch")
        error = _single_query_value(query, "error")
        if error:
            raise ConnectCallbackError(redact_text(error))
        code = _single_query_value(query, "code")
        if not code:
            raise ConnectCallbackError("connect_code_missing")
        self._handled = True
        return {"code": code, "state": state}


@dataclass
class LoopbackCallbackServer:
    server: ThreadingHTTPServer
    thread: threading.Thread
    callback_queue: "queue.Queue[dict[str, str]]"
    error_queue: "queue.Queue[Exception]"

    @property
    def redirect_uri(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}{CALLBACK_PATH}"

    def wait(self, timeout_seconds: float) -> dict[str, str]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                return self.callback_queue.get(timeout=0.05)
            except queue.Empty:
                pass
            try:
                error = self.error_queue.get_nowait()
            except queue.Empty:
                error = None
            if error is not None:
                raise error
            if time.monotonic() >= deadline:
                raise ConnectCallbackError("connect_timeout")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@dataclass
class PendingConnect:
    handle: str
    callback: LoopbackCallbackServer
    authorization_url: str
    state: str
    code_verifier: str
    client: dict[str, Any]
    credential_backend_base_url: str
    orchestrator_base_url: str
    home: str | Path | None
    created_at: float


_PENDING_CONNECTS: dict[str, PendingConnect] = {}
_PENDING_CONNECTS_LOCK = threading.Lock()


def start_loopback_callback_server(*, expected_state: str) -> LoopbackCallbackServer:
    callback_queue: "queue.Queue[dict[str, str]]" = queue.Queue(maxsize=1)
    error_queue: "queue.Queue[Exception]" = queue.Queue(maxsize=1)
    guard = ConnectCallbackGuard(expected_state=expected_state)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def _send(self, status: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            callback_url = f"http://{CALLBACK_HOST}:{self.server.server_address[1]}{self.path}"
            try:
                result = guard.accept(callback_url)
            except ConnectCallbackError as exc:
                self._send(400, f"Quandora Local Agent Connect failed: {exc}")
                return
            try:
                callback_queue.put_nowait(result)
            except queue.Full:
                self._send(409, "Quandora Local Agent Connect callback was already handled.")
                return
            self._send(200, "Quandora Local Agent Connect complete. You can close this tab.")

    server = ThreadingHTTPServer((CALLBACK_HOST, 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return LoopbackCallbackServer(server=server, thread=thread, callback_queue=callback_queue, error_queue=error_queue)


def build_authorization_url(
    *,
    web_connect_url: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    client: Mapping[str, Any],
    label: str | None = None,
    replace_existing: bool = True,
) -> str:
    parsed = parse.urlsplit(web_connect_url)
    query = parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(
        [
            ("redirect_uri", redirect_uri),
            ("state", state),
            ("code_challenge", code_challenge),
            ("code_challenge_method", "S256"),
            ("client", json.dumps(dict(client), separators=(",", ":"), sort_keys=True)),
            ("replace_existing", "true" if replace_existing else "false"),
        ]
    )
    if label:
        query.append(("label", label))
    return parse.urlunsplit(parsed._replace(query=parse.urlencode(query)))


def exchange_connect_code(
    *,
    credential_backend_base_url: str,
    code: str,
    state: str,
    code_verifier: str,
    client: Mapping[str, Any],
    opener: Any = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    body = json.dumps(
        {
            "code": code,
            "state": state,
            "code_verifier": code_verifier,
            "client": dict(client),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    req = request.Request(
        f"{credential_backend_base_url.rstrip('/')}/agent/connect/exchange",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    active_opener = opener or request.build_opener()
    try:
        with active_opener.open(req, timeout=timeout) as response:
            payload = _decode_response(response)
            status = int(getattr(response, "status", getattr(response, "code", 200)) or 200)
    except HTTPError as exc:
        payload = _decode_response(exc)
        raise ConnectExchangeError(_safe_exchange_error(payload, status=exc.code)) from exc
    except URLError as exc:
        raise ConnectExchangeError(f"network failure: {redact_text(_redact_paths(str(exc.reason)))}") from exc
    if not isinstance(payload, dict):
        raise ConnectExchangeError("connect exchange returned a non-JSON response")
    if status >= 400:
        raise ConnectExchangeError(_safe_exchange_error(payload, status=status))
    return payload


def disconnect_local_agent(
    *,
    home: str | Path | None = None,
    opener: Any = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    try:
        record = load_connected_credential(home)
    except ConnectExchangeError:
        return {"ok": True, "connected": False, "removed": False, "remote_revoked": False}

    credential = record["credential"]["value"]
    backend_base_url = str(record.get("credential_backend_base_url") or record.get("connect_base_url") or "").rstrip("/")
    remote_revoked = False
    warning = None
    if backend_base_url:
        req = request.Request(
            f"{backend_base_url}/agent/connect/revoke",
            data=b"{}",
            method="POST",
            headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"},
        )
        active_opener = opener or request.build_opener()
        try:
            with active_opener.open(req, timeout=timeout):
                remote_revoked = True
        except Exception as exc:
            warning = redact_text(_redact_paths(str(exc)), extra_secrets=[credential])
    else:
        warning = "Stored credential did not include a credential backend base URL; remote revoke was skipped."

    removed = clear_connected_credential(home)
    result = {"ok": True, "connected": False, "removed": removed, "remote_revoked": remote_revoked}
    if warning:
        result["warning"] = warning
    return result


def pending_connect_status() -> list[dict[str, Any]]:
    now = time.time()
    _cleanup_expired_pending_connects(now=now)
    with _PENDING_CONNECTS_LOCK:
        pending = list(_PENDING_CONNECTS.values())
    return [_safe_pending_connect_record(item, now=now) for item in pending]


def cancel_pending_connect(connect_handle: str) -> bool:
    _cleanup_expired_pending_connects()
    with _PENDING_CONNECTS_LOCK:
        pending = _PENDING_CONNECTS.pop(connect_handle, None)
    if pending is None:
        return False
    pending.callback.close()
    return True


def cancel_pending_connect_request(connect_handle: str | None = None) -> dict[str, Any]:
    now = time.time()
    expired_handles = _cleanup_expired_pending_connects(now=now)
    if connect_handle is not None and connect_handle in expired_handles:
        return {
            "ok": False,
            "cancelled": False,
            "connect_handle": connect_handle,
            "error": "Pending Quandora Local Agent Connect request expired. Run quandora_connect again.",
        }

    with _PENDING_CONNECTS_LOCK:
        pending_items = list(_PENDING_CONNECTS.values())
        if connect_handle is None:
            if not pending_items:
                return {"ok": True, "cancelled": False}
            if len(pending_items) > 1:
                return {
                    "ok": False,
                    "cancelled": False,
                    "error": "connect_handle is required when multiple pending connects exist.",
                    "pending": [_safe_pending_connect_summary(item, now=now) for item in pending_items],
                }
            connect_handle = pending_items[0].handle
        pending = _PENDING_CONNECTS.pop(connect_handle, None)

    if pending is None:
        return {
            "ok": False,
            "cancelled": False,
            "connect_handle": connect_handle,
            "error": "No pending Quandora Local Agent Connect request found for that handle.",
        }
    pending.callback.close()
    return {"ok": True, "cancelled": True, "connect_handle": connect_handle}


def clear_pending_connects() -> None:
    with _PENDING_CONNECTS_LOCK:
        pending = list(_PENDING_CONNECTS.values())
        _PENDING_CONNECTS.clear()
    for item in pending:
        item.callback.close()


def wait_for_pending_connect(
    connect_handle: str | None = None,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    home: str | Path | None = None,
    opener: Any = None,
) -> dict[str, Any]:
    pending = _take_pending_connect(connect_handle)
    active_home = home if home is not None else pending.home
    try:
        return _wait_for_connect_completion(
            pending,
            timeout_seconds=timeout_seconds,
            home=active_home,
            opener=opener,
        )
    finally:
        pending.callback.close()


def connect_local_agent(
    *,
    web_connect_url: str = DEFAULT_WEB_CONNECT_URL,
    credential_backend_base_url: str | None = None,
    orchestrator_base_url: str = DEFAULT_BASE_URL,
    label: str | None = None,
    replace_existing: bool = True,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    open_browser: bool = True,
    home: str | Path | None = None,
    opener: Any = None,
    browser_open: Callable[[str], Any] | None = None,
    client: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    state = generate_state()
    verifier, challenge = generate_pkce_pair()
    callback = start_loopback_callback_server(expected_state=state)
    client_metadata = dict(client or {"name": "quandora-factor-mining-plugin", "adapter": "codex"})
    backend_base_url = (credential_backend_base_url or _origin_from_url(web_connect_url)).rstrip("/")
    pending_registered = False
    try:
        authorization_url = build_authorization_url(
            web_connect_url=web_connect_url,
            redirect_uri=callback.redirect_uri,
            state=state,
            code_challenge=challenge,
            client=client_metadata,
            label=label,
            replace_existing=replace_existing,
        )
        pending = PendingConnect(
            handle=f"connect_{secrets.token_urlsafe(18)}",
            callback=callback,
            authorization_url=authorization_url,
            state=state,
            code_verifier=verifier,
            client=client_metadata,
            credential_backend_base_url=backend_base_url,
            orchestrator_base_url=orchestrator_base_url,
            home=home,
            created_at=time.time(),
        )

        if not open_browser:
            pending_registered = True
            return _register_pending_connect(pending, reason="browser_not_opened")

        try:
            open_result = (browser_open or webbrowser.open)(authorization_url)
        except Exception as exc:
            pending_registered = True
            return _register_pending_connect(
                pending,
                reason="browser_open_failed",
                warning=redact_text(_redact_paths(str(exc))),
            )
        if open_result is False:
            pending_registered = True
            return _register_pending_connect(pending, reason="browser_open_failed")

        return _wait_for_connect_completion(
            pending,
            timeout_seconds=timeout_seconds,
            home=home,
            opener=opener,
        )
    except Exception:
        if not credential_exists(home):
            clear_connected_credential(home)
        raise
    finally:
        if not pending_registered:
            callback.close()


def _register_pending_connect(
    pending: PendingConnect,
    *,
    reason: str,
    warning: str | None = None,
) -> dict[str, Any]:
    _cleanup_expired_pending_connects()
    with _PENDING_CONNECTS_LOCK:
        _PENDING_CONNECTS[pending.handle] = pending
    result: dict[str, Any] = {
        "ok": True,
        "connected": False,
        "pending": True,
        "reason": reason,
        "connect_handle": pending.handle,
        "authorization_url": pending.authorization_url,
        "next_step": _pending_next_step(pending.handle),
    }
    if warning:
        result["warning"] = warning
    return result


def _pending_next_step(connect_handle: str) -> str:
    return (
        "Open the authorization_url in a browser, approve Quandora Local Agent Connect, "
        f"then call quandora_connect_wait with connect_handle={connect_handle!r}."
    )


def _take_pending_connect(connect_handle: str | None) -> PendingConnect:
    expired_handles = _cleanup_expired_pending_connects()
    if connect_handle is not None and connect_handle in expired_handles:
        raise ConnectExchangeError("Pending Quandora Local Agent Connect request expired. Run quandora_connect again.")
    with _PENDING_CONNECTS_LOCK:
        if connect_handle is None:
            if len(_PENDING_CONNECTS) != 1:
                raise ConnectExchangeError("connect_handle is required when there is not exactly one pending connect.")
            connect_handle = next(iter(_PENDING_CONNECTS))
        pending = _PENDING_CONNECTS.pop(connect_handle, None)
    if pending is None:
        raise ConnectExchangeError("No pending Quandora Local Agent Connect request found for that handle.")
    return pending


def _cleanup_expired_pending_connects(*, now: float | None = None) -> set[str]:
    current_time = time.time() if now is None else now
    expired: list[PendingConnect] = []
    with _PENDING_CONNECTS_LOCK:
        for handle, pending in list(_PENDING_CONNECTS.items()):
            if _pending_age_seconds(pending, current_time) >= PENDING_CONNECT_TTL_SECONDS:
                expired.append(_PENDING_CONNECTS.pop(handle))
    for pending in expired:
        pending.callback.close()
    return {pending.handle for pending in expired}


def _safe_pending_connect_record(pending: PendingConnect, *, now: float | None = None) -> dict[str, Any]:
    current_time = time.time() if now is None else now
    summary = _safe_pending_connect_summary(pending, now=current_time)
    return {
        **summary,
        "authorization_url": pending.authorization_url,
        "expires_at": _format_timestamp(pending.created_at + PENDING_CONNECT_TTL_SECONDS),
        "next_step": _pending_next_step(pending.handle),
    }


def _safe_pending_connect_summary(pending: PendingConnect, *, now: float | None = None) -> dict[str, Any]:
    current_time = time.time() if now is None else now
    age_seconds = _pending_age_seconds(pending, current_time)
    return {
        "connect_handle": pending.handle,
        "created_at": _format_timestamp(pending.created_at),
        "age_seconds": age_seconds,
        "expires_in_seconds": max(0, int(PENDING_CONNECT_TTL_SECONDS - age_seconds)),
    }


def _pending_age_seconds(pending: PendingConnect, now: float) -> int:
    return max(0, int(now - pending.created_at))


def _format_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _wait_for_connect_completion(
    pending: PendingConnect,
    *,
    timeout_seconds: float,
    home: str | Path | None,
    opener: Any,
) -> dict[str, Any]:
    try:
        callback_result = pending.callback.wait(max(1.0, float(timeout_seconds)))
        exchange = exchange_connect_code(
            credential_backend_base_url=pending.credential_backend_base_url,
            code=callback_result["code"],
            state=callback_result["state"],
            code_verifier=pending.code_verifier,
            client=pending.client,
            opener=opener,
        )
        record = save_connected_credential(
            exchange,
            home=home,
            orchestrator_base_url=pending.orchestrator_base_url,
            credential_backend_base_url=pending.credential_backend_base_url,
        )
        credential = load_connected_credential(home)["credential"]["value"]
        api_client = ApiClient(pending.orchestrator_base_url, credential, opener=opener)
        agent_status = api_client.agent_status()
        return {
            "ok": True,
            "connected": True,
            "redacted": record["credential"]["redacted"],
            "capabilities": record.get("capabilities", []),
            "identity": record.get("identity", {}),
            "agent_status": redact_obj(agent_status),
        }
    except Exception:
        if not credential_exists(home):
            clear_connected_credential(home)
        raise


def _decode_response(response: Any) -> Any:
    raw = response.read()
    if not raw:
        return {}
    text = raw.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _safe_exchange_error(payload: Any, *, status: int) -> str:
    code = None
    message = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            code = error.get("code")
            message = error.get("message")
        else:
            message = payload.get("detail") or payload.get("message")
    rendered = " ".join(str(part) for part in (code, message) if part)
    if not rendered:
        rendered = "connect exchange failed"
    return f"{redact_text(_redact_paths(rendered))} status={status}"


def _redact_paths(value: str) -> str:
    decoded = parse.unquote(value).replace(str(Path.home()), "[home]")
    return re.sub(r"/Users/[^/\s?#]+", "[home]", decoded)


def _origin_from_url(value: str) -> str:
    parsed = parse.urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        raise ConnectExchangeError("web_connect_url must be an absolute URL")
    return parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _single_query_value(query: Mapping[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    if not values:
        return None
    return values[0]
