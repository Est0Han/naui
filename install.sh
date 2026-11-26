#!/bin/sh
set -eu
naui_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$naui_root/scripts/install.sh" "$@"
