#!/usr/bin/env bash
# premium-factory launcher (Linux / macOS / Termux)
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$DIR/pfcli.py" "$@"
