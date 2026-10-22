"""Interactive account setup. Execute in the user's terminal, never via MCP.

Claim input belongs to the existing helper. Configuration is leased only for
the owned Codex session; originals and unrelated settings remain recoverable.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import uuid
import webbrowser
import tomllib

from . import entitlement
from . import helper as release_helper
from .meter import ROOT, binary, reference
from .auto_hook import install as install_auto_hook
from .storage import private_directory


MACOS_APP_CODEX = "ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"


def _executable(candidate):
    """Return the resolved path when candidate is an executable regular file."""
    try:
        resolved = Path(candidate).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved.is_file() and os.access(resolved, os.X_OK):
        return str(resolved)
    return None


def _configured_codex(home):
    """CODEX_CLI_PATH as the ChatGPT desktop app records it in Codex configuration.

    The app writes it into an MCP server environment table (for example
    ``[mcp_servers.node_repl.env]``), not at the top level.
    """
    try:
        config = tomllib.loads(read_config(Path(home) / "config.toml").decode("utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        return None
    names = sorted(servers, key=lambda name: name != "node_repl")
    for name in names:
        server = servers[name]
        env = server.get("env") if isinstance(server, dict) else None
        value = env.get("CODEX_CLI_PATH") if isinstance(env, dict) else None
        if isinstance(value, str) and value:
            return value
    return None


def _well_known_codex():
    if os.name == "nt":
        return []
    home = Path.home()
    places = []
    if sys.platform == "darwin":
        places += [Path("/Applications") / MACOS_APP_CODEX, home / "Applications" / MACOS_APP_CODEX]
    places += [Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex")]
    prefix = os.environ.get("NPM_CONFIG_PREFIX") or os.environ.get("npm_config_prefix")
    if prefix:
        places.append(Path(prefix) / "bin/codex")
    places.append(home / ".npm-global/bin/codex")
    return places


def native_codex(explicit=None, home=None):
    """Locate the native Codex CLI.

    Order: ``--codex PATH``; ``CODEX_CLI_PATH``; ``CODEX_CLI_PATH`` recorded in
    ``$CODEX_HOME/config.toml``; PATH (with the Windows npm launcher fallback);
    then well-known install locations. An explicit or environment path that is
    not an executable file is an error rather than silently skipped.
    """
    if explicit:
        found = _executable(explicit)
        if not found:
            raise ValueError(f"--codex {explicit} is not an executable Codex CLI file")
        return found
    configured = os.environ.get("CODEX_CLI_PATH")
    if configured:
        found = _executable(configured)
        if not found:
            raise ValueError(f"CODEX_CLI_PATH={configured} is not an executable Codex CLI file")
        return found
    if home is None:
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    looked = ["--codex", "CODEX_CLI_PATH environment variable"]
    recorded = _configured_codex(home)
    looked.append(f"CODEX_CLI_PATH in {Path(home) / 'config.toml'}" +
                  (f" ({recorded}, not executable)" if recorded else ""))
    if recorded:
        found = _executable(recorded)
        if found:
            return found
    found = shutil.which("codex.exe" if os.name == "nt" else "codex")
    if found:
        return found
    looked.append("PATH")
    if os.name == "nt":
        launcher = shutil.which("codex.cmd")
        if launcher:
            vendor = Path(launcher).parent / "node_modules/@openai/codex/node_modules"
            candidates = list(vendor.glob("@openai/codex-win32-*/vendor/*/bin/codex.exe"))
            if len(candidates) == 1:
                return str(candidates[0])
    for place in _well_known_codex():
        found = _executable(place)
        if found:
            return found
        looked.append(str(place))
    raise ValueError("Codex CLI executable unavailable; install the Codex CLI first. Looked in: " +
                     "; ".join(looked) + ". Point setup at it with --codex PATH or CODEX_CLI_PATH.")


def public_result(argv):
    result = subprocess.run(argv, capture_output=True, timeout=45)
    if result.returncode:
        match = re.search(rb"Recovery reference: ([A-Za-z0-9_-]+)", result.stderr)
        suffix = f"; recovery reference: {match[1].decode()}" if match else ""
        raise ValueError("Setup operation failed; existing credentials were not replaced" + suffix)
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise ValueError("Setup response unavailable; no delivery is implied") from None


def no_telemetry(raw):
    try:
        config = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ValueError("Codex configuration is invalid; setup did not overwrite it") from None
    profiles = config.get("profiles", {})
    if "otel" in config or (isinstance(profiles, dict) and
                            any(isinstance(p, dict) and "otel" in p for p in profiles.values())):
        raise ValueError("Existing Codex telemetry needs a deliberate merge; setup will not replace it")
    selected = config.get("profile")
    return selected


def read_config(path):
    if path.is_symlink():
        raise ValueError("Codex configuration must be a regular file")
    raw = path.read_bytes() if path.exists() else b""
    if len(raw) > 1024 * 1024:
        raise ValueError("Codex configuration is too large for automatic setup")
    return raw


def validate_fragment(raw):
    """Accept the helper's exact local-counter routing, never arbitrary TOML."""
    try:
        config = tomllib.loads(raw.decode("utf-8"))
        otel = config["otel"]
        exporter = otel["exporter"]["otlp-http"]
        valid = (set(config) == {"otel"} and
                 set(otel) == {"log_user_prompt", "metrics_exporter", "trace_exporter", "exporter"} and
                 otel["log_user_prompt"] is False and
                 otel["metrics_exporter"] == otel["trace_exporter"] == "none" and
                 set(otel["exporter"]) == {"otlp-http"} and
                 set(exporter) == {"endpoint", "protocol", "headers"} and
                 re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/v1/logs", exporter["endpoint"]))
        if not valid:
            raise ValueError("invalid fragment")
        port = int(exporter["endpoint"].split(":")[2].split("/")[0])
        if not (0 < port < 65536 and exporter["protocol"] == "json" and
                set(exporter["headers"]) == {"Authorization"} and
                re.fullmatch(r"Bearer [a-f0-9]{64}", exporter["headers"]["Authorization"])):
            raise ValueError("invalid fragment")
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise ValueError("Helper telemetry fragment rejected; Codex configuration was not changed") from None


class ConfigLease:
    def __init__(self, config, state):
        self.config, self.state = Path(config), Path(state)
        self.before = read_config(self.config)
        self.existed = self.config.exists()
        no_telemetry(self.before)
        self.block = None
        self.backup = self.state / "config-before.toml"

    def publish(self, raw):
        # Private staging inherits the state directory's narrow Windows ACL.
        temp = self.state / ("config-" + uuid.uuid4().hex + ".toml")
        try:
            with temp.open("xb") as out:
                if os.name != "nt":
                    temp.chmod(0o600)
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, self.config)
        finally:
            temp.unlink(missing_ok=True)

    def install(self, fragment):
        validate_fragment(fragment)
        if read_config(self.config) != self.before:
            raise ValueError("Codex configuration changed during setup; no settings were replaced")
        with self.backup.open("xb") as out:
            if os.name != "nt":
                self.backup.chmod(0o600)
            out.write(self.before)
            out.flush()
            os.fsync(out.fileno())
        marker = uuid.uuid4().hex.encode()
        block = (b"\n# Ork Meter session " + marker + b" begin\n" + fragment +
                 b"\n# Ork Meter session " + marker + b" end\n")
        candidate = self.before + block
        tomllib.loads(candidate.decode("utf-8"))
        # Save cleanup identity before publishing; interruption cannot strand
        # an installed config merely because the next Python line did not run.
        if read_config(self.config) != self.before:
            raise ValueError("Codex configuration changed during setup; no settings were replaced")
        self.block = block
        self.publish(candidate)

    def restore(self):
        if self.block is None:
            return True
        current = read_config(self.config)
        if current == self.before:
            return True
        if current.count(self.block) != 1:
            return False
        restored = current.replace(self.block, b"", 1)
        no_telemetry(restored)
        if not self.existed and not restored:
            self.config.unlink()
        else:
            self.publish(restored)
        return True


