import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ork_codex import auto_hook
from ork_codex.auto_hook import hook_command, install


class HookLaunchTests(unittest.TestCase):
    def test_windows_command_uses_literal_arguments(self):
        self.assertEqual(
            hook_command([r"C:\Program Files\Python\python.exe", "-I", "-B",
                          r"C:\Users\O'Brien\$profile\run.py"], "nt"),
            "& 'C:\\Program Files\\Python\\python.exe' '-I' '-B' "
            "'C:\\Users\\O''Brien\\$profile\\run.py'")

    def test_host_shell_preserves_paths_arguments_and_stdin(self):
        with tempfile.TemporaryDirectory(prefix="ork hook's $literal ") as directory:
            script = Path(directory) / "run.py"
            script.write_text("import json, sys\nprint(json.dumps([sys.argv[1:], sys.stdin.read()]))\n")
            argument = "apostrophe's $HOME `literal`"
            command = hook_command([sys.executable, "-I", "-B", str(script), argument])
            if os.name == "nt":
                shell = shutil.which("powershell") or shutil.which("pwsh")
                self.assertIsNotNone(shell, "PowerShell required to launch Windows hooks")
                argv = [shell, "-NoProfile", "-NonInteractive", "-Command", command]
            else:
                argv = ["/bin/sh", "-c", command]
            result = subprocess.run(argv, input="{}", capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [[argument], "{}"])

    def test_installer_registers_host_shell_command(self):
        with tempfile.TemporaryDirectory(prefix="ork hook's $literal ") as directory:
            result = install(directory)
            config = json.loads(Path(result["config"]).read_text(encoding="utf-8"))
            command = config["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
            self.assertEqual(command, hook_command([sys.executable, "-I", "-B", result["hook"]]))

    def test_installer_registers_powershell_quoting_for_windows(self):
        # configure() must build the command through hook_command, not cmd-style
        # list2cmdline quoting, which PowerShell would mis-parse on Windows.
        real = auto_hook.hook_command
        with tempfile.TemporaryDirectory(prefix="ork hook's $literal ") as directory, \
                patch.object(auto_hook, "hook_command", side_effect=lambda argv, platform=None: real(argv, "nt")):
            result = install(directory)
            config = json.loads(Path(result["config"]).read_text(encoding="utf-8"))
            command = config["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        expected = real([sys.executable, "-I", "-B", result["hook"]], "nt")
        self.assertEqual(command, expected)
        self.assertTrue(command.startswith("& '"), command)
        self.assertIn("''", command)  # the apostrophe in the directory name is doubled, PowerShell-literal
