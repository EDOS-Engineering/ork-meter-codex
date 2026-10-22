"""CLI commands for plugin tools and local workflows."""
import argparse
import json
import os
from pathlib import Path
import sys
import sqlite3
import subprocess
from dataclasses import replace
from . import __version__
from .audit import audit
from .meter import command, binary
from .mcp import Service, serve
from . import entitlement
from .policy import Policy
from .retrieval import artifact
from .runner import run
from .storage import Store, workspace_state
from .delivery import OutputDeliveryError


def parser():
    root = argparse.ArgumentParser(prog="ork-codex")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--state", type=Path, help="private local state directory")
    sub = root.add_subparsers(dest="command", required=True)
    runner = sub.add_parser("run", help="wrap a selected noninteractive command; keep its exit status")
    runner.add_argument("--kind", choices=["unknown", "test", "build", "log", "search", "git"], default="unknown")
    runner.add_argument("--full", action="store_true")
    runner.add_argument("--keep", action="append", default=[], help="retain context around this literal text; repeat for additional evidence")
    runner.add_argument("--workspace", type=Path, help="explicit project root shared with MCP")
    runner.add_argument("argv", nargs=argparse.REMAINDER)
    sub.add_parser("serve", help="MCP stdio retrieval server")
    sub.add_parser("hook", help="silent compatibility handler; no startup context")
    optimize = sub.add_parser("optimize", help="install, inspect or remove automatic local output filtering")
    optimize.add_argument("action", choices=["install", "status", "uninstall"])
    optimize.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    tracking = sub.add_parser("tracking", help="integrated usage tracking; no separate helper or daemon")
    tracking.add_argument("action", choices=["connect", "prepare", "claim", "install", "status", "sync"])
    tracking.add_argument("--dev", action="store_true")
    tracking.add_argument("--reference")
    tracking.add_argument("--session", help="current Codex session ID for an immediate sync")
    tracking.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex"))
    savings = sub.add_parser("savings", help="local estimates, not billing evidence")
    savings.add_argument("--workspace", type=Path)
    a = sub.add_parser("audit", help="instruction sizes and MCP names only")
    a.add_argument("--workspace", type=Path, default=Path.cwd())
    a.add_argument("--codex-home", type=Path)
    source = sub.add_parser("source", help="workspace source retrieval (JSON arguments)")
    source.add_argument("--workspace", type=Path, default=Path.cwd())
    source.add_argument("arguments", help="source tool arguments as JSON")
    recovery = sub.add_parser("artifact")
    recovery.add_argument("handle")
    recovery.add_argument("--workspace", type=Path)
    recovery.add_argument("--op", choices=["expand", "search", "json", "raw"], default="expand")
    recovery.add_argument("--start", type=int, default=1)
    recovery.add_argument("--count", type=int, default=200)
    recovery.add_argument("--query")
    recovery.add_argument("--pointer", action="append", dest="pointers")
    recovery.add_argument("--full", action="store_true")
    recovery.add_argument("--intervention", help="delivery ID for unambiguous recovery accounting")
    meter = sub.add_parser("meter", help="Ork Meter helper integration")
    meter.add_argument("action", choices=["prepare", "claim", "show", "verify", "capture", "status", "diagnose", "check-local", "progress", "retry", "prepare-config", "prepare-removal", "forget"])
    meter.add_argument("--reference")
    meter.add_argument("--settings")
    meter.add_argument("--config")
    meter.add_argument("--output")
    meter.add_argument("--confirm-local-removal", action="store_true")
    meter.add_argument("--dev", action="store_true")
    sub.add_parser("doctor", help="check package and runtime dependencies locally")
    setup = sub.add_parser("setup", help="interactive pairing, installation and a tracked Codex session")
    target = setup.add_mutually_exclusive_group()
    target.add_argument("--dev", dest="dev", action="store_const", const=True, default=None)
    target.add_argument("--prod", dest="dev", action="store_const", const=False)
    setup.add_argument("--codex", metavar="PATH",
                       help="native Codex CLI executable (default: CODEX_CLI_PATH, Codex config, PATH, known install locations)")
    connection = setup.add_mutually_exclusive_group()
    connection.add_argument("--reference", help="resume an existing public pairing reference")
    connection.add_argument("--new-connection", action="store_true",
                            help="start a new browser approval after an expired, revoked or unavailable connection")
    return root


