"""Ork Meter for Codex: install the pinned release from orkmeter.com, then run it.

This file is the whole plugin that the marketplace installs. It holds no
product code. `release.json` beside it pins one released version of the
`ork-meter-codex` wheel and that wheel's SHA-256; the wheel and the Ork
Meter helper live at https://orkmeter.com/releases/codex/<version>/.

First run: download the wheel over HTTPS (no redirects, no credential),
refuse it unless its SHA-256 equals the pinned digest, create a private
virtual environment under `$CODEX_HOME/ork-meter-codex/package/<version>/`,
install the wheel and its dependencies (tiktoken, keyring, from PyPI) with
pip, and record the digest. Every run: verify that record, then run the
installed `ork-codex` command with the arguments given. A new release is a
new `release.json`; the earlier environment stays until you delete it.

Any Python 3.8 or later can start this file. The released wheel's pinned
dependencies ship binary wheels only for the Python versions `release.json`
lists under `python`, so before the first install this file finds one of
those on its own: the interpreter running it, `pythonX.Y` on PATH, the
Windows `py -X.Y` launcher, or `uv python find X.Y`. It then runs itself
again under that interpreter. Nobody has to pick a Python version by hand.
Standard library only.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import venv

ROOT = Path(__file__).resolve().parents[1]
WHEEL_LIMIT = 32 * 1024 * 1024
TIMEOUT = 120
# Python versions the 0.1.x wheels' pinned dependencies have binary wheels for,
# in preference order. release.json's "python" list overrides this.
PYTHONS = ("3.13", "3.12", "3.11")
REEXEC = "ORK_CODEX_BOOTSTRAP_PYTHON"


def release():
    data = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
    for key in ("version", "wheel", "sha256", "base"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"release.json lacks {key}")
    pythons = data.setdefault("python", list(PYTHONS))
    if not isinstance(pythons, list) or not pythons or not all(isinstance(v, str) and v.count(".") == 1 for v in pythons):
        raise ValueError("release.json python is not a list of X.Y versions")
    if len(data["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in data["sha256"]):
        raise ValueError("release.json sha256 is not a lowercase hex SHA-256")
    return data


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def package_dir(data):
    return codex_home() / "ork-meter-codex" / "package" / data["version"]


def base_url(data):
    return os.environ.get("ORK_CODEX_RELEASE_BASE", data["base"]).rstrip("/")


def allowed(url, base):
    parsed, origin = urllib.parse.urlparse(url), urllib.parse.urlparse(base)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and origin.scheme == "http" and parsed.hostname == "127.0.0.1" == origin.hostname


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url, base):
    if not allowed(url, base):
        raise ValueError("Download refused: the release URL is not HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "ork-meter-codex-bootstrap", "Accept": "application/octet-stream"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=TIMEOUT) as response:
            chunks, total = [], 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > WHEEL_LIMIT:
                    raise ValueError("Download refused: the response is larger than the release could be")
                chunks.append(chunk)
            return b"".join(chunks)
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError(f"Download failed: HTTP {error.code} for {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError(f"Download failed: {url} could not be reached; check the network and retry") from None


def python_in(environment):
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def command_in(environment):
    return environment / ("Scripts/ork-codex.exe" if os.name == "nt" else "bin/ork-codex")


def private(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def installed(data):
    """The installed command for the pinned release, or None. No network."""
    directory = package_dir(data)
    record, command = directory / "installed.json", command_in(directory / "venv")
    try:
        recorded = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    if recorded.get("sha256") != data["sha256"] or recorded.get("wheel") != data["wheel"] or not command.is_file():
        return None
    return command


def install(data, log=print):
    base = base_url(data)
    url = f"{base}/{data['version']}/{data['wheel']}"
    log(f"Ork Meter for Codex {data['version']}: installing from {url}")
    body = fetch(url, base)
    actual = hashlib.sha256(body).hexdigest()
    if actual != data["sha256"]:
        raise ValueError("Download refused: the wheel's SHA-256 does not match release.json; nothing was installed")
    private(codex_home() / "ork-meter-codex")
    private(codex_home() / "ork-meter-codex" / "package")
    directory = private(package_dir(data))
    wheel = directory / data["wheel"]
    fd, staging = tempfile.mkstemp(prefix=".wheel-", dir=directory)
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
    if hashlib.sha256(Path(staging).read_bytes()).hexdigest() != data["sha256"]:
        Path(staging).unlink(missing_ok=True)
        raise ValueError("Install refused: the written wheel does not match release.json")
    os.replace(staging, wheel)
    environment = directory / "venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    pip = [str(python_in(environment)), "-I", "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
           "--only-binary=:all:", "--quiet", str(wheel)]
    if os.environ.get("ORK_CODEX_PIP_INDEX"):
        pip += ["--index-url", os.environ["ORK_CODEX_PIP_INDEX"]]
    result = subprocess.run(pip, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError("Install failed: pip could not install the wheel and its dependencies:\n" + result.stderr.strip())
    command = command_in(environment)
    if not command.is_file():
        raise ValueError("Install failed: the wheel installed no ork-codex command")
    (directory / "installed.json").write_text(json.dumps({"version": data["version"], "wheel": data["wheel"], "sha256": data["sha256"],
                                                          "source": url}, indent=2) + "\n", encoding="utf-8")
    wheel.unlink(missing_ok=True)
    return command


def running():
    return "%d.%d" % sys.version_info[:2]


def probe(command):
    """The interpreter path and X.Y version that `command` runs, or None."""
    try:
        result = subprocess.run([*command, "-I", "-c", "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2])"],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = result.stdout.split()
    if result.returncode != 0 or len(lines) != 2:
        return None
    return lines[0], lines[1]


def candidates(version):
    if os.name == "nt" and shutil.which("py"):
        yield ["py", f"-{version}"]
    found = shutil.which(f"python{version}")
    if found:
        yield [found]
    uv = shutil.which("uv")
    if uv:
        try:
            result = subprocess.run([uv, "python", "find", "--no-project", version], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return
        if result.returncode == 0 and result.stdout.strip():
            yield [result.stdout.strip().splitlines()[-1]]


def supported_python(data):
    """A supported interpreter's path, preferring release.json's order, or None."""
    for version in data["python"]:
        for command in candidates(version):
            found = probe(command)
            if found and found[1] == version:
                return found[0]
    return None


def no_python_message(data):
    wanted = data["python"][0]
    listed = ", ".join(data["python"])
    if sys.platform == "darwin":
        hint = f"brew install python@{wanted}  (or download it from https://www.python.org/downloads/)"
    elif os.name == "nt":
        hint = f"winget install Python.Python.{wanted}  (or download it from https://www.python.org/downloads/)"
    else:
        hint = f"install python{wanted} with your package manager, or run: uv python install {wanted}"
    return (f"Ork Meter for Codex {data['version']} needs Python {listed}; this computer runs {running()} and has none of those.\n"
            f"Install one alongside your current Python, then run the same command again:\n  {hint}")


def run(command, argv):
    if os.name == "nt":
        return subprocess.run([str(command), *argv]).returncode
    os.execv(str(command), [str(command), *argv])


def main(argv):
    try:
        data = release()
        command = installed(data)
        if command is None and running() not in data["python"]:
            if os.environ.get(REEXEC):
                raise ValueError(no_python_message(data))
            python = supported_python(data)
            if python is None:
                raise ValueError(no_python_message(data))
            print(f"Ork Meter for Codex: using Python at {python}", file=sys.stderr, flush=True)
            os.environ[REEXEC] = "1"
            return run(python, ["-I", "-B", str(Path(__file__).resolve()), *argv[1:]])
        command = command or install(data)
    except (OSError, ValueError) as error:
        print(f"ork-meter-codex: {error}", file=sys.stderr)
        return 1
    return run(command, argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