def install_plugin(codex, state, helper):
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).absolute()
    config = tomllib.loads(read_config(home / "config.toml").decode("utf-8"))
    existing = config.get("marketplaces", {}).get("edos-codex")
    version = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["version"]
    package = state.parent / "package"
    if existing:
        source = Path(existing.get("source", ""))
        try:
            is_checkout = existing.get("source_type") == "local" and source.samefile(ROOT)
            is_owned = existing.get("source_type") == "local" and package.exists() and source.samefile(package)
        except OSError:
            is_checkout = is_owned = False
        if is_checkout:
            cached = home / "plugins/cache/edos-codex/ork-meter-codex" / version
            bundled = cached / ".local" / ("ork-meter.exe" if os.name == "nt" else "ork-meter")
            current = (bundled.is_file() and hashlib.sha256(bundled.read_bytes()).digest() == hashlib.sha256(Path(helper).read_bytes()).digest())
            # Compare every shipped runtime file: a fixed subset can leave
            # entitlement or tracking fixes behind in an otherwise current cache.
            runtime = ROOT / "ork_codex"
            for new in runtime.rglob("*"):
                name = new.relative_to(runtime)
                if not new.is_file() or "__pycache__" in name.parts or new.suffix == ".pyc":
                    continue
                old = cached / "ork_codex" / name
                if old.is_file():
                    old_bytes, new_bytes = old.read_bytes(), new.read_bytes()
                    if name.suffix == ".py":
                        old_bytes, new_bytes = old_bytes.replace(b"\r\n", b"\n"), new_bytes.replace(b"\r\n", b"\n")
                    current = current and old_bytes == new_bytes
                else:
                    current = False
            current = current and not (cached / "skills").exists() and not (cached / "hooks/hooks.json").exists()
            current = current and not (cached / ".mcp.json").exists() and not (cached / "mcp.json").exists()
            if current and config.get("plugins", {}).get("ork-meter-codex@edos-codex", {}).get("enabled"):
                print("Using the existing installed plugin and verified helper.", flush=True)
                return
        if not is_owned and not is_checkout:
            raise ValueError("The business marketplace uses another source; setup will not replace it")
    package.mkdir(exist_ok=True)
    if package.is_symlink() or not package.resolve().is_relative_to(state.parent.resolve()):
        raise ValueError("Setup package must stay inside its private directory")
    # These are generated plugin components, never user configuration or
    # runtime originals. Remove legacy discovery roots from our own snapshot.
    for name in ("skills", "hooks"):
        target = package / name
        if target.exists():
            if target.is_symlink() or not target.resolve().is_relative_to(package.resolve()):
                raise ValueError("Legacy component must stay inside the setup package")
            shutil.rmtree(target)
    for name in (".mcp.json", "mcp.json"):
        target = package / name
        if target.exists():
            if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(package.resolve()):
                raise ValueError("Legacy MCP component must stay inside the setup package")
            target.unlink()
    for name in ("plugin.json", "LICENSE"):
        shutil.copy2(ROOT / name, package / name)
    for name in (".codex-plugin", ".claude-plugin", "ork_codex", "hooks"):
        shutil.copytree(ROOT / name, package / name, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (package / "scripts").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "scripts/ork_codex.py", package / "scripts/ork_codex.py")
    (package / ".local").mkdir(exist_ok=True)
    bundled = package / ".local" / ("ork-meter.exe" if os.name == "nt" else "ork-meter")
    shutil.copy2(helper, bundled)
    if not existing:
        public_result([codex, "plugin", "marketplace", "add", str(package), "--json"])
    # Use the CLI's ordinary per-command configuration override to install
    # only the prepared components. Keep the registered marketplace unchanged;
    # never install directly from a checkout containing private runtime state.
    overrides = ["-c", "marketplaces.edos-codex.source=" + json.dumps(str(package)),
                 "-c", 'marketplaces.edos-codex.source_type="local"']
    installed = public_result([codex, *overrides, "plugin", "add", "ork-meter-codex@edos-codex", "--json"])
    cached = Path(installed["installedPath"]) / ".local" / bundled.name
    if not cached.is_file() or hashlib.sha256(cached.read_bytes()).digest() != hashlib.sha256(bundled.read_bytes()).digest():
        raise ValueError("Installed helper could not be verified; setup stopped before pairing")
    if (cached.parent.parent / "skills").exists() or (cached.parent.parent / "hooks/hooks.json").exists():
        raise ValueError("Installed plugin still exposes legacy instructions; setup stopped before pairing")


