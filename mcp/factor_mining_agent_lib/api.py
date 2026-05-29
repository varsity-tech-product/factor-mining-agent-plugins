import json
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib import parse, request
from urllib.error import HTTPError, URLError

from .metadata import PluginMetadata
from .redaction import redact_text


class ApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        method: str | None = None,
        url: str | None = None,
        body: Any = None,
        api_key: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.method = method
        self.url = url
        self.body = body
        self.api_key = api_key

    def __str__(self) -> str:
        parts = [self.message]
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.method and self.url:
            parts.append(f"request={self.method} {self.url}")
        if self.body not in (None, ""):
            try:
                rendered_body = json.dumps(self.body, separators=(",", ":"), sort_keys=True)
            except TypeError:
                rendered_body = str(self.body)
            parts.append(f"body={rendered_body}")
        return redact_text(" ".join(parts), extra_secrets=[self.api_key] if self.api_key else None)


class AgentStatusError(ApiError):
    pass


AGENT_KEY_GUIDANCE = (
    "Run quandora_connect to authorize this plugin with Quandora Local Agent Connect. "
    "Buddy is optional and only provides the desktop fishing animation companion."
)


def validate_agent_status(status: Mapping[str, Any]) -> Mapping[str, Any]:
    if status.get("ok") is False:
        raise AgentStatusError(AGENT_KEY_GUIDANCE, body=dict(status))
    if "status" in status and status.get("status") != "ok":
        raise AgentStatusError(AGENT_KEY_GUIDANCE, body=dict(status))
    if "agent_key" in status and status.get("agent_key") != "valid":
        raise AgentStatusError(AGENT_KEY_GUIDANCE, body=dict(status))
    if "key_purpose" in status and status.get("key_purpose") != "external_agent":
        raise AgentStatusError(AGENT_KEY_GUIDANCE, body=dict(status))

    backend_main_proof = status.get("status") == "ok" and status.get("agent_key") == "valid"
    richer_external_agent_proof = status.get("key_purpose") == "external_agent"
    if not (backend_main_proof or richer_external_agent_proof):
        raise AgentStatusError(AGENT_KEY_GUIDANCE, body=dict(status))
    return status


class ApiClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        opener: Any = None,
        timeout: float = 30.0,
    ):
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.opener = opener or request.build_opener()
        self.timeout = timeout

    def _url(self, path: str, query: Mapping[str, Any] | None = None) -> str:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            clean_query = {key: value for key, value in query.items() if value is not None}
            if clean_query:
                url = f"{url}?{parse.urlencode(clean_query)}"
        return url

    def _make_request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        data: bytes | None = None,
        query: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
        idempotency_key: str | None = None,
    ) -> request.Request:
        outgoing_headers: dict[str, str] = {}
        if auth:
            outgoing_headers["Authorization"] = f"Bearer {self.api_key}"
        if idempotency_key:
            outgoing_headers["Idempotency-Key"] = idempotency_key
        if headers:
            outgoing_headers.update(headers)

        payload = data
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
            outgoing_headers.setdefault("Content-Type", "application/json")

        req = request.Request(self._url(path, query), data=payload, method=method)
        for key, value in outgoing_headers.items():
            req.add_header(key, value)
            if key == "Idempotency-Key":
                req.headers.pop("Idempotency-key", None)
                req.headers[key] = value
        return req

    def _decode(self, response: Any) -> Any:
        raw = response.read()
        if not raw:
            return None
        text = raw.decode("utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        data: bytes | None = None,
        query: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
        idempotency_key: str | None = None,
    ) -> Any:
        req = self._make_request(
            method,
            path,
            body=body,
            data=data,
            query=query,
            headers=headers,
            auth=auth,
            idempotency_key=idempotency_key,
        )
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                payload = self._decode(response)
                status = getattr(response, "status", getattr(response, "code", None))
        except HTTPError as exc:
            payload = self._decode(exc)
            raise ApiError(
                "Factor Mining API request failed",
                status=exc.code,
                method=method,
                url=req.full_url,
                body=payload,
                api_key=self.api_key,
            ) from exc
        except URLError as exc:
            raise ApiError(
                f"Factor Mining API request could not connect: {exc.reason}",
                method=method,
                url=req.full_url,
                api_key=self.api_key,
            ) from exc

        if status is not None and int(status) >= 400:
            raise ApiError(
                "Factor Mining API request failed",
                status=int(status),
                method=method,
                url=req.full_url,
                body=payload,
                api_key=self.api_key,
            )
        return payload

    def health(self) -> Any:
        return self.request("GET", "/health", auth=False)

    def agent_status(self) -> Mapping[str, Any]:
        try:
            status = self.request("GET", "/agent/status")
        except ApiError as exc:
            if exc.status in (401, 403):
                raise AgentStatusError(
                    AGENT_KEY_GUIDANCE,
                    status=exc.status,
                    method=exc.method,
                    url=exc.url,
                    body=exc.body,
                    api_key=self.api_key,
                ) from exc
            if exc.status == 404:
                raise AgentStatusError(
                    "Factor Mining API environment must include the external-agent status endpoint at /agent/status.",
                    status=exc.status,
                    method=exc.method,
                    url=exc.url,
                    body=exc.body,
                    api_key=self.api_key,
                ) from exc
            raise
        if not isinstance(status, dict):
            raise AgentStatusError("Factor Mining agent status response was not a JSON object")
        return validate_agent_status(status)

    def list_tasks(self, *, limit: int = 20, status: str | None = "open") -> Any:
        return self.request("GET", "/tasks", query={"limit": limit, "status": status})

    def create_session(
        self,
        *,
        idea: str | None = None,
        task_id: str | None = None,
        task_payload: Mapping[str, Any] | None = None,
        client_run_id: str | None = None,
    ) -> Any:
        if bool(idea) == bool(task_id):
            raise ValueError("Provide exactly one of idea or task_id")
        body = {"origin": "custom", "idea": idea} if idea else {"origin": "worker", "task_id": task_id}
        if task_payload is not None:
            body["task_payload"] = dict(task_payload)
        return self.request("POST", "/sessions", body=body, idempotency_key=client_run_id)

    def dedup_context(
        self,
        *,
        session_id: str,
        description: str,
        formula: str,
        limit: int = 8,
    ) -> Any:
        return self.request(
            "POST",
            "/factors/dedup-context",
            body={
                "session_id": session_id,
                "description": description,
                "formula": formula,
                "limit": limit,
            },
        )

    def upload_plugin(
        self,
        *,
        session_id: str,
        plugin_path: str | Path,
        metadata: PluginMetadata,
        client_run_id: str,
        fwd_period: int = 7,
        parent_client_run_id: str | None = None,
        decision_summary: str | None = None,
        expected_effect: Mapping[str, Any] | None = None,
        submitter_label: str | None = None,
        agent_id: str | None = None,
        research_session_id: str | None = None,
        boundary: str | None = None,
    ) -> Any:
        fields: dict[str, str] = {
            "factor_type": metadata.factor_type,
            "factor_name": metadata.factor_name,
            "params": json.dumps(metadata.params, separators=(",", ":"), sort_keys=True),
            "fwd_period": str(fwd_period),
            "client_run_id": client_run_id,
        }
        optional_fields = {
            "parent_client_run_id": parent_client_run_id,
            "decision_summary": decision_summary,
            "submitter_label": submitter_label,
            "agent_id": agent_id,
            "research_session_id": research_session_id,
            "expected_effect": json.dumps(expected_effect, separators=(",", ":"), sort_keys=True)
            if expected_effect is not None
            else None,
        }
        fields.update({key: value for key, value in optional_fields.items() if value})
        data, content_type = encode_multipart_form(
            fields=fields,
            files={"plugin.py": Path(plugin_path)},
            boundary=boundary,
        )
        return self.request(
            "POST",
            f"/sessions/{parse.quote(session_id, safe='')}/plugins/upload",
            data=data,
            headers={"Content-Type": content_type},
            idempotency_key=client_run_id,
        )

    def submit_backtest(
        self,
        session_id: str,
        plugin_id: str,
        *,
        position_mode: str = "both",
        wfo_mode: bool = False,
        params_override: Mapping[str, Any] | None = None,
        client_run_id: str | None = None,
    ) -> Any:
        return self.request(
            "POST",
            f"/sessions/{parse.quote(session_id, safe='')}/plugins/{parse.quote(plugin_id, safe='')}/backtest",
            body={
                "position_mode": position_mode,
                "wfo_mode": wfo_mode,
                "params_override": dict(params_override or {}),
            },
            idempotency_key=client_run_id,
        )

    def workflow(self, session_id: str) -> Any:
        return self.request("GET", f"/workflows/{parse.quote(session_id, safe='')}")

    def job(self, job_id: str) -> Any:
        return self.request("GET", f"/jobs/{parse.quote(job_id, safe='')}")

    def artifact(self, job_id: str, name: str = "default_factor_card.json") -> Any:
        return self.request(
            "GET",
            f"/jobs/{parse.quote(job_id, safe='')}/files/{parse.quote(name, safe='')}",
        )


def encode_multipart_form(
    *,
    fields: Mapping[str, str],
    files: Mapping[str, Path],
    boundary: str | None = None,
) -> tuple[bytes, str]:
    boundary = boundary or f"----factor-mining-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    for name, path in files.items():
        content = path.read_bytes()
        filename = path.name
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("utf-8"),
                b"Content-Type: text/x-python\r\n\r\n",
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