def emit(value):
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    receipt = getattr(value, "receipt", None)
    try:
        if sys.stdout.write(rendered) != len(rendered):
            raise OSError("short JSON output write")
        sys.stdout.flush()
    except (OSError, ValueError):
        if receipt:
            receipt.abandon("write_failed")
        raise
    if receipt:
        receipt.confirm(len(rendered.encode("utf-8")))


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "tracking":
        from . import tracking
        try:
            home = args.codex_home
            state = tracking.private_directory(home / "ork-tracking")
            ref = args.reference
            if args.action in {"prepare", "connect"}:
                if args.action == "connect" and not sys.stdin.isatty():
                    raise ValueError("Run tracking connect in your own terminal; browser claim codes never go through chat")
                pairing = tracking.public(tracking.load(ref)) if ref else tracking.prepare(args.dev)
                ref = pairing["reference"]
                tracking.write_json(state / "pending.json", {"reference": ref})
                emit(pairing)
                if args.action == "prepare":
                    return
                import webbrowser
                webbrowser.open(pairing["approval_url"])
            if ref is None:
                candidate = state / ("pending.json" if args.action == "claim" else "connection.json")
                if not candidate.is_file():
                    raise ValueError("No WSL tracking pairing; run tracking connect --dev in your terminal")
                ref = json.loads(candidate.read_text())["reference"]
            if args.action in {"claim", "connect"}:
                emit(tracking.claim(ref))
                emit(tracking.install(home, ref))
                if args.session:
                    emit(tracking.sync(home, ref, args.session))
                return
            if args.action == "install":
                emit(tracking.install(home, ref))
            elif args.action == "sync":
                emit(tracking.sync(home, ref, args.session))
            elif args.action == "status":
                result = tracking.status(ref)
                error = state / "last-error.json"
                result["last_hook_error"] = json.loads(error.read_text()) if error.exists() else None
                emit(result)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            raise SystemExit(2)
        except EOFError:
            print("Claim-code entry cancelled.", file=sys.stderr)
            raise SystemExit(2)
        except KeyboardInterrupt:
            raise SystemExit(130)
        except (OSError, KeyError, TypeError):
            print("Tracking could not finish. Check the WSL credential store, browser pairing and site connectivity; no transcript content was uploaded.", file=sys.stderr)
            raise SystemExit(2)
        return
    if args.command == "optimize":
        from .auto_hook import configure
        try:
            emit(configure(args.codex_home, args.action))
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            raise SystemExit(2)
        return
    if args.command == "setup":
        from .onboarding import setup
        try:
            code = setup(args.dev, args.reference, new_connection=args.new_connection, codex=args.codex)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            code = 2
        except (OSError, subprocess.TimeoutExpired):
            print("Setup could not finish. Check terminal access, the helper and network connectivity; existing pairing references can be resumed.", file=sys.stderr)
            code = 2
        except KeyboardInterrupt:
            code = 130
        raise SystemExit(code)
    if args.command == "hook":
        # Older hook definitions remain fail-open while their plugin is
        # refreshed. Never append model instructions.
        return
    if args.command == "meter":
        try:
            code = command(args.action, args.reference, args.settings, args.config, args.output,
                           args.confirm_local_removal, args.dev)
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            code = 2
        raise SystemExit(code)
    if args.command == "audit":
        emit(audit(args.workspace, args.codex_home))
        return
    if args.command == "serve":
        # Startup must not create state in a plugin cache or guess a project.
        serve(Store(args.state) if args.state else None)
        return
    if args.command == "doctor":
        root = Path(__file__).resolve().parents[1]
        try:
            helper = binary()
        except ValueError:
            helper = None
        emit({"plugin_version": __version__, "python": sys.version.split()[0],
              "meter_helper": helper, "manifest": (root / "plugin.json").is_file(),
              "mode": entitlement.gate(Policy.environment()).mode, "workspace": str(Path.cwd()),
              "savings": entitlement.status(), "savings_detail": entitlement.message(),
              "hosted_delivery_verified": False,
              "note": "Local dependency check. Use meter status/diagnose for authorization and receipt evidence."})
        return
    if args.command == "run":
        cmd = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        # A broken state store cannot prevent an already selected command.
        try:
            store = Store(args.state or (workspace_state(args.workspace, anchor=True) if args.workspace else None))
        except (OSError, RuntimeError, sqlite3.Error):
            store = None
        try:
            if len(args.keep) > 20 or any(not query or len(query) > 1000 for query in args.keep):
                raise ValueError("--keep requires 1..1000 characters per query, at most 20 queries")
            code = run(cmd, args.kind, store, policy=entitlement.gate(replace(Policy.environment(), keep=tuple(args.keep))), full=args.full)
        except OutputDeliveryError as error:
            print(f"Command ran (exit {error.exit_code}); output delivery failed. Do not rerun automatically."
                  + (f" Saved original: artifact:{error.artifact}." if error.artifact else ""), file=sys.stderr)
            code = error.exit_code if error.exit_code and error.exit_code > 0 else 74
        except (OSError, ValueError) as error:
            print("Command unavailable: " + str(error), file=sys.stderr)
            code = 127
        raise SystemExit(code)
    try:
        workspace = getattr(args, "workspace", None)
        store = Store(args.state or (workspace_state(workspace, anchor=True) if workspace else None))
        if args.command == "savings":
            emit(store.report())
        elif args.command == "source":
            emit(Service(store, args.workspace).call("source", json.loads(args.arguments)))
        elif args.command == "artifact":
            if args.op == "raw":
                raw = store.get(args.handle)
                context = store.recovery_context(args.handle, args.intervention)
                if sys.stdout.buffer.write(raw) != len(raw):
                    raise OSError("short raw recovery write")
                sys.stdout.buffer.flush()
                accounted = store.charge_recovery(args.handle, len(raw), context)
                if not accounted:
                    print("Recovery returned; optional accounting is unavailable.", file=sys.stderr)
            else:
                emit(artifact(store, args.handle, args.op, args.start, args.count,
                              args.query, args.pointers, args.full, args.intervention))
    except (OSError, ValueError, KeyError, UnicodeError) as error:
        print("Local evidence unavailable: " + str(error), file=sys.stderr)
        raise SystemExit(2)
