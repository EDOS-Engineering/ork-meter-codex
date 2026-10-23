# Ork Meter for Codex

Choose a tracking path before setup:

| Platform / workflow | Entry point | Requirements |
| --- | --- | --- |
| Integrated WSL/Linux tracking | `tracking connect`, below | Python 3.11+, installed package dependencies, unlocked Linux Secret Service; no helper or telemetry configuration |
| Helper-based session setup | `setup`, under Install | Reviewed helper for your platform; OS credential store; starts a Codex session with temporary telemetry configuration |

Both paths keep usage capture free and require Plus only for output reduction.
Installed user hooks require a one-time Codex `/hooks` trust review. The plugin
manifest itself registers no hooks, skills or MCP servers.

## Integrated usage tracking (WSL/Linux)

Usage tracking runs inside the plugin, without a separate helper, capture
process, or Codex telemetry configuration. In a regular WSL terminal, run:

```text
python -I -B scripts/ork_codex.py tracking connect --dev
```

Approve the browser pairing after comparing its fingerprint, then enter the
claim code at the terminal prompt (characters appear as asterisks). Review and
enable **Ork usage tracking** in
Codex `/hooks`. The plugin installs versioned hook code and preserves other
hooks. Repeat `tracking install` after updating the plugin. Omit `--dev` to
connect to production. WSL needs its own pairing; Windows credentials are not
copied across OS credential stores. Python's `keyring` package and an unlocked
Linux Secret Service are required (`python -m pip install -e .`). Credentials
never fall back to plaintext files.

The hooks send the latest native total and last-request token snapshots from
the active session's local usage log. Only allowlisted counters, a capture time,
and an opaque delivery ID leave the machine. Transcript text, session IDs,
paths, models and account information are excluded. Snapshots overlap and are
not additive billed usage; the site reports them separately from spend.

An existing session can be synced immediately, without restarting Codex:

```text
python -I -B scripts/ork_codex.py tracking sync --session SESSION_ID
python -I -B scripts/ork_codex.py tracking status
```

`tracking prepare --dev` returns a browser link without opening a terminal
claim prompt; finish with `tracking claim --reference REFERENCE` in your own
terminal. Add `--session SESSION_ID` to that command for an immediate sync.
Status verifies the hosted receipt; installation alone does not prove delivery.
Hook failures pass through silently and leave a local diagnostic visible in
`tracking status`. Automatic delivery runs in an isolated child with a two-second
wait budget; a timed-out child is killed and reaped. One automatic delivery runs
per profile at a time. Failures back off for 1, 2, 4, 8, then 15 minutes; a later
hook retries the latest snapshot after that interval. There is no background
queue or timer, and intermediate snapshots can be skipped. An acknowledged ID is
checkpointed; an interrupted acknowledgment retries the same ID for unchanged
usage. Explicit `tracking sync` bypasses the automatic backoff and retains the
normal network timeout. Reinstall tracking hooks after updating to use this
behavior. Process startup and local filesystem work are outside the child wait
budget; native-host timings are separate qualification evidence. This integration reads Codex's current local usage-log format;
if that format changes or no counters are available it reports no delivery.

Automatic filtering installed by setup can now be inspected or removed explicitly:

```text
python -I -B scripts/ork_codex.py optimize install
python -I -B scripts/ork_codex.py optimize status
python -I -B scripts/ork_codex.py optimize uninstall
```

Installation preserves other hooks, uses an immutable package snapshot, and
requires the normal Codex `/hooks` review. Reinstall after updating. Recovery
remains available through `--state <profile>/ork-auto-state artifact HANDLE`.
The new package-based launcher also works from an installed wheel.

Explicit `--keep` evidence and surrounding lines now override routine-output
filtering. Up to eight named pytest passing rows stay visible, including progress
suffixes, while parametrized passing sweeps remain eligible for reduction. This
avoids dropping evidence that would otherwise require another model/tool turn.
The existing Go, Ninja, token reserve and exact-recovery behavior is preserved.

The plugin registers no skills or MCP servers. `setup` installs automatic output filtering into Codex's user hook configuration; the agent continues using its native shell tool. Codex requires a one-time `/hooks` trust review before this non-managed hook runs. MCP is an explicit opt-in adapter; see `docs/optional-mcp/README.md`.

Codex plugin for recoverable output reduction and token tracking through EDOS's
Ork Meter platform. It brings the output trimming and diagnostics from Ork Meter for Claude Code
together with cache-preserving retrieval.

