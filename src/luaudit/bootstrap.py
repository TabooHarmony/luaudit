"""Toolchain bootstrap for luaudit.

Downloads luau-lsp, selene, stylua, and Roblox type definitions on first run
into ~/.luaudit/, with retry on failure. The v2 CLI calls this lazily when
a command needs the toolchain; the happy path stays silent so agent output
stays clean.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

# LUAUDIT_HOME overrides the cache location. The plugin hook engine honors
# the same variable, so tests and sandboxes can redirect both identically.
CACHE_DIR = Path(os.environ.get("LUAUDIT_HOME", str(Path.home() / ".luaudit")))
BIN_DIR = CACHE_DIR / "bin"
DEFS_DIR = CACHE_DIR / "defs"
CONFIG_DIR = CACHE_DIR / "config"

# Failure log: appended on every bootstrap warning/error so a failing user
# has one artifact to paste into a bug report (see `luaudit doctor --bug-report`).
LOG_FILENAME = "luaudit.log"
LOG_MAX_BYTES = 512 * 1024

DEFS_URL = "https://luau-lsp.pages.dev/type-definitions/globalTypes.d.luau"
DEFS_FILENAME = "globalTypes.d.luau"

LUAU_LSP_VERSION = "1.68.1"
SELENE_VERSION = "0.31.0"
STYLUA_VERSION = "2.5.2"

DEFS_MAX_AGE = 7 * 24 * 60 * 60  # 7 days

# SHA256 pins for every toolchain artifact, keyed by download URL. The zip is
# verified before extraction; a mismatch aborts that tool's install. When
# bumping a *_VERSION constant you MUST add the new artifact's hash here --
# an unpinned URL is a hard error, never a silent downgrade of supply-chain
# guarantees.
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

_ready = False
_last_error: str | None = None


# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------

def _get_platform() -> tuple[str, str]:
    os_name = platform.system().lower()
    machine = platform.machine().lower()
    if os_name == "windows":
        return "windows", "x86_64"
    if os_name == "darwin":
        return ("macos", "arm64") if ("arm" in machine or "aarch" in machine) else ("macos", "x86_64")
    return ("linux", "arm64") if ("arm" in machine or "aarch" in machine) else ("linux", "x86_64")


def _get_urls() -> dict[str, str]:
    os_name, arch = _get_platform()
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


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _download(url: str, dest: Path, timeout: int = 60) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "luaudit/bootstrap"})
    # Download to a temp sibling, then atomically rename into place so a
    # truncated/interrupted write can never sit at the live path.
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + f".tmp-{os.getpid()}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        os.replace(tmp, dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _download_and_extract_zip(url: str, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        _download(url, tmp_path)
        expected = SHA256_PINS.get(url)
        if expected is None:
            raise RuntimeError(f"no SHA256 pin registered for {url}; refusing to install unverified binaries")
        actual = _sha256_of(tmp_path)
        if actual != expected:
            raise RuntimeError(f"SHA256 mismatch for {url}: expected {expected}, got {actual}")
        with zipfile.ZipFile(tmp_path) as zf:
            # Reject zip-slip outright: absolute members, `..` traversal, and
            # backslash separators (mirrors the plugin engine's guard).
            for info in zf.infolist():
                name = info.filename
                if (
                    name.startswith(("/", "\\"))
                    or ".." in Path(name).parts
                    or "\\" in name
                ):
                    raise ValueError(f"unsafe zip member: {name!r}")
            zf.extractall(dest_dir)
        if platform.system() != "Windows":
            for p in dest_dir.iterdir():
                if p.is_file() and not p.suffix:
                    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    finally:
        tmp_path.unlink(missing_ok=True)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _download(url, dest)


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------

SELENE_TOML = '''# luaudit default selene config.
# All error-severity lints and bug-signal warnings stay on; the three
# known-noisy pure-style lints (multiple_statements, parenthese_conditions,
# shadowing) are off out of the box. Delete this file to get selene's
# unfiltered defaults, or add [lints] entries to re-enable anything.
std = "roblox"

[lints]
multiple_statements = "allow"
parenthese_conditions = "allow"
shadowing = "allow"
'''
LUAURC = '{\n  "languageMode": "strict"\n}\n'


def _write_configs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    selene_toml = CONFIG_DIR / "selene.toml"
    luaurc = CONFIG_DIR / ".luaurc"
    if not selene_toml.exists():
        selene_toml.write_text(SELENE_TOML)
    if not luaurc.exists():
        luaurc.write_text(LUAURC)


def init_configs(directory: Path) -> list[str]:
    """Write project configs into directory. Returns names written."""
    directory.mkdir(parents=True, exist_ok=True)
    wrote: list[str] = []
    selene_toml = directory / "selene.toml"
    luaurc = directory / ".luaurc"
    if not selene_toml.exists():
        selene_toml.write_text(SELENE_TOML)
        wrote.append("selene.toml")
    if not luaurc.exists():
        luaurc.write_text(LUAURC)
        wrote.append(".luaurc")
    return wrote


def format_files(paths: list[str], cwd: str = ".") -> dict:
    """Format files (or Luau files under directories) in place with stylua.

    Every named path is classified -- nothing is silently skipped. Returns
    {"changed", "clean", "missing", "failed"}:
    - changed: files whose bytes stylua rewrote
    - clean: existing files stylua left untouched (already formatted)
    - missing: paths that exist neither as file nor directory. Agents run
      through shells that eat Windows backslashes, so "C:Users..." shows up
      here; reporting it is the whole point (the old silent skip printed
      "nothing to format" over a genuinely unformatted file).
    - failed: existing files where stylua itself errored
    """
    original = list(paths)
    paths = [p if os.path.isabs(p) else os.path.join(cwd, p) for p in paths]
    stylua = BIN_DIR / _exe("stylua")
    files: list[str] = []
    missing: list[str] = []
    for p, raw in zip(paths, original):
        if os.path.isfile(p):
            files.append(p)
        elif os.path.isdir(p):
            for root, dirnames, fs in os.walk(p):
                dirnames[:] = [d for d in dirnames
                               if d not in (".git", "node_modules", "Packages", "Vendor")]
                for f in fs:
                    if f.endswith((".luau", ".lua")):
                        files.append(os.path.join(root, f))
        else:
            # Echo the caller's own token, not the cwd-joined form: a mangled
            # Windows path must read as the mangled path so the caller sees
            # what their shell actually passed.
            missing.append(raw)
    changed: list[str] = []
    clean: list[str] = []
    failed: list[str] = []
    if not stylua.exists():
        # CLI guards with has_stylua() and refuses earlier; a direct caller
        # still learns which paths were unusable.
        return {"changed": [], "clean": [], "missing": missing, "failed": files}
    for f in files:
        try:
            before = Path(f).read_bytes()
            proc = subprocess.run(
                [str(stylua), f],
                capture_output=True,
                text=True,
                timeout=30,
            )
            after = Path(f).read_bytes()
        except (OSError, subprocess.SubprocessError) as e:
            log_event(f"ERROR stylua failed on {f}: {e}")
            failed.append(f)
            continue
        if proc.returncode != 0:
            log_event(f"ERROR stylua failed on {f} (exit {proc.returncode}): {proc.stderr.strip()}")
            failed.append(f)
        elif before != after:
            changed.append(f)
        else:
            clean.append(f)
    return {"changed": changed, "clean": clean, "missing": missing, "failed": failed}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_ready() -> bool:
    return _ready


def last_error() -> str | None:
    return _last_error


# ---------------------------------------------------------------------------
# Failure log
# ---------------------------------------------------------------------------

def log_event(message: str) -> None:
    """Append a timestamped line to the failure log (best-effort).

    Never raises: logging must not break the run it is describing.
    """
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / LOG_FILENAME
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            # Rotate by truncation: keep the newest half of the cap.
            data = path.read_bytes()[-LOG_MAX_BYTES // 2:]
            path.write_bytes(data)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"{stamp} {message}\n")
    except Exception:
        pass


def read_log_tail(max_chars: int = 4000) -> str:
    """Return the tail of the failure log, or a note if it doesn't exist."""
    path = CACHE_DIR / LOG_FILENAME
    if not path.is_file():
        return "(no luaudit.log found; nothing has failed yet)"
    try:
        data = path.read_bytes()[-max_chars:].decode("utf-8", errors="replace")
        return data
    except Exception as e:
        return f"(luaudit.log unreadable: {e})"


