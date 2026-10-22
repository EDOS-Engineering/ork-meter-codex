"""Obtain the reviewed Ork Meter helper for this computer from the plugin's release.

The helper (ork-meter's cmd/ork-meter) is built and published by the
package-release workflow as GitHub Release v<plugin version> on
EDOS-Engineering/ork-meter-codex, one asset per platform plus a manifest.json
that records each asset's SHA-256 (docs/release.md). Setup downloads the
asset for the host platform, verifies it against that manifest and installs
it owner-only under the profile's ork-meter-codex directory. Nothing is built
on the customer's machine and nothing is executed before its digest matches.

The digest check guards against a corrupt or altered download. It does not by
itself prove provenance: the manifest and the asset come from the same
release, whose integrity rests on the reviewed release workflow, the tag and
the build provenance attestation on the release page. Standard library only.
"""
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from .storage import private_directory

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "EDOS-Engineering/ork-meter-codex"
API = "https://api.github.com"
HELPER = "ork-meter"
RECORD = "helper.json"
MANIFEST_LIMIT = 64 * 1024
HELPER_LIMIT = 96 * 1024 * 1024
TIMEOUT = 60
DIGEST = re.compile(r"[a-f0-9]{64}")


def target():
    system = platform.system().lower()
    machine = platform.machine().lower()
    target_os = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(system)
    target_arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(machine)
    if target_os is None or target_arch is None or (target_os == "windows" and target_arch != "amd64"):
        raise ValueError(f"No reviewed Ork Meter helper is published for {system}/{machine}; set ORK_METER_BINARY to a helper you built")
    return f"{target_os}-{target_arch}"


def asset_name(target_name):
    return f"{HELPER}-{target_name}" + (".exe" if target_name.startswith("windows") else "")


def binary_name():
    return HELPER + (".exe" if os.name == "nt" else "")


def version():
    try:
        manifest = (ROOT / "plugin.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        # Wheels contain package metadata rather than repository manifests.
        return metadata.version("ork-meter-codex")
    return json.loads(manifest)["version"]


def base(home):
    return Path(home).absolute() / "ork-meter-codex"


def install_dir(home, plugin_version=None):
    return base(home) / "helper" / (plugin_version or version())


def installed(home, plugin_version=None):
    """The verified installed helper for the pinned version, or None. No network."""
    directory = install_dir(home, plugin_version)
    path, record = directory / binary_name(), directory / RECORD
    if not path.is_file() or path.is_symlink() or not record.is_file():
        return None
    try:
        expected = json.loads(record.read_text(encoding="utf-8")).get("sha256")
    except (ValueError, UnicodeError, OSError):
        return None
    if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
        return None
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        return None
    return path


def _api():
    return os.environ.get("ORK_CODEX_RELEASE_API", API).rstrip("/")


def _token():
    # The repository is private while the plugin is pre-production, so a read
    # token is needed; a public release downloads anonymously.
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


def _allowed(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https":
        return True
    api = urllib.parse.urlparse(_api())
    return parsed.scheme == "http" and api.scheme == "http" and parsed.hostname == "127.0.0.1" == api.hostname


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _fetch(url, accept, limit, authenticated=True):
    """GET url with at most one redirect; credentials never follow the redirect."""
    if not _allowed(url):
        raise ValueError("Helper download refused: release URL is not HTTPS")
    headers = {"Accept": accept, "User-Agent": "ork-meter-codex", "X-GitHub-Api-Version": "2022-11-28"}
    token = _token()
    if authenticated and token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(request, timeout=TIMEOUT) as response:
            return _read(response, limit)
    except urllib.error.HTTPError as error:
        if error.code in (301, 302, 303, 307, 308) and authenticated:
            location = error.headers.get("Location", "")
            error.close()
            if not location:
                raise ValueError("Helper download failed: redirect without a destination") from None
            # GitHub serves assets from a storage host with a signed URL; the
            # API credential must not be sent there.
            return _fetch(urllib.parse.urljoin(url, location), accept, limit, authenticated=False)
        error.close()
        if error.code in (401, 403, 404):
            raise ValueError("Helper release unavailable: the plugin release could not be read. If the repository or release is private, set GH_TOKEN to a token that can read EDOS-Engineering/ork-meter-codex") from None
        raise ValueError(f"Helper download failed with HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("Helper download failed: the release could not be reached; check the network and retry setup") from None


def _read(response, limit):
    chunks, total = [], 0
    while True:
        chunk = response.read(1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise ValueError("Helper download refused: the response is larger than the release could be")
        chunks.append(chunk)


def release(plugin_version):
    body = _fetch(f"{_api()}/repos/{REPOSITORY}/releases/tags/v{plugin_version}", "application/vnd.github+json", MANIFEST_LIMIT * 16)
    try:
        data = json.loads(body)
        assets = {asset["name"]: asset["url"] for asset in data["assets"]}
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise ValueError("Helper release unavailable: the release listing could not be read") from None
    return data.get("html_url", ""), assets


def manifest(url, plugin_version):
    try:
        data = json.loads(_fetch(url, "application/octet-stream", MANIFEST_LIMIT))
        assets = data["assets"]
        if data["plugin_version"] != plugin_version or data.get("plugin") != "ork-meter-codex":
            raise ValueError
        if not all(isinstance(k, str) and isinstance(v, str) and DIGEST.fullmatch(v) for k, v in assets.items()):
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeError, AttributeError):
        raise ValueError(f"Helper release refused: manifest.json does not describe plugin version {plugin_version}") from None
    return data


def install(home, plugin_version=None):
    """Download, verify and install the host's helper. Returns its path."""
    plugin_version = plugin_version or version()
    found = installed(home, plugin_version)
    if found:
        return found
    name = asset_name(target())
    html_url, assets = release(plugin_version)
    if "manifest.json" not in assets or name not in assets:
        raise ValueError(f"Helper release refused: v{plugin_version} has no {name} or no manifest.json")
    record = manifest(assets["manifest.json"], plugin_version)
    expected = record["assets"].get(name)
    if not expected:
        raise ValueError(f"Helper release refused: manifest.json has no digest for {name}")
    body = _fetch(assets[name], "application/octet-stream", HELPER_LIMIT)
    actual = hashlib.sha256(body).hexdigest()
    if actual != expected:
        raise ValueError("Helper download refused: SHA-256 does not match the release manifest; nothing was installed")
    private_directory(base(home))
    private_directory(base(home) / "helper")
    directory = private_directory(install_dir(home, plugin_version))
    path = directory / binary_name()
    fd, staging = tempfile.mkstemp(prefix=".helper-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(body)
            out.flush()
            os.fsync(out.fileno())
        if hashlib.sha256(Path(staging).read_bytes()).hexdigest() != expected:
            raise ValueError("Helper install refused: the written file does not match the release manifest")
        if os.name != "nt":
            os.chmod(staging, 0o700)
        os.replace(staging, path)
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise
    evidence = {"plugin_version": plugin_version, "asset": name, "sha256": expected,
                "ork_meter_ref": record.get("ork_meter_ref"), "ork_meter_commit": record.get("ork_meter_commit"),
                "release": html_url, "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    record_path = directory / RECORD
    record_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        record_path.chmod(0o600)
    return path
