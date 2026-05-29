---
name: factor-mining
description: Use when Codex should create or submit a Quandora Factor Mining plugin.py, run a user-scoped backtest, wait for workflow and job results, fetch artifacts, summarize outcomes, or resume a local-agent Factor Mining run through the bundled Quandora MCP tools.
---

# Quandora Factor Mining

Use this skill for Factor Mining work in the Quandora local-agent product.
Codex writes or locates one local `plugin.py`. Quandora validates, stores,
backtests, and returns workflow, job, artifact, and factor result data.

The plugin owns local-agent authorization through Quandora Local Agent Connect.
Use `quandora_connect` when no plugin-local credential is connected. Raw
credentials must never be pasted into chat, terminal logs, UI text, or docs.
Current public `main` is temporarily configured for localhost Local Agent
Connect testing, so `quandora_connect` opens
`http://127.0.0.1:3037/local-agent/connect` and Factor Mining API calls use
`http://127.0.0.1:18080`. Production releases should switch these defaults
back to `https://app.quandora.ai/local-agent/connect` and
`https://d25q1jf66e8y4g.cloudfront.net`.

Buddy is optional. It provides desktop fishing animation and a sanitized local
event receiver, but it is not required for authorization or backtesting. Plugin
installation never downloads or installs Buddy in the background. Optional
Buddy download:

```text
https://app.quandora.ai/download/buddy
```

Use the bundled Quandora MCP tools for the product workflow. If the MCP tools
are not available, stop and explain that the Quandora plugin tools are not
loaded. Do not fall back to raw HTTP calls or manual credential entry.

## MCP Workflow

Use these tools for the formal workflow:

- `quandora_status`
- `quandora_connect`
- `quandora_connect_pending`
- `quandora_connect_cancel`
- `quandora_connect_wait`
- `quandora_connect_status`
- `quandora_disconnect`
- `quandora_list_public_tasks`
- `quandora_create_task_session`
- `quandora_create_custom_session`
- `quandora_parse_plugin_metadata`
- `quandora_request_dedup_context`
- `quandora_upload_backtest_wait`
- `quandora_resume_run`
- `quandora_emit_buddy_event`

Start with `quandora_connect_status` or `quandora_status`. Continue only when
the plugin-local delegated credential validates. If no credential is connected,
run `quandora_connect` and wait for the user to complete web authorization. Do
not call upload, backtest, polling, artifact, or session tools before connect
is complete.

If `quandora_connect` returns `pending=true`, open the returned
`authorization_url` for the user if possible, then call `quandora_connect_wait`
with the returned `connect_handle`. The authorization URL is intended for
browser navigation, but do not display PKCE verifiers, bearer headers, raw
credentials, or any value outside the returned URL. The pending PKCE verifier
and loopback callback server live only in the current MCP process; call
`quandora_connect_wait` in the same agent session. Use
`quandora_connect_pending` to inspect safe pending records and
`quandora_connect_cancel` if the connect session is no longer needed.

Determine whether the user is starting from a public task or a custom idea. For
a public task flow, use `quandora_list_public_tasks`, show concise choices, and
ask the user to pick one unless they explicitly ask Codex to choose. Create the
session with `quandora_create_task_session`.

For a custom idea, create a direct `task_payload` and call
`quandora_create_custom_session`. The payload must include `task_id`, `title`,
`category`, `description`, non-empty `allowed_data`, and `fwd_period`.
`allowed_data` must include every input column the generated `plugin.py` needs,
such as `close`, `volume`, `funding_rate_close`, or `open_interest_close`.
Include useful hints, economic mechanisms, regime considerations, risk sources,
and target behavior when they are available.

When you have a draft description and formula, call
`quandora_request_dedup_context` and use the returned similar-factor guidance to
avoid near-duplicates. This context informs local revision only; do not imply
Buddy can observe local drafting or editing.

