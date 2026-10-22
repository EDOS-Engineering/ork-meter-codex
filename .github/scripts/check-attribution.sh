#!/usr/bin/env bash
# Attribution check (SEC-005 §11.1). See CONTRIBUTING.md, "Attribution".
#
# Every commit in BASE..HEAD declares who wrote it with an `Agent:` trailer:
#   Agent: none                       a human wrote it (AI assistance included, §11.1 item 4)
#   Agent: <spirit>                   an agent wrote it; it also carries
#   Agent-Version: <runtime/version>    the agent runtime and its version
#   Model: <model id>                   the model used
#
# With REQUIRE_PR_BODY=1, the PR description in PR_BODY must also fill in Requester
# and Task (§11.1 item 2), and Model when any commit is agent-authored.
#
# Usage: check-attribution.sh <base-sha> <head-sha>
set -euo pipefail

base=${1:?base sha}
head=${2:?head sha}
fail=0
agent_commits=0

trailer() { # <sha> <key> -> values, one per line
  git log -1 --format="%(trailers:key=$2,valueonly,separator=%x0A)" "$1" | sed '/^[[:space:]]*$/d'
}

err() { echo "::error::$*"; fail=1; }

shas=$(git rev-list --no-merges "$base..$head")
if [ -z "$shas" ]; then
  echo "No commits in $base..$head."
  exit 0
fi

for sha in $shas; do
  short=${sha:0:7}
  subject=$(git log -1 --format=%s "$sha")
  agents=$(trailer "$sha" Agent)
  count=$(printf '%s' "$agents" | grep -c . || true)

  if [ "$count" -ne 1 ]; then
    err "$short \"$subject\": needs exactly one 'Agent:' trailer ('Agent: none' for a human), found $count."
    continue
  fi

  if [ "$(printf '%s' "$agents" | tr '[:upper:]' '[:lower:]' | xargs)" = "none" ]; then
    echo "ok  $short human    $subject"
    continue
  fi

  agent_commits=$((agent_commits + 1))
  ok=1
  [ -n "$(trailer "$sha" Agent-Version)" ] || { err "$short \"$subject\": agent commit has no 'Agent-Version:' trailer."; ok=0; }
  [ -n "$(trailer "$sha" Model)" ] || { err "$short \"$subject\": agent commit has no 'Model:' trailer."; ok=0; }
  [ "$ok" -eq 0 ] || echo "ok  $short $(printf '%s' "$agents" | xargs)  $subject"
done

if [ "${REQUIRE_PR_BODY:-}" = "1" ]; then
  field() { # value of a "**Field:** value" line in the PR template, comments stripped
    printf '%s\n' "${PR_BODY:-}" | tr -d '\r' | sed 's/<!--.*-->//g' \
      | sed -n "s/^[-*[:space:]]*\*\*$1:\*\*[[:space:]]*//p" | head -1 | xargs
  }
  [ -n "$(field Requester)" ] || err "PR description: 'Requester' is empty. Name the human who asked for this change."
  [ -n "$(field Task)" ] || err "PR description: 'Task' is empty. Link the issue or task."
  if [ "$agent_commits" -gt 0 ]; then
    [ -n "$(field Model)" ] || err "PR description: 'Model' is empty, and this PR has agent-authored commits."
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "Attribution check failed. See CONTRIBUTING.md, \"Attribution\"."
  exit 1
fi
echo "Attribution check passed ($agent_commits agent-authored commit(s))."
