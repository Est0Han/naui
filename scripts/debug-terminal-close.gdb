set pagination off
set debuginfod enabled off
set environment PYTHONFAULTHANDLER 1

# Stop at the original native fault and capture every thread before continuing.
# Do not change signal delivery, catch Python errors, or hide native warnings.
define hook-stop
  python
# hook-stop also runs after a normal inferior exit. There is no stack then;
# keep that normal exit distinct from an actual signal stop with live threads.
if gdb.selected_inferior().threads():
    gdb.execute('bt')
    gdb.execute('bt full')
    gdb.execute('thread apply all bt full')
  end
end

run