class Capture:
    def __init__(self, helper, ref):
        self.fragment = None
        self.lines = queue.Queue()
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        self.process = subprocess.Popen([helper, "collect", "capture", "--client", "codex", "--reference", ref],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", **kwargs)
        self.reader = threading.Thread(target=self.drain, daemon=True)
        self.reader.start()

    def drain(self):
        try:
            for line in self.process.stdout:
                if line.startswith("Codex telemetry fragment: "):
                    self.lines.put(line.removeprefix("Codex telemetry fragment: ").strip())
        finally:
            self.lines.put(None)

    def ready(self):
        try:
            path = self.lines.get(timeout=30)
        except queue.Empty:
            raise ValueError("Capture did not start; Codex configuration was not changed") from None
        if path is None or self.process.poll() is not None:
            raise ValueError("Capture could not start; check the account connection")
        if os.name != "nt" and len(path) > 3 and path[0].isalpha() and path[1:3] == ":\\":
            path = "/mnt/" + path[0].lower() + "/" + path[3:].replace("\\", "/")
        self.fragment = Path(path)
        if not self.fragment.is_absolute() or self.fragment.is_symlink() or self.fragment.stat().st_size > 8192:
            raise ValueError("Capture settings unavailable")
        return self.fragment.read_bytes()

    def stop(self):
        if self.process.poll() is None:
            try:
                if os.name == "nt":
                    self.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdout.close()


def obtain_helper(home):
    """An explicit, development or already installed helper; otherwise the
    reviewed release helper for this plugin version, downloaded and verified
    against the release manifest (ork_codex/helper.py)."""
    try:
        return binary()
    except ValueError:
        if os.environ.get("ORK_METER_BINARY"):
            raise
    print("Installing the reviewed Ork Meter helper for this computer from the plugin release...", flush=True)
    return str(release_helper.install(home))


def setup(dev=None, ref=None, *, new_connection=False, codex=None):
    if new_connection and ref is not None:
        raise ValueError("Choose --new-connection or --reference, not both")
    if not sys_terminal():
        raise ValueError("Run setup in a regular interactive terminal; claim codes must not go through chat or tool input")
    resume_saved = ref is None
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).absolute()
    helper, codex = obtain_helper(home), native_codex(codex, home)
    config = home / "config.toml"
    no_telemetry(read_config(config))
    selected = tomllib.loads(read_config(config).decode("utf-8")).get("profile")
    if selected:
        if not isinstance(selected, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", selected):
            raise ValueError("Selected Codex profile name is unsupported for automatic setup")
        no_telemetry(read_config(home / (str(selected) + ".config.toml")))
    base = private_directory(home / "ork-meter-codex")
    lock = base / "setup.lock"
    try:
        lock_fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError("Another setup session owns this profile; exit it before starting another") from None
    os.close(lock_fd)
    capture = lease = None
    try:
        state = private_directory(base / uuid.uuid4().hex)
        print("Installing Ork Meter for Codex...", flush=True)
        install_plugin(codex, state, helper)
        install_auto_hook(home)
        lease = ConfigLease(config, state)
        saved = base / "pairing.json"
        if dev is None:
            if saved.is_file():
                active = json.loads(saved.read_text(encoding="utf-8"))
                if type(active.get("dev")) is not bool:
                    raise ValueError("Saved pairing environment is invalid")
                dev = active["dev"]
            else:
                dev = False
        if ref is None and saved.is_file() and not new_connection:
            record = json.loads(saved.read_text(encoding="utf-8"))
            if record.get("dev") != dev:
                raise ValueError("Saved pairing belongs to another environment; supply its public reference explicitly")
            ref = reference(record["reference"])
        origin = "https://dev.ork-meter.edos.io" if dev else "https://orkmeter.com"
        if ref is not None:
            ref = reference(ref)
            pairing = public_result([helper, "pair", "show", "--reference", ref])
            if not pairing.get("approval_url", "").startswith(origin + "/pair#proposal="):
                raise ValueError("Pairing destination does not match the selected platform")
            deadline = pairing.get("acknowledged_until")
            if deadline is not None:
                try:
                    until = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
                    if until.tzinfo is None:
                        raise ValueError()
                except (AttributeError, TypeError, ValueError):
                    raise ValueError("Saved pairing expiry could not be verified; existing credentials were preserved") from None
                if until <= datetime.now(timezone.utc):
                    if not resume_saved:
                        raise ValueError("This pairing expired; rerun setup without --reference to approve a new connection")
                    print("Your saved connection expired. Approve a new connection in your browser to resume capture.", flush=True)
                    ref = None
        if ref is None:
            args = [helper, "pair", "prepare", "--source", "openai", "--client", "codex"]
            if dev:
                args += ["--origin", "https://dev.ork-meter.edos.io"]
            pairing = public_result(args)
            ref = reference(pairing["reference"])
            origin = "https://dev.ork-meter.edos.io" if dev else "https://orkmeter.com"
            url = pairing["approval_url"]
            if not url.startswith(origin + "/pair#proposal="):
                raise ValueError("Unexpected pairing destination; browser was not opened")
            # Persist only the public reference before the interactive handoff
            # so interrupted/failed claims resume the same pairing next time.
            saved.write_text(json.dumps({"reference": ref, "dev": dev}), encoding="utf-8")
            if os.name != "nt":
                saved.chmod(0o600)
            print(f"Public reference: {ref}\nCompare browser fingerprint: {pairing['fingerprint']}\nApproval link: {url}", flush=True)
            webbrowser.open(url)
        else:
            if not pairing.get("acknowledged_until"):
                url = pairing["approval_url"]
                if not url.startswith(origin + "/pair#proposal="):
                    raise ValueError("Pairing environment does not match; browser was not opened")
                print(f"Public reference: {ref}\nCompare browser fingerprint: {pairing['fingerprint']}\nApproval link: {url}", flush=True)
                webbrowser.open(url)
        if not pairing.get("acknowledged_until"):
            # Direct inherited terminal streams: this process never reads,
            # logs, serializes or accepts the browser's claim code as an arg.
            if subprocess.call([helper, "pair", "claim", "--reference", ref]):
                raise ValueError(f"Pairing was not claimed; rerun setup to resume reference {ref}. "
                                 "If the browser says this approval expired or was cancelled, run setup --new-connection.")
        public_result([helper, "pair", "verify", "--reference", ref])
        saved.write_text(json.dumps({"reference": ref, "dev": dev}), encoding="utf-8")
        if os.name != "nt":
            saved.chmod(0o600)
        # Savings are an Ork Meter Plus feature; capture works either way.
        entitlement.refresh(home, background=False)
        savings_message = entitlement.message(home)
        if savings_message:
            print(savings_message, flush=True)
        capture = Capture(helper, ref)
        fragment = capture.ready()
        public_result([helper, "collect", "check-local", "--client", "codex", "--settings", str(capture.fragment)])
        lease.install(fragment)
        print("Capture is ready. Opening Codex with your existing permissions and model settings.\nExit this Codex session to stop capture and restore telemetry settings.", flush=True)
        previous = signal.signal(signal.SIGINT, lambda *_: None)
        try:
            code = subprocess.call([codex])
        finally:
            signal.signal(signal.SIGINT, previous)
        try:
            receipt = public_result([helper, "collect", "status", "--reference", ref]).get("latest_export")
            if receipt:
                print(f"Latest hosted receipt: {receipt.get('received_at', 'unknown')}. It may include other sessions; native snapshots are non-additive.")
            else:
                print("No hosted receipt verified. Missing evidence is not zero usage.")
        except (ValueError, OSError, subprocess.TimeoutExpired):
            print("Hosted receipt could not be verified; capture startup alone does not prove delivery.")
        return code
    finally:
        try:
            if lease and not lease.restore():
                print(f"Configuration changed during the session; automatic cleanup needs review. Private backup: {lease.backup}")
        except (ValueError, OSError):
            print(f"Automatic configuration cleanup needs review. Private backup: {lease.backup}")
        finally:
            try:
                if capture:
                    capture.stop()
            finally:
                lock.unlink(missing_ok=True)


def sys_terminal():
    import sys
    return sys.stdin.isatty() and sys.stdout.isatty()