The optimization engine is Python 3.11+ with a pinned `tiktoken==0.9.0`
dependency. Install it with `python -m pip install -e .` from this checkout.
The public vocabulary ships in the package; reduction makes no downloads or
model calls. Missing dependencies or corrupt vocabulary return full output.
The helper-based setup path uses the existing Ork Meter helper, whose
receiver projects native Codex telemetry before delivery and keeps credentials
in the OS credential store.

## Ork Meter Plus

Output reduction (the automatic `PostToolUse` filter, `run` and MCP
`compact`) is an Ork Meter Plus feature; free Ork Meter is visibility only.
Entitlement refresh uses the integrated pairing's OS credential directly when
present; otherwise it uses the helper (`ork-meter pair entitlement`). Both paths
ask the platform at most every 15 minutes and keep the answer in
`$CODEX_HOME/ork-meter-codex/entitlements/<reference>/entitlement.json`, owner-only, with exactly
`optimize`, `state` (`promo`, `trial`, `active`, `past_due` or `free`), `until`
and `checked_at`. The hook refreshes it in the background and never waits on
the network. Reduction runs only when `optimize` is true and it is at most 24
hours past `until` (or `checked_at` when `until` is null); otherwise output
passes through unchanged and Codex runs normally. No saved pairing means not
connected, which is not entitled. `ORK_CODEX_MODE=off` and the other switches
still apply on top. Setup prints "Ork Meter savings are off: they are part of
Ork Meter Plus. Upgrade at https://orkmeter.com/billing" when the account has
no Plus in a recent hosted answer. If a required helper is missing, the cache is invalid,
or verification is unavailable or stale, setup explains how to retry without
asking you to purchase again. Codex and usage capture continue while savings
are off. `doctor` reports `savings` and recovery guidance in `savings_detail`.
Telemetry capture, receipts, recovery of saved artifacts and local reports are not gated.
Only the helper-based pairing path requires a helper with `pair entitlement`.
The automatic output filter refreshes entitlement independently; automatic usage
delivery does not spawn an entitlement refresh. The hook respects the separate
`entitlement-attempt` retry timestamp, including failed first requests. Existing
unbound caches are ignored and refreshed for the saved pairing; no new pairing
is required. A late helper response for an older reference cannot grant access
to the current reference.

## Install (helper-based session setup)

Add the marketplace and the plugin, then run setup from the installed plugin:

```text
codex plugin marketplace add EDOS-Engineering/ork-meter-codex
codex plugin add ork-meter-codex@edos-codex
python -I -B <installed plugin>/scripts/ork_codex.py setup
```

Nothing is built on your machine. Setup downloads the reviewed Ork Meter
helper for your platform from this repository's GitHub Release
`v<plugin version>`, verifies its SHA-256 against that release's
`manifest.json`, and installs it owner-only under
`$CODEX_HOME/ork-meter-codex/helper/<version>/`. A download whose digest does
not match installs nothing. A public release needs no token; set `GH_TOKEN`
(or `GITHUB_TOKEN`) only to install from a private fork or to avoid GitHub's
anonymous API rate limit. An
`ORK_METER_BINARY` path or a development build at `.local/ork-meter` takes
precedence; see [docs/release.md](docs/release.md). Metering is free;
output reduction needs Ork Meter Plus (above).

Setup finds the native Codex CLI from, in order: `setup --codex PATH`; the
`CODEX_CLI_PATH` environment variable; the `CODEX_CLI_PATH` that the ChatGPT
desktop app records under an `[mcp_servers.*.env]` table in
`$CODEX_HOME/config.toml`; `PATH`; then the ChatGPT app bundle
(`/Applications` or `~/Applications`), Homebrew and npm global locations on
macOS and Linux. Windows keeps its existing `PATH` and npm launcher lookup.

For Windows setup from this checkout with the local helper already built:

1. Run `python -I -B <checkout>/scripts/ork_codex.py setup` in a regular PowerShell window.
2. Approve the opened browser pairing after checking the fingerprint, then enter its claim code at the terminal prompt.

When the saved collector connection reaches its expiry, rerunning setup starts a
new browser approval. It keeps the new public reference if claiming is interrupted,
so the next setup resumes that approval. It does not extend the old credential or
reuse its Plus cache. An explicitly supplied expired `--reference` is refused with
guidance to rerun setup without that option. Existing local evidence is preserved.

If a browser approval expired before you entered its code, the connection was
revoked, or the local helper record is unavailable, run `setup --new-connection`.
It starts a new browser approval and keeps the saved environment unless you select
`--dev` or `--prod`. It preserves old credentials and history; revoke an old
connection in Ork Meter if it should no longer have access. After an interrupted
claim, run plain `setup` to resume the new approval. A network failure alone does
not replace your connection. `--new-connection` cannot be combined with `--reference`.