Write or locate one `plugin.py`, then call `quandora_parse_plugin_metadata`.
The parser is static and must not import or execute generated code. When the
metadata is valid and the user is ready to submit, call
`quandora_upload_backtest_wait`. Use `fwd_period=7` when neither the task nor
user specifies a horizon.

Summarize the JSON returned by `quandora_upload_backtest_wait` or
`quandora_resume_run`. Always inspect `ok`, `status`, `terminal_status`,
`failures`, sanitized job statuses, artifact availability, and factor-card
metrics. If `ok` is false or failures are present, report the failed or
cancelled terminal status clearly instead of presenting the run as successful.

Buddy event emission is best-effort. Before upload or backtest submission, do
not emit deterministic business-progress states for brainstorming, drafting,
editing, or plugin-defined progress. After upload or submission, Buddy events
may reflect only backend-visible workflow, job, artifact, result, and
fish-grade data. Never include credentials, generated source, full workspace
paths, presigned URLs, or backend internals in event payloads.
Never show backend job IDs, presigned URLs, local absolute paths, raw
credentials, bearer tokens, or `plugin.py` source in user-facing summaries.

## plugin.py Contract

Use this minimum shape when the user has not supplied an existing plugin. The
metadata values must be static top-level literals so Quandora can parse them
without executing source code.

```python
from typing import Any, Dict

import pandas as pd

FACTOR_TYPE = "snake_case_unique_factor_type"
FACTOR_NAME = "human_readable_factor_name"
FACTOR_DEFAULT_PARAMS = {"window": 7}

FACTOR_SECTIONS = {
    "__FACTOR_DESCRIPTION__": "Trailing close-to-close momentum.",
    "__FACTOR_FORMULA__": "close / close[window bars ago] - 1",
    "__FACTOR_TYPE__": FACTOR_TYPE,
    "__FACTOR_PARAM_FIELDS__": "        private int _window;\n",
    "__FACTOR_INIT__": '            _window = GetIntParameter("window", 7);\n',
    "__FACTOR_LOG__": '            Log($"[INIT] window={_window}");\n',
    "__PRICE_WINDOW_EXPR__": "_window + 1",
    "__EXTRA_BUF_FIELDS__": "",
    "__EXTRA_BUF_ENQUEUE__": "",
    "__EXTRA_BUF_DEQUEUE__": "",
    "__EXTRA_BUF_TOARRAY__": "",
    "__FACTOR_COMPUTE_BODY__": """
            var n = prices.Length;
            if (n < _window + 1) return false;
            var past = prices[n - _window - 1];
            if (past == 0) return false;
            rawSignal = prices[n - 1] / past - 1.0;
            return true;
""",
}


def build_signal(close: pd.DataFrame, params: Dict[str, Any], **data: Any) -> pd.DataFrame:
    window = int(params.get("window", FACTOR_DEFAULT_PARAMS["window"]))
    signal = close.pct_change(window)
    return signal.reindex_like(close)
```

Keep Python `build_signal` and `FACTOR_SECTIONS` compute logic aligned. Return
a `pd.DataFrame` aligned with `close`, use only current and historical data,
and keep all data columns within the session `allowed_data` contract.

## Security

- Use only the bundled Quandora MCP tools for formal product actions.
- Never ask for OpenAI API keys, Codex auth files, BYOK secrets, frontend user
  credentials, or raw Factor Mining credentials.
- Never print, persist in logs, or summarize full credential values.
- Do not call hosted generation endpoints; local-agent mode generates code
  inside Codex.
- Do not call frontend-user credential management, BYOK, Codex profile, task
  publishing, or generic URL/API surfaces.
- Do not import, exec, eval, or otherwise execute generated `plugin.py`.
- Do not print generated `plugin.py` source in summaries.
- Treat downstream IDs, presigned URLs, and service metadata as internal.
- Artifact `404` and `410` responses mean unavailable. Authentication,
  authorization, network, malformed response, and server errors must fail
  clearly.
