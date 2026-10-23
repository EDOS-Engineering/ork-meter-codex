# Releasing the plugin

How a release of Ork Meter for Codex is cut, what it contains, how setup
obtains the helper from it, and what the process does not cover.

## Shape of a release

The plugin is Python; the only compiled part is the reviewed Ork Meter helper,
`cmd/ork-meter` in [EDOS-Engineering/ork-meter](https://github.com/EDOS-Engineering/ork-meter),
which pairs the account, captures native Codex telemetry locally and asks the
platform about Ork Meter Plus. Customers never build it. A release is a
GitHub Release **`v<version>`** on this repository, on the tag of a `main`
commit, with these assets:

```text
ork-meter-darwin-arm64                     the helper, one per platform
ork-meter-darwin-amd64
ork-meter-linux-amd64
ork-meter-linux-arm64
ork-meter-windows-amd64.exe
ork-meter-THIRD_PARTY_NOTICES.txt          licence texts of the Go standard library and every
                                           Go module compiled into any helper
ork_meter_codex-<version>-py3-none-any.whl the plugin package
<asset>.sha256                             "<hex>  <asset>", one per asset above
manifest.json                              plugin version, tag, plugin commit, ork-meter ref and
                                           commit, Go version, build command, sha256 per asset
```

`<version>` is the one `plugin.json`, `.codex-plugin/plugin.json` and
`pyproject.toml` carry; the three must agree, and `scripts/validate.py` pins
them. A release reuses no version: bump it by pull request first. Codex
installs the plugin from the repository (`codex plugin marketplace add
EDOS-Engineering/ork-meter-codex`), so `main` remains what the marketplace
serves; the Release is where the compiled helper lives and where setup gets it.

## How setup obtains the helper

`setup` (`ork_codex/onboarding.py`, `obtain_helper`) looks for a helper in
this order and stops at the first:

1. `ORK_METER_BINARY`, an absolute path to a helper you chose. An invalid value
   is an error, never a fallback.
2. A development build at `.local/ork-meter` in the checkout
   (`scripts/build_meter.py`).
3. The release helper already installed for this plugin version, verified
   against its recorded digest.
4. A helper named `ork-meter` on `PATH`.

With none of those, `ork_codex/helper.py` downloads it from the Release for
the pinned plugin version (`plugin.json` `version`):

1. Reads the release listing for tag `v<version>` through the GitHub API,
   then `manifest.json` and the one asset for the host platform
   (`ork-meter-<os>-<arch>`, `.exe` on Windows). Nothing else is fetched.
   Each request is HTTPS with at most one redirect, and the API credential
   is **not** sent to the storage host the redirect points at.
2. Refuses the manifest unless it names `ork-meter-codex`, the same version,
   and a 64-character lowercase hex digest for the asset.
3. Computes the asset's SHA-256 and refuses it unless it equals the manifest's.
   Nothing is written before that check passes, and a mismatch installs
   nothing.
4. Writes the helper through a staging file, re-verifies the written bytes,
   makes it owner-only (`0700`, or the current-user ACL on Windows) and moves
   it to `$CODEX_HOME/ork-meter-codex/helper/<version>/ork-meter`, next to a
   `helper.json` recording the version, asset, digest, ork-meter ref and
   release page. The whole `ork-meter-codex` directory is private to the user.

Later runs use the installed helper after re-verifying its digest against
`helper.json`; a corrupted file is not trusted and is downloaded again. The
plugin's other commands (`meter`, `doctor`) find it through
`ork_codex/meter.py` `binary()` in the same order.

Releases are public, so the download needs no credential. `GH_TOKEN` or
`GITHUB_TOKEN`, when set, is sent only to GitHub (for a private fork or to
avoid the anonymous API rate limit). A failed or refused download says so and
installs nothing; Codex itself is unaffected.

The digest check guards against a corrupted or altered download. It does not
by itself prove provenance: the manifest and the asset come from the same
release. Provenance rests on the reviewed workflow run that produced them,
the tag, and the build provenance attestation on each asset
(`gh attestation verify <asset> --repo EDOS-Engineering/ork-meter-codex`).

## Cutting a release

Releases are cut by the `package-release` workflow
(`.github/workflows/package-release.yml`). It runs on **manual dispatch
only**, never on push, because publication is a reviewed operation
(CONTRIBUTING: CI never deploys or publishes a release). A human dispatches it
from `main` after the version bump and every change it will publish have
been reviewed and merged.

The workflow accepts dispatches only from `main`. Its read-only `verify` job
runs `scripts/validate.py` and the unit tests and checks the existing GitHub
`production` environment with `.github/scripts/check-release-gate.py`: the
environment must exist, require reviewers with **self-approval allowed**,
disable administrator bypass and allow the single deployment branch `main`
with no tag policies. A missing environment or weakened protections fail the
job and nothing is built. The `package` job depends on `verify` and runs in
that environment and waits for an authorized founder to approve before it can
build or publish. The three founders have equal authority: each may dispatch
and approve the same release, directly or through an agent authenticated as that
founder. No second person is required. Agents use the founder’s existing GitHub
authority; this policy does not grant arbitrary bot identities access. Keep all
three founders on the environment reviewer list. The gate rejects self-review
prevention so a settings change cannot silently reintroduce a second-person rule.

Dispatch input:

| input | meaning |
|---|---|
| `ork_meter_ref` | The ork-meter tag or commit to build the helper from. It must contain `cmd/ork-meter/main.go`; the workflow checks and fails fast otherwise. Prefer a tag or a full commit SHA over a branch name so the release is reproducible. |

What the `package` job does, in order:

1. Checks out this repository at the dispatching `main` commit with full
   history, and `EDOS-Engineering/ork-meter` at `ork_meter_ref` into
   `ork-meter/` using the `ORK_METER_READ_TOKEN` secret (below).
2. Reads the version from the three manifests (they must agree), and fails
   if tag `v<version>` or a GitHub Release `v<version>` already exists.
3. Installs the Go toolchain named by ork-meter's `go.mod`.
4. Runs `scripts/package_release.py`, which builds the five helpers with
   `CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -buildid=" -buildvcs=false`,
   writes `ork-meter-THIRD_PARTY_NOTICES.txt` from the licence files of every
   Go module those builds compile in (`go list -deps` per target, read from
   the module cache; a module without a licence file fails the job),
   builds the wheel with `pip wheel --no-deps`, writes a `.sha256` per asset
   and `manifest.json`, and prints each digest.
5. Smoke-tests the linux-amd64 helper: run with no arguments it prints its
   usage and exits 2. It also runs `sha256sum -c` over every `.sha256` file.
   This proves the binary starts; it is not platform acceptance for the other
   targets.
6. Attests build provenance for the helpers, the wheel and `manifest.json`
   with `actions/attest-build-provenance` (SHA-pinned). A failure here stops
   the run before anything is published.
7. Creates the GitHub Release `v<version>` with `gh release create --target
   <dispatching commit>`, which creates the tag at that `main` commit, and
   uploads every asset with release notes listing the digests. The same table
   goes to the run summary.

The workflow never pushes a branch and never writes to `main`; its only writes
are the tag and the Release, with the workflow's own `GITHUB_TOKEN`
(`contents: write`, plus `id-token: write` and `attestations: write` for the
attestation).

### The `ORK_METER_READ_TOKEN` secret

ork-meter is private, so the workflow needs a credential to read it. Create
one of:

- a **fine-grained personal access token** scoped to the single repository
  `EDOS-Engineering/ork-meter` with repository permission **Contents:
  Read-only** (and Metadata: Read-only, which GitHub adds), or
- a **GitHub App** installation token with the same scope.

Store it as the organization secret **`ORK_METER_READ_TOKEN`**, available to
`EDOS-Engineering/ork-meter-codex`. It is used only by
the `actions/checkout` step for ork-meter with `persist-credentials: false`.
Set an expiry and rotate it; a missing or expired token fails the checkout
step and nothing is published.

### Rollback

A release is never rewritten. To roll the helper back, bump the version by
pull request, then re-dispatch the workflow with the older `ork_meter_ref`.
Customers who run setup again get the new version's helper; the bad release
and tag stay in history as a record.

### Running the builder locally

The builder runs on a developer machine for inspection (Python 3.11 or later
and a Go toolchain); it never commits, tags, pushes or publishes:

```sh
python3 scripts/package_release.py --platform-source ../ork-meter \
  --ork-meter-ref "$(git -C ../ork-meter rev-parse HEAD)" \
  --targets linux-amd64 --skip-wheel --output /tmp/ork-meter-codex-release
```

Omit `--targets` for all five and `--skip-wheel` to build the wheel too. The
output directory must be new or empty. `scripts/build_meter.py` stays the
development builder and writes only `.local/ork-meter` for the host.

## Syncing from the private repository

This repository is a public mirror of the shipped files of
`EDOS-Engineering/codex-plugin`. To take its changes:

```text
python scripts/sync_from_private.py /path/to/codex-plugin
```

The script copies every file this repository already ships under `ork_codex/`,
`hooks/`, `contracts/`, `scripts/`, `tests/`, `.codex-plugin/`,
`.claude-plugin/`, `pyproject.toml`, `plugin.json` and the CI and release
workflows, reapplies the public-only edits (repository name, licence
identifier) and prunes test methods that import benchmark modules this
repository does not carry. It lists files new in the private checkout without
copying them. It never touches LICENSE, SECURITY.md, README.md, `docs/`, the
ruleset, the notices or `scripts/replay.py`. Review `git diff`, run
`scripts/validate.py`, `scripts/replay.py` and the unit tests, then open a
pull request.

## What this process does not cover

- **No automated acceptance.** The workflow proves the linux-amd64 helper
  starts on the runner. It does not run setup on macOS, Windows or
  linux-arm64, and does not pair with Ork Meter. Someone installs the release
  on each platform and runs `setup`, pairs, and checks a hosted receipt before
  announcing it.
- **No signing.** The helpers are not Developer ID signed or notarized and
  the Windows binary is unsigned. A downloaded binary carries the
  `com.apple.quarantine` attribute on macOS when a browser fetched it; the
  plugin's own download writes the file with Python, which sets no quarantine
  attribute, but endpoint-security products and MDM policies can still refuse
  unsigned binaries. Signing is a follow-up.
- **No reproducibility claim.** The build flags are deterministic and the
  ork-meter ref and Go version are recorded, but byte-for-byte
  reproducibility across runners has not been checked.
- **No changelog.** Release notes list digests, not changes.
