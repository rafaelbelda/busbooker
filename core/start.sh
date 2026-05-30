#!/usr/bin/env bash
#
# bus-reserver launcher.
#
#   * mirrors the original script's DISPLAY / xvfb-run check
#   * starts uvicorn with a SINGLE worker — the Playwright persistent profile
#     must not be shared across processes
#
set -euo pipefail

# Resolve project root (parent of core/) so `uvicorn core.main:app` imports cleanly.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

UVICORN_CMD=(uvicorn core.main:app --host 127.0.0.1 --port 8000 --workers 1)

# Headed Chromium needs an X display. Skip entirely when HEADLESS is enabled.
HEADLESS_LOWER="$(printf '%s' "${HEADLESS:-false}" | tr '[:upper:]' '[:lower:]')"

if [[ "${HEADLESS_LOWER}" == "true" || "${HEADLESS_LOWER}" == "1" ]]; then
    echo "[start] HEADLESS mode — no virtual display"
    exec "${UVICORN_CMD[@]}"
fi

if [[ -n "${DISPLAY:-}" ]]; then
    echo "[start] using existing DISPLAY=${DISPLAY}"
    exec "${UVICORN_CMD[@]}"
fi

if command -v xvfb-run >/dev/null 2>&1; then
    echo "[start] no DISPLAY — launching under xvfb-run"
    export _XVFB_RUNNING=1
    exec xvfb-run -a "${UVICORN_CMD[@]}"
fi

echo "[start] ERROR: no DISPLAY and xvfb-run not found. Install xvfb or set HEADLESS=true." >&2
exit 2
