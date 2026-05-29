#!/usr/bin/env bash
set -euo pipefail

MARKETPLACE_SOURCE="${FACTOR_MINING_PLUGIN_SOURCE:-varsity-tech-product/factor-mining-agent-plugins}"
MARKETPLACE_REF="${FACTOR_MINING_PLUGIN_REF:-main}"
MARKETPLACE_NAME="${FACTOR_MINING_PLUGIN_MARKETPLACE:-factor-mining-marketplace}"
PLUGIN_NAME="${FACTOR_MINING_PLUGIN_NAME:-factor-mining}"
START_MODE="${FACTOR_MINING_START_MODE:-cli}"
INSTALL_ONLY="0"
WORKSPACE_PATH="${FACTOR_MINING_WORKSPACE:-.}"
CODEX_PROMPT="${FACTOR_MINING_CODEX_PROMPT:-Use Quandora Factor Mining. First use the bundled Quandora MCP tools to run quandora_connect_status. If no local-agent credential is connected, run quandora_connect and let me complete web authorization. Buddy is optional for desktop fishing animation. Show me the Factor Mining public task list. Do not create a session until I choose a public task or provide a custom idea. Then write a valid plugin.py locally, upload it, wait for the backtest, fetch the default factor card if available, and summarize the result.}"
BUDDY_DOWNLOAD_URL="https://app.quandora.ai/download/buddy"

if [[ "${FACTOR_MINING_START_CODEX:-1}" == "0" ]]; then
  START_MODE="none"
fi

usage() {
  cat <<'USAGE'
Usage: install-codex.sh [options]

Options:
  --desktop             Install, print connect next steps, then open Codex Desktop.
  --no-start            Install and print connect next steps without starting Codex.
  --install-only        Install the Codex plugin and print connect next steps.
  -h, --help            Show this help.

Default flow:
  codex plugin marketplace add varsity-tech-product/factor-mining-agent-plugins --ref main
  codex plugin add factor-mining@factor-mining-marketplace
  codex "Show me the Factor Mining public task list."

Plugin installation never downloads or installs Buddy in the background. Buddy
is an optional desktop animation companion installed through an explicit user
action.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --desktop)
      START_MODE="desktop"
      ;;
    --no-start)
      START_MODE="none"
      ;;
    --install-only)
      START_MODE="none"
      INSTALL_ONLY="1"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if ! command -v codex >/dev/null 2>&1; then
  echo "Codex CLI is required. Install or update Codex, then run this script again." >&2
  exit 1
fi

marketplace_configured() {
  codex plugin marketplace list 2>/dev/null | awk 'NR > 1 { print $1 }' | grep -Fxq "${MARKETPLACE_NAME}"
}

plugin_installed() {
  codex plugin list --marketplace "${MARKETPLACE_NAME}" 2>/dev/null \
    | grep -E "^${PLUGIN_NAME}@${MARKETPLACE_NAME}[[:space:]]+installed, enabled" >/dev/null
}

plugin_root() {
  codex plugin list --marketplace "${MARKETPLACE_NAME}" 2>/dev/null \
    | awk -v plugin="${PLUGIN_NAME}@${MARKETPLACE_NAME}" '$1 == plugin { print $NF; exit }'
}

print_connect_next_steps() {
  echo "Codex plugin is installed."
  echo "Authorize the local agent with the bundled quandora_connect MCP tool."
  echo "Buddy is optional and provides desktop fishing animation."
  echo "Plugin installation never downloads or installs Buddy in the background."
  echo "Download Buddy for animation: ${BUDDY_DOWNLOAD_URL}"
  echo "Next steps:"
  echo "  1. Start Codex with:"
  printf '     codex %q\n' "${CODEX_PROMPT}"
  echo "  2. Let the plugin open Quandora Local Agent Connect when prompted."
}

echo "Configuring Codex marketplace: ${MARKETPLACE_NAME}"
if marketplace_configured; then
  echo "Marketplace already configured; refreshing if it is Git-backed."
  codex plugin marketplace upgrade "${MARKETPLACE_NAME}" >/dev/null 2>&1 || true
else
  if [[ -d "${MARKETPLACE_SOURCE}" ]]; then
    codex plugin marketplace add "${MARKETPLACE_SOURCE}"
  else
    codex plugin marketplace add "${MARKETPLACE_SOURCE}" --ref "${MARKETPLACE_REF}"
  fi
fi

echo "Installing Codex plugin: ${PLUGIN_NAME}@${MARKETPLACE_NAME}"
if plugin_installed; then
  echo "Plugin already installed."
else
  codex plugin add "${PLUGIN_NAME}@${MARKETPLACE_NAME}"
fi

PLUGIN_ROOT="$(plugin_root)"
if [[ -z "${PLUGIN_ROOT}" || ! -d "${PLUGIN_ROOT}" ]]; then
  echo "Could not locate installed plugin root for ${PLUGIN_NAME}@${MARKETPLACE_NAME}." >&2
  exit 1
fi

if [[ "${INSTALL_ONLY}" == "1" ]]; then
  print_connect_next_steps
  echo "Codex startup was skipped because --install-only was used."
  exit 0
fi

if [[ "${START_MODE}" == "none" ]]; then
  print_connect_next_steps
  echo "For Codex Desktop, run:"
  printf 'codex app %q\n' "${WORKSPACE_PATH}"
  exit 0
fi

if [[ "${START_MODE}" == "desktop" ]]; then
  echo "Opening Codex Desktop."
  print_connect_next_steps
  exec codex app "${WORKSPACE_PATH}"
fi

echo "Starting Codex CLI."
print_connect_next_steps
exec codex "${CODEX_PROMPT}"
