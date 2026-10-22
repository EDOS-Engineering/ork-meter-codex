"""Setup obtains the helper from a release it can verify, or installs nothing.

A loopback HTTP fixture stands in for the GitHub Releases API; no test reaches
the network, GitHub or a real release.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import threading
import unittest
from unittest.mock import patch

from ork_codex import helper, meter
from ork_codex import onboarding
from workspace_temp import WorkspaceTemp as TemporaryDirectory

VERSION = helper.version()
TARGET = helper.target()
ASSET = helper.asset_name(TARGET)
HELPER_BYTES = b"#!/bin/sh\necho synthetic helper\n"


class Release:
    """One synthetic release: a listing, a manifest and one helper asset."""

    def __init__(self, body=HELPER_BYTES, manifest_version=VERSION, redirect=True):
        self.body = body
        self.manifest = {"plugin": "ork-meter-codex", "plugin_version": manifest_version, "tag": "v" + manifest_version,
                         "ork_meter_ref": "v9.9.9", "ork_meter_commit": "f" * 40,
                         "assets": {ASSET: hashlib.sha256(HELPER_BYTES).hexdigest()}}
        self.redirect = redirect
        self.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def handler(release):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, code, body, content_type="application/octet-stream"):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                release.requests.append((self.path, self.headers.get("Authorization")))
                if self.path == f"/repos/EDOS-Engineering/ork-meter-codex/releases/tags/v{VERSION}":
                    listing = {"html_url": release.url + "/release-page", "assets": [
                        {"name": "manifest.json", "url": release.url + "/assets/1"},
                        {"name": ASSET, "url": release.url + "/assets/2"},
                        {"name": "unrelated.whl", "url": release.url + "/assets/3"}]}
                    return self.send(200, json.dumps(listing).encode(), "application/json")
                if self.path == "/assets/1":
                    return self.send(200, json.dumps(release.manifest).encode())
                if self.path == "/assets/2":
                    if release.redirect:
                        self.send_response(302)
                        self.send_header("Location", "/storage/" + ASSET)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return None
                    return self.send(200, release.body)
                if self.path == "/storage/" + ASSET:
                    return self.send(200, release.body)
                return self.send(404, b"missing")
        return Handler


class HelperInstall(unittest.TestCase):
    def test_version_uses_distribution_metadata_without_a_checkout(self):
        with TemporaryDirectory() as temp:
            site_packages = Path(temp) / "site-packages"
            site_packages.mkdir()
            with patch.object(helper, "ROOT", site_packages), \
                 patch.object(helper.metadata, "version", return_value=VERSION) as installed_version:
                self.assertEqual(helper.version(), VERSION)
                self.assertEqual(helper.install_dir(Path(temp)),
                                 Path(temp) / "ork-meter-codex" / "helper" / VERSION)
                installed_version.assert_called_with("ork-meter-codex")
                # Repository installs keep their checked-out version even when
                # another distribution version is installed in the environment.
                (site_packages / "plugin.json").write_text('{"version":"9.8.7"}')
                installed_version.reset_mock()
                self.assertEqual(helper.version(), "9.8.7")
                installed_version.assert_not_called()

    def run_install(self, release, home):
        with patch.dict(os.environ, {"ORK_CODEX_RELEASE_API": release.url, "GH_TOKEN": "synthetic-token", "CODEX_HOME": str(home)}):
            return helper.install(home)

    def test_host_target_maps_to_a_published_asset_name(self):
        self.assertRegex(TARGET, r"^(darwin|linux)-(arm64|amd64)$|^windows-amd64$")
        self.assertEqual(ASSET, "ork-meter-" + TARGET + (".exe" if TARGET.startswith("windows") else ""))
        with patch.object(helper.platform, "system", return_value="FreeBSD"):
            with self.assertRaises(ValueError):
                helper.target()

    def test_verified_download_installs_owner_only_and_drops_the_credential_on_redirect(self):
        release = Release()
        self.addCleanup(release.close)
        with TemporaryDirectory() as temp:
            home = Path(temp) / "codex-home"
            home.mkdir()
            path = self.run_install(release, home)
            self.assertEqual(path, home / "ork-meter-codex" / "helper" / VERSION / helper.binary_name())
            self.assertEqual(path.read_bytes(), HELPER_BYTES)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((home / "ork-meter-codex").stat().st_mode), 0o700)
            record = json.loads((path.parent / "helper.json").read_text())
            self.assertEqual(record["sha256"], hashlib.sha256(HELPER_BYTES).hexdigest())
            self.assertEqual(record["plugin_version"], VERSION)
            self.assertEqual(record["ork_meter_ref"], "v9.9.9")
            self.assertEqual(record["asset"], ASSET)
            self.assertFalse([p for p in path.parent.iterdir() if p.name.startswith(".helper-")], "no staging file left")
            authorized = {p: a for p, a in release.requests}
            self.assertEqual(authorized["/assets/2"], "Bearer synthetic-token")
            self.assertIsNone(authorized["/storage/" + ASSET], "the API credential must not follow the storage redirect")
            self.assertNotIn("/assets/3", authorized, "only the manifest and the host asset are fetched")
            # Installed and verified: later lookups make no request.
            count = len(release.requests)
            self.assertEqual(helper.installed(home), path)
            self.assertEqual(self.run_install(release, home), path)
            self.assertEqual(len(release.requests), count)
            with patch.dict(os.environ, {"CODEX_HOME": str(home)}, clear=False), \
                 patch.object(meter, "ROOT", Path(temp) / "no-checkout"), \
                 patch.object(meter.shutil, "which", return_value=None):
                os.environ.pop("ORK_METER_BINARY", None)
                self.assertEqual(meter.binary(), str(path))
            # A corrupted installed helper is not trusted and is replaced.
            path.write_bytes(b"corrupted\n")
            self.assertIsNone(helper.installed(home))
            self.assertEqual(self.run_install(release, home).read_bytes(), HELPER_BYTES)

    def test_digest_mismatch_wrong_manifest_and_missing_release_install_nothing(self):
        cases = {"tampered asset": Release(body=b"not the reviewed helper\n"),
                 "manifest for another version": Release(manifest_version="0.0.0"),
                 "unreachable release": None}
        for name, release in cases.items():
            with self.subTest(case=name), TemporaryDirectory() as temp:
                home = Path(temp) / "codex-home"
                home.mkdir()
                if release is None:
                    release = Release()
                    release.close()
                else:
                    self.addCleanup(release.close)
                with self.assertRaises(ValueError):
                    self.run_install(release, home)
                self.assertFalse((home / "ork-meter-codex" / "helper" / VERSION / helper.binary_name()).exists())
                self.assertIsNone(helper.installed(home))
                with patch.dict(os.environ, {"CODEX_HOME": str(home)}), \
                     patch.object(meter, "ROOT", Path(temp) / "no-checkout"), \
                     patch.object(meter.shutil, "which", return_value=None):
                    os.environ.pop("ORK_METER_BINARY", None)
                    with self.assertRaises(ValueError):
                        meter.binary()

    def test_plain_http_release_api_is_refused_unless_loopback(self):
        with patch.dict(os.environ, {"ORK_CODEX_RELEASE_API": "http://example.invalid"}):
            with self.assertRaises(ValueError):
                helper.release(VERSION)

    def test_setup_installs_the_release_helper_only_when_no_helper_is_available(self):
        with TemporaryDirectory() as temp:
            home = Path(temp)
            with patch.object(onboarding, "binary", return_value="dev-helper"), \
                 patch.object(onboarding.release_helper, "install") as install:
                self.assertEqual(onboarding.obtain_helper(home), "dev-helper")
                install.assert_not_called()
            with patch.object(onboarding, "binary", side_effect=ValueError("unavailable")), \
                 patch.object(onboarding.release_helper, "install", return_value=home / "released") as install, \
                 patch("builtins.print"):
                self.assertEqual(onboarding.obtain_helper(home), str(home / "released"))
                install.assert_called_once_with(home)
            with patch.dict(os.environ, {"ORK_METER_BINARY": str(home / "missing")}), \
                 patch.object(onboarding, "binary", side_effect=ValueError("explicit path invalid")), \
                 patch.object(onboarding.release_helper, "install") as install:
                with self.assertRaises(ValueError):
                    onboarding.obtain_helper(home)
                install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
