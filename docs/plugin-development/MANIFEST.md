# Plugin manifest (API v1)

```toml
id = "hello-test"
name = "Hello Test"
version = "0.1.0"
api_version = 1
entrypoint = "plugin.py"
description = "Add a notification command"
author = "Your name" # optional
source = "ai-created" # optional, presentation only

[permissions]
commands = true
terminal = false
filesystem_read = false
filesystem_write = false
ssh = false
network = false
file_transfer = false

[ui]
sidebar = false
right_panel = false
command_palette = true
editor_actions = false
```

ID: lowercase letter followed by lowercase letters/digits/hyphens, 1–64 chars;
`base-` and `python-` prefixes reserved. Name: 1–128 chars. Version is a nonempty
string (max 64 chars). API version must equal 1. Entrypoint must be a regular
relative `.py` file inside the plugin; traversal and symlinks are rejected.
Description/author optional. Unknown top-level/UI fields produce warnings;
unknown permission names or nonboolean permission/UI values fail validation.
Source: local, zip, git, ai-created; bundled is core-assigned. These labels do
not grant trust or permissions. Git installation is not implemented yet.

Additional permission names used by current built-ins/public APIs:
`commands`, `views`, `editor_read`, `editor_navigation`, `filesystem_watch`,
`command_execution`. `terminal` aliases `terminal_execution`; `ssh` aliases
`remote_access`. Legacy spellings and plugin.json permission arrays still work.
UI flags describe intended placement; they do not grant capabilities.

Optional README.md, assets/, tests/ are not required. Prefer tests/test*.py using
unittest. Plugins are bounded to 5,000 files / 64 MiB; manifests 16 KiB; Python
source files 1 MiB. Avoid caches, venvs, credentials or generated build artifacts.
Relative helper imports (`from .helpers import ...`) use a fresh plugin namespace
on each reload. Do not import helper modules by bare global names in the live
application. Tests may import plugin.py directly from their own directory.
