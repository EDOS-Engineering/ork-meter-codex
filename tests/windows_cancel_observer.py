"""Detached native observer for a hidden, owned Windows console fixture."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
directory = Path(sys.argv[1])
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
kernel.AttachConsole.argtypes = [wintypes.DWORD]
kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
Handler = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
kernel.SetConsoleCtrlHandler.argtypes = [Handler, wintypes.BOOL]
callback = Handler(lambda kind: True)
console_processes = (wintypes.DWORD * 8)()
if kernel.GetConsoleProcessList(console_processes, 8):
    raise RuntimeError("observer must be launched with DETACHED_PROCESS")
startup = subprocess.STARTUPINFO()
startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
startup.wShowWindow = 0
process = subprocess.Popen([sys.executable, "-I", "-B", str(ROOT / "scripts/ork_codex.py"),
                            "--state", str(directory / "state"), "run", "--kind", "log", "--",
                            sys.executable, str(ROOT / "tests/fixtures/process_tree.py"), "parent", str(directory)],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
attached = False
registered = False
try:
    deadline = time.monotonic() + 5
    while not (directory / "descendant.started").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (directory / "descendant.started").exists(), "descendant did not start"
    if not kernel.AttachConsole(process.pid):
        raise OSError("AttachConsole failed", ctypes.get_last_error())
    attached = True
    if not kernel.SetConsoleCtrlHandler(callback, True):
        raise OSError("observer control handler unavailable", ctypes.get_last_error())
    registered = True
    if not kernel.GenerateConsoleCtrlEvent(1, process.pid):
        raise OSError("CTRL_BREAK delivery failed", ctypes.get_last_error())
    out, err = process.communicate(timeout=8)
    assert process.returncode == 130, (process.returncode, err)
    assert b"parent before interruption" in out
    assert b"descendant before interruption" in out
    assert (directory / "parent.ended").exists()
    assert (directory / "descendant.ended").exists()
    print(json.dumps({"wrapper_exit": 130, "partial_output": True,
                      "parent_interrupted": True, "descendant_interrupted": True}))
finally:
    if registered:
        kernel.SetConsoleCtrlHandler(callback, False)
    if attached:
        kernel.FreeConsole()
    if process.poll() is None:
        (directory / "fixture.stop").write_text("stop owned fixture")
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)
    process.stdout.close()
    process.stderr.close()
