"""Subprocess wrappers for luau-lsp analyze, selene, and stylua.

Pure CLI semantics: check_files accepts file or directory paths,
prints nothing on clean output (hooks/agents rely on exit code + silence).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from . import bootstrap
from .parsers import Diagnostic, merge_diagnostics, to_dict


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 60,
         stdin_input: str | None = None) -> tuple[str, str, int]:
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_input,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        return "", f"Command timed out after {timeout}s: {' '.join(cmd)}", -1
    except FileNotFoundError:
        return "", f"Binary not found: {cmd[0]}", -1


# ---------------------------------------------------------------------------
# Config discovery (walks up the tree)
# ---------------------------------------------------------------------------

def _find_config(start_dir: str, config_names: tuple[str, ...]) -> str | None:
    current = Path(start_dir).resolve()
    while True:
        for name in config_names:
            candidate = current / name
            if candidate.exists():
                return str(candidate)
        if current.parent == current:
            break
        current = current.parent
    return None


def _find_luaurc(start_dir: str) -> str | None:
    return _find_config(start_dir, (".luaurc",))


def _find_selene_toml(start_dir: str) -> str | None:
    return _find_config(start_dir, ("selene.toml", "selene.yml"))


def _find_stylua_toml(start_dir: str) -> str | None:
    return _find_config(start_dir, (".stylua.toml", "stylua.toml"))


def _find_sourcemap(start_dir: str) -> str | None:
    """Walk up from start_dir for a usable sourcemap.

    Returns the path to a sourcemap.json if one exists (used as-is), or
    generates one from a rojo default.project.json via `rojo sourcemap`
    when rojo is installed. Returns None for per-file fallback mode.
    """
    import shutil

    current = Path(start_dir).resolve()
    while True:
        sm = current / "sourcemap.json"
        if sm.exists():
            return str(sm)
        proj = current / "default.project.json"
        if proj.exists():
            rojo = shutil.which("rojo")
            if rojo:
                try:
                    out = subprocess.run(
                        [rojo, "sourcemap", "--output", "sourcemap.json", str(proj)],
                        capture_output=True, text=True, timeout=60, cwd=str(current),
                    )
                    if out.returncode == 0 and sm.exists():
                        return str(sm)
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass
        if current.parent == current:
            break
        current = current.parent
    return None


def _is_abs_like(p: str) -> bool:
    """os.path.isabs, plus Windows drive-letter paths (c:/proj/...) which
    POSIX hosts see as relative but are absolute to a Windows workspace."""
    return os.path.isabs(p) or bool(re.match(r"^[A-Za-z]:[\\/]", p))


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

def run_luau_lsp(filepath: str, project_root: str | None = None,
                 cwd: str | None = None) -> list[Diagnostic]:
    paths = bootstrap.get_paths()
    luau_lsp = str(paths["luau_lsp"])
    defs = str(paths["defs"])
    luaurc = str(paths["luaurc"])

    cmd = [
        luau_lsp, "analyze",
        "--platform", "roblox",
        "--formatter", "plain",
        f"--definitions=@roblox={defs}",
    ]
    target_dir = project_root if project_root else os.path.dirname(filepath)
    project_luaurc = _find_luaurc(target_dir) if target_dir else None
    cmd.append(f"--base-luaurc={project_luaurc or luaurc}")
    sourcemap = _find_sourcemap(target_dir) if target_dir else None
    analyze_cwd = cwd
    if sourcemap:
        cmd.append(f"--sourcemap={sourcemap}")
        analyze_cwd = os.path.dirname(sourcemap)
    cmd.append(filepath)

    stdout, stderr, exit_code = _run(cmd, timeout=60, cwd=analyze_cwd)
    if (exit_code == -1 and not stdout) or (exit_code != 0 and not stdout.strip()):
        # Missing binary (spawn error) OR tool ran but crashed with no output
        # (corrupted download). Both are toolchain failures, never "clean".
        bootstrap.log_event(
            f"ERROR luau-lsp failed on {filepath} (exit {exit_code}): "
            f"{stderr.strip() or 'no output — toolchain likely broken'}"
        )
        return [Diagnostic(
            file=filepath, line=1, column=1, end_line=None, end_column=None,
            code="InternalError", severity="error",
            message=f"luau-lsp failed (exit {exit_code}): {stderr.strip() or 'no output — toolchain likely broken'}", source="luau-lsp",
        )]
    return parse_luau_lsp_safe(stdout, stderr)


def parse_luau_lsp_safe(stdout: str, stderr: str) -> list[Diagnostic]:
    # import inside to avoid circular import of the module that wraps parsers
    from .parsers import parse_luau_lsp
    return parse_luau_lsp(stdout, stderr)


def run_selene(filepath: str, project_root: str | None = None,
               cwd: str | None = None) -> list[Diagnostic]:
    if not bootstrap.has_selene():
        return []
    paths = bootstrap.get_paths()
    selene = str(paths["selene"])
    selene_toml = str(paths["selene_toml"])

    cmd = [selene, "--display-style", "json", "--no-summary"]
    target_dir = project_root if project_root else os.path.dirname(filepath)
    project_selene = _find_selene_toml(target_dir) if target_dir else None
    cmd.append(f"--config={project_selene or selene_toml}")
    cmd.append(filepath)

    stdout, stderr, exit_code = _run(cmd, timeout=60, cwd=cwd)
    if (exit_code == -1 and not stdout) or (exit_code != 0 and not stdout.strip()):
        bootstrap.log_event(
            f"ERROR selene failed on {filepath} (exit {exit_code}): "
            f"{stderr.strip() or 'no output — toolchain likely broken'}"
        )
        return [Diagnostic(
            file=filepath, line=1, column=1, end_line=None, end_column=None,
            code="InternalError", severity="error",
            message=f"selene failed (exit {exit_code}): {stderr.strip() or 'no output — toolchain likely broken'}", source="selene",
        )]
    from .parsers import parse_selene
    return parse_selene(stdout)


def run_stylua_check(filepath: str, project_root: str | None = None,
                     cwd: str | None = None) -> list[Diagnostic]:
    if not bootstrap.has_stylua():
        return []
    paths = bootstrap.get_paths()
    stylua = str(paths["stylua"])

    cmd = [stylua, "--check"]
    target_dir = project_root if project_root else os.path.dirname(filepath)
    if target_dir:
        project_stylua = _find_stylua_toml(target_dir)
        if project_stylua:
            cmd.append(f"--config-path={project_stylua}")
    cmd.append(filepath)

    stdout, stderr, exit_code = _run(cmd, timeout=30, cwd=cwd)
    if exit_code == -1 and not stdout:
        bootstrap.log_event(f"ERROR stylua failed on {filepath} (exit {exit_code}): {stderr.strip()}")
        return [Diagnostic(
            file=filepath, line=1, column=1, end_line=None, end_column=None,
            code="InternalError", severity="error",
            message=f"stylua failed: {stderr}", source="stylua",
        )]
    diagnostics: list[Diagnostic] = []
    if exit_code != 0:
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("Diff in "):
                diff_file = line.replace("Diff in ", "").rstrip(":")
                diagnostics.append(Diagnostic(
                    file=diff_file, line=1, column=1, end_line=None, end_column=None,
                    code="StyLuaFormat", severity="warning",
                    message="Code is not formatted (run luaudit format to fix)",
                    source="stylua",
                ))
    return diagnostics


# ---------------------------------------------------------------------------
# check_files: public entry used by CLI
# ---------------------------------------------------------------------------

def check_files(targets: list[str], cwd: str = ".") -> dict:
    """Check files or directories. Returns MCP-style diagnostics dict.

    If a target is a directory, walks it for .luau/.lua files. On a
    completely clean tree, returns summary total 0 (no diagnostics).
    """
    abs_targets: list[str] = []
    for t in targets:
        p = t if os.path.isabs(t) else os.path.join(cwd, t)
        abs_targets.append(os.path.abspath(p))

    files: list[str] = []
    missing: list[str] = []
    for t in abs_targets:
        if os.path.isfile(t):
            files.append(t)
        elif os.path.isdir(t):
            for root, _, fs in os.walk(t):
                for f in fs:
                    if f.endswith((".luau", ".lua")):
                        files.append(os.path.join(root, f))
        else:
            missing.append(t)

    if missing:
        # A nonexistent target is a hard error, never "clean"
        return {
            "diagnostics": [
                {
                    "file": t,
                    "line": 0,
                    "column": 0,
                    "severity": "error",
                    "source": "luaudit",
                    "code": "NoSuchFile",
                    "message": f"path does not exist: {t}",
                }
                for t in missing
            ],
            "summary": {"errors": len(missing), "warnings": 0, "total": len(missing)},
            "note": "nonexistent target path(s)",
        }

    if not files:
        return {"diagnostics": [], "summary": {"errors": 0, "warnings": 0, "total": 0},
                "note": "No .luau or .lua files found"}

    all_diags: list[Diagnostic] = []
    for f in files:
        # Absolutize inputs: tools echo back what they are given. Feeding
        # relative paths makes luau-lsp emit cwd-relative diagnostics that
        # then get rebased against the wrong base (doubled-path bug).
        f = os.path.abspath(f)
        project_root = os.path.dirname(f)
        luau_results = run_luau_lsp(f, project_root=project_root)
        selene_results = run_selene(f, project_root=project_root)
        stylua_results = run_stylua_check(f, project_root=project_root)
        for d in luau_results + selene_results + stylua_results:
            if not _is_abs_like(d.file):
                # luau-lsp may have run from the sourcemap dir (cwd changed),
                # so relative output paths resolve against that dir.
                sm = _find_sourcemap(project_root)
                base = os.path.dirname(sm) if sm else project_root
                d.file = os.path.abspath(os.path.join(base, d.file))
            else:
                # Drive-letter absolute paths arrive in tool-specific shapes
                # (forward slashes, differing drive case). Normalize so the
                # same file always looks identical across tools.
                d.file = os.path.normpath(d.file)
        all_diags.extend(luau_results + selene_results + stylua_results)

    merged = merge_diagnostics(all_diags)
    merged = _collapse_near_dups(merged)
    return to_dict(merged)


def _collapse_near_dups(merged: list[Diagnostic]) -> list[Diagnostic]:
    """Drop diagnostics repeating one finding a few columns apart.

    luau-lsp sometimes reports a single type mismatch twice (once per
    operand), e.g. TypeError at 4:23 and 4:28 with identical messages.
    Same file+code+message within 10 columns => keep the first.
    """
    out: list[Diagnostic] = []
    for d in merged:
        dup = any(
            o.line == d.line
            and o.code == d.code
            and o.message == d.message
            and os.path.normcase(o.file) == os.path.normcase(d.file)
            and abs(o.column - d.column) <= 10
            for o in out
        )
        if not dup:
            out.append(d)
    return out