Setup installs the plugin when needed, starts capture, backs up and temporarily
adds its telemetry settings, then opens an interactive Codex CLI session using
your existing model and permissions. Exit that session to stop capture and
restore telemetry settings. Later runs reuse the public pairing reference.
Setup also installs a versioned `PostToolUse` hook into the selected Codex
profile. Review and trust it with `/hooks` in Codex once; later Bash output is
filtered automatically without an agent-selected wrapper or added tool.
The code is read directly by the helper; it never goes through chat. Existing
foreign telemetry is refused rather than overwritten. This session workflow
does not restart or reconfigure an already-running desktop session.

First installation uses a private component snapshot containing only plugin
files and the existing helper, excluding runtime logs, artifacts and Git data.
An existing matching checkout installation is reused without replacing its
marketplace. Private backups and the package live under the selected Codex
profile's `ork-meter-codex` directory. Use `setup --dev` for the development
platform or `setup --reference REF` to resume an existing public reference.
Later `setup` runs use the saved pairing's environment; `--dev` and `--prod`
select it explicitly.

For automatic filtering without telemetry setup, run
`python -B scripts/install_auto_hook.py` once from the checkout, then trust the
hook through `/hooks`. Manual installation and persistent capture instructions follow.

From a checkout, with `python` on PATH:

```text
codex plugin marketplace add <absolute checkout path>
codex plugin add ork-meter-codex@edos-codex
```

