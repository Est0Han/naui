# AI-assisted plugin development

Open **Plugins → Create Plugin with AI**. Enter the plugin name, ID, behavior
and minimum permissions. Choose **Manual copy mode**, a detected CLI, or a
configured custom agent. Creating the scaffold does not run or enable code.
The resulting dialog offers **Copy AI Prompt** and, for a CLI, **Start AI Agent**.

The scaffold lives under the configured user plugin directory. Find actual paths:

```sh
naui plugin paths
naui plugin directory
naui plugin sdk-path
naui plugin api-version
```

The prompt contains the exact target directory, SDK/example paths, requested
behavior and permissions. It requires the agent to inspect these CLI commands,
modify only the assigned plugin, use public APIs, and report
`PLUGIN_API_CAPABILITY_MISSING: <description>` instead of changing core.
Manual **Copy Master AI Prompt**, modification and repair prompts remain available.

## Providers and models

**Plugins → AI Agents** shows availability and configures defaults. Program
detection uses PATH on each lookup; no account-specific executable path is baked
in. Selecting an absent CLI reports its status before launching anything.

* **Codex CLI:** runs interactively with workspace-write sandbox, on-request
  approvals and the plugin directory as cwd. Existing CLI login remains CLI-owned.
* **Claude CLI:** runs interactively in the plugin directory with normal CLI
  authentication and approval behavior; no bypass flags.
* **Custom CLI Agent:** configure an explicit executable, non-secret argument
  list and prompt delivery. Saving validates executable availability; it does
  not execute the program. Arguments support shell-style quoting in the form,
  are converted to argv, and are never evaluated by a shell.
* **API Provider:** a provider/configuration contract is present for future
  adapters; live API calls and credential storage are **not implemented**.
  The UI says this and does not collect tokens. Future persistent credentials
  must use a core-owned secure store (Secret Service/Keychain), not JSON.

Codex/Claude accept an optional model ID; blank means **Provider Default**.
Naui does not maintain a vendor model catalog or claim it can enumerate models.
Custom agents use their own configured/default model.
CLI flags follow the [Codex CLI reference](https://developers.openai.com/codex/cli/reference/)
and [Claude CLI reference](https://code.claude.com/docs/en/cli-reference).
Older CLI versions may require updating to support these flags.

Custom prompt delivery modes:

* `stdin`: a private finite prompt file becomes stdin (EOF after the prompt).
  Output remains in the real PTY. Use for agents that accept noninteractive stdin.
* `prompt-file`: use `{prompt_file}` in one or more arguments; Naui substitutes
  the private absolute file path. The CLI reads the file and retains PTY stdin.
* `terminal`: start the interactive CLI, wait for its input UI, then click
  **Send Prompt (agent ready)** once. This sends bracketed paste and Enter.
  Compatibility depends on the custom CLI; there is no readiness guessing.

## Task review and lifecycle

Each launch owns a distinct terminal/process session. The Plugin Manager has an
**AI TASKS** section with status, **Open Agent Terminal**, **Stop Task** and
**View Changes**. Completed terminals retain their output until explicitly closed;
closing one task does not quit Naui or affect other terminals. **Stop Task** uses
the existing graceful PTY session termination and bounded escalation, never a
global kill-by-name. Cleanup is idempotent.

**View Changes** opens a main-area created/modified/deleted listing with text
diffs against the pre-task snapshot. Review is bounded to 5,000 files / 64 MiB;
large/binary diffs are omitted. No Git repository is required. The snapshot is
for review, not an automatic rollback/backup. Then **Validate → Enable/Reload**.
Naui never auto-enables AI-generated code. Enable/reload/uninstall are refused
while an agent is still editing or stopping. Disable remains available.

Validation errors offer **Copy Error for AI** and **Send Fix to Agent**.
Send Fix launches a fresh bounded plugin task; it does not invent a provider
resume/session identifier. For additional changes use the existing modification
prompt or Start AI Agent. Enable/reload still require reviewed code/permissions.

## Secrets and limitations

Naui forwards only a small desktop/user environment allowlist. It does not inject
Naui authentication tokens, SSH agent sockets, sudo/SSH passwords or unrelated
API keys. CLI authentication files and normal home/settings remain accessible
to the CLI as the current user. Users who authenticate a CLI solely through
environment API keys must configure/login to that CLI separately.

Prompts and launch metadata live in a private runtime directory, with owner-only
permissions, and are removed after process termination. No full prompt/output
is written to Naui's durable logs/settings. The agent itself may maintain its
own history. An abrupt process kill can leave private runtime files until runtime
directory cleanup; Naui does not delete arbitrary stale directories.

This is an instruction/API boundary, **not** a universal OS sandbox. Claude/custom
tools run with their normal user privileges; even Codex may request approval for
extra access. Review agent approvals and generated code. Never put credentials
in descriptions, model fields, arguments, prompts or plugin settings. Plugins
cannot retrieve the AI manager or credentials through the public API.

## Recovery and CLI management

```sh
naui --safe-mode
naui plugin list
naui plugin validate hello-test
naui plugin enable hello-test --trust
naui plugin disable hello-test
naui plugin uninstall hello-test --yes
```

Safe mode disables all user-plugin loading, including explicit `--plugin` paths,
for this launch only; bundled functionality and saved preferences remain intact.
The Plugins view allows validation, disabling and uninstalling in safe mode.
Restart normally to enable code. A failed/interrupted user-plugin startup is
marked for recovery and is not retried automatically; review and explicitly
Enable to try again. The marker is persisted before in-process activation.

CLI management is **offline recovery**: it changes next-launch settings, not
an unrelated running GUI. Close Naui before CLI changes/uninstall to avoid
concurrent settings writes. GUI actions take effect immediately. Uninstall
requires confirmation, removes only the managed plugin folder and retains
plugin settings. Bundled plugins cannot be uninstalled through this path.

## Verification

No paid provider calls are required by tests. `tests/test_ai_agents.py` uses
harmless local executables with real owned PTYs, checking cwd, prompt delivery,
secret filtering, CLI/SDK selection, recovery and 50 cancel cycles. On an
accessible Linux desktop run:

```sh
PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/ai_agents_smoke.py
PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/plugin_manager_smoke.py
PYTHONPATH=src PYTHONFAULTHANDLER=1 G_DEBUG=fatal-warnings python3 tests/terminal_tabs_smoke.py
```

The desktop tests keep the real XDG_RUNTIME_DIR intact and use disposable
config/cache. An inaccessible display is a skip, not proof of GUI stability.
