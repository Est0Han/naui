# RPF archive inspection

Open a local `.rpf` in the file explorer to browse its contents. NaUi uses the
optional [OpenRAGE CLI](https://github.com/Two-Play/openrage) (tested with v0.6.0).
Install its official Linux release on PATH as `openrage`, or place the executable
at `~/.local/share/naui-tools/openrage/openrage`. This separately installed tool
has its own GPL license; NaUi does not bundle its code, game files or keys.

- **Open** enters a directory or nested archive; **Up** returns to its parent.
- **Extract original** creates a separate copy and opens it in NaUi's normal viewer.
  Text files can be edited and saved there.
- **Open readable** chooses XML for supported metadata, text for supported
  string tables, and disassembly for supported `.ysc` scripts. Existing UTF-8
  text opens directly. Unknown binaries produce an explicitly labeled inspection
  report with bounded printable strings and hexadecimal bytes, not source code.
  Decoder errors remain visible; unknown hashed names are not invented.
- **Raw bytes** shows the first 4096 archive bytes as hexadecimal and ASCII.
  This is binary inspection, not original source code or decompilation.

Copies persist under `$XDG_DATA_HOME/naui/extracted` (normally
`~/.local/share/naui/extracted`). Each extraction uses a new folder, preserving
previous edits. The path appears in the tab and archive status. Delete unwanted
copies using the file explorer.

## Current limits

Archives are read-only in this integration. Saving an extracted file does **not**
rebuild or update its source archive. In particular OpenRAGE's GTA VI RPF9 support
is read-only, and its GTA VI scripts are not currently decoded. RDR2 writing is
also not exposed here. DLL decompilation/editing is not part of this feature.

RPF9 without a supported executable may display recovered entries with synthetic
names rather than the original directory structure. Encrypted/unsupported entries
may be unavailable. No game executable is launched by NaUi.

Browsing currently requires local files. Individual previews are limited to
128 MiB, listings display the first 1000 entries, helper output is bounded, and
operations time out after two minutes. Closing the tab cancels its helper.
For larger recovery scans, use OpenRAGE directly. Original archives remain intact.
