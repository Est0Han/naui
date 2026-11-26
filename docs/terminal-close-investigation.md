# Terminal-close SIGSEGV: null PTY setter in VTE 0.84.0

The user reproduced the two-terminal close crash under desktop gdb and supplied
the all-thread backtrace in `/tmp/naui-terminal-close.<run>/gdb.log`. The failing
call has been identified and removed. The subsequent user-supplied desktop gdb
log (`/tmp/naui-terminal-close.<run>/gdb.log`) showed normal application exit
but remaining GTK/Wayland validation failures. Following the tab-geometry and
test-runtime corrections, **desktop acceptance passed**: the user supplied a
successful 54-close native stress run and a fatal-warning gdb run that exited
normally (`/tmp/naui-terminal-close.<run>/gdb.log`). Neither final run reported
SIGSEGV, GTK/VTE warnings, or a Wayland protocol error.

## Backtrace and exact root cause

Relevant supplied frames, on the main GTK thread:

```text
Thread 1 "python3" received signal SIGSEGV
#0  0x00007fffe2e7ac9c in vte_terminal_set_pty ()
    from /usr/lib/x86_64-linux-gnu/libvte-2.91-gtk4.so.0
#1 … libffi.so.8
#3 … ffi_call
#4 … gi/_gi.cpython-314-x86_64-linux-gnu.so
#6 … PyObject_Vectorcall
…
#15 … libglib-2.0.so.0
#17 … g_main_context_iteration
#18 … g_application_run
```

Installed versions: VTE GTK4 `0.84.0-2`, PyGObject `3.56.2-1`. Naui's sole
null-PTY setter was in `TerminalPane.close()`:

```python
self.view.set_pty(None)
```