From [EDOS-Engineering/ork-meter-codex](https://github.com/EDOS-Engineering/ork-meter-codex)
on GitHub:

```text
codex plugin marketplace add EDOS-Engineering/ork-meter-codex
codex plugin add ork-meter-codex@edos-codex
```

The repository uses Codex's supported `.claude-plugin/marketplace.json` catalog
location. The plugin has portable and Codex compatibility manifests. It registers
no skills, bundled startup hooks or MCP servers; optional MCP examples live under docs.

OpenAI-specific component declarations use the compatibility overlay. Native
installation tests confirm no packaged skills, startup hooks or MCP servers,
plus CLI retrieval, mode controls, exact recovery and local reporting. Optional
MCP tests require `--with-mcp`. See the
verification record for evidence and limits.

The plugin registers no visible skills. Usage instructions remain in
`docs/manual-workflows/` for manual reference, outside skill discovery. After updating an
existing installation, start a new conversation to refresh its loaded context.
The CLI handles setup, receipt checks, source retrieval, recovery and reports.
The optional MCP server exposes three stable tools:
`source`, `artifact`, and `report`. It performs retrieval and local bookkeeping;
commands execute through the ordinary Codex shell tool and its sandbox.

Default installation adds no MCP tool schemas. Explicitly enabling the optional
adapter adds three schemas; complete-task measurements must include that
overhead and any recovery.

Pass `workspace` as the absolute project directory on each MCP source, artifact
or local report call. Installed MCP processes start in the plugin cache; this
client supplies no project roots. The server requires explicit scope rather
than reading cached plugin files as project source.

## Reduce output

Use a selected noninteractive command through the wrapper:

```text
python -I -B scripts/ork_codex.py run --workspace PROJECT_ROOT --kind test -- python -m pytest -v
python -I -B scripts/ork_codex.py run --workspace PROJECT_ROOT --kind build -- cargo build
python -I -B scripts/ork_codex.py run --workspace PROJECT_ROOT --kind build --keep "unit conversion" -- cargo build
python -I -B scripts/ork_codex.py run --workspace PROJECT_ROOT --kind search -- rg -n TODO src
```

These examples run from the checkout. When installed, use the absolute path to
the installed plugin's `scripts/ork_codex.py`. Replace `PROJECT_ROOT` with the
absolute active project directory; this scopes storage without changing the
command's working directory.

The wrapper preserves argv, working directory, environment, separate stdout and
stderr, and exit status. Shortened failing results also
show the child's actual exit code because PowerShell tool boundaries may
normalize nonzero statuses to 1. It buffers until completion; retain native execution
for interactive tools, PTYs or live progress. It never retries commands. For
pipelines, supply the original shell explicitly. `--full` returns full output;
`ORK_CODEX_MODE=off` disables reduction; `shadow` returns full output and records
candidate sizes. For MCP `source` compaction, pass `mode` explicitly: the native
host does not inherit these shell environment settings. Existing conversation
content is never rewritten.

Small, unknown, binary and JSON/XML results pass through. Recognized text gets
line elision and passing-test filtering while diagnostic context remains. The
complete candidate, including its recovery footer, must save at least 512
`o200k_base` tokens before it can replace the original. This margin reserves
room for wrapper instructions and selective recovery; it cannot cover arbitrary
recovery, additional model reasoning or provider-specific tokenization. Byte
reduction alone is insufficient. Pathological tokenization pieces or outputs
beyond the offline counter's bounds remain exact. CLI/MCP compaction also
checks serialized result metadata against the same reserve.
Use repeated `--keep TEXT` options to retain context around known relevant
literal text. Optional MCP `source` compaction uses its existing `query` field
for the same purpose. These hints can avoid additional recovery turns. The
8 KiB budget is soft for errors and explicitly requested evidence. A shortened result contains an immutable
artifact handle; the full original is written, synced and atomically published
before its handle is emitted. Storage failure
returns the original output from that same command execution.

```text
python -I -B scripts/ork_codex.py artifact HANDLE --workspace PROJECT_ROOT --start 100 --count 50
python -I -B scripts/ork_codex.py artifact HANDLE --workspace PROJECT_ROOT --op search --query timeout
python -I -B scripts/ork_codex.py artifact HANDLE --workspace PROJECT_ROOT --op raw
python -I -B scripts/ork_codex.py artifact HANDLE --workspace PROJECT_ROOT --intervention DELIVERY_ID --count 50
python -I -B scripts/ork_codex.py savings --workspace PROJECT_ROOT
python -I -B scripts/ork_codex.py audit
```

Use native search and exact ranged reads for ordinary source exploration. The
current plugin outline/symbol route costs more than efficient native reads;
choose it when exact spans, freshness hashes or batching help the task. The
CLI and optional MCP `source` offer these operations; `artifact` offers
recovery/search/JSON pointers; `report` offers
estimates, context overhead or a hosted receipt. Python outlines use AST spans
including decorators. Other languages return labelled lexical candidates;
exact ranged reads remain available. Source hashes reject stale edit evidence.
Explicit full/exact requests remain full/exact. Repeated-read suppression stays
off because context retention across compaction is not proved.

Prefer selective artifact recovery for omitted evidence. When complete output
is needed from the start, use `run --full`; a preview followed by full recovery
adds tokens. Reduction only changes newly emitted results. Keep earlier
conversation messages, tool results, model settings and tool definitions stable
within a session to preserve reusable prompt prefixes. Cache eligibility and
actual provider cache hits are separate measurements.

Artifact search `start` is a physical line number. Continue at `next_start_line`
when `truncated` is true. Match counts identify their scope; omitted matches
remain available in the immutable original.

## Optional legacy telemetry helper

`setup` installs the reviewed Ork Meter helper from the plugin's GitHub
Release (see "Install"). To use another helper, put it on PATH or set
`ORK_METER_BINARY` to its absolute path. Development builds can reuse the
sibling platform checkout:

```text
python scripts/build_meter.py --platform-source ../ork-meter --go PATH_TO_MATCHING_GO
python -I -B scripts/ork_codex.py doctor
python -I -B scripts/ork_codex.py meter prepare
```

The helper must use Go matching the platform's `go.mod` when built from source.
The account holder approves the returned browser link and fingerprint, then
claims the connection in their terminal:

```text
python -I -B scripts/ork_codex.py meter claim --reference PUBLIC_REFERENCE
python -I -B scripts/ork_codex.py meter capture --reference PUBLIC_REFERENCE
```

Keep capture running. Review the private telemetry fragment locally, merge its
telemetry settings into Codex configuration, and restart Codex. Never paste its
contents or the browser claim code into chat. On Unix `meter prepare-config`
can create a review candidate; the platform helper does not support that
operation on Windows. Restarting capture rotates the local port/header and
requires a new fragment. Apply your organization's telemetry policy.

```text
python -I -B scripts/ork_codex.py meter status --reference PUBLIC_REFERENCE
python -I -B scripts/ork_codex.py meter diagnose --reference PUBLIC_REFERENCE
```

Authorization alone does not prove delivery. Check the hosted receipt's capture
and receipt times. Native snapshots overlap and are **not additive billed
events**. Cached and reasoning counters can be subsets of input/output.
The platform's existing snapshot dashboard receives the filtered counters;
optimization artifacts, raw logs and source content stay local. The current
Codex platform schema has no optimization-feature field, so this plugin does
not append unsupported savings metadata to hosted observations.

To disconnect, stop capture, remove only its reviewed telemetry configuration,
restart Codex, revoke the reference in the browser, then run `meter forget
--reference PUBLIC_REFERENCE --confirm-local-removal`. Local removal does not
prove hosted revocation or erase hosted usage. Successful removal clears only
the matching saved pairing and its entitlement cache. Forgetting another
reference leaves the current pairing available. Exit an active setup/Codex
session first so setup cannot replace the pairing during removal.

## State and controls

Artifacts and SQLite counters live under the selected workspace's
`.local/ork-codex`. Use the same absolute MCP `workspace` and CLI `--workspace`
to share handles. Explicit scope anchors at that directory, including projects
without Git. Without `--workspace`, the CLI infers the nearest Git root.
`ORK_CODEX_STATE` selects a different base (its `ork-codex` child is used) for
processes that receive it; installed native MCP does not inherit it. `--state
PATH` overrides the exact CLI directory. This project-local default works
inside Codex's workspace-write shell
sandbox. Keep `.local/ork-codex` out of commits in consuming repositories.
Windows uses DACLs for the current user, OS administrators/system, and explicitly
authorized named workspace/sandbox principals; broad public user grants are
excluded. Unix uses directory mode 0700 and file mode 0600. State contains
original command output, so keep it out of source control. Originals persist
until the user removes the state directory; no silent expiration or rerun.

| Setting | Default | Purpose |
| --- | --- | --- |
| `ORK_CODEX_MODE` | `compact` | `compact`, `shadow`, or `off` |
| `ORK_CODEX_BUDGET_BYTES` | 2048 | Soft result budget, floor 1024, ceiling 1 MiB |
| `ORK_CODEX_MIN_BYTES` | 4096 | Minimum eligible output size, same bounds |
| `ORK_CODEX_MIN_TOKENS_SAVED` | 512 | Required payload-token saving after footer; 0..1 Mi tokens; counts remain surrogate |
| `ORK_CODEX_TEST_FILTER` | `on` | `off`, `0`, `false`, or `no` disables pass filtering |
| `ORK_CODEX_WORKSPACE` | standalone server cwd | Standalone MCP default; installed native calls require `workspace` |
| `ORK_CODEX_SESSION` | unset | Optional explicit context identity for three-failure warnings |
| `ORK_CODEX_RUN` | generated per Store/process | Explicit task/attempt ID shared by wrapper and MCP for task grouping |
| `ORK_METER_BINARY` | local development helper, then PATH | Absolute reviewed helper override |

Each shortened result includes a `delivery:` identifier. Pass it as MCP
`intervention` or CLI `--intervention` for exact recovery attribution. Recovery
is charged to the originating run/policy, even when requested later. Without an
identifier, a unique originating run/policy is used; reused artifacts across
multiple runs/policies and historical artifacts are labelled unknown. Set one
deliberate `ORK_CODEX_RUN` for shell processes and the same MCP `source`
compaction `run_id` for a task; generated per-process defaults do not
establish whole-task attribution. Native MCP does not inherit the shell's run
variable.

Recovery still returns intact evidence if optional statistics fail. The response
marks the accounting gap, raw CLI recovery reports it on stderr, and subsequent
reports label recorded totals as partial. Integrity/missing-original failures
still prevent recovery. A missing accounting value is not a zero-cost recovery.

Reduction and recovery events count only after successful output write and
flush. Cancelled, pending, failed and unverified legacy events are excluded from
claimed avoided bytes. An output failure reports the actual command outcome
and saved handle when available; recovery never requires another execution.
Emission establishes a local transport observation, not model consumption.

Schemas and environment defaults stay fixed for each MCP process; explicit
`mode` selects compaction policy per call. Estimates use UTF-8
bytes/4, charge recovery against avoided bytes, and may become negative. They
exclude model reasoning, plugin instruction/schema overhead, other retrievals,
MCP result-envelope overhead, and provider billing; they are not measured
end-to-end token or cost savings.

## Verify and develop

```text
python -B scripts/validate.py
python -B -m unittest discover -s tests -v
python -B scripts/replay.py --output .local/replay.json
```

The native helper projection test runs when `.local/ork-meter[.exe]` is built or
`ORK_TEST_METER_BINARY` is set. Otherwise it is explicitly skipped. CI runs local
contracts and synthetic replay on Windows/Linux with Python 3.11–3.13; it does
not deploy, publish a release or make paid inference calls.

Copyright 2026 Enterprise DevOps Solutions LLC. Licensed for use with an Ork Meter or Ork Meter Plus
account; see [LICENSE](LICENSE). Third-party components are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
