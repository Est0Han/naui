"""Optional Ghidra adapter: bounded, cancellable C-like decompilation."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time

SCRIPT = r'''
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.listing.Data;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.SourceType;
import java.util.TreeSet;
import java.io.*;
import java.nio.charset.StandardCharsets;

public class NauiDecompile extends GhidraScript {
    // Binary text is untrusted: keep it inside one bounded comment line.
    private String comment(Object value) {
        return String.valueOf(value).replace("*/", "* /").replace("\r", "\\r")
            .replace("\n", "\\n").replace("\t", "\\t");
    }
    private String context(Function function) throws Exception {
        StringBuilder text = new StringBuilder("\n/* Function at " + function.getEntryPoint() + "\n");
        SourceType source = function.getSymbol().getSource();
        text.append(" * Name: ").append(comment(function.getName())).append(" [").append(source).append("]\n");
        if (source == SourceType.DEFAULT) {
            text.append(" * Original name unavailable; this address-based name is a generated placeholder.\n");
        } else {
            text.append(" * Non-default symbol retained by analysis/import; not proof of the original source spelling.\n");
        }
        TreeSet<String> calls = new TreeSet<>();
        for (Function called : function.getCalledFunctions(monitor)) {
            if (calls.size() >= 16) break;
            calls.add(comment(called.getName()));
        }
        if (!calls.isEmpty()) text.append(" * Observed calls (up to 16): ").append(String.join(", ", calls)).append("\n");
        if (calls.contains("atexit") || calls.contains("_atexit"))
            text.append(" * API context: atexit registers a callback to run when the process exits.\n");
        if (calls.contains("InitializeSListHead"))
            text.append(" * API context: InitializeSListHead initializes a singly linked list header.\n");
        TreeSet<String> strings = new TreeSet<>();
        InstructionIterator instructions = currentProgram.getListing().getInstructions(function.getBody(), true);
        int scanned = 0;
        while (instructions.hasNext() && scanned++ < 2000 && strings.size() < 8 && !monitor.isCancelled()) {
            Instruction instruction = instructions.next();
            for (Reference reference : instruction.getReferencesFrom()) {
                Data data = currentProgram.getListing().getDataContaining(reference.getToAddress());
                if (data != null && data.hasStringValue()) {
                    String value = String.valueOf(data.getValue());
                    if (value.length() > 160) value = value.substring(0, 160) + "…";
                    strings.add(comment(value));
                    if (strings.size() >= 8) break;
                }
            }
        }
        for (String value : strings) text.append(" * Referenced text: ").append(value).append("\n");
        return text.append(" */").toString();
    }
    public void run() throws Exception {
        DecompInterface decompiler = new DecompInterface();
        try (PrintWriter out = new PrintWriter(getScriptArgs()[0], StandardCharsets.UTF_8)) {
            out.println("/* Reconstructed C-like pseudocode, NOT original source.\n" +
                " * Names/types may be inferred or missing. Not guaranteed compilable.\n" +
                " * FUN_ = unnamed function; DAT_ = unnamed data; local_/param_ = inferred variables.\n" +
                " * Original names/comments are often removed by compilation. Matching symbols or source are needed to restore them.\n" +
                " * Context below reports observed calls and text, not a guessed function purpose.\n" +
                " * Preview: first 100 functions, 5 seconds per function, 2 MiB output.\n */\n");
            if (!decompiler.openProgram(currentProgram)) throw new IOException("Cannot open program in decompiler");
            FunctionIterator functions = currentProgram.getFunctionManager().getFunctions(true);
            int count = 0, chars = 0;
            while (functions.hasNext() && count < 100 && chars < 2000000 && !monitor.isCancelled()) {
                Function function = functions.next();
                if (function.isExternal()) continue;
                count++;
                String details = context(function);
                if (chars + details.length() > 2000000) break;
                out.println(details); chars += details.length();
                DecompileResults result = decompiler.decompileFunction(function, 5, monitor);
                if (result.decompileCompleted()) {
                    String code = result.getDecompiledFunction().getC();
                    if (chars + code.length() > 2000000) { out.println("/* Output limit reached. */"); break; }
                    out.println(code); chars += code.length();
                } else out.println("/* This function could not be decompiled within the preview limits. */");
                out.flush();
            }
            out.println("\n/* Attempted " + count + " functions; " + currentProgram.getFunctionManager().getFunctionCount() + " identified in analysis. */");
            if (functions.hasNext()) out.println("/* Partial preview. Use Ghidra for full interactive analysis. */");
            if (count == 0) out.println("/* No functions identified. The binary may be unsupported, packed or need manual analysis. */");
        } finally { decompiler.dispose(); }
    }
}
'''


def ghidra_path():
    configured = os.environ.get('NAUI_GHIDRA_HOME')
    root = Path(configured) if configured else Path.home() / '.local/share/naui-tools/ghidra_12.2_PUBLIC'
    return root / 'support/analyzeHeadless'


def decompile(path, cancelled=None):
    cancelled = cancelled or threading.Event()
    launcher = ghidra_path()
    if not launcher.is_file():
        raise ValueError('Ghidra is not installed. Set NAUI_GHIDRA_HOME to its installation folder.')
    if Path(path).stat().st_size > 256 * 1024 * 1024:
        raise ValueError('This file exceeds the 256 MiB integrated preview limit. Use Ghidra directly.')
    with tempfile.TemporaryDirectory(prefix='naui-decompile-') as directory:
        root = Path(directory)
        (root / 'NauiDecompile.java').write_text(SCRIPT)
        output = root / 'output.c'
        command = [str(launcher), directory, 'preview', '-import', str(Path(path).absolute()),
                   '-scriptPath', directory, '-postScript', 'NauiDecompile.java', str(output),
                   '-analysisTimeoutPerFile', '90', '-max-cpu', '2', '-deleteProject']
        with (root / 'log').open('w+b') as log:
            if cancelled.is_set(): raise ValueError('Decompilation cancelled')
            environment = os.environ.copy()
            private_jdk = Path.home() / '.local/share/naui-tools/jdk-25'
            java_home = environment.get('NAUI_JAVA_HOME')
            if java_home or (private_jdk / 'bin/javac').is_file():
                java_home = java_home or str(private_jdk)
                environment['JAVA_HOME'] = java_home
                environment['PATH'] = str(Path(java_home) / 'bin') + os.pathsep + environment.get('PATH', '')
            process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            deadline = time.monotonic() + 240
            try:
                while process.poll() is None:
                    if cancelled.wait(.2): raise ValueError('Decompilation cancelled')
                    if time.monotonic() > deadline: raise ValueError('Decompilation exceeded four minutes. Use Ghidra directly for this file.')
                    if log.tell() > 16 * 1024 * 1024: raise ValueError('Decompiler log limit exceeded')
                if process.returncode or not output.exists():
                    log.seek(max(0, log.tell()-3000))
                    raise ValueError('Ghidra could not produce a code preview.\n' + log.read().decode('utf-8','replace'))
                if output.stat().st_size > 8 * 1024 * 1024: raise ValueError('Decompiled output exceeds the preview limit')
                return output.read_text()
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
