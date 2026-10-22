"""Build the release assets for one plugin version: helpers, wheel, digests, manifest.

Compiles ork-meter's cmd/ork-meter (the reviewed helper this plugin drives)
for each target into <output>/ork-meter-<os>-<arch> (.exe on Windows) with
CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -buildid=" -buildvcs=false,
builds this package's wheel, writes a <asset>.sha256 file per asset in
sha256sum -c format and a manifest.json (plugin version, ork-meter ref and
commit, Go version, sha256 per asset). It also writes
ork-meter-THIRD_PARTY_NOTICES.txt next to the helpers: the licence and notice
files of every non-standard-library Go module compiled into any target, read
from the module cache by the same Go toolchain. A module without a licence
file stops the build, so no helper ships without its notices.
ork_codex/helper.py downloads the host's
helper from the published GitHub Release and verifies it against that
manifest. The package-release workflow calls this; it also runs locally. It
builds files only: it never commits, tags, pushes or publishes anything.

The development builder, scripts/build_meter.py, is separate and writes only
.local/ork-meter for the host. Python 3.11+, standard library only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

TARGETS = ("darwin-arm64", "darwin-amd64", "linux-amd64", "linux-arm64", "windows-amd64")
HELPER_PACKAGE = "./cmd/ork-meter"
HELPER_MAIN = "cmd/ork-meter/main.go"
HELPER = "ork-meter"
NOTICES = "ork-meter-THIRD_PARTY_NOTICES.txt"
LICENCE_FILE = re.compile(r"^(licen[cs]e|copying|notice|patents)([._-].*)?$", re.IGNORECASE)
# The Go distribution's LICENSE (https://go.dev/LICENSE); some toolchain
# packages omit it from GOROOT, so it is reproduced here.
GO_LICENSE = """Copyright 2009 The Go Authors.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

   * Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.
   * Redistributions in binary form must reproduce the above