def ensure_tools() -> None:
    """Download all required tools if not present. Retry on failure."""
    global _ready, _last_error
    if _ready:
        return
    _last_error = None

    urls = _get_urls()
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    DEFS_DIR.mkdir(parents=True, exist_ok=True)

    luau_lsp_path = BIN_DIR / _exe("luau-lsp")
    if not luau_lsp_path.exists():
        try:
            _download_and_extract_zip(urls["luau-lsp"], BIN_DIR)
        except Exception as e:
            _last_error = f"Failed to download luau-lsp: {e}"
            log_event(f"ERROR {_last_error}")
            print(f"[luaudit] ERROR: {_last_error}", file=sys.stderr)
            return
        if not luau_lsp_path.exists():
            _last_error = "luau-lsp binary not found after extraction"
            log_event(f"ERROR {_last_error}")
            print(f"[luaudit] ERROR: {_last_error}", file=sys.stderr)
            return

    selene_path = BIN_DIR / _exe("selene")
    if _get_platform() == ("linux", "arm64"):
        # selene ships no native linux-arm64 binary; the x86_64 build would
        # fail with "Exec format error" on every check and surface as a false
        # InternalError per file. Skip linting instead of installing a dead
        # binary.
        log_event("WARNING selene has no native linux-arm64 build, linting skipped")
        print("[luaudit] WARNING: selene has no native Linux arm64 build; linting skipped", file=sys.stderr)
    elif not selene_path.exists():
        try:
            _download_and_extract_zip(urls["selene"], BIN_DIR)
        except Exception as e:
            log_event(f"WARNING selene download failed: {e}, linting skipped")
            print(f"[luaudit] WARNING: selene download failed: {e}, linting skipped", file=sys.stderr)
        else:
            if not selene_path.exists():
                log_event("WARNING selene binary missing after install, linting skipped")
                print("[luaudit] WARNING: selene binary missing, linting skipped", file=sys.stderr)

    stylua_path = BIN_DIR / _exe("stylua")
    if not stylua_path.exists():
        try:
            _download_and_extract_zip(urls["stylua"], BIN_DIR)
        except Exception as e:
            log_event(f"WARNING stylua download failed: {e}, formatting skipped")
            print(f"[luaudit] WARNING: stylua download failed: {e}, formatting skipped", file=sys.stderr)
        else:
            if not stylua_path.exists():
                log_event("WARNING stylua binary missing after install, formatting skipped")
                print("[luaudit] WARNING: stylua binary missing, formatting skipped", file=sys.stderr)

    defs_path = DEFS_DIR / DEFS_FILENAME
    # A zero-length/undersized file (e.g. from an old interrupted write) is
    # treated as missing, never as a valid install.
    defs_ok = defs_path.exists() and defs_path.stat().st_size > 0
    need_defs = not defs_ok
    if defs_ok:
        age = time.time() - defs_path.stat().st_mtime
        if age > DEFS_MAX_AGE:
            need_defs = True
            print("[luaudit] refreshing Roblox type definitions (stale)...", file=sys.stderr)
    if need_defs:
        try:
            _download_file(DEFS_URL, defs_path)
            if not defs_path.exists() or defs_path.stat().st_size == 0:
                raise RuntimeError("downloaded type definitions are empty")
        except Exception as e:
            _last_error = f"Failed to download type definitions: {e}"
            log_event(f"ERROR {_last_error}")
            print(f"[luaudit] ERROR: {_last_error}", file=sys.stderr)
            return

    _write_configs()

    _ready = True
    print("[luaudit] ready", file=sys.stderr)


def get_paths() -> dict[str, Path]:
    return {
        "luau_lsp": BIN_DIR / _exe("luau-lsp"),
        "selene": BIN_DIR / _exe("selene"),
        "stylua": BIN_DIR / _exe("stylua"),
        "defs": DEFS_DIR / DEFS_FILENAME,
        "selene_toml": CONFIG_DIR / "selene.toml",
        "luaurc": CONFIG_DIR / ".luaurc",
    }


def has_selene() -> bool:
    # selene has no native linux-arm64 build; never report the x86_64 binary
    # (if present) as runnable there.
    if _get_platform() == ("linux", "arm64"):
        return False
    return (BIN_DIR / _exe("selene")).exists()


def has_stylua() -> bool:
    return (BIN_DIR / _exe("stylua")).exists()