[VTE 0.84.0 Widget::set_pty](https://github.com/GNOME/vte/blob/0.84.0/src/widget.cc#L2050-L2058)
stores the null PTY and calls `_vte_pty_get_impl(pty())` unconditionally.
[That accessor](https://github.com/GNOME/vte/blob/0.84.0/src/vtepty.cc#L77-L81)
dereferences `pty->priv`. The public API documents NULL support, but this version
contains the missing guard. Setting the GObject `pty` property to None also
dispatches to the same setter and would retain the fault.

The installed ELF's `vte_terminal_set_pty` starts at offset `0x8fbe0`. Its sole
instruction at offset ending `c9c`, matching the supplied PC's page offset, is:

```text
8fc8a: mov %rbx,0x50(%r13)      # store the new PTY (NULL)
8fc8e: test %rdi,%rdi           # previous PTY
8fc93: call g_object_unref
8fc98: mov 0x50(%r13),%rbx      # load the new PTY (NULL)
8fc9c: mov 0x18(%rbx),%rax      # dereference NULL: original SIGSEGV
```

This locates the fault in the first cleanup's explicit public detach. There is
no evidence that duplicate Python cleanup, VTE `child-exited`, or an unintended
application quit caused this crash. The application exits because of SIGSEGV
inside the setter, before notebook page removal.

## Narrow fix and ownership

`TerminalPane.close()` still marks CLOSING, stops its callbacks/sources,
disconnects its handlers, terminates/reaps its owned shell and closes the relay.
It now **drops only Naui's Python PTY reference**, preserving VTE's own reference.
Closing the front slave delivers EOF. VTE's internal `unset_pty`/widget disposal
paths release the native PTY without the broken public null setter. Normal
notebook/container removal disposes the widget; no manual destruction, native
reference manipulation, fd double-close or app quit is introduced.

The existing per-tab deferred removal, exact mappings, double-close guards,
SSH transport isolation and final-local-shell exit confirmation are unchanged.
Tests now fail if cleanup calls the public setter, and model the actual GTK
widget's PTY ownership until EOF/container disposal rather than prematurely
closing its fd. The real-PTY ownership stress test runs 50 closes with forced GC.

## Post-change checks completed in this sandbox

* Targeted terminal tab/PTY tests: 14 passed, including 50 real-PTY closes and
  surviving-shell execution (VTE widget surface remains mocked without a display).
* Targeted shutdown tests: 17 passed, including double-close and no-public-setter
  cleanup assertions.
* Full suite with the installed local Pyright: 292 tests, 287 passed, 5 skipped.
  Four askpass tests require sockets denied here; one image-decoder test also
  requires denied native socket access. No Python callback traceback was logged.
* Python compilation and targeted syntax/undefined-name static checks passed.
* The rendered 50-cycle test still returns 77/SKIP: no accessible display.
* Post-change sandbox gdb still cannot start the inferior due to denied ptrace.

These results verify the narrow code change and headless ownership behavior.
They supplement the desktop acceptance recorded below; headless tests alone
would not establish native widget safety.

## Earlier follow-up desktop failures and corrections

The supplied second gdb run ends with:

```text
Gtk-WARNING: GtkGizmo (tabs) reported min height -3, but sizes must be >= 0
...
[Inferior 1 (process <pid>) exited normally]
Error while running hook_stop: No stack.
```

This contains no SIGSEGV. The `No stack` message came from the diagnostic hook
requesting backtraces after normal exit; the hook now checks for live threads
before requesting stacks. Live native stops still record every backtrace; no
signals/warnings are suppressed. Loading and invoking the hook without an
inferior now succeeds without an error.

GTK 4.22.4's [default notebook CSS](https://github.com/GNOME/gtk/blob/4.22.4/gtk/theme/Default/_common.scss#L2030-L2044)
uses -1px top and -2px bottom margins for its top tab strip. Its
[size validation](https://github.com/GNOME/gtk/blob/4.22.4/gtk/gtksizerequest.c#L271-L299)
clamps the content minimum to zero *before* adding CSS margins, so a transient
zero-height strip produces exactly -3px. Notebook
[measurement](https://github.com/GNOME/gtk/blob/4.22.4/gtk/gtknotebook.c#L2088-L2292)
can return zero when no visible pages are measured. This explains the reported
size; the exact transient measurement call stack was not captured.
Naui now scopes `margin: 0` to its own notebook tab strip, removing the negative
geometry rather than filtering warnings. The final desktop runs below passed
with fatal warnings enabled and no reported tab-size warning.

The separate native stress test reported:

```text
Gdk-Message: Error 71 (Protocol error) dispatching to Wayland display.
```

It did not print PASS and cannot count as successful stress acceptance. The
harness was changing XDG_RUNTIME_DIR after Gtk.init connected to the desktop;
it now preserves that runtime directory while isolating its config/cache. This
removes an inappropriate test-environment mutation; it is **not proof of the
Wayland error's cause**. The default renderer/backend is unchanged. The harness
now prints progress after the first close-order cases and every ten stress
closes, so a display failure can be tied to a test stage.

The full suite rerun after all follow-up changes again reported 292 tests, 287
passed, five environment skips, and no callback exceptions. Compilation,
targeted syntax/undefined-name checks, and native CSS parsing with fatal warnings
enabled passed. This sandbox still returns 77/SKIP for native rendered tests;
the user completed those checks on the desktop, as recorded below.

## Confirmed desktop acceptance

The user ran the corrected harness with `PYTHONFAULTHANDLER=1` and
`G_DEBUG=fatal-warnings`. Its supplied output verifies both two-tab close orders,
three-tab middle/first removal, and 50 additional closes while a shell survives
(54 closes total). Every surviving shell executed commands after each close and
forced Python GC. The test completed normal application shutdown with code 0:

```text
Verified terminal closes: 54 ; all survivors executed commands after GC
PASS: both two-tab close orders, three-tab middle/first closes, 50 stress closes;
every survivor executes commands after each close/GC; normal exit 0
```

The user then ran `G_DEBUG=fatal-warnings sh scripts/debug-terminal-close.sh`.
The locally inspected log `/tmp/naui-terminal-close.<run>/gdb.log` ends with:

```text
[Inferior 1 (process <pid>) exited normally]
```

The final supplied output/log contains no SIGSEGV or GTK/VTE warning, no Wayland
protocol error, and no diagnostic-hook stack error. The later shell job-control
line `Stopped ... debug-terminal-close.sh` refers to the debugger suspended at
its prompt **after Naui had exited**, not an application failure.

These results establish acceptance for the reported local multi-terminal crash
on the user's desktop. Dedicated real-SSH multi-terminal acceptance was not run;
the SSH ownership/isolation checks in the automated suite remain mocked.

## Close-path inventory (before changes)

| Entry point | Route and scope |
| --- | --- |
| Per-tab close button (`NauiWindow.create_terminal`) | Captured terminal ID → `close_terminal(id)`; never resolves disposal through current focus |
| Toolbar Close Terminal | `close_active_terminal()` selects an ID, then the same per-tab route |
| Active-process close confirmation | Calls `close_terminal(original_id, force=True)` |
| Deferred tab disposal | `dispose_terminal(id)` releases this tab's SSH leases, calls this `TerminalPane.close()`, removes exact mappings and notebook page |
| Final explicit tab close | Deferred `last_terminal_closed()` starts a fresh local terminal; does not quit |
| Natural shell/PTY exit | `TerminalPane._tick()` polls the child, drains output, closes that pane, invokes its captured `terminal_exit(id)` callback |
| Natural-exit policy | `terminal_exit(id)` → `terminal_exited()` → deferred `after_terminal_exit(id)`; another terminal means close only the exited tab |
| Final local shell exit confirmation | Only explicit acceptance of Exit Naui in `after_terminal_exit` calls the window's normal close path |
| SSH shell integration session-end | `disconnect_session(session)` releases that transport lease; a local outer shell remains available |
| Explicit SSH device disconnect | `disconnect_device(device_id)` stops that device's owned SSH sessions; dedicated SSH tabs enter their own close route |
| Remove SSH workspace device | Confirmed `remove_device` calls device disconnect first; does not quit the application |
| Main window close | `close_requested()` checks saves/dirty files/transfers, then calls global `shutdown()` and lets GTK close normally |
| GTK application shutdown | `do_shutdown()` calls idempotent global shutdown as a fallback and releases the window graph |
| Global cleanup | `shutdown()` stops shared callbacks/services, then closes each remaining terminal before normal widget destruction |

Each tab has a `TerminalPane` and a `TerminalState` under the same ID in
`terminals`/`runtimes`. GTK pages are the pane objects. Disposal removes the
exact mappings before `Gtk.Notebook.remove_page`. TerminalPane state is
RUNNING/CLOSING/CLOSED; the runtime separately has closing/closed guards.

There is **no application connection to VTE `child-exited`**. The real shell is
owned by `PtyBridge` on a separate PTY and observed by a GLib timer. VTE owns the
front PTY master. TerminalPane owns its I/O watch IDs and timer; navigation owns
a per-runtime timeout ID in the application source registry. The tab button
signal and terminal path-click callbacks are disconnected during close.

Current cleanup guards prevent a second Python-level close from doing work.
No `app.quit()`, main-window `destroy()` or main-window `close()` appears in
the tab-close/disposal path.

## Sandbox limitations (still apply after the user supplied a desktop trace)

* `gdb --args /usr/bin/python3 ./naui` cannot start the inferior:
  `Could not trace the inferior process`, `ptrace: Operation not permitted`,
  `During startup program exited with code 127`, `No stack`.
* `PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings ./naui` exits 1 before creating a
  window: `Naui cannot access the graphical display from this session`.
* The native tab test exits 77/SKIP for the same missing display.
* No Naui core was found in the checkout, `/var/crash`, or
  `/var/lib/systemd/coredump`; `coredumpctl` is not installed here.

## Desktop reproduction and regression commands

From the checkout on an unrestricted Linux desktop:

```bash
sh scripts/debug-terminal-close.sh
```

Open two terminal tabs, close either, and run `echo hello` in the survivor.
Repeat in the reverse order and then close Naui normally. There must be no
SIGSEGV; gdb must report normal exit. If a native stop occurs, the hook records
`bt`, `bt full`, and `thread apply all bt full` in the private log directory.
No signal is suppressed.

Repeat separately with native warnings made fatal:

```bash
G_DEBUG=fatal-warnings sh scripts/debug-terminal-close.sh
```

Run the exact GUI regression:

```bash
PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/terminal_tabs_smoke.py
```

It covers both two-tab close orders, three-tab middle/first removal, then 50
new-tab closes while one shell stays alive. Every surviving terminal executes
commands after each removal, and GC is forced for testing only. Final normal
application exit must return zero. The user's final runs passed these checks,
as recorded above. Keep these commands for regression testing after future
terminal lifecycle changes.
