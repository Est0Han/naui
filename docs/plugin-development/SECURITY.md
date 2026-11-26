# Security and lifetime rules

The API checks permissions, ownership and device identity. In-process Python
plugins are **not sandboxed**: malicious code can import os/subprocess or inspect
objects. Install only trusted/reviewed plugins. Naui requests explicit trust
before importing/testing/enabling a user plugin; manifests are not a guarantee against malicious
Python. Process isolation is future work.

* Never read private SSH keys, agent internals or credentials. Never create an
  SSH implementation/credential store. Authentication stays in core OpenSSH.
* Never use shell strings for filesystem copying. Request backend/transfer API
  operations. Treat file contents, filenames and terminal messages as data.
* Do not interpret opened files as commands or automatically execute scripts.
* filesystem_watch does not grant terminal_execution or command_execution.
  Runner commands require explicit actions. Use argv through public transport;
  never interpolate filenames, Git records or watch events into shell strings.
* Request only nonrecursive relevant directories. Core owns monitors, remote
  inotify channels and polling. Never create a parallel plugin polling system.
* Do not replace a document's backend/device when sidebar selection changes.
* Never discard unsaved buffers or silently overwrite a transfer destination.
* Never modify private core terminal/session state. Never stop another plugin's
  transfer or another device's PTY.
* Avoid per-chunk GTK updates. Core polls progress at 200 ms; streams use 64 KiB
  chunks and backpressure. Directory totals stay unknown during enumeration.
* Release all activity during stop(); late UI completions are ignored. Do not
  destroy windows or call application quit from worker callbacks.

OpenSSH prefers keys/agent and uses normal config/ProxyJump/known_hosts. Interactive
sessions retain OpenSSH's real PTY prompts. Noninteractive backend/transfer
operations may use a core-owned masked askpass prompt over a private local Unix
socket. The helper is local only. Credential data travels through that socket,
not argv, environment, logs, shell history or workspace/plugin JSON.

Session memory is optional and scoped to device plus prompt. Rejected cached
responses are discarded and reprompted. Disconnect forgets that device's values;
exit drops all retained values. Python cannot promise secure zeroing of every
string allocation. No persistent keyring storage exists. Host confirmations
require explicit approval and are never remembered as credentials. OpenSSH owns
known_hosts changes and authentication policy.

Transfer files are staged in destination-directory .naui-partial-* files. EOF
or missing COMMIT never publishes remote data. Explicit Replace checks the
original destination revision; atomic rename protects against partial content
but its optimistic revision check has a small check/rename race. New targets use
atomic no-overwrite linking. ACLs/xattrs/hard-link identity are not preserved.
Symlinks and special files are skipped, including directory links. Completed
files/directories remain after cancellation; recursive jobs are not transactions.
An abrupt machine/server crash can leave a private partial file. Naui does not
glob-delete unknown partials; normal job cancellation removes only its own stage.

## Plugin Manager trust, storage and validation

`naui plugin validate` is an explicit request to execute trusted plugin
imports/tests in a separate bounded interpreter. Use `--static` for code-free
inspection. This is an API-boundary validator, not an OS sandbox or malware
scanner. Live plugins remain in-process; arbitrary Python can bypass API checks.
Obvious private Naui/GTK/native imports and private API access fail validation.
Network requires a separate declared grant. Enable only reviewed code.

Reviewed enabled state/permission grants are stored with a content fingerprint.
Changed code is left inactive on the next launch until reviewed/validated again.
AI prompts and authentication values are not persisted. Source labels such as
ai-created/local/zip are presentation, never trust decisions.

ZIP installation rejects traversal, absolute/Windows paths, symlinks, special
files, duplicates and oversized archives. Folder install rejects symlinks and
special files, inspects a private stage, reviews capabilities and installs
**disabled**. No setup.py/install.sh/postinstall is invoked. Uninstall removes
only the exact confirmed managed plugin folder; unrelated files and plugin
settings are retained.

Credential-like settings keys are rejected, but plugins must not disguise
secrets under innocent names. Logs are bounded and session-only with basic
key/value redaction, not a guarantee for arbitrary secret text. Never log
passwords, tokens or keys. Native widgets and callback/source ownership remain
core-owned; disable/reload is deferred out of widget signal stacks.
