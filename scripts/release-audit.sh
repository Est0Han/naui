#!/bin/sh
set -eu
naui_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 -B "$naui_root/scripts/release_audit.py" "$@"
