#!/usr/bin/env bash
# Sync the deployment-wide uv environment exactly once.
#
# `rbnx build` may invoke package build.sh files in parallel; all of them
# target the same root .venv, so serialize the sync with flock.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
    echo "[uv_sync] error: uv not found; install from https://docs.astral.sh/uv/" >&2
    exit 2
fi

export UV_PROJECT_ENVIRONMENT="$ROOT/.venv"

(
    flock 9
    if [[ -f "$ROOT/uv.lock" ]]; then
        uv sync --project "$ROOT" --locked
    else
        # First bootstrap: resolve and write uv.lock, then commit it.
        uv sync --project "$ROOT"
    fi
) 9>"$ROOT/.uv-sync.lock"