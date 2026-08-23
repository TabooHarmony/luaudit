#!/usr/bin/env python3
"""Standalone luaudit engine bundled inside the luaudit plugin.

Self-contained (stdlib only) so the plugin works after being copied into a
harness's plugin cache with no pip install. Shares the toolchain cache
(~/.luaudit) with the full luaudit CLI.

Two modes:
- hook (default): read a PostToolUse hook event JSON on stdin
  ({tool_name, tool_input:{file_path|file|path}}), check the edited Luau file,
  and print the harness contract
  {"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":...}}
  ONLY when there are errors/warnings. Clean => empty stdout, exit 0.
- check:  luaudit_hook.py check [--json] <paths...>
  Plain CLI semantics (text or JSON), exit non-zero on errors.
  This is the "minimal CLI": it lives inside the plugin, zero install.

Hand-synced copy of the luaudit package engine. Kept in lockstep with
src/luaudit/bootstrap.py; tests/test_plugin_parity.py fails CI if the
versions, download pins, or behavior ever diverge.
"""

from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Toolchain (mirror of luaudit.bootstrap)
# ---------------------------------------------------------------------------

CACHE_DIR = Path(os.environ.get("LUAUDIT_HOME", Path.home() / ".luaudit"))
BIN_DIR = CACHE_DIR / "bin"
DEFS_DIR = CACHE_DIR / "defs"
CONFIG_DIR = CACHE_DIR / "config"

# Failure log (mirror of luaudit.bootstrap): appended on bootstrap/tool
# failures so a failing user has one artifact to report.
LOG_FILENAME = "luaudit.log"
LOG_MAX_BYTES = 512 * 1024


