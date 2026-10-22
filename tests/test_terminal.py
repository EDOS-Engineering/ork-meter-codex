"""Exercise masking and terminal restoration through a real pseudo-terminal."""
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import unittest


@unittest.skipUnless(os.name == "posix", "requires a POSIX pseudo-terminal")
class MaskedTerminal(unittest.TestCase):
    def run_prompt(self, typed=None, interrupt=False, skip_restore=False):
        import pty
        import termios
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        program = '''from ork_codex.terminal import masked_input
try:
    code = masked_input("Claim code: ")
    print("MATCH" if code == "ABCD-EFGH" else "MISMATCH")
except (KeyboardInterrupt, EOFError):
    print("CANCELLED")
'''
        if skip_restore:
            # Negative control: activate masking normally but discard restore.
            program = """import termios
real_setattr = termios.tcsetattr
calls = 0
def omit_restore(fd, when, attributes):
    global calls
    calls += 1
    if calls == 1:
        real_setattr(fd, when, attributes)
termios.tcsetattr = omit_restore
""" + program
        process = subprocess.Popen([sys.executable, "-B", "-c", program],
                                   cwd=Path(__file__).resolve().parents[1],
                                   stdin=slave, stdout=slave, stderr=slave)
        output = b""
        try:
            deadline = time.monotonic() + 5
            sent = False
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.05)
                if readable:
                    output += os.read(master, 4096)
                if not sent and b"Claim code: " in output:
                    # Input is sent only after the child has disabled terminal echo.
                    if interrupt:
                        process.send_signal(signal.SIGINT)
                    else:
                        os.write(master, typed)
                    sent = True
                if process.poll() is not None:
                    while select.select([master], [], [], 0)[0]:
                        output += os.read(master, 4096)
                    break
            self.assertEqual(process.wait(timeout=1), 0)
            restored = termios.tcgetattr(slave)
            if sys.platform == "darwin":
                # macOS termios(4) labels PENDIN "retype pending input (state)".
                # Switching ICANON back on can set it even after restoring the
                # original attributes. Every configuration bit, speed and
                # control character must still match, including ECHO/ICANON.
                restored[3] &= ~termios.PENDIN
                original[3] &= ~termios.PENDIN
            self.assertEqual(restored, original, "terminal settings were not restored")
            return output
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
            os.close(slave)

    def test_missing_restore_is_still_detected(self):
        with self.assertRaisesRegex(AssertionError, "terminal settings were not restored"):
            self.run_prompt(b"ABCD-EFGH\n", skip_restore=True)

    def test_code_is_rendered_as_asterisks(self):
        output = self.run_prompt(b"ABCD-EFGH\n")
        self.assertIn(b"Claim code: *********", output)
        self.assertIn(b"MATCH", output)
        self.assertNotIn(b"ABCD-EFGH", output)

    def test_backspace_updates_mask_and_submitted_code(self):
        output = self.run_prompt(b"ABCX\x7fD-EFGH\n")
        self.assertIn(b"\b \b", output)
        self.assertIn(b"MATCH", output)
        self.assertNotIn(b"MISMATCH", output)
        self.assertNotIn(b"ABCX", output)

    def test_cancel_restores_terminal(self):
        self.assertIn(b"CANCELLED", self.run_prompt(interrupt=True))
        self.assertIn(b"CANCELLED", self.run_prompt(b"\x04"))


if __name__ == "__main__":
    unittest.main()
