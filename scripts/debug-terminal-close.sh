#!/bin/sh
# Run from the user's desktop. Full backtraces stay in a private temporary dir.
set -eu
umask 077
naui_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
naui_debug_dir=$(mktemp -d /tmp/naui-terminal-close.XXXXXX)
printf 'Backtrace log: %s/gdb.log\n' "$naui_debug_dir"
printf 'Open two terminal tabs, then close one. A native stop captures all backtraces.\n'
cd -- "$naui_root"
exec gdb -iex 'set debuginfod enabled off' \
    -ex "set logging file $naui_debug_dir/gdb.log" \
    -ex 'set logging overwrite on' -ex 'set logging enabled on' \
    -x "$naui_root/scripts/debug-terminal-close.gdb" --args python3 ./naui