def _log_event(message: str) -> None:
    """Append a timestamped line to the failure log. Best-effort, never raises."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / LOG_FILENAME
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            data = path.read_bytes()[-LOG_MAX_BYTES // 2:]
            path.write_bytes(data)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"{stamp} {message}\n")
    except Exception:
        pass

DEFS_URL = "https://luau-lsp.pages.dev/type-definitions/globalTypes.d.luau"
DEFS_FILENAME = "globalTypes.d.luau"

LUAU_LSP_VERSION = "1.68.1"
SELENE_VERSION = "0.31.0"
STYLUA_VERSION = "2.5.2"
DEFS_MAX_AGE = 7 * 24 * 60 * 60

# SHA256 pins for every toolchain artifact, keyed by download URL. Verified
# before extraction; a mismatch aborts that tool's install. Keep in sync with
# src/luaudit/bootstrap.py (CI checks parity). When bumping a *_VERSION
# constant you MUST add the new artifact's hash here -- an unpinned URL is a
# hard error, never a silent downgrade of supply-chain guarantees.
SHA256_PINS: dict[str, str] = {
    # luau-lsp 1.68.1 (macos zip serves both arm64 and x86_64)
    f"https://github.com/JohnnyMorganz/luau-lsp/releases/download/{LUAU_LSP_VERSION}/luau-lsp-win64.zip":
        "15f2add7c70191c5cd636b047968760f0056893b63be10294453c75430bcb339",
    f"https://github.com/JohnnyMorganz/luau-lsp/releases/download/{LUAU_LSP_VERSION}/luau-lsp-macos.zip":
        "e32a71823ee47471d931a03e4186ced2b4c43bb785c8fe05de901fe54c6ebe21",
    f"https://github.com/JohnnyMorganz/luau-lsp/releases/download/{LUAU_LSP_VERSION}/luau-lsp-linux-x86_64.zip":
        "ddb5fe8fd503bbcb76ee439fbd6522efbfe9f0098be5a233401e493c579fc4a9",
    f"https://github.com/JohnnyMorganz/luau-lsp/releases/download/{LUAU_LSP_VERSION}/luau-lsp-linux-arm64.zip":
        "4ab4906dee6041ec23a8b0abdd81c1fdbd770c8c2dcb931e39a33f6790d779f3",
    # selene 0.31.0 (macos zip serves both arches)
    f"https://github.com/Kampfkarren/selene/releases/download/{SELENE_VERSION}/selene-{SELENE_VERSION}-windows.zip":
        "c5d5d087daa8e38bd71680b2202a407e5d4bc00fd584a648dec17ef9b29a2b73",
    f"https://github.com/Kampfkarren/selene/releases/download/{SELENE_VERSION}/selene-{SELENE_VERSION}-macos.zip":
        "67f644e57e14ccb74a0c272bc44af0dc7909d8bdff58e4e59bb3524717da5741",
    f"https://github.com/Kampfkarren/selene/releases/download/{SELENE_VERSION}/selene-{SELENE_VERSION}-linux.zip":
        "dac452422747999ec4919bbb8bb52992b66aae533b60022bf005669de8616671",
    # stylua 2.5.2
    f"https://github.com/JohnnyMorganz/StyLua/releases/download/v{STYLUA_VERSION}/stylua-windows-x86_64.zip":
        "e77d0ea1226b8b389b43f702240091249a96eea25857281f90ea24d0eb9eb969",
    f"https://github.com/JohnnyMorganz/StyLua/releases/download/v{STYLUA_VERSION}/stylua-macos-aarch64.zip":
        "92ff0889e16324801bc072692974bb67f8161e62010fc90f96c62a17f81f32c7",
    f"https://github.com/JohnnyMorganz/StyLua/releases/download/v{STYLUA_VERSION}/stylua-macos-x86_64.zip":
        "53c50a1605d0a6345d160a1a5a21db40bcf2bf9cd23c17f7c277a63a1bff3a7f",
    f"https://github.com/JohnnyMorganz/StyLua/releases/download/v{STYLUA_VERSION}/stylua-linux-x86_64.zip":
        "bcb0d855e91f102f28a370e850f8566b3b44b79e6274d806ea5246837c0fd5ab",
    f"https://github.com/JohnnyMorganz/StyLua/releases/download/v{STYLUA_VERSION}/stylua-linux-aarch64.zip":
        "0ef2ebf0b7e5a652b65c4cb96c6d9ffb3981a98547de3c764465bbf54a8d761a",
}


def _platform() -> tuple[str, str]:
    os_name = platform.system().lower()
    machine = platform.machine().lower()
    if os_name == "windows":
        return "windows", "x86_64"
    if os_name == "darwin":
        return ("macos", "arm64") if ("arm" in machine or "aarch" in machine) else ("macos", "x86_64")
    return ("linux", "arm64") if ("arm" in machine or "aarch" in machine) else ("linux", "x86_64")


def _urls() -> dict[str, str]:
    os_name, arch = _platform()
    base = {
        "luau-lsp": {
            ("windows", "x86_64"): f"https://github.com/JohnnyMorganz/luau-lsp/releases/download/{LUAU_LSP_VERSION}/luau-lsp-win64.zip",
            ("macos", "arm64"): f"https://github.com/JohnnyMorganz/luau-lsp/releases/download/{LUAU_LSP_VERSION}/luau-lsp-macos.zip",
            ("macos", "x86_64"): f"https://github.com/JohnnyMorganz/luau-lsp/releases/download/{LUAU_LSP_VERSION}/luau-lsp-macos.zip",
            ("linux", "x86_64"): f"https://github.com/JohnnyMorganz/luau-lsp/releases/download/{LUAU_LSP_VERSION}/luau-lsp-linux-x86_64.zip",
            ("linux", "arm64"): f"https://github.com/JohnnyMorganz/luau-lsp/releases/download/{LUAU_LSP_VERSION}/luau-lsp-linux-arm64.zip",
        },
        "selene": {
            ("windows", "x86_64"): f"https://github.com/Kampfkarren/selene/releases/download/{SELENE_VERSION}/selene-{SELENE_VERSION}-windows.zip",
            ("macos", "arm64"): f"https://github.com/Kampfkarren/selene/releases/download/{SELENE_VERSION}/selene-{SELENE_VERSION}-macos.zip",
            ("macos", "x86_64"): f"https://github.com/Kampfkarren/selene/releases/download/{SELENE_VERSION}/selene-{SELENE_VERSION}-macos.zip",
            ("linux", "x86_64"): f"https://github.com/Kampfkarren/selene/releases/download/{SELENE_VERSION}/selene-{SELENE_VERSION}-linux.zip",
            ("linux", "arm64"): f"https://github.com/Kampfkarren/selene/releases/download/{SELENE_VERSION}/selene-{SELENE_VERSION}-linux.zip",
        },
        "stylua": {
            ("windows", "x86_64"): f"https://github.com/JohnnyMorganz/StyLua/releases/download/v{STYLUA_VERSION}/stylua-windows-x86_64.zip",
            ("macos", "arm64"): f"https://github.com/JohnnyMorganz/StyLua/releases/download/v{STYLUA_VERSION}/stylua-macos-aarch64.zip",
            ("macos", "x86_64"): f"https://github.com/JohnnyMorganz/StyLua/releases/download/v{STYLUA_VERSION}/stylua-macos-x86_64.zip",
            ("linux", "x86_64"): f"https://github.com/JohnnyMorganz/StyLua/releases/download/v{STYLUA_VERSION}/stylua-linux-x86_64.zip",
            ("linux", "arm64"): f"https://github.com/JohnnyMorganz/StyLua/releases/download/v{STYLUA_VERSION}/stylua-linux-aarch64.zip",
        },
    }
    return {k: v[(os_name, arch)] for k, v in base.items()}


def _exe(name: str) -> str:
    return f"{name}.exe" if platform.system() == "Windows" else name


def _download(url: str, dest: Path, timeout: int = 60, min_size: int = 1024) -> None:
    """Download URL to dest atomically (tmp + os.replace).

    min_size is a cheap integrity sanity check: a successful download smaller
    than min_size bytes is treated as a failure (error page / empty body),
    since real tool binaries and defs are always >1KB.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "luaudit/plugin"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".tmp-{os.getpid()}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(tmp, "wb") as f:
                size = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    size += len(chunk)
        if size < min_size:
            raise ValueError(f"download too small ({size} bytes < {min_size})")
        os.replace(tmp, dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _download_and_extract_zip(url: str, dest_dir: Path) -> None:
    """Download a zip and extract it atomically into dest_dir.

    Extraction goes into a temp sibling dir first, then entries are moved into
    dest_dir, so a crash or concurrent second caller never sees a half-written
    tree. Individual file moves are atomic on the same filesystem.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    stage = dest_dir.parent / f".stage-{dest_dir.name}-{os.getpid()}"
    try:
        _download(url, tmp_path)
        expected = SHA256_PINS.get(url)
        if expected is None:
            raise RuntimeError(f"no SHA256 pin registered for {url}; refusing to install unverified binaries")
        h = hashlib.sha256()
        with open(tmp_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        if h.hexdigest() != expected:
            raise RuntimeError(f"SHA256 mismatch for {url}: expected {expected}, got {h.hexdigest()}")
        with zipfile.ZipFile(tmp_path) as zf:
            # Reject zip-slip (members escaping the target dir) outright.
            for info in zf.infolist():
                name = info.filename
                # Reject zip-slip outright: absolute members, `..` traversal,
                # and backslash separators (zip spec uses `/`; a `\` member
                # name is either a Windows path-escape attempt or malformed).
                if (
                    name.startswith(("/", "\\\\"))
                    or ".." in Path(name).parts
                    or "\\" in name
                ):
                    raise ValueError(f"unsafe zip member: {name!r}")
            zf.extractall(stage)
        if not stage.exists():
            stage.mkdir(parents=True)
        for p in stage.iterdir():
            target = dest_dir / p.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            os.replace(p, target)
        if platform.system() != "Windows":
            for p in dest_dir.iterdir():
                if p.is_file() and not p.suffix:
                    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    finally:
        tmp_path.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)


def ensure_tools() -> bool:
    """Download tools that are missing. Returns True if usable, False if any
    hard requirement (luau-lsp or defs) is missing."""
    urls = _urls()
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    DEFS_DIR.mkdir(parents=True, exist_ok=True)

    luau_lsp = BIN_DIR / _exe("luau-lsp")
    if not luau_lsp.exists():
        try:
            _download_and_extract_zip(urls["luau-lsp"], BIN_DIR)
        except Exception as e:
            _log_event(f"ERROR luau-lsp download failed: {e}")
            return False
        if not luau_lsp.exists():
            _log_event("ERROR luau-lsp binary not found after extraction")
            return False

    selene = BIN_DIR / _exe("selene")
    if _platform() == ("linux", "arm64"):
        # selene ships no native linux-arm64 binary; the x86_64 build would
        # fail with "Exec format error" on every check. Skip linting instead
        # of installing a dead binary.
        _log_event("WARNING selene has no native linux-arm64 build, linting skipped")
    elif not selene.exists():
        try:
            _download_and_extract_zip(urls["selene"], BIN_DIR)
        except Exception as e:
            _log_event(f"WARNING selene download failed: {e}, linting skipped")
        else:
            if not selene.exists():
                _log_event("WARNING selene binary missing after install, linting skipped")

    stylua = BIN_DIR / _exe("stylua")
    if not stylua.exists():
        try:
            _download_and_extract_zip(urls["stylua"], BIN_DIR)
        except Exception as e:
            _log_event(f"WARNING stylua download failed: {e}, formatting skipped")
        else:
            if not stylua.exists():
                _log_event("WARNING stylua binary missing after install, formatting skipped")

    defs = DEFS_DIR / DEFS_FILENAME
    defs_ok = defs.exists() and defs.stat().st_size > 0
    need_defs = not defs_ok
    if defs_ok and time.time() - defs.stat().st_mtime > DEFS_MAX_AGE:
        need_defs = True
    if need_defs:
        try:
            _download(defs_url(), defs)
            if not defs.exists() or defs.stat().st_size == 0:
                _log_event("ERROR type definitions download produced an empty file")
                return False
        except Exception as e:
            _log_event(f"ERROR type definitions download failed: {e}")
            return False

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    selene_toml = CONFIG_DIR / "selene.toml"
    if not selene_toml.exists():
        selene_toml.write_text('std = "roblox"\n', encoding="utf-8")
    luaurc = CONFIG_DIR / ".luaurc"
    if not luaurc.exists():
        luaurc.write_text('{\n  "languageMode": "strict"\n}\n', encoding="utf-8")
    return True


def defs_url() -> str:
    return DEFS_URL


def _find_sourcemap(project_root: str) -> str | None:
    """Walk up from project_root for a usable sourcemap.

    Returns the path to a sourcemap.json if one exists:
    - an existing sourcemap.json is used as-is
    - a default.project.json (rojo project) is turned into a sourcemap by
      running rojo if installed
    Returns None if neither is found (per-file fallback mode).

    The sourcemap filePaths are relative to the sourcemap file's directory
    (rojo emits paths relative to the project file), so callers must run
    luau-lsp with cwd set to that directory.
    """
    current = Path(project_root).resolve()
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
            # rojo missing: fall through to check parents, then None
        if current.parent == current:
            break
        current = current.parent
    return None


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


# ---------------------------------------------------------------------------
# Parsers (mirror of luaudit.parsers)
# ---------------------------------------------------------------------------

_LUAU_LSP_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+)(?:-(?P<endcol>\d+))?"
    r":\s+\(W0\)\s+(?P<category>\w+):\s+(?P<message>.+)$"
)
# With --sourcemap, plain-formatter lines gain a virtual-instance path after
# the file: `path [game/ServerScriptService/Utils]:8:27-29: (W0) TypeError: ...`
_LUAU_LSP_SOURCEMAP_RE = re.compile(
    r"^(?P<file>.+?) \[[^\]]+\]:(?P<line>\d+):(?P<col>\d+)(?:-(?P<endcol>\d+))?"
    r":\s+\(W0\)\s+(?P<category>\w+):\s+(?P<message>.+)$"
)
_SKIP_PREFIXES = ("[INFO]", "[WARN]", "[DEBUG]", "WARNING:", "Analyzing")
_SELENE_SEVERITY = {"Error": "error", "Warning": "warning"}


def _parse_luau_lsp(output: str, stderr: str = "") -> list[dict]:
    diags: list[dict] = []
    for line in (output + "\n" + stderr).splitlines():
        line = line.strip()
        if not line or any(line.startswith(p) for p in _SKIP_PREFIXES):
            continue
        m = _LUAU_LSP_SOURCEMAP_RE.match(line) or _LUAU_LSP_RE.match(line)
        if not m:
            continue
        category = m.group("category")
        diags.append({
            "file": m.group("file"),
            "line": int(m.group("line")),
            "column": int(m.group("col")),
            "code": category,
            "severity": "error" if "Error" in category else "warning",
            "message": m.group("message"),
            "source": "luau-lsp",
        })
    return diags


def _parse_selene(output: str) -> list[dict]:
    diags: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("Results:"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        label = obj.get("primary_label", {})
        span = label.get("span", {})
        diags.append({
            "file": label.get("filename", "unknown"),
            "line": span.get("start_line", 0) + 1,
            "column": span.get("start_column", 0) + 1,
            "code": obj.get("code", "unknown"),
            "severity": _SELENE_SEVERITY.get(obj.get("severity", ""), "warning"),
            "message": obj.get("message", ""),
            "source": "selene",
        })
    return diags


def _merge(diags: list[dict]) -> list[dict]:
    seen: set[tuple[str, int, int, str]] = set()
    merged: list[dict] = []
    for d in diags:
        # normcase: windows paths may arrive with differing drive casings
        # (C:\ vs c:/) depending on which tool emitted them; fold them so
        # dedupe works. No-op on POSIX.
        key = (os.path.normcase(d["file"]), d["line"], d["column"], d["code"])
        if key not in seen:
            seen.add(key)
            merged.append(d)
    merged.sort(key=lambda d: (d["file"], d["line"], d["column"]))
    return merged


def _collapse_near_dups(merged: list[dict]) -> list[dict]:
    """Drop diagnostics that repeat the same finding a few columns over.

    luau-lsp sometimes reports one type mismatch twice (once per operand),
    e.g. TypeError at 4:23 and 4:28 with identical messages. Agents do not
    need both. Same file+code+message within 10 columns => keep the first.
    """
    out: list[dict] = []
    for d in merged:
        dup = any(
            o["line"] == d["line"]
            and o["code"] == d["code"]
            and o["message"] == d["message"]
            and os.path.normcase(o["file"]) == os.path.normcase(d["file"])
            and abs(o["column"] - d["column"]) <= 10
            for o in out
        )
        if not dup:
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# Runners (mirror of luaudit.runners)
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> tuple[str, str, int]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        return "", f"Command timed out after {timeout}s", -1
    except FileNotFoundError:
        return "", f"Binary not found: {cmd[0]}", -1


def _ensure_utf8(filepath: str) -> tuple[str, str | None]:
    """Return (path_to_check, temp_to_cleanup). If the file is UTF-16 (BOM
    sniff), write a UTF-8 transcode to a temp file next to it so the native
    tools (luau-lsp/selene/stylua) can parse it. PowerShell's Set-Content
    writes UTF-16LE by default, so Windows-checked files are often UTF-16."""
    with open(filepath, "rb") as f:
        head = f.read(4)
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        data = open(filepath, "rb").read()
        enc = "utf-16-le" if head.startswith(b"\xff\xfe") else "utf-16-be"
        text = data.decode(enc, errors="replace").lstrip("\ufeff")
        tmp = filepath + ".lc-utf8"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        return tmp, tmp
    return filepath, None


def _normalize_tool_path(p: str, resolve_base: str) -> str:
    """Normalize a diagnostic path from any tool into a real absolute path.

    Handles three shapes:
    - plain relative:  resolved against resolve_base
    - drive absolute:  C:\\x or c:/x (luau-lsp emits these even on POSIX when
      a sourcemap came from a Windows workspace)
    - MSYS/git-bash:   /c/Users/x or /mnt/c/Users/x -- fake toolchains run
      under git-bash on Windows runners and rewrite path arguments like this;
      translate back instead of joining them onto a base (which mangles them).
    """
    m = re.match(r"^/(?:mnt/)?([A-Za-z])(/.*)$", p)
    if m:
        p = f"{m.group(1).upper()}:{m.group(2)}"
    if re.match(r"^[A-Za-z]:[\\/]", p):
        return os.path.normpath(p)
    if os.path.isabs(p):
        return os.path.normpath(p)
    return os.path.abspath(os.path.join(resolve_base, p))


def check_file(filepath: str) -> list[dict]:
    """Run all three tools on one file, return merged diagnostics with
    absolute paths. A broken toolchain surfaces as an InternalError diagnostic
    (never silently 'clean')."""
    ensure_tools()
    # NOTE: no early return on ensure_tools() failure. If luau-lsp/defs are
    # missing, the InternalError branch below reports it instead of hiding it.

    luau_lsp = BIN_DIR / _exe("luau-lsp")
    selene = BIN_DIR / _exe("selene")
    stylua = BIN_DIR / _exe("stylua")
    defs = DEFS_DIR / DEFS_FILENAME
    base_luaurc = CONFIG_DIR / ".luaurc"
    base_selene = CONFIG_DIR / "selene.toml"

    project_root = os.path.dirname(filepath)
    all_diags: list[dict] = []

    # Transcode UTF-16 sources so the native tools can consume them.
    check_target, tmp_target = _ensure_utf8(filepath)
    check_target = check_target or filepath

    if not luau_lsp.exists() or not defs.exists():
        _log_event(
            f"ERROR toolchain missing for {filepath}: luau_lsp={luau_lsp.exists()} defs={defs.exists()}"
        )
        all_diags.append({
            "file": filepath, "line": 1, "column": 1, "code": "InternalError",
            "severity": "error", "source": "luaudit",
            "message": "luaudit toolchain missing (luau-lsp or defs); run the luaudit CLI once to bootstrap",
        })
        return all_diags

    project_luaurc = _find_config(project_root, (".luaurc",))
    sourcemap = _find_sourcemap(project_root)
    analyze_cwd = os.path.dirname(sourcemap) if sourcemap else None
    cmd = [
        str(luau_lsp), "analyze", "--platform", "roblox", "--formatter", "plain",
        f"--definitions=@roblox={defs}", f"--base-luaurc={project_luaurc or base_luaurc}",
    ]
    if sourcemap:
        cmd.append(f"--sourcemap={sourcemap}")
    cmd.append(check_target)
    stdout, stderr, code = _run(cmd, cwd=analyze_cwd, timeout=60)
    if (code == -1 and not stdout) or (code != 0 and not stdout.strip()):
        _log_event(f"ERROR luau-lsp failed on {filepath} (exit {code}): {stderr.strip() or 'no output'}")
        all_diags.append({
            "file": filepath, "line": 1, "column": 1, "code": "InternalError",
            "severity": "error", "source": "luau-lsp",
            "message": f"luau-lsp failed (exit {code}): {stderr.strip() or 'no output'}",
        })
    else:
        all_diags.extend(_parse_luau_lsp(stdout, stderr))

    if selene.exists() and _platform() != ("linux", "arm64"):
        project_selene = _find_config(project_root, ("selene.toml", "selene.yml"))
        cmd = [str(selene), "--display-style", "json", "--no-summary",
               f"--config={project_selene or base_selene}", check_target]
        stdout2, stderr2, code2 = _run(cmd, timeout=60)
        if (code2 == -1 and not stdout2) or (code2 != 0 and not stdout2.strip()):
            _log_event(f"ERROR selene failed on {filepath} (exit {code2}): {stderr2.strip() or 'no output'}")
            all_diags.append({
                "file": filepath, "line": 1, "column": 1, "code": "InternalError",
                "severity": "error", "source": "selene",
                "message": f"selene failed (exit {code2}): {stderr2.strip() or 'no output'}",
            })
        else:
            all_diags.extend(_parse_selene(stdout2))

    if stylua.exists():
        cmd = [str(stylua), "--check"]
        project_stylua = _find_config(project_root, (".stylua.toml", "stylua.toml"))
        if project_stylua:
            cmd.append(f"--config-path={project_stylua}")
        cmd.append(check_target)
        stdout3, stderr3, code3 = _run(cmd, timeout=30)
        if code3 != 0:
            if code3 == -1 and not stdout3:
                _log_event(f"ERROR stylua failed on {filepath} (exit {code3}): {stderr3.strip()}")
                all_diags.append({
                    "file": filepath, "line": 1, "column": 1, "code": "InternalError",
                    "severity": "error", "source": "stylua", "message": f"stylua failed: {stderr3}",
                })
            else:
                for line in stdout3.splitlines():
                    line = line.strip()
                    if line.startswith("Diff in "):
                        diff_file = line.replace("Diff in ", "").rstrip(":")
                        all_diags.append({
                            "file": diff_file, "line": 1, "column": 1,
                            "code": "StyLuaFormat", "severity": "warning",
                            "source": "stylua",
                            "message": "Code is not formatted",
                        })

    result: list[dict] = []
    resolve_base = analyze_cwd or project_root
    for d in all_diags:
        d["file"] = _normalize_tool_path(d["file"], resolve_base)
        result.append(d)
    if tmp_target:
        try:
            os.unlink(tmp_target)
        except OSError:
            pass
    return _collapse_near_dups(_merge(result))


def check_paths(paths: list[str], cwd: str = ".") -> dict:
    """CLI-mode check: files or directories. Returns diagnostics dict."""
    files: list[str] = []
    missing: list[str] = []
    for t in paths:
        p = t if os.path.isabs(t) else os.path.join(cwd, t)
        p = os.path.abspath(p)
        if os.path.isfile(p):
            files.append(p)
        elif os.path.isdir(p):
            for root, _, fs in os.walk(p):
                for f in fs:
                    if f.endswith((".luau", ".lua")):
                        files.append(os.path.join(root, f))
        else:
            missing.append(p)
    if missing:
        diags = [{
            "file": t, "line": 0, "column": 0, "code": "NoSuchFile",
            "severity": "error", "source": "luaudit",
            "message": f"path does not exist: {t}",
        } for t in missing]
        return _result(diags)
    if not files:
        return _result([])
    all_diags: list[dict] = []
    for f in files:
        # Absolutize inputs so tools echo absolutes back and path rebasing
        # never doubles up (see package-side fix in runners.check_files).
        f = os.path.abspath(f)
        all_diags.extend(check_file(f))
    return _result(_merge(all_diags))


def _result(diags: list[dict]) -> dict:
    errors = sum(1 for d in diags if d["severity"] == "error")
    warnings = sum(1 for d in diags if d["severity"] == "warning")
    return {"diagnostics": diags, "summary": {"errors": errors, "warnings": warnings, "total": len(diags)}}


# ---------------------------------------------------------------------------
# Delta store (mirror of luaudit.deltastore)
#
# Remembers what the agent was already shown so repeats collapse to a count
# instead of re-injecting identical context every edit. Identity = code +
# normalized message, stable across line shifts and unrelated edits; a
# fingerprint absent from a pass is forgotten so fix -> regress reads as new.
# Every failure mode degrades to "everything is new": bookkeeping must never
# break the hook.
# ---------------------------------------------------------------------------

DELTA_SCHEMA = 1
STATE_MAX_FILES = 256
STATE_MAX_AGE_DAYS = 14
STATE_MAX_FINDINGS = 64

STATE_DIR = CACHE_DIR / "state"
_WS_RE = re.compile(r"\s+")

# Turn-end sweep budget: never scan more than this many files or spend more
# than this many seconds wall time in one Stop hook. The sweep is a safety
# net for cross-file breakage, not a full CI substitute.
SWEEP_MAX_FILES = 400
SWEEP_BUDGET_SECONDS = 20.0

# Adaptive muting: a warning surfaced this many times without ever being
# acted on is treated as revealed noise for this project. Warnings only;
# errors are immune. Every mute is announced and reversible (luaudit unmute).
MUTE_THRESHOLD = 5


def _delta_normalize_key(filepath: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(filepath))
    except (TypeError, ValueError):
        return str(filepath)


def _delta_fingerprint(diag: dict) -> str:
    code = str(diag.get("code", ""))
    message = _WS_RE.sub(" ", str(diag.get("message", ""))).strip()
    return f"{code}|{message}"


def _delta_prune(data: dict, now: float) -> None:
    cutoff = now - STATE_MAX_AGE_DAYS * 86400
    files = data.get("files", {})
    for key in list(files.keys()):
        entry = files[key]
        if float(entry.get("last_seen", 0)) < cutoff:
            del files[key]
    overflow = len(files) - STATE_MAX_FILES
    if overflow > 0:
        by_age = sorted(files.items(), key=lambda kv: float(kv[1].get("last_seen", 0)))
        for key, _ in by_age[:overflow]:
            del files[key]
    for entry in files.values():
        findings = entry.get("findings", {})
        extra = len(findings) - STATE_MAX_FINDINGS
        if extra > 0:
            by_age = sorted(findings.items(), key=lambda kv: float(kv[1].get("last_seen", 0)))
            for fp, _ in by_age[:extra]:
                del findings[fp]


class DeltaStore:
    def __init__(self, state_dir=None):
        self.state_dir = Path(state_dir) if state_dir else STATE_DIR
        self.path = self.state_dir / "delta.json"

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("schema") == DELTA_SCHEMA:
                return raw
        except (OSError, ValueError):
            pass
        return {"schema": DELTA_SCHEMA, "files": {}, "muted": {}, "dirty": {}}

    def _save(self, data: dict) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    @staticmethod
    def _empty_file_entry() -> dict:
        return {"hash": None, "findings": {}, "last_seen": 0.0}

    def classify(self, filepath: str, diagnostics: list, now: float | None = None) -> dict:
        """Split diagnostics into new vs repeats. Identity = fingerprint seen
        before (regardless of line shifts or unrelated edits). A fingerprint
        absent from this pass is forgotten, so fix -> regress reads as new.
        Returns {"new": [...], "repeats": [...], "repeat_count": int,
        "suppressed": int}. Never raises; any internal failure reports
        everything as new."""
        now = time.time() if now is None else now
        result = {"new": [], "repeats": [], "repeat_count": 0, "suppressed": 0}
        try:
            data = self._load()
            key = _delta_normalize_key(filepath)
            entry = data["files"].get(key) or self._empty_file_entry()
            findings: dict = entry.get("findings", {})
            muted: dict = data.get("muted", {})

            seen_fps: set = set()
            for diag in diagnostics:
                fp = _delta_fingerprint(diag)
                seen_fps.add(fp)
                if fp in muted:
                    result["suppressed"] += 1
                    continue
                rec = findings.get(fp)
                if rec is not None:
                    rec["n"] = int(rec.get("n", 1)) + 1
                    rec["last_seen"] = now
                    result["repeats"].append(diag)
                    result["repeat_count"] += 1
                else:
                    findings[fp] = {"n": 1, "first_seen": now, "last_seen": now}
                    result["new"].append(diag)

            # Forget fingerprints that vanished this pass.
            for gone in [fp for fp in findings if fp not in seen_fps]:
                del findings[gone]

            entry["findings"] = findings
            entry.pop("hash", None)
            entry["last_seen"] = now
            data["files"][key] = entry
            _delta_prune(data, now)
            self._save(data)
        except Exception:
            result = {"new": list(diagnostics), "repeat_count": 0, "suppressed": 0}
        return result

    def surface_counts(self, filepath: str) -> dict:
        try:
            data = self._load()
            entry = data["files"].get(_delta_normalize_key(filepath))
            if not entry:
                return {}
            return {fp: int(rec.get("n", 0)) for fp, rec in entry.get("findings", {}).items()}
        except Exception:
            return {}

    def muted(self) -> dict:
        try:
            return dict(self._load().get("muted", {}))
        except Exception:
            return {}

    def mute(self, fp: str, sample_message: str = "", now: float | None = None) -> bool:
        """Record a muted fingerprint. True only on FIRST insert so callers
        can announce the mute exactly once."""
        now = time.time() if now is None else now
        try:
            data = self._load()
            muted = data.setdefault("muted", {})
            if fp in muted:
                return False
            muted[fp] = {"sample": sample_message[:200], "at": now}
            self._save(data)
            return True
        except Exception:
            return False

    def unmute(self, fp=None) -> int:
        """Remove one or all muted fingerprints; also resets their surface
        counters so a restored warning starts counting from zero."""
        try:
            data = self._load()
            muted = data.get("muted", {})
            targets = list(muted.keys()) if fp is None else [fp]
            removed = 0
            for t in targets:
                if t in muted:
                    del muted[t]
                    removed += 1
                    for entry in data.get("files", {}).values():
                        entry.get("findings", {}).pop(t, None)
            if removed:
                self._save(data)
            return removed
        except Exception:
            return 0

    def mark_dirty(self, root: str, now: float | None = None) -> None:
        try:
            now = time.time() if now is None else now
            data = self._load()
            data.setdefault("dirty", {})[_delta_normalize_key(root)] = now
            self._save(data)
        except Exception:
            pass

    def pop_dirty(self, root: str) -> bool:
        try:
            data = self._load()
            key = _delta_normalize_key(root)
            dirty = data.get("dirty", {})
            was = key in dirty
            dirty.pop(key, None)
            self._save(data)
            return was
        except Exception:
            return False

    def pop_dirty_all(self) -> list:
        try:
            data = self._load()
            dirty = data.get("dirty", {})
            data["dirty"] = {}
            self._save(data)
            return list(dirty.keys())
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Hook mode
# ---------------------------------------------------------------------------

def _hook_event_file(event: dict) -> str | None:
    """Extract the written Luau file from a harness hook event.

    Claude: Write|Edit|MultiEdit carry tool_input.file_path.
    Codex:  writes arrive as Bash/shell commands. Detected forms: shell
            write-redirections (`> path`, `>> path`), `sed -i ... path`,
            PowerShell write cmdlets, .NET File APIs, `apply_patch`
            heredocs (codex's native edit format), `tee <file>`, and
            python one-liners (`open(p,'w')`, `Path(p).write_text`).
            stderr redirections (`2>`) and read-only commands are NOT
            writes.
    MCP:    Studio-bridge tools (edit_script, execute_luau, update_script)
            edit scripts that exist only in Studio; the mirror plugin
            materializes them, and the hook routes to mirror mode.
    """
    tool_name = event.get("tool_name", "")
    ti = event.get("tool_input") or {}
    if isinstance(ti, str):
        try:
            ti = json.loads(ti)
        except json.JSONDecodeError:
            ti = {}
    if not isinstance(ti, dict):
        return None

    # MCP Studio-bridge tools: no local file; signal mirror mode with a
    # sentinel so run_hook checks the mirrored tree instead.
    if re.search(r"^(edit_script|update_script|execute_luau|set_script_source)$", tool_name):
        return "__MCP_MIRROR__"

    # Claude tools: file path is a first-class field.
    if not tool_name or re.search(r"^(Write|Edit|MultiEdit)$", tool_name):
        fp = ti.get("file_path") or ti.get("file") or ti.get("path")
        return fp if isinstance(fp, str) else None

    # Codex: file writes arrive as shell commands. The tool name varies by
    # harness/version: "Bash" (codex), "shell_command" (codex exec on
    # Windows), "PowerShell" (some setups). All carry the command text in
    # tool_input.command, so route them all through the same extraction.
    if tool_name in ("Bash", "shell_command", "PowerShell", "Cmd", "cmd"):
        cmd = ti.get("command")
        if not isinstance(cmd, str):
            return None

        def _is_luau(p: str) -> bool:
            return p.endswith((".luau", ".lua"))

        # 1. shell write redirections: `> path` / `>> path` / `>| path`, optionally
        #    quoted. Exclude fd-number redirections (`2> err`, `1> out`) but
        #    KEEP all-stream `&>` (writes stdout+stderr to the target).
        for m in re.finditer(r"(?<![0-9])(?:>>|>\||>)\s*('(?:[^']*)'|\"(?:[^\"]*)\"|\S+)", cmd):
            raw = m.group(1)
            if raw.startswith(("'", '"')) and len(raw) >= 2:
                p = raw[1:-1]
            else:
                p = raw
            if _is_luau(p):
                return p
        # 2. sed -i ... path (in-place edit): the guard is mandatory BEFORE the
        #    path; skip the sed script/flag tokens so `'s/foo.lua/bar/'` is not
        #    mistaken for the target; a bare `.lua` token elsewhere is a read.
        for m in re.finditer(
            r"\bsed\s+-i(?:\S*)\s+(?:(?:'[^']*'|\"[^\"]*\"|s/.*?/[^/]*/[a-z]*)\s+)*([^\s;|&]+\.(?:luau|lua))\b",
            cmd,
        ):
            p = m.group(1).strip("'\"")
            if p and _is_luau(p):
                return p
        # 3. PowerShell write cmdlets, used by codex on Windows
        #    (`shell_command` runs PowerShell). Forms:
        #    Set-Content -Path <p> -Value ...   Set-Content -LiteralPath <p>
        #    Set-Content <p> -Value ... (positional)   Out-File -FilePath <p>
        #    Add-Content -Path <p> ...   New-Item -Path <p> (sometimes)
        #    Only a target ending .luau/.lua counts.
        ps_cmdlet = r"(?i)\b(?:Set-Content|Add-Content|Out-File|New-Item|Set-Item|Copy-Item)\b"
        ps_pathval = r"(?:'([^']*\.(?:luau|lua))'|\"([^\"]*\.(?:luau|lua))\"|([^\s;|&\"']*\.(?:luau|lua)))"
        # 3a. explicit -Path / -LiteralPath / -FilePath <value>
        for m in re.finditer(
            ps_cmdlet + r"(?:\s+-(?:Path|LiteralPath|FilePath)\s+)" + ps_pathval,
            cmd,
        ):
            p = m.group(1) or m.group(2) or m.group(3)
            if p and _is_luau(p):
                return p
        # 3b. positional: <cmdlet> <value> (with optional leading -Value flag)
        for m in re.finditer(
            ps_cmdlet + r"(?:\s+-Value\s+)?\s*" + ps_pathval,
            cmd,
        ):
            p = m.group(1) or m.group(2) or m.group(3)
            if p and _is_luau(p):
                return p
        # 4. .NET File API used by some codex Windows shells:
        #    [System.IO.File]::WriteAllText("C:/x/broken.luau", "...")
        #    [System.IO.File]::AppendAllText("C:/x/broken.luau", "...")
        for m in re.finditer(
            r"(?i)\[System\.IO\.File\]::(?:WriteAllText|AppendAllText|WriteAllLines)\("
            r"\s*('(?:[^']*\.(?:luau|lua))'|\"(?:[^\"]*\.(?:luau|lua))\")",
            cmd,
        ):
            p = m.group(1).strip("'\"")
            if p and _is_luau(p):
                return p
        # 5. apply_patch heredoc (codex's native edit format):
        #    apply_patch <<'PATCH' ... *** Update File: src/x.luau ... PATCH
        for m in re.finditer(r"\*\*\* (?:Add|Update|Delete) File:[ \t]+([^\r\n]+)", cmd):
            p = m.group(1).strip("'\" \t")
            if _is_luau(p):
                return p
        # 6. tee: writes to every file argument (`cat a | tee b.luau`,
        #    `printf .. | tee -a b.luau`). Not preceded by a path separator
        #    so `/usr/bin/tee`-style paths don't false-match the keyword.
        for m in re.finditer(r"(?<![/\w])tee\b((?:\s+(?:-\w+|'(?:[^']*)'|\"(?:[^\"]*)\"|[^\s;|&]+))*)", cmd):
            for tok in m.group(1).split():
                p = tok.strip("'\"")
                if p.startswith("-"):
                    continue
                if _is_luau(p):
                    return p
        # 7. python one-liners: open('x.luau','w') / Path('x.luau').write_text(...)
        for m in re.finditer(
            r"(?i)\bopen\s*\(\s*('(?:[^']*\.(?:luau|lua))'|\"(?:[^\"]*\.(?:luau|lua))\")\s*,\s*['\"]([wa])",
            cmd,
        ):
            p = m.group(1).strip("'\"")
            if p and _is_luau(p):
                return p
        for m in re.finditer(
            r"(?i)\bPath\s*\(\s*('(?:[^']*\.(?:luau|lua))'|\"(?:[^\"]*\.(?:luau|lua))\")\s*\)\s*\.\s*write_(?:text|bytes)",
            cmd,
        ):
            p = m.group(1).strip("'\"")
            if p and _is_luau(p):
                return p
        return None

    return None


# ---------------------------------------------------------------------------
# Studio mirror plugin nag
#
# The wheel-installed mirror artifact carries its schema key inside Source;
# a stale/missing plugin means Studio-only scripts get zero diagnostics.
# We surface that at most once per NAG_INTERVAL, riding hook output. Never
# affects exit codes or the diagnostics themselves.
# ---------------------------------------------------------------------------

MIRROR_SCHEMA = "luaudit-mirror-v1"
_NAG_SCHEMA_RE = re.compile(r"(luaudit-mirror-v\d+)")

# Mirrors the engine-side mode token (src/luaudit/plugin.py). An installed
# artifact whose mode is external/ask is *deliberately* idle, never stale
# behavior to nag about.
_NAG_MODE_RE = re.compile(r"--LUAUDIT-MODE=(\S+)")

# Project files that mark an externally-managed (Rojo-style) workflow.
# When these sit near the edited file, Studio-only scripts are not the
# user's situation and the mirror nag would be noise.
_NAG_MARKER_FILES = (
    "default.project.json",
    "argon.project.json",
    "azul.toml",
    ".azulrc",
)

NAG_INTERVAL = 24 * 60 * 60
_NAG_TEXT = (
    "[luaudit] your Roblox Studio mirror plugin is missing or stale. "
    "without it, scripts that live only inside Studio are invisible to "
    "you: no type errors, no lint, no formatting on anything that isn't a "
    "local file, so problems there surface late, at runtime, from the "
    "user. surfacing this to the user is worth one line: ask them to run "
    '"luaudit plugin install --yes", then have them restart Studio.'
)


def _installed_mirror_ok() -> bool:
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        pdir = base / "Roblox" / "Plugins"
    elif sys.platform == "darwin":
        pdir = home / "Documents" / "Roblox" / "Plugins"
    else:
        pdir = home / ".local" / "share" / "Roblox" / "Plugins"
    target = pdir / "luaudit-mirror.rbxmx"
    if not target.is_file():
        return False
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    m = _NAG_SCHEMA_RE.search(text)
    return bool(m and m.group(1) == MIRROR_SCHEMA)


def _installed_mirror_idle() -> bool:
    """True when an installed artifact is deliberately idle (external/ask)."""
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        pdir = base / "Roblox" / "Plugins"
    elif sys.platform == "darwin":
        pdir = home / "Documents" / "Roblox" / "Plugins"
    else:
        pdir = home / ".local" / "share" / "Roblox" / "Plugins"
    target = pdir / "luaudit-mirror.rbxmx"
    if not target.is_file():
        return False
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    m = _NAG_MODE_RE.search(text)
    return bool(m and m.group(1) in ("external", "ask"))


def _external_sync_near(filepath: str) -> bool:
    """True when a sync-tool marker sits near the edited file."""
    try:
        current = Path(filepath).resolve().parent
        for _ in range(6):
            for name in _NAG_MARKER_FILES:
                if (current / name).exists():
                    return True
            if current.parent == current:
                break
            current = current.parent
    except OSError:
        pass
    return False


def _plugin_nag(now: float | None = None, filepath: str | None = None) -> str:
    """Return the nag text when due, else ''. Never raises."""
    try:
        if _installed_mirror_ok() or _installed_mirror_idle():
            return ""
        if filepath and _external_sync_near(filepath):
            # Rojo/Argon/Azul-style project: the mirror is irrelevant here.
            return ""
        now = time.time() if now is None else now
        stamp = CACHE_DIR / "plugin-nag.json"
        try:
            last = float(stamp.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            last = 0.0
        if now - last < NAG_INTERVAL:
            return ""
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(f"{now}\n", encoding="utf-8")
        except OSError:
            pass
        return _NAG_TEXT
    except Exception:
        return ""


def _fmt_diag(d: dict) -> str:
    return f"{d['file']}:{d['line']}:{d['column']}: [{d['severity'].upper()}] {d['code']} {d['message']}"


# ---------------------------------------------------------------------------
# Injection policy (the v1.1 anti-noise behavior)
#
#   errors          : always inline, every time (they compound if ignored)
#   new warnings    : inline once, full detail (first sighting)
#   repeat warnings : collapsed to one count line (agent already saw them)
#
# Nothing is dropped; repeats are summarized. State bookkeeping is the delta
# store's problem and can fail open silently.
# ---------------------------------------------------------------------------

def _hook_context(store: DeltaStore, entries: list) -> str | None:
    """Build hook context under the injection policy.

    entries: list of (abs_path, diagnostics) pairs. Returns the additionalContext
    payload, or None when there is nothing worth interrupting the agent for.
    """
    inline_parts: list = []
    repeat_total = 0
    suppressed_total = 0
    for path, diags in entries:
        errors = [d for d in diags if d.get("severity") == "error"]
        warnings = [d for d in diags if d.get("severity") == "warning"]
        if errors or warnings:
            # Only warnings participate in delta tracking; errors bypass it.
            classified = store.classify(path, warnings)
            repeat_total += classified["repeat_count"]
            suppressed_total += classified["suppressed"]
            shown = sorted(
                errors + classified["new"],
                key=lambda d: (int(d.get("line", 0)), int(d.get("column", 0))),
            )
            inline_parts.extend(_fmt_diag(d) for d in shown)
        else:
            # Fully clean pass: forget stale fingerprints so a later
            # regression reads as new again and gets injected.
            store.classify(path, [])
    parts: list = []
    if inline_parts:
        parts.append("luaudit diagnostics:\n" + "\n".join(inline_parts))
    if repeat_total:
        parts.append(
            f"[luaudit] {repeat_total} previously reported warning(s) still present, unchanged"
            " (not repeated here). Full list anytime: luaudit check <file>"
        )
    return "\n\n".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Turn-end sweep (Stop hook)
#
# When the agent finishes its turn, check the directories that saw edits this
# session and report what the per-edit hook could not see: breakage in files
# the agent never touched. Strictly informational: prints context only,
# never blocks, never loops. Skips entirely when nothing changed.
# ---------------------------------------------------------------------------

def _collect_luau_files(root: str) -> list:
    """All .luau/.lua files under root, nearest-first, capped."""
    out: list = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip dependency/vendor trees; their diagnostics are not ours to fix.
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "Packages", "Vendor")]
        for f in filenames:
            if f.endswith((".luau", ".lua")):
                out.append(os.path.join(dirpath, f))
                if len(out) >= SWEEP_MAX_FILES:
                    return out
    return out


def run_stop_hook() -> int:
    try:
        store = DeltaStore()
        dirty = store.pop_dirty_all()
        if not dirty:
            return 0  # no Luau edits this session => stay silent
        started = time.time()
        all_diags: list = []
        scanned = 0
        truncated = False
        seen_roots: set = set()
        for root in dirty:
            key = os.path.normcase(os.path.abspath(root))
            if key in seen_roots or not os.path.isdir(root):
                continue
            seen_roots.add(key)
            for f in _collect_luau_files(root):
                if time.time() - started > SWEEP_BUDGET_SECONDS:
                    truncated = True
                    break
                scanned += 1
                result = check_paths([f], cwd=os.getcwd())
                all_diags.extend(result["diagnostics"])
            if truncated:
                break

        errors = [d for d in all_diags if d["severity"] == "error"]
        warnings = [d for d in all_diags if d["severity"] == "warning"]

        # Group by file so delta tracking stays per-file like the edit hook.
        groups: dict = {}
        order: list = []
        for d in sorted(errors + warnings, key=lambda d: (os.path.normcase(d["file"]), d["line"])):
            k = os.path.normcase(d["file"])
            if k not in groups:
                groups[k] = (d["file"], [])
                order.append(k)
            groups[k][1].append(d)

        lines: list = []               # error detail lines
        new_warnings: list = []
        repeat_warning_count = 0
        muted_announcements: list = []
        for path, diags in (groups[k] for k in order):
            errs = [d for d in diags if d["severity"] == "error"]
            warns = [d for d in diags if d["severity"] == "warning"]
            lines.extend(_fmt_diag(d) for d in errs)
            if warns:
                cls = store.classify(path, warns)
                repeat_warning_count += cls["repeat_count"]
                new_warnings.extend(cls["new"])
                # Adaptive muting: surface-count each warning; when one has
                # been shown MUTE_THRESHOLD times without ever disappearing
                # (an edit that would have cleared it), treat it as revealed
                # noise and mute it going forward. Loud and reversible.
                counts = store.surface_counts(path)
                for fp, n in counts.items():
                    if n >= MUTE_THRESHOLD:
                        sample = next((w["message"] for w in warns
                                       if _delta_fingerprint(w) == fp), "")
                        if store.mute(fp, sample_message=sample):
                            muted_announcements.append(
                                f"muted `{fp.split('|', 1)[0]}` after {n} unfixed surfaces"
                                " (luaudit unmute restores)"
                            )

        summary_bits = [f"{scanned} file(s) checked across {len(seen_roots)} edited folder(s)"]
        if truncated:
            summary_bits.append("stopped early at the time budget")
        parts: list = []
        if lines:
            parts.append("luaudit turn-end sweep:\n" + "\n".join(lines))
        if new_warnings:
            wlines = [_fmt_diag(d) for d in new_warnings[:10]]
            more = "" if len(new_warnings) <= 10 else f"\n... and {len(new_warnings) - 10} more"
            parts.append("new warnings:\n" + "\n".join(wlines) + more)
        tail: list = []
        if repeat_warning_count:
            tail.append(f"{repeat_warning_count} known warning(s) unchanged")
        if muted_announcements:
            # Mute announcements ride every path (full summary or quiet line)
            # so they are always visible exactly once, at the moment they fire.
            tail.extend(muted_announcements)
        if not parts:
            # Nothing new anywhere: one quiet line so a Stop hook with output
            # still reads as intentional, then silence next time.
            if tail:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "Stop",
                    "additionalContext": "[luaudit] sweep clean: " + ", ".join(tail) + ".",
                }}))
            return 0
        if tail:
            summary_bits.append(", ".join(tail))
        parts.insert(0, "[" + "; ".join(summary_bits) + "]")
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": "\n\n".join(parts),
        }}))
        return 0
    except Exception:
        # The Stop hook must never wedge the agent's turn end. Any failure:
        # silence.
        return 0


def run_hook() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    filepath = _hook_event_file(event)
    if not filepath:
        return 0
    store = DeltaStore()
    if filepath == "__MCP_MIRROR__":
        # Studio-bridge MCP edit: check the mirrored tree (materialized by
        # the mirror plugin from Studio). Diagnostics carry their own script
        # paths, so delta tracking groups per script. Clean => silent.
        result = _mirror_check(check_all=True)
        diags = [d for d in result.get("diagnostics", []) if d["severity"] in ("error", "warning")]
        groups: dict = {}
        order: list = []
        for d in diags:
            k = os.path.normcase(str(d.get("file", "")))
            if k not in groups:
                groups[k] = (str(d.get("file", "")), [])
                order.append(k)
            groups[k][1].append(d)
        ctx = _hook_context(store, [groups[k] for k in order])
        if not ctx:
            return 0
        nag = _plugin_nag(filepath=filepath)
        if nag:
            ctx = ctx + "\n\n" + nag
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": ctx,
            }
        }))
        return 0
    if not filepath.endswith((".luau", ".lua")):
        return 0
    if not os.path.isfile(filepath):
        return 0

    abspath = os.path.abspath(filepath)
    # Any Luau write marks the directory dirty for the turn-end sweep,
    # regardless of whether this pass produced diagnostics.
    try:
        store.mark_dirty(os.path.dirname(abspath))
    except Exception:
        pass
    result = check_paths([filepath], cwd=os.getcwd())
    diags = [d for d in result["diagnostics"] if d["severity"] in ("error", "warning")]
    ctx = _hook_context(store, [(abspath, diags)])
    if not ctx:
        return 0  # nothing new => silent, the documented contract
    nag = _plugin_nag(filepath=filepath)
    if nag:
        ctx = ctx + "\n\n" + nag
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": ctx,
        }
    }))
    return 0


# ---------------------------------------------------------------------------
# Mirror mode (Studio plugin bridge)
# ---------------------------------------------------------------------------

def _find_studio_settings() -> str | None:
    """Locate the Roblox Studio plugin settings.json.

    Local plugins write to %LOCALAPPDATA%/Roblox/<UserId>/InstalledPlugins/0/
    settings.json. We glob for it under LOCALAPPDATA (Windows) or the
    equivalent on other OSes (unlikely; Studio is Windows-only).
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    roblox_dir = Path(base) / "Roblox"
    if not roblox_dir.exists():
        return None
    # settings.json lives under Roblox/<UserId>/InstalledPlugins/0/settings.json.
    # Multiple user dirs can exist (different logged-in accounts); prefer the
    # one that actually carries the mirror payload, else the freshest file.
    candidates: list[tuple[float, str]] = []
    for user_dir in roblox_dir.iterdir():
        if not user_dir.is_dir():
            continue
        cand = user_dir / "InstalledPlugins" / "0" / "settings.json"
        if cand.exists():
            try:
                mtime = cand.stat().st_mtime
            except OSError:
                mtime = 0.0
            candidates.append((mtime, str(cand)))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    # Prefer a candidate that already has the mirror payload (the active
    # Studio account), else the most recently written settings file.
    for _, path in candidates:
        if _read_mirror_payload(path) is not None:
            return path
    return candidates[0][1]


def _read_mirror_payload(settings_path: str) -> dict | None:
    """Read the luaudit mirror payload from the plugin settings.json.

    The plugin stores one JSON string under the key 'luaudit-mirror-v1'.
    The settings.json itself is a JSON map of key -> value; the value is a
    JSON-encoded string containing {sources: {rel: source}, tree: {...}}.
    """
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    blob = data.get("luaudit-mirror-v1")
    if not isinstance(blob, str):
        return None
    try:
        payload = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "sources" not in payload:
        return None
    return payload


def materialize_mirror(payload: dict, mirror_dir: Path) -> Path:
    """Write the mirror payload's sources + sourcemap into mirror_dir.

    Returns the path to the generated sourcemap.json. Files are written
    atomically (tmp + rename). The sourcemap is the flat rojo format.
    """
    mirror_dir.mkdir(parents=True, exist_ok=True)
    sources = payload.get("sources") or {}
    # Luau's JSONEncode writes an EMPTY table as [] (no array/dict
    # distinction), so an empty sources map arrives as a list. Non-empty
    # maps arrive as a dict. Normalize both to {rel: source}.
    if isinstance(sources, list):
        norm: dict[str, str] = {}
        for item in sources:
            if isinstance(item, list) and len(item) >= 2 and isinstance(item[0], str) and isinstance(item[1], str):
                norm[item[0]] = item[1]
        sources = norm
    if isinstance(sources, dict):
        for rel, content in sources.items():
            if not isinstance(rel, str) or not isinstance(content, str):
                continue
            # guard against path traversal from the payload
            target = (mirror_dir / rel).resolve()
            if not str(target).startswith(str(mirror_dir.resolve())):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
    tree = payload.get("tree")
    if isinstance(tree, dict):
        sm = {
            "name": tree.get("name", "place"),
            "className": "DataModel",
            "children": tree.get("children", []),
        }
        sm_path = mirror_dir / "sourcemap.json"
        tmp = sm_path.with_name(sm_path.name + ".tmp")
        tmp.write_text(json.dumps(sm), encoding="utf-8")
        os.replace(tmp, sm_path)
    return mirror_dir / "sourcemap.json"


def _mirror_check(check_all: bool = True) -> dict:
    """Materialize the Studio plugin payload and check the mirrored files.

    Returns the diagnostics dict. Missing settings/payload => empty result
    with a note (never 'clean' silently when the plugin isn't present).
    """
    settings = _find_studio_settings()
    if not settings:
        return {"diagnostics": [], "summary": {"errors": 0, "warnings": 0, "total": 0},
                "note": "no Studio settings.json found (is the Studio plugin installed and running?)"}
    payload = _read_mirror_payload(settings)
    if not payload:
        return {"diagnostics": [], "summary": {"errors": 0, "warnings": 0, "total": 0},
                "note": "no mirror payload in settings.json (is the Studio plugin running?)"}
    mirror_dir = CACHE_DIR / "mirror"
    materialize_mirror(payload, mirror_dir)

    files: list[str] = []
    if check_all:
        for root, _, fs in os.walk(mirror_dir):
            for f in fs:
                if f.endswith((".luau", ".lua")):
                    files.append(os.path.join(root, f))
    else:
        # most recently modified mirrored file
        candidates: list[tuple[float, str]] = []
        for root, _, fs in os.walk(mirror_dir):
            for f in fs:
                if f.endswith((".luau", ".lua")):
                    p = os.path.join(root, f)
                    candidates.append((os.path.getmtime(p), p))
        if candidates:
            candidates.sort(reverse=True)
            files = [candidates[0][1]]
    if not files:
        return {"diagnostics": [], "summary": {"errors": 0, "warnings": 0, "total": 0}}
    return check_paths(files, cwd=str(mirror_dir))


def run_mirror(argv: list[str]) -> int:
    """Mirror mode: materialize the Studio plugin payload and check.

    Usage: mirror [--json] [--check-all]
    - Without --check-all: checks the most recently modified mirrored file.
    - With --check-all: checks every mirrored .luau/.lua file.
    Returns 0 on clean, 1 on errors.
    """
    as_json = "--json" in argv
    check_all = "--check-all" in argv
    result = _mirror_check(check_all=check_all)
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        for d in result["diagnostics"]:
            print(f"{d['file']}:{d['line']}:{d['column']}: {d['severity'].upper()} [{d['source']}/{d['code']}] {d['message']}")
        s = result["summary"]
        if s["total"]:
            print(f"summary: {s['errors']} errors, {s['warnings']} warnings, {s['total']} total")
        if result.get("note"):
            print(f"note: {result['note']}", file=sys.stderr)
    return 1 if result["summary"]["errors"] > 0 else 0


# ---------------------------------------------------------------------------
# CLI mode
# ---------------------------------------------------------------------------

def run_cli(argv: list[str]) -> int:
    args = list(argv)
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    strict = "--warnings" in args
    args = [a for a in args if a != "--warnings"]
    if args and args[0] == "unmute":
        store = DeltaStore()
        removed = store.unmute(args[1] if len(args) > 1 else None)
        print(f"unmuted {removed} fingerprint(s); those warnings will surface again"
              if removed else "nothing to unmute")
        return 0
    if not args:
        print("usage: luaudit_hook.py check [--json] [--warnings] <file|dir> ... | unmute [fingerprint]", file=sys.stderr)
        return 2
    result = check_paths(args, cwd=os.getcwd())
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        for d in result["diagnostics"]:
            print(f"{d['file']}:{d['line']}:{d['column']}: {d['severity'].upper()} [{d['source']}/{d['code']}] {d['message']}")
        s = result["summary"]
        if s["total"]:
            print(f"summary: {s['errors']} errors, {s['warnings']} warnings, {s['total']} total")
    if result["summary"]["errors"] > 0:
        return 1
    if strict and result["summary"]["warnings"] > 0:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "check":
        return run_cli(argv[1:])
    if argv and argv[0] == "mirror":
        return run_mirror(argv[1:])
    if argv and argv[0] == "stop-hook":
        # Turn-end sweep. Invoked by the harnesses' Stop hook entries.
        return run_stop_hook()
    if argv and argv[0] in ("--help", "-h", "help"):
        print("luaudit plugin engine: hook mode (stdin event), 'check [--json] <paths>', 'mirror [--json] [--check-all]', or 'stop-hook' (turn-end sweep)")
        return 0
    return run_hook()


if __name__ == "__main__":
    sys.exit(main())
