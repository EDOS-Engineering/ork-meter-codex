# Ork Meter for Codex

Ork Meter tracks your Codex usage in one live dashboard and, with Ork Meter
Plus, trims the tool output your agent never needed to read. This repository
is the installer. It holds no product code: the plugin itself is a released
Python package and a reviewed helper binary, both served from
`https://orkmeter.com/releases/codex/`, and `release.json` here pins the one
version you get, by SHA-256.

Current release: **0.1.7**.

## Install

macOS and Linux, in your own terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/EDOS-Engineering/ork-meter-codex/main/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/EDOS-Engineering/ork-meter-codex/main/install.ps1 | iex
```

The installer is [install.sh](install.sh) / [install.ps1](install.ps1) in this
repository; read it first if you like. It adds this marketplace and the
plugin with Codex's own commands (`codex plugin marketplace add`,
`codex plugin add`), asks Codex where it installed the plugin, and runs that
release's setup. Setup opens Ork Meter in your browser; approve the
connection there and it returns to your terminal by itself. If your browser
is on another computer (SSH), choose to approve from another computer and
type the code it shows into the terminal. Run the installer yourself rather
than through an agent: it pairs your account.

After the first run, the same setup is always at a path with no version in
it, which runs the release this repository pins:

```sh
"${CODEX_HOME:-$HOME/.codex}/ork-meter-codex/bin/ork-codex" setup
```

On Windows it is `ork-codex.cmd` in the same folder. The same command takes
`capture status` and `disconnect`.

<details><summary>Install step by step instead</summary>

```sh
codex plugin marketplace add EDOS-Engineering/ork-meter-codex
codex plugin add ork-meter-codex@edos-codex
python3 -I -B "${CODEX_HOME:-$HOME/.codex}/plugins/cache/edos-codex/ork-meter-codex/0.1.7/scripts/ork_codex.py" setup
```

```powershell
codex plugin marketplace add EDOS-Engineering/ork-meter-codex
codex plugin add ork-meter-codex@edos-codex
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { "$HOME\.codex" }
py -I -B "$codexHome\plugins\cache\edos-codex\ork-meter-codex\0.1.7\scripts\ork_codex.py" setup
```

</details>

Requires the Codex CLI and any Python 3 to start the command (on Windows,
`py`). This release installs into Python 3.14, 3.13, 3.12 or 3.11 (the versions
`release.json` lists); the command finds one of those itself, on PATH, through
the Windows `py` launcher or through `uv python find`, and reruns under it. If
none is installed it says so and names the one install command for your
system. The first run downloads the
`ork-meter-codex` wheel over HTTPS, refuses it unless its SHA-256 equals the
digest in `release.json`, creates a private virtual environment under
`$CODEX_HOME/ork-meter-codex/package/0.1.7/`, installs the wheel and its
dependencies (`tiktoken`, `keyring`, `aiohttp`, `tomlkit`) from PyPI with
pip, and then runs the installed `ork-codex` command. Every later run verifies that record and
runs the command. Setup then downloads the Ork Meter helper for your
platform from the same release, verified against the release's
`manifest.json`, and pairs it with your account in the browser.

Nothing is built on your machine, no credential is sent to any download, and
nothing runs before its digest matches. Nothing needs administrator rights.

## Capture

Once paired, every Codex session is metered. Setup adds one `[otel]` table to
`~/.codex/config.toml` (after keeping a private copy of the file) that sends
Codex's own usage telemetry to a small receiver on `127.0.0.1`, which starts
when you log in: a launchd agent on macOS, a systemd user service on Linux,
a per-user startup entry on Windows. If Codex already sends telemetry
elsewhere, setup stops and asks before replacing it. Restart Codex sessions
or the Codex app that were already open.

`ork-codex disconnect` stops and removes the receiver and puts back the
`[otel]` settings you had before, leaving everything else as it is. To revoke
the connection itself, use Connected plugins at <https://orkmeter.com/pair>.

## What leaves your machine

Usage metadata only: token counts, the model, the time of each response, where
reported the plugin version and its settings, and an opaque delivery id, sent to Ork Meter
after you pair. The receiver on your computer drops everything else in
Codex's telemetry before handing it on: prompts, responses, tool arguments
and output, file contents, paths and account identifiers are never sent. The
full statement is at <https://orkmeter.com/privacy>.

## Updates

A new release is a new `release.json` and a new commit here. Run
`codex plugin marketplace upgrade edos-codex`; the next command run installs
the new version beside the old one.

## Support

<https://orkmeter.com/support>. Security reports: [SECURITY.md](SECURITY.md).
Licence: [LICENSE](LICENSE). Third-party components:
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
