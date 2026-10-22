"""Physical universal-newline coordinates shared with Python's parser."""
import re

PHYSICAL_LINES = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$")


def lines(text):
    # Unlike str.splitlines(), form feed and Unicode separators in comments
    # or strings do not create extra source lines. Keep terminators exact.
    return PHYSICAL_LINES.findall(text)
