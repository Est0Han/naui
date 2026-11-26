# EXE and DLL inspection

Opening a Windows PE executable or DLL displays its architecture, sections,
printable strings and raw bytes. The file is never executed. These views are
read-only and need no additional tools.

## C-like code preview

1. Download an official [Ghidra release](https://github.com/NationalSecurityAgency/ghidra/releases).
2. Install the Java **JDK** required by that release. Ghidra 12.2 requires JDK 25;
   a runtime alone is insufficient.
3. Extract Ghidra, and launch NaUi with its path:

   ```bash
   export NAUI_GHIDRA_HOME="$HOME/tools/ghidra_12.2_PUBLIC"
   # Optional: select a specific JDK without changing system Java.
   export NAUI_JAVA_HOME="$HOME/tools/jdk-25"
   naui
   ```

4. Open an EXE or DLL and choose **Decompile to C-like code**. The **Code** tab
   displays syntax-highlighted pseudocode with line numbers. Cancel stops the
   job; closing its tab also cancels it.

Set paths to where you actually extracted the tools. `NAUI_JAVA_HOME` is optional
when a compatible JDK is already discoverable. NaUi also recognizes a private
installation at `~/.local/share/naui-tools/ghidra_12.2_PUBLIC`, with an optional
JDK at `~/.local/share/naui-tools/jdk-25`. No tools are downloaded automatically.

The preview covers at most the first 100 functions. Analysis has a 90-second
budget, decompilation allows five seconds per function, and the whole job has a
four-minute limit. Integrated inputs are limited to 256 MiB. Complex, packed,
unsupported or managed-code binaries may need a different decompiler or manual
analysis. Use Ghidra directly for full interactive analysis.

Reconstructed types and names can be incomplete or wrong. Output is not exact
original source and is not guaranteed to compile. Editing/rebuilding binaries
is not supported. Third-party Ghidra and JDK licenses apply to those tools;
NaUi does not redistribute them.
