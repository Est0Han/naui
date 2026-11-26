#!/bin/sh
set -eu
naui_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python3 "$naui_root/scripts/package.py" uninstall "$@"
