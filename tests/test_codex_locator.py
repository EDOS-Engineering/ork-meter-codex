"""Setup locates the native Codex CLI beyond PATH; fixtures are synthetic files."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from ork_codex import onboarding as setup
from ork_codex import cli
from workspace_temp import WorkspaceTemp as TemporaryDirectory


class FakeOs:
    """The onboarding module's view of ``os`` with a different platform name."""

    def __init__(self, name):
        self.name = name

    def __getattr__(self, attribute):
        return getattr(os, attribute)


def tool(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\n")
    path.chmod(0o755)
    return path


def resolved(path):
    return str(Path(path).resolve())


def config_with(home, value, server="node_repl"):
    Path(home).mkdir(parents=True, exist_ok=True)
    (Path(home) / "config.toml").write_text(
        f'model = "x"\n\n[mcp_servers.{server}]\ncommand = "node"\n\n'
        f'[mcp_servers.{server}.env]\nCODEX_CLI_PATH = {value!r}\n'.replace("'", '"'),
        encoding="utf-8")


class Locator(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        environ = {k: v for k, v in os.environ.items()
                   if k not in ("CODEX_CLI_PATH", "NPM_CONFIG_PREFIX", "npm_config_prefix")}
        for patcher in (patch.dict(os.environ, environ, clear=True),
                        patch.object(setup.shutil, "which", return_value=None),
                        patch.object(setup, "_well_known_codex", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)

    def test_explicit_option_wins_over_every_other_source(self):
        explicit, env = tool(self.root / "explicit/codex"), tool(self.root / "env/codex")
        config_with(self.home, str(tool(self.root / "config/codex")))
        os.environ["CODEX_CLI_PATH"] = str(env)
        with patch.object(setup.shutil, "which", return_value=str(tool(self.root / "path/codex"))):
            self.assertEqual(setup.native_codex(str(explicit), self.home), resolved(explicit))

    def test_explicit_option_that_is_not_executable_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "--codex .* is not an executable"):
            setup.native_codex(str(self.root / "missing"), self.home)
        with self.assertRaisesRegex(ValueError, "--codex .* is not an executable"):
            setup.native_codex(str(self.root), self.home)

    def test_environment_variable_wins_over_config_and_path(self):
        env = tool(self.root / "env/codex")
        config_with(self.home, str(tool(self.root / "config/codex")))
        os.environ["CODEX_CLI_PATH"] = str(env)
        with patch.object(setup.shutil, "which", return_value=str(tool(self.root / "path/codex"))):
            self.assertEqual(setup.native_codex(None, self.home), resolved(env))

    def test_environment_variable_that_is_not_executable_is_an_error(self):
        os.environ["CODEX_CLI_PATH"] = str(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "CODEX_CLI_PATH=.* is not an executable"):
            setup.native_codex(None, self.home)

    def test_config_value_recorded_by_desktop_app_wins_over_path(self):
        recorded = tool(self.root / "config/codex")
        config_with(self.home, str(recorded))
        with patch.object(setup.shutil, "which", return_value=str(tool(self.root / "path/codex"))):
            self.assertEqual(setup.native_codex(None, self.home), resolved(recorded))

    def test_config_value_under_any_mcp_server_env_is_found(self):
        recorded = tool(self.root / "config/codex")
        config_with(self.home, str(recorded), server="other")
        self.assertEqual(setup.native_codex(None, self.home), resolved(recorded))

    def test_config_home_defaults_to_codex_home_environment(self):
        recorded = tool(self.root / "config/codex")
        config_with(self.home, str(recorded))
        os.environ["CODEX_HOME"] = str(self.home)
        self.assertEqual(setup.native_codex(), resolved(recorded))

    def test_stale_or_invalid_config_falls_through_to_path(self):
        on_path = str(tool(self.root / "path/codex"))
        for content in (None, "not = [valid", 'CODEX_CLI_PATH = "/top/level/is/not/where/the/app/writes"\n'):
            with self.subTest(content=content):
                if content is None:
                    config_with(self.home, str(self.root / "gone/codex"))
                else:
                    (self.home / "config.toml").write_text(content, encoding="utf-8")
                with patch.object(setup.shutil, "which", return_value=on_path):
                    self.assertEqual(setup.native_codex(None, self.home), on_path)

    def test_path_lookup_is_unchanged_on_posix(self):
        on_path = str(tool(self.root / "path/codex"))
        with patch.object(setup, "os", FakeOs("posix")), \
             patch.object(setup.shutil, "which", return_value=on_path) as which:
            self.assertEqual(setup.native_codex(None, self.home), on_path)
        which.assert_called_once_with("codex")

    def test_well_known_locations_are_tried_after_path_in_order(self):
        missing, second, third = self.root / "a/codex", tool(self.root / "b/codex"), tool(self.root / "c/codex")
        with patch.object(setup, "_well_known_codex", return_value=[missing, second, third]):
            self.assertEqual(setup.native_codex(None, self.home), resolved(second))

    def test_well_known_location_must_be_an_executable_regular_file(self):
        directory = self.root / "dir/codex"
        directory.mkdir(parents=True)
        plain = self.root / "plain/codex"
        plain.parent.mkdir()
        plain.write_bytes(b"")
        candidates = [directory]
        if os.name != "nt":  # Windows reports every existing file as executable
            plain.chmod(0o644)
            candidates.append(plain)
        with patch.object(setup, "_well_known_codex", return_value=candidates):
            with self.assertRaisesRegex(ValueError, "Codex CLI executable unavailable"):
                setup.native_codex(None, self.home)

    @unittest.skipIf(os.name == "nt", "symlink fixture requires POSIX")
    def test_symlinks_resolve_to_the_executable_target(self):
        target = tool(self.root / "real/codex")
        link = self.root / "link/codex"
        link.parent.mkdir()
        link.symlink_to(target)
        dangling = self.root / "dangling/codex"
        dangling.parent.mkdir()
        dangling.symlink_to(self.root / "nowhere")
        with patch.object(setup, "_well_known_codex", return_value=[dangling, link]):
            self.assertEqual(setup.native_codex(None, self.home), resolved(target))

    def test_well_known_location_list_per_platform(self):
        user = self.root / "user"
        os.environ["NPM_CONFIG_PREFIX"] = str(self.root / "npm")
        real = Locator._real_well_known
        with patch.object(setup.Path, "home", return_value=user), patch.object(setup, "os", FakeOs("posix")):
            with patch.object(setup.sys, "platform", "darwin"):
                mac = [str(p) for p in real()]
            with patch.object(setup.sys, "platform", "linux"):
                linux = [str(p) for p in real()]
        app = setup.MACOS_APP_CODEX
        self.assertEqual(mac, [str(Path("/Applications") / app), str(user / "Applications" / app),
                               str(Path("/opt/homebrew/bin/codex")), str(Path("/usr/local/bin/codex")),
                               str(self.root / "npm/bin/codex"), str(user / ".npm-global/bin/codex")])
        self.assertEqual(linux, mac[2:])
        with patch.object(setup, "os", FakeOs("nt")):
            self.assertEqual(real(), [])

    def test_windows_path_and_npm_launcher_behaviour_is_unchanged(self):
        launcher_dir = self.root / "npm"
        exe = tool(launcher_dir / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64"
                   "/vendor/x86_64-pc-windows-msvc/bin/codex.exe")
        calls = []

        def which(name, table):
            calls.append(name)
            return table.get(name)

        with patch.object(setup, "os", FakeOs("nt")):
            with patch.object(setup.shutil, "which", side_effect=lambda n: which(n, {"codex.exe": "C:/codex.exe"})):
                self.assertEqual(setup.native_codex(None, self.home), "C:/codex.exe")
            self.assertEqual(calls, ["codex.exe"])
            calls.clear()
            with patch.object(setup.shutil, "which",
                              side_effect=lambda n: which(n, {"codex.cmd": str(launcher_dir / "codex.cmd")})):
                self.assertEqual(setup.native_codex(None, self.home), str(exe))
            self.assertEqual(calls, ["codex.exe", "codex.cmd"])
            with patch.object(setup.shutil, "which", return_value=None), \
                 patch.object(setup, "_well_known_codex", Locator._real_well_known):
                with self.assertRaisesRegex(ValueError, "install the Codex CLI first"):
                    setup.native_codex(None, self.home)

    def test_error_lists_where_it_looked_and_how_to_point_at_the_cli(self):
        config_with(self.home, str(self.root / "gone/codex"))
        missing = self.root / "brew/codex"
        with patch.object(setup, "_well_known_codex", return_value=[missing]):
            with self.assertRaises(ValueError) as raised:
                setup.native_codex(None, self.home)
        message = str(raised.exception)
        self.assertTrue(message.startswith("Codex CLI executable unavailable; install the Codex CLI first"))
        for expected in ("CODEX_CLI_PATH environment variable", str(self.home / "config.toml"),
                         "not executable", "PATH", str(missing), "--codex PATH or CODEX_CLI_PATH"):
            self.assertIn(expected, message)

    def test_setup_cli_passes_codex_option(self):
        with patch.object(setup, "setup", return_value=0) as run, self.assertRaises(SystemExit):
            cli.main(["setup", "--codex", "/opt/codex"])
        self.assertEqual(run.call_args.kwargs["codex"], "/opt/codex")


Locator._real_well_known = staticmethod(setup._well_known_codex)


if __name__ == "__main__":
    unittest.main()
