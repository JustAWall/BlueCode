#!/usr/bin/env bash
# BlueCode launcher: works from any directory, bootstraps the venv on first run.
set -euo pipefail

ROOT="${BLUECODE_HOME:-@ROOT@}"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
    echo "BlueCode: first run, creating $VENV ..." >&2
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -q -e "$ROOT"
fi

# Reinstall quietly if the package is gone (e.g. venv recreated).
if ! "$PY" -c 'import bluecode' >/dev/null 2>&1; then
    "$VENV/bin/pip" install -q -e "$ROOT"
fi

exec "$PY" -m bluecode "$@"
