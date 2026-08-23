"""CLI entry point for luaudit.

The contract is a plain, fast, deterministic CLI that agents call through
their own terminal. No MCP server, no always-on process, no LLM turn needed.

Commands:
    luaudit check FILE|DIR ...   run luau-lsp + selene, print diagnostics
    luaudit format FILE ...      format files in place with stylua
    luaudit init                 write default selene/luaurc configs
    luaudit doctor               verify toolchain (default no-op happy path)

Distribution is plugin-first: install the plugin for Claude Code or Codex
from the luaudit marketplace (see README). The CLI remains the engine
for on-demand checks.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import bootstrap
from . import plugin as plugin_mod
from .runners import check_files
from .version import __version__


def _cmd_check(args: argparse.Namespace) -> int:
    bootstrap.ensure_tools()
    targets = args.paths
    if not targets:
        targets = ["./"]
    result = check_files(targets, cwd=args.cwd)
    summary = result.get("summary", {})
    if args.json:
        print(json.dumps(result, indent=2))
    elif summary.get("total", 0) == 0:
        pass  # silent on clean: exit 0, no output (the documented contract)
    else:
        for d in result.get("diagnostics", []):
            rel = d["file"]
            try:
                rel = str(Path(d["file"]).resolve().relative_to(Path(args.cwd).resolve()))
            except (ValueError, OSError):
                pass
            print(f"{rel}:{d['line']}:{d['column']}: {d['severity'].upper()} [{d['source']}/{d['code']}] {d['message']}")
        print(f"summary: {summary['errors']} errors, {summary['warnings']} warnings, {summary['total']} total")
    if summary.get("errors", 0) > 0:
        return 1
    if getattr(args, "warnings", False) and summary.get("warnings", 0) > 0:
        return 1
    return 0


def _cmd_format(args: argparse.Namespace) -> int:
    bootstrap.ensure_tools()
    if not bootstrap.has_stylua():
        print("stylua unavailable; cannot format", file=sys.stderr)
        return 2
    changed = bootstrap.format_files(args.paths, cwd=args.cwd)
    for f in changed:
        print(f"formatted {f}")
    if not changed:
        print("nothing to format")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    wrote = bootstrap.init_configs(Path(args.dir))
    if wrote:
        print(f"wrote configs to {args.dir}")
    else:
        print(f"configs already present in {args.dir}")
    return 0


def _cmd_sourcemap(args: argparse.Namespace) -> int:
    from . import sourcemapper
    out = sourcemapper.generate(args.dir, args.output)
    if not out["ok"]:
        print(out["error"], file=sys.stderr)
        return 2
    print(f"wrote {out['output']} ({out['scripts']} scripts)")
    return 0


def _cmd_unmute(args: argparse.Namespace) -> int:
    from .deltastore import DeltaStore
    removed = DeltaStore().unmute(getattr(args, "fingerprint", None))
    if removed:
        print(f"unmuted {removed} fingerprint(s); those warnings will surface again")
    else:
        print("nothing to unmute")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    bootstrap.ensure_tools()
    if getattr(args, "bug_report", False):
        st = plugin_mod.status()
        paths = bootstrap.get_paths()
        print("luaudit bug report -- copy everything below this line")
        print(f"luaudit: {__version__}")
        print(f"python: {sys.version.split()[0]} ({sys.platform})")
        print(f"platform: {bootstrap._get_platform()}")
        print(f"cache: {bootstrap.CACHE_DIR}")
        for name in ("luau_lsp", "selene", "stylua", "defs"):
            p = paths.get(name)
            ok = p is not None and p.exists()
            print(f"{name}: {'ok' if ok else 'missing'} ({p})")
        print(f"studio-mirror installed: {st['installed']} schema: {st.get('schema')} up_to_date: {st.get('up_to_date')}")
        print(f"last_error: {bootstrap.last_error() or 'none'}")
        print("--- luaudit.log tail ---")
        print(bootstrap.read_log_tail())
        print("--- end bug report ---")
        return 0
    paths = bootstrap.get_paths()
    problems: list[str] = []
    for name in ("luau_lsp", "selene", "stylua"):
        p = paths.get(name)
        ok = p is not None and p.exists()
        print(f"{name}: {'ok' if ok else 'missing'}")
        if not ok:
            problems.append(name)
    defs = paths.get("defs")
    ok_defs = defs is not None and defs.exists()
    print(f"defs: {'ok' if ok_defs else 'missing'}")
    if not ok_defs:
        problems.append("defs")
    st = plugin_mod.status()
    if st["installed"] and st["up_to_date"]:
        print("studio-mirror: ok")
    elif st["installed"]:
        print(f"studio-mirror: stale ({st['note']})")
        print('fix: luaudit plugin install --yes')
    else:
        print(f"studio-mirror: not installed ({st['plugins_dir']})")
        print('fix (optional): luaudit plugin install --yes')
    if problems:
        print(f"problems: {', '.join(problems)}", file=sys.stderr)
        return 1
    return 0


def _cmd_plugin(args: argparse.Namespace) -> int:
    if args.action == "install":
        out = plugin_mod.install(yes=args.yes,
                                 root=getattr(args, "root", ".") or ".",
                                 force_mode=getattr(args, "mirror_mode", None))
        print(f"{out['note']}: {out['path']}")
        if out.get("mode"):
            print(f"mirror-mode: {out['mode']}")
        for key in ("restart_note", "standdown_note", "ask_note"):
            if out.get(key):
                print(out[key])
        return 0 if out["installed"] else 2
    if args.action == "remove":
        out = plugin_mod.remove()
        print(f"{out['note']}: {out['path']}")
        return 0 if out["removed"] else 0
    # status (default when no action given)
    st = plugin_mod.status()
    for k in ("engine_version", "engine_schema", "plugins_dir", "installed",
              "schema", "up_to_date", "mode"):
        print(f"{k}: {st[k]}")
    if st["note"]:
        print(f"note: {st['note']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="luaudit",
        description="Luau diagnostics for AI coding agents (luau-lsp + selene + stylua).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="type-check and lint files or a directory")
    p_check.add_argument("paths", nargs="*")
    p_check.add_argument("--json", action="store_true", help="emit JSON")
    p_check.add_argument("--warnings", action="store_true", help="exit non-zero if warnings are present (strict gate)")
    p_check.add_argument("--cwd", default=".")

    p_fmt = sub.add_parser("format", help="format files in place")
    p_fmt.add_argument("paths", nargs="+")
    p_fmt.add_argument("--cwd", default=".")

    p_init = sub.add_parser("init", help="write default selene.toml and .luaurc")
    p_init.add_argument("dir", nargs="?", default=".",
                        help="project directory to write configs into (default: cwd)")
    p_sm = sub.add_parser("sourcemap",
                          help="generate a sourcemap.json for a Script Sync / plain directory tree")
    p_sm.add_argument("dir", nargs="?", default=".",
                      help="directory whose .luau/.lua tree to map (default: cwd)")
    p_sm.add_argument("--output", default=None,
                      help="output path (default: <dir>/sourcemap.json)")
    p_doctor = sub.add_parser("doctor", help="verify toolchain and studio mirror plugin")
    p_doctor.add_argument("--bug-report", action="store_true", dest="bug_report",
                          help="print a paste-ready environment + failure log report")
    p_unmute = sub.add_parser("unmute",
                              help="restore auto-muted warnings so they surface again")
    p_unmute.add_argument("fingerprint", nargs="?", default=None,
                          help="specific fingerprint (code|message); omit to unmute all")
    p_plugin = sub.add_parser("plugin", help="manage the Roblox Studio mirror plugin")
    p_plugin.add_argument("action", nargs="?", default="status",
                          choices=("install", "remove", "status"))
    p_plugin.add_argument("--yes", action="store_true", dest="yes",
                          help="reinstall even if already up to date; required for non-interactive runs")
    p_plugin.add_argument("--mirror-mode", dest="mirror_mode", default=None,
                          choices=("mirror", "external", "ask"),
                          help="skip auto-detection and force the mirror mode")
    p_plugin.add_argument("--root", default=".",
                          help="project directory to probe for existing sync tools (default: cwd)")
    sub.add_parser("version", help="print version")

    args = parser.parse_args(argv)
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "format":
        return _cmd_format(args)
    if args.command == "init":
        return _cmd_init(args)
    if args.command == "sourcemap":
        return _cmd_sourcemap(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "unmute":
        return _cmd_unmute(args)
    if args.command == "plugin":
        return _cmd_plugin(args)
    if args.command == "version":
        print(__version__)
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
