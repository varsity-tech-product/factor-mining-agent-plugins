# Quandora Factor Mining

Quandora Factor Mining is a Codex plugin for turning public factor tasks or
custom factor ideas into local `plugin.py` factors, then submitting them to
Quandora for validation, upload, backtesting, artifact retrieval, and result
summaries.

The plugin owns local-agent authorization through Quandora Local Agent Connect.
Codex opens the Quandora authorization page, receives the local callback,
exchanges it for a delegated local-agent credential, stores that credential
locally, and uses it through the bundled Quandora MCP tools. Users do not need
to paste full credential values into chat.

Quandora Buddy is optional. It provides desktop fishing animation and sanitized
local event display, but it is not required for plugin authorization or
backtesting. Plugin installation never downloads, installs, starts, or updates
Buddy in the background.

```text
https://app.quandora.ai/download/buddy
```

## Codex CLI Install

Install from the public marketplace source:

```bash
codex plugin marketplace add varsity-tech-product/factor-mining-agent-plugins --ref main
codex plugin add factor-mining@factor-mining-marketplace
```

Or run the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/varsity-tech-product/factor-mining-agent-plugins/main/install-codex.sh | bash
```

## Codex Desktop Install

In Codex Desktop, add this marketplace:

```text
Marketplace source: varsity-tech-product/factor-mining-agent-plugins
Marketplace ref: main
Plugin: factor-mining@factor-mining-marketplace
```

You can also run:

```bash
./install-codex-desktop.sh
```

## First Prompts

```text
Show me the Factor Mining public task list.
Use Factor Mining with my custom factor idea.
Resume my Factor Mining run and summarize results.
```

If no local-agent credential is connected, Codex will use `quandora_connect`
to open Quandora Local Agent Connect. Complete authorization in the browser,
then let Codex continue the workflow.

## Product Workflow

Codex uses the bundled `quandora-factor-mining` MCP server for formal Factor
Mining actions:

- connect, pending-connect wait/cancel/status, and disconnect
- public task listing and task-backed session creation
- custom idea session creation with explicit task payload
- static `plugin.py` metadata parsing without executing generated code
- deduplication context requests
- upload, backtest, workflow/job polling, artifact retrieval, and summaries
- optional sanitized Buddy events

The plugin does not expose generic API-call, URL-fetch, or raw-credential
tools. Generated `plugin.py` source stays local until the user asks Codex to
submit it through the bundled Factor Mining workflow.

## Security And Privacy

- Use only plugin-owned Local Agent Connect credentials through the bundled
  Quandora MCP tools.
- Never paste full Factor Mining credentials into chat.
- Never print, persist in logs, or summarize full credential values.
- Do not import, exec, eval, or otherwise execute generated `plugin.py`.
- Do not print generated `plugin.py` source in final summaries.
- Treat downstream job IDs, presigned URLs, and service metadata as internal.
- Buddy events are best-effort and must never include credentials, generated
  source, full workspace paths, presigned URLs, or backend internals.

## Troubleshooting

If Codex says the Quandora MCP tools are unavailable, upgrade and reinstall the
plugin, then fully restart Codex:

```bash
codex plugin marketplace upgrade factor-mining-marketplace
codex plugin remove factor-mining@factor-mining-marketplace
codex plugin add factor-mining@factor-mining-marketplace
```

If authorization is not connected, ask Codex to run Quandora connect again. If
the browser cannot be opened automatically, Codex will show a safe
authorization URL and wait for the connect callback in the same session.
