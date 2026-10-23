"""Sync the shipped files of this public mirror from a private codex-plugin checkout.

    python scripts/sync_from_private.py /path/to/codex-plugin

Copies every file this repository already ships under the mirrored paths from
the private checkout, then reapplies the public-only edits (repository name,
licence identifier) and drops test methods that import benchmark modules the
mirror does not carry. Files that exist only in the private checkout are listed,
not copied: add them by hand if they belong here. Public-owned files (LICENSE,
SECURITY.md, README.md, docs/, the ruleset, scripts/replay.py and the notices)
are never touched. Review `git diff`, run scripts/validate.py, scripts/replay.py
and the unit tests, then commit.
"""
import ast
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PUBLIC = "EDOS-Engineering/ork-meter-codex"
PRIVATE = "EDOS-Engineering/codex-plugin"

# Paths whose files are mirrored verbatim (then substituted), when the mirror already has them.
MIRRORED = ["ork_codex", "hooks", "contracts", "scripts", "tests", ".codex-plugin", ".claude-plugin",
            "pyproject.toml", "plugin.json", ".github/workflows/ci.yml", ".github/workflows/package-release.yml"]
# Shipped files the mirror maintains itself; the private copy is not taken.
PUBLIC_OWNED = {"scripts/replay.py", "scripts/sync_from_private.py"}
# Private modules that the mirror does not ship; tests importing them are pruned.
PRIVATE_MODULES = {"experiments", "benchmark_tokens", "benchmark_auto_hook", "benchmark_baseline",
                   "benchmark_decision", "benchmark_filters", "benchmark_idle", "benchmark_live_codex",
                   "benchmark_meter", "benchmark_native_otel", "benchmark_native_task", "benchmark_usage_event",
                   "audit_rollout_trace", "live_budget", "pilot_budget", "probe_codex",
                   "public_snake_contract_tests", "qualify_auto_output", "qualify_first_output_context",
                   "qualify_tracking_latency", "screen_output_retention", "snake3d_verifier_probe",
                   "snake3d_verifier_tests", "verify_snake3d"}
SUBSTITUTIONS = {
    "ork_codex/helper.py": [(PRIVATE, PUBLIC)],
    "tests/test_helper.py": [(PRIVATE, PUBLIC)],
    ".github/workflows/package-release.yml": [(f"marketplace add {PRIVATE}", f"marketplace add {PUBLIC}")],
    "contracts/README.md": [("<codex-plugin checkout>", "<ork-meter-codex checkout>")],
    ".codex-plugin/plugin.json": [('"license": "Proprietary"', '"license": "LicenseRef-Ork-Meter-Limited-Use"')],
}


def shipped(rel):
    return not any(part == "__pycache__" or part.endswith(".pyc") for part in rel.parts)


def prune_private_tests(path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    names, drop = set(), []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in PRIVATE_MODULES:
            names |= {a.asname or a.name for a in node.names}
            drop.append((node.lineno, node.end_lineno))
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] in PRIVATE_MODULES for a in node.names):
            names |= {a.asname or a.name for a in node.names}
            drop.append((node.lineno, node.end_lineno))
    if not names:
        return False
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for fn in cls.body:
            if isinstance(fn, ast.FunctionDef):
                used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
                if used & names:
                    drop.append((min([fn.lineno] + [d.lineno for d in fn.decorator_list]), fn.end_lineno))
    kill = {i for a, b in drop for i in range(a - 1, b)}
    lines = [l for i, l in enumerate(src.split("\n")) if i not in kill]
    path.write_text(re.sub(r"\n{4,}", "\n\n\n", "\n".join(lines)), encoding="utf-8", newline="")
    return True


def main():
    if len(sys.argv) != 2 or not (pathlib.Path(sys.argv[1]) / "ork_codex").is_dir():
        raise SystemExit(__doc__)
    private = pathlib.Path(sys.argv[1]).resolve()
    before, new = {}, []
    for top in MIRRORED:
        here, there = ROOT / top, private / top
        files = [p.relative_to(ROOT) for p in here.rglob("*") if p.is_file()] if here.is_dir() else [pathlib.Path(top)]
        for rel in sorted(files):
            if not shipped(rel) or rel.as_posix() in PUBLIC_OWNED:
                continue
            src = private / rel
            if not src.is_file():
                print("private lacks:", rel)
                continue
            before[rel] = (ROOT / rel).read_bytes()
            shutil.copyfile(src, ROOT / rel)
        if there.is_dir():
            for p in there.rglob("*"):
                rel = p.relative_to(private)
                if p.is_file() and shipped(rel) and not (ROOT / rel).exists():
                    new.append(rel.as_posix())
    for rel, pairs in SUBSTITUTIONS.items():
        p = ROOT / rel
        text = p.read_text(encoding="utf-8")
        for old, new_text in pairs:
            text = text.replace(old, new_text)
        p.write_text(text, encoding="utf-8", newline="")
    for p in sorted((ROOT / "tests").glob("*.py")):
        if prune_private_tests(p):
            print("pruned benchmark tests:", p.relative_to(ROOT))
    for rel, old in before.items():
        if (ROOT / rel).read_bytes() != old:
            print("changed:", rel.as_posix())
    for rel in new:
        print("new in private (not copied):", rel)


if __name__ == "__main__":
    main()
