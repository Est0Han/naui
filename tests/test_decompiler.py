import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import patch
from naui.decompiler import decompile


class DecompilerTests(unittest.TestCase):
    def test_helper_output_and_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); binary=root/'spaces ; sample.exe'; binary.write_bytes(b'MZ')
            helper=root/'helper'
            helper.write_text('#!/usr/bin/python3\nimport pathlib,sys\na=sys.argv\nassert "-deleteProject" in a\nassert a[a.index("-import")+1].endswith("spaces ; sample.exe")\np=a[a.index("-postScript")+2]\npathlib.Path(p).write_text("/* pseudocode */ int test() { return 1; }")\n')
            helper.chmod(0o700)
            with patch('naui.decompiler.ghidra_path',return_value=helper):
                self.assertIn('int test()',decompile(binary))
            self.assertEqual(binary.read_bytes(),b'MZ')

    def test_cancel_and_missing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);binary=root/'file.exe';binary.write_bytes(b'MZ')
            helper=root/'helper';helper.write_text('#!/bin/sh\nexit 0\n');helper.chmod(0o700)
            with patch('naui.decompiler.ghidra_path',return_value=helper):
                cancelled=threading.Event();cancelled.set()
                with self.assertRaisesRegex(ValueError,'cancelled'):decompile(binary,cancelled)
                with self.assertRaisesRegex(ValueError,'could not produce'):decompile(binary)

    def test_running_child_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);binary=root/'file.exe';binary.write_bytes(b'MZ')
            helper=root/'helper';helper.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(60)\n');helper.chmod(0o700)
            event=threading.Event();timer=threading.Timer(.3,event.set);timer.start()
            try:
                with patch('naui.decompiler.ghidra_path',return_value=helper):
                    with self.assertRaisesRegex(ValueError,'cancelled'):decompile(binary,event)
            finally:timer.join()


class GhidraReadabilityTests(unittest.TestCase):
    @unittest.skipUnless(__import__('os').environ.get('NAUI_TEST_GHIDRA') == '1',
                         'Set NAUI_TEST_GHIDRA=1 with Ghidra, gcc and strip installed')
    def test_real_named_and_stripped_binary_context(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, binary = root/'fixture.c', root/'fixture'
            source.write_text('#include <stdio.h>\nvoid describe(void) { puts("Readable fixture message"); }\n'
                              'int main(void) { describe(); return 0; }\n')
            subprocess.run(['gcc', '-O0', '-o', str(binary), str(source)], check=True)
            original = binary.read_bytes()
            report = decompile(binary)
            self.assertIn('Name: describe', report)
            self.assertIn('Observed calls', report)
            self.assertIn('Referenced text: Readable fixture message', report)
            self.assertEqual(original, binary.read_bytes())
            subprocess.run(['strip', str(binary)], check=True)
            original = binary.read_bytes()
            report = decompile(binary)
            self.assertIn('Original name unavailable', report)
            self.assertIn('Referenced text: Readable fixture message', report)
            self.assertEqual(original, binary.read_bytes())
