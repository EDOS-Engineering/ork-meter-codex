# Ork Meter for Codex

Ork Meter tracks your Codex usage in one live dashboard and, with Ork Meter
Plus, trims the tool output your agent never needed to read. This repository
is the installer. It holds no product code: the plugin itself is a released
Python package and a reviewed helper binary, both served from
`https://orkmeter.com/releases/codex/`, and `release.json` here pins the one
version you get, by SHA-256.

Current release: **0.1.3**.

## Install

macOS and Linux:

```sh
codex plugin marketplace add EDOS-Engineering/ork-meter-codex
codex plugin add ork-meter-codex@edos-codex
python3 -I -B "${CODEX_HOME:-$HOME/.codex}/plugins/cache/edos-codex/ork-meter-codex/0.1.3/scripts/ork_codex.py" setup
```

Windows (PowerShell):

```powershell
codex plugin marketplace add EDOS-Engineering/ork-meter-codex
codex plugin add ork-meter-codex@edos-codex
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { "$HOME\.codex" }
python -I -B "$codexHome\plugins\cache\edos-codex\ork-meter-codex\0.1.3\scripts\ork_codex.py" setup
```

Requires Python 3.11 or later and the Codex CLI. The first run downloads the
`ork-meter-codex` wheel over HTTPS, refuses it unless its SHA-256 equals the
digest in `release.json`, creates a private virtual environment under
`$CODEX_HOME/ork-meter-codex/package/0.1.3/`, installs the wheel and its
two dependencies (`tiktoken`, `keyring`) from PyPI with pip, and then runs
the installed `ork-codex` command. Every later run verifies that record and
runs the command. Setup then downloads the Ork Meter helper for your
platform from the same release, verified against the release's
`manifest.json`, and pairs it with your account in the browser.

Nothing is built on your machine, no credential is sent to any download, and
nothing runs before its digest matches.

## What leaves your machine

Usage metadata only: token counters, a capture time and an opaque delivery
id, sent to Ork Meter after you pair. Transcript text, prompts, file
contents, paths, models and account identifiers are never sent. The full
statement is at <https://orkmeter.com/privacy>.

## Updates

A new release is a new `release.json` and a new commit here. Run
`codex plugin marketplace upgrade edos-codex`; the next command run installs
the new version beside the old one.

## Support

<https://orkmeter.com/support>. Security reports: [SECURITY.md](SECURITY.md).
Licence: [LICENSE](LICENSE). Third-party components:
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
