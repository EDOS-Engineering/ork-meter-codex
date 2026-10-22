"""Masked terminal input, including Python versions before getpass echo_char."""
import os
import sys


def _read_masked(read_char, output):
    chars = []
    while True:
        char = read_char()
        if char in {"\r", "\n"}:
            return "".join(chars)
        if char == "\x03":
            raise KeyboardInterrupt
        if char in {"", "\x04", "\x1a"}:
            raise EOFError("Claim-code entry cancelled")
        if char in {"\b", "\x7f"}:
            if chars:
                chars.pop()
                output.write("\b \b")
        elif char == "\x15":
            output.write("\b \b" * len(chars))
            chars.clear()
        elif char.isprintable():
            chars.append(char)
            output.write("*")
        output.flush()


def masked_input(prompt):
    if not sys.stdin.isatty():
        raise ValueError("Enter the browser claim code in your own terminal")
    output = sys.stderr
    if os.name == "nt":
        import msvcrt

        def read_char():
            char = msvcrt.getwch()
            if char in {"\x00", "\xe0"}:
                msvcrt.getwch()  # Consume function/arrow keys without accepting them.
                return "\x00"
            return char

        restore = lambda: None
        activate = lambda: None
    else:
        import termios
        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
        masked = termios.tcgetattr(fd)
        masked[3] &= ~(termios.ECHO | termios.ICANON)
        masked[6][termios.VMIN] = 1
        masked[6][termios.VTIME] = 0
        read_char = lambda: sys.stdin.read(1)
        restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, original)
        activate = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, masked)
    try:
        activate()
        output.write(prompt)
        output.flush()
        return _read_masked(read_char, output)
    finally:
        restore()
        output.write("\n")
        output.flush()
