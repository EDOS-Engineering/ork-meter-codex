#!/bin/sh
# Ork Meter for Codex: install and pair in one line.
#
#   curl -fsSL https://raw.githubusercontent.com/EDOS-Engineering/ork-meter-codex/main/install.sh | sh
#
# It adds this marketplace and the plugin to Codex with Codex's own commands,
# then runs the installed plugin's setup, which installs the release pinned in
# release.json (verified by SHA-256), pairs this computer in your browser and
# turns on capture for every Codex session. Arguments after `sh -s --` go to
# setup, for example `--replace-telemetry`. Nothing here needs administrator
# rights. Read it first if you like: it is short.
set -eu

marketplace=EDOS-Engineering/ork-meter-codex
plugin=ork-meter-codex@edos-codex

say() { printf '%s\n' "$*" >&2; }

if ! command -v codex >/dev/null 2>&1; then
  say "Ork Meter for Codex needs the Codex CLI first: https://developers.openai.com/codex/cli"
  exit 1
fi

python=
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
    python=$candidate
    break
  fi
done
if [ -z "$python" ]; then
  case "$(uname -s)" in
    Darwin) say "Ork Meter for Codex needs Python 3. Install it with: brew install python@3.13  (or from https://www.python.org/downloads/)" ;;
    *) say "Ork Meter for Codex needs Python 3. Install python3 with your package manager, then run this again." ;;
  esac
  exit 1
fi

say "Adding the Ork Meter marketplace and plugin to Codex..."
codex plugin marketplace add "$marketplace" >&2
# A rerun picks up the newest release, not the snapshot taken the first time.
codex plugin marketplace upgrade edos-codex >&2
installed=$(codex plugin add "$plugin" --json)

# The installed plugin's root comes from Codex itself; never guess a version.
bootstrap=$(printf '%s' "$installed" | "$python" -I -c '
import json, os, sys
root = json.load(sys.stdin)["installedPath"]
path = os.path.join(root, "scripts", "ork_codex.py")
sys.exit("Codex did not report where it installed the plugin") if not os.path.isfile(path) else print(path)
')

# Setup reads the browser approval and its prompts from your terminal, not
# from this pipe.
if (: </dev/tty) 2>/dev/null; then
  exec "$python" -I -B "$bootstrap" setup "$@" </dev/tty
fi
say "No terminal is attached. Run this to finish: $python -I -B \"$bootstrap\" setup"
exit 1