copyright notice, this list of conditions and the following disclaimer
in the documentation and/or other materials provided with the
distribution.
   * Neither the name of Google LLC nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE."""
MODULE_FORMAT = "{{with .Module}}{{if not .Main}}{{.Path}}\t{{.Version}}\t{{with .Replace}}{{.Dir}}{{else}}{{.Dir}}{{end}}{{end}}{{end}}"
PLUGIN = "ork-meter-codex"
BUILD_FLAGS = ["-trimpath", "-ldflags", "-s -w -buildid=", "-buildvcs=false"]
ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def asset_name(target):
    return f"{HELPER}-{target}" + (".exe" if target.startswith("windows") else "")


def parse_target(value):
    if value not in TARGETS:
        raise argparse.ArgumentTypeError(f"unsupported target {value!r}; choose from {', '.join(TARGETS)}")
    return value


def plugin_version(root):
    """The one version every manifest in the checkout agrees on."""
    portable = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    compat = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    versions = {portable.get("version"), compat.get("version"), project.get("version")}
    if portable.get("name") != PLUGIN or compat.get("name") != PLUGIN or project.get("name") != PLUGIN:
        raise SystemExit("plugin.json, .codex-plugin/plugin.json and pyproject.toml must all name " + PLUGIN)
    if len(versions) != 1:
        raise SystemExit(f"plugin.json, .codex-plugin/plugin.json and pyproject.toml disagree on the version: {sorted(map(str, versions))}")
    version = versions.pop()
    if not isinstance(version, str) or not SEMVER.match(version):
        raise SystemExit(f"plugin version must be semver, got {version!r}")
    return version


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_sum(output, name, value):
    (output / (name + ".sha256")).write_text(f"{value}  {name}\n", encoding="utf-8", newline="\n")


def build_helper(go, source, output, target):
    target_os, target_arch = target.split("-", 1)
    name = asset_name(target)
    environment = dict(os.environ, GOOS=target_os, GOARCH=target_arch, CGO_ENABLED="0")
    subprocess.run([go, "build", *BUILD_FLAGS, "-o", str(output / name), HELPER_PACKAGE],
                   cwd=source, env=environment, check=True)
    value = digest(output / name)
    write_sum(output, name, value)
    return name, value


def helper_modules(go, source, target):
    """{path: (version, dir)} for every non-main module compiled into the target."""
    target_os, target_arch = target.split("-", 1)
    environment = dict(os.environ, GOOS=target_os, GOARCH=target_arch, CGO_ENABLED="0")
    listing = subprocess.run([go, "list", "-mod=readonly", "-deps", "-f", MODULE_FORMAT, HELPER_PACKAGE],
                             cwd=source, env=environment, check=True, capture_output=True, text=True).stdout
    modules = {}
    for line in listing.splitlines():
        if not line.strip():
            continue
        path, version, directory = line.split("\t")
        if not directory:
            raise SystemExit(f"Go module {path} {version} is not in the module cache; cannot read its licence")
        modules[path] = (version, directory)
    return modules


def write_notices(go, source, output, targets):
    """Write the licence and notice files of every Go module compiled into the helpers."""
    modules = {}
    for target in targets:
        modules.update(helper_modules(go, source, target))
    sections = [f"{'=' * 78}\nGo standard library: LICENSE\n{'=' * 78}\n\n{GO_LICENSE}\n"]
    for path, (version, directory) in sorted(modules.items()):
        files = sorted(p for p in Path(directory).iterdir() if p.is_file() and LICENCE_FILE.match(p.name))
        if not files:
            raise SystemExit(f"Go module {path} {version} has no licence file in {directory}; refusing to ship it without notices")
        for file in files:
            text = file.read_text(encoding="utf-8", errors="replace").rstrip()
            sections.append(f"{'=' * 78}\n{path} {version or '(local)'}: {file.name}\n{'=' * 78}\n\n{text}\n")
    header = ("Third-party notices for the Ork Meter helper (ork-meter-<os>-<arch>).\n\n"
              f"The helper statically links the Go standard library and the {len(modules)} Go module(s) below.\n"
              "Each is licensed under the terms reproduced here.\n")
    (output / NOTICES).write_text(header + "".join("\n" + s for s in sections), encoding="utf-8", newline="\n")
    value = digest(output / NOTICES)
    write_sum(output, NOTICES, value)
    return value


def build_wheel(python, root, output):
    before = set(output.iterdir())
    subprocess.run([python, "-m", "pip", "wheel", "--no-deps", "--quiet", "--wheel-dir", str(output), str(root)], check=True)
    wheels = [p for p in output.iterdir() if p not in before and p.suffix == ".whl"]
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one new wheel in {output}, found {[p.name for p in wheels]}")
    value = digest(wheels[0])
    write_sum(output, wheels[0].name, value)
    return wheels[0].name, value


def git_commit(path):
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def build(source, output, ork_meter_ref, targets=TARGETS, *, go="go", python=sys.executable, wheel=True, root=ROOT):
    source, output, root = Path(source).resolve(), Path(output).resolve(), Path(root).resolve()
    if not (source / HELPER_MAIN).is_file():
        raise SystemExit(f"{source} has no {HELPER_MAIN}; choose an ork-meter ref that carries the helper")
    version = plugin_version(root)
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"{output} is not empty; assets are assembled into a fresh directory")
    output.mkdir(parents=True, exist_ok=True)
    go_version = subprocess.check_output([go, "version"], cwd=source, text=True).strip()
    helpers = {}
    for target in targets:
        name, value = build_helper(go, source, output, target)
        helpers[target] = {"asset": name, "sha256": value}
    notices_digest = write_notices(go, source, output, targets)
    wheel_name = wheel_digest = None
    if wheel:
        wheel_name, wheel_digest = build_wheel(python, root, output)
    assets = {entry["asset"]: entry["sha256"] for entry in helpers.values()}
    assets[NOTICES] = notices_digest
    if wheel_name:
        assets[wheel_name] = wheel_digest
    record = {
        "plugin": PLUGIN,
        "plugin_version": version,
        "tag": f"v{version}",
        "plugin_commit": git_commit(root),
        "ork_meter_repository": "EDOS-Engineering/ork-meter",
        "ork_meter_ref": ork_meter_ref,
        "ork_meter_commit": git_commit(source),
        "go_version": go_version,
        "build": "CGO_ENABLED=0 go build " + " ".join(f'"{f}"' if " " in f else f for f in BUILD_FLAGS) + " " + HELPER_PACKAGE,
        "helpers": helpers,
        "notices": NOTICES,
        "wheel": wheel_name,
        "assets": dict(sorted(assets.items())),
    }
    (output / "manifest.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    for name, value in sorted(assets.items()):
        print(f"{value}  {name}")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--platform-source", required=True, type=Path, help="an EDOS-Engineering/ork-meter checkout at the ref to build")
    parser.add_argument("--ork-meter-ref", required=True, help="the ork-meter tag or commit that checkout is at, recorded in manifest.json")
    parser.add_argument("--targets", nargs="+", type=parse_target, default=list(TARGETS), metavar="OS-ARCH")
    parser.add_argument("--output", required=True, type=Path, help="an empty or new directory for the assets")
    parser.add_argument("--go", default="go", help="the Go toolchain; the workflow installs the one ork-meter's go.mod names")
    parser.add_argument("--skip-wheel", action="store_true", help="build the helpers only")
    args = parser.parse_args(argv)
    build(args.platform_source, args.output, args.ork_meter_ref, args.targets, go=args.go, wheel=not args.skip_wheel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
