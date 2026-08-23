"""Tests for the shipped plugin tree (plugins/luaudit/).

These validate the artifacts that actually ship to users in Claude Code and
Codex marketplaces:
- both plugin manifests parse and carry the required fields
- hooks/hooks.json references a script that exists
- the hook .sh is executable and the .cmd wrapper is present
- the engine runs: hook mode (real event in -> contract JSON out) and check
  mode (exit code + summary), driven with fake binaries so it's offline.

The engine is stdlib-only and shares the real cache, so tests point HOME at
a tmp dir and install a fake toolchain into it.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugins" / "luaudit"
ENGINE = PLUGIN_DIR / "scripts" / "luaudit_hook.py"

# Import the engine module directly for unit-level helpers (stdlib-only).
sys.path.insert(0, str(PLUGIN_DIR / "scripts"))
import luaudit_hook as engine  # noqa: E402

PLUGIN_FILES = [
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "hooks/hooks.json",
    "scripts/luaudit-hook.sh",
    "scripts/luaudit-hook.cmd",
    "scripts/luaudit_hook.py",
    "skills/luaudit/SKILL.md",
]


def _run_hook(tmp_home: Path, event: dict, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(tmp_home)
    # Windows Python ignores HOME (Path.home() reads USERPROFILE), so point
    # the engine at the fake cache explicitly via its own override var.
    env["LUAUDIT_HOME"] = str(tmp_home / ".luaudit")
    env["PYTHON"] = sys.executable
    env.pop("LUAUDIT_HOME_OVERRIDE_SENTINEL", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(ENGINE)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        env=env,
    )


def _write_fake_bin(dirpath: Path, name: str, content: str) -> Path:
    """Write a fake executable (a shell script) that behaves like the tool."""
    p = dirpath / name
    p.write_text(content, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


# ---------------------------------------------------------------------------
# Artifact checks
# ---------------------------------------------------------------------------

def test_plugin_files_exist():
    for rel in PLUGIN_FILES:
        assert (PLUGIN_DIR / rel).exists(), f"missing {rel}"


def test_claude_manifest_valid():
    data = json.loads((PLUGIN_DIR / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert data["name"] == "luaudit"
    # No pinned `version`: Claude Code then falls back to the resolved commit
    # SHA, so pushes are picked up by `/plugin marketplace update` instead of
    # being masked behind a stale manifest string. See README Updating.
    assert "version" not in data
    assert "description" in data


def test_codex_manifest_valid():
    data = json.loads((PLUGIN_DIR / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert data["name"] == "luaudit"
    assert "version" not in data
    # codex loads ./hooks/hooks.json by default; manifest may omit "hooks"
    assert "interface" in data


def test_hooks_json_references_existing_script():
    data = json.loads((PLUGIN_DIR / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    post = data["hooks"]["PostToolUse"]
    assert isinstance(post, list) and post
    group = post[0]
    assert "Write|Edit" in group.get("matcher", "")
    hook = group["hooks"][0]
    cmd = hook["command"]
    # POSIX command uses ${CLAUDE_PLUGIN_ROOT} (set by both codex and claude)
    # and points at the extensionless .sh (bash shebang).
    assert "${CLAUDE_PLUGIN_ROOT}" in cmd
    m = re.match(r'^"(\$\{CLAUDE_PLUGIN_ROOT\}/[^"]+)"$', cmd)
    assert m, f"hook command must be a single quoted path, got {cmd}"
    ref = m.group(1).replace("${CLAUDE_PLUGIN_ROOT}/", "", 1)
    assert (PLUGIN_DIR / ref).exists(), f"hooks.json references missing {ref}"

    # Windows command (codex 0.147's command_windows, used verbatim on win32)
    # MUST be a literal absolute-style path with BACKSLASHES and the .cmd
    # extension, inside one pair of quotes. The forward-slash/extensionless
    # forms break under `cmd /C ""...""` (see skill reference).
    cw = hook.get("command_windows") or hook.get("commandWindows")
    assert cw, "hooks.json must ship command_windows for Windows"
    mw = re.match(r'^"(\$\{CLAUDE_PLUGIN_ROOT\}\\[^"]+\.cmd)"$', cw)
    assert mw, f"command_windows must be a quoted backslash .cmd path, got {cw}"
    refw = mw.group(1).replace("${CLAUDE_PLUGIN_ROOT}\\", "", 1)
    refw_native = Path(*refw.split("\\"))
    assert (PLUGIN_DIR / refw_native).exists(), f"command_windows references missing {refw}"
    assert "/" not in refw, "command_windows path must use backslashes only"
    # the .cmd wrapper must not rely on %~dp0 concatenation (breaks under
    # nested `cmd /C ""...""`)
    cmd_src = (PLUGIN_DIR / refw_native).read_text(encoding="utf-8")
    assert "%SCRIPT_DIR%" not in cmd_src, "cmd wrapper must not use SCRIPT_DIR concat"
    assert 'pushd "%~dp0"' not in cmd_src, "cmd wrapper must not use pushd %~dp0"


def test_hook_script_executable_on_posix():
    if platform.system() == "Windows":
        pytest.skip("POSIX exec bits are not representable on NTFS checkouts")
    sh = PLUGIN_DIR / "scripts" / "luaudit-hook.sh"
    mode = sh.stat().st_mode
    assert mode & stat.S_IXUSR, "hook .sh must be executable"
    # the .cmd wrapper is only used on Windows, but must be present
    assert (PLUGIN_DIR / "scripts" / "luaudit-hook.cmd").exists()


def test_marketplace_files():
    for rel in (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"):
        data = json.loads((REPO_ROOT / rel).read_text(encoding="utf-8"))
        assert data["name"] == "luaudit"
        plugins = data["plugins"]
        assert len(plugins) == 1
        entry = plugins[0]
        assert entry["name"] == "luaudit"
        assert entry["source"].startswith("./plugins/luaudit"), entry["source"]
        # the plugin the marketplace points to exists
        assert (REPO_ROOT / entry["source"]).exists(), entry["source"]


# ---------------------------------------------------------------------------
# Engine behavior (offline, fake toolchain in a temp HOME)
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_toolchain(tmp_path):
    """Install fake luau-lsp/selene/stylua + defs into a temp ~/.luaudit.

    Fake luau-lsp emits a real-style diagnostic line for a 'bad' marker.
    Fake selene emits JSON diagnostics. Fake stylua exits 1 with a diff line.

    Skipped on Windows: the fakes are #!/bin/sh scripts, which cannot be
    executed by native Windows Python (and under git-bash they rewrite path
    arguments MSYS-style). Engine-on-Windows behavior is covered live on the
    WINDEV VM; detection/parity logic runs everywhere via test_plugin_parity.
    """
    if os.name == "nt":
        pytest.skip("fake POSIX toolchain cannot execute on Windows")
    home = tmp_path / "home"
    bin_dir = home / ".luaudit" / "bin"
    defs_dir = home / ".luaudit" / "defs"
    config_dir = home / ".luaudit" / "config"
    bin_dir.mkdir(parents=True)
    defs_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (defs_dir / "globalTypes.d.luau").write_text("declare global game: any\n")
    (config_dir / "selene.toml").write_text('std = "roblox"\n')
    (config_dir / ".luaurc").write_text('{"languageMode": "strict"}')

    suffix = ".exe" if os.name == "nt" else ""

    _write_fake_bin(bin_dir, f"luau-lsp{suffix}", """#!/bin/sh
for f in "$@"; do
  case "$f" in
    *bad*) echo "/path/to/bad.luau:1:1-5: (W0) TypeError: Expected this to be 'number', got 'string'";;
  esac
done
exit 0
""")
    _write_fake_bin(bin_dir, f"selene{suffix}", """#!/bin/sh
JSON='{"primary_label":{"filename":"/path/to/bad.luau","span":{"start_line":2,"start_column":1,"end_line":2,"end_column":5}},"severity":"Warning","code":"unused_variable","message":"unused variable"}'
for f in "$@"; do
  case "$f" in
    *bad*) echo "$JSON";;
  esac
done
exit 0
""")
    _write_fake_bin(bin_dir, f"stylua{suffix}", """#!/bin/sh
for f in "$@"; do
  case "$f" in
    *bad*) echo "Diff in /path/to/bad.luau:1:1";;
  esac
done
exit 1
""")
    return home


def _write_sample(root: Path, name: str, content: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    # Binary write: text mode translates \n to \r\n on Windows and stylua
    # --check then flags every fixture as unformatted.
    p.write_bytes(content.encode("utf-8"))
    return p


def test_engine_hook_bad_file_emits_contract(fake_toolchain, tmp_path):
    """A bad .luau file produces the exact PostToolUse JSON contract."""
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\nlocal y = 1\n")
    r = _run_hook(fake_toolchain, {"tool_name": "Write", "tool_input": {"file_path": str(f)}})
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "luaudit diagnostics" in ctx
    assert "[ERROR]" in ctx                      # errors inject inline
    assert "[WARNING]" not in ctx                # warnings held to turn end
    assert "held for the end-of-turn summary" in ctx


def test_engine_hook_clean_file_silent(fake_toolchain, tmp_path):
    """A clean .luau file produces NO output (the documented silent contract)."""
    f = _write_sample(tmp_path, "clean.luau", "local x: number = 1\n")
    r = _run_hook(fake_toolchain, {"tool_name": "Write", "tool_input": {"file_path": str(f)}})
    assert r.returncode == 0
    assert r.stdout.strip() == "", f"expected silent, got: {r.stdout!r}"


def test_engine_hook_non_luau_silent(fake_toolchain, tmp_path):
    """Non-Luau files and missing files are ignored entirely."""
    f = _write_sample(tmp_path, "notes.md", "# hi\n")
    r = _run_hook(fake_toolchain, {"tool_name": "Write", "tool_input": {"file_path": str(f)}})
    assert r.stdout.strip() == ""
    r2 = _run_hook(fake_toolchain, {"tool_name": "Write", "tool_input": {"file_path": "/no/such/bad.luau"}})
    assert r2.stdout.strip() == ""


def test_engine_check_mode(fake_toolchain, tmp_path):
    """check mode prints diagnostics text and exits non-zero on errors."""
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\n")
    r = subprocess.run(
        [sys.executable, str(ENGINE), "check", str(f)],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(fake_toolchain), "PYTHON": sys.executable},
    )
    assert r.returncode == 1
    assert "TypeError" in r.stdout
    assert "summary:" in r.stdout


def test_engine_check_mode_json(fake_toolchain, tmp_path):
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\n")
    r = subprocess.run(
        [sys.executable, str(ENGINE), "check", "--json", str(f)],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(fake_toolchain), "PYTHON": sys.executable},
    )
    assert r.returncode == 1
    data = json.loads(r.stdout)
    assert data["summary"]["errors"] >= 1
    assert data["summary"]["warnings"] >= 1


def test_engine_sourcemap_discovery_and_parse(fake_toolchain, tmp_path):
    """With a sourcemap.json present, luau-lsp runs with --sourcemap and the
    emitted `path [game/...]:line:col-col:` diagnostics are parsed correctly.

    The fake luau-lsp emits a sourcemap-style line ONLY when --sourcemap is in
    argv; without it, it emits nothing (mimicking the real tool: no sourcemap,
    no cross-file resolution).
    """
    project = tmp_path / "project"
    _write_sample(project, "src/Server/Utils.luau", "--!strict\nlocal Utils = {}\nfunction Utils.makePoint(x: number, y: number) return {x=x, y=y} end\nreturn Utils\n")
    f = _write_sample(project, "src/Server/Consumer.luau", "--!strict\nlocal S = game:GetService('ServerScriptService')\nlocal U = require(S.Utils)\nlocal p = U.makePoint('x', 'y')\nprint(p)\n")
    (project / "sourcemap.json").write_text(json.dumps({
        "name": "test", "className": "DataModel",
        "children": [{
            "className": "ServerScriptService", "name": "ServerScriptService",
            "children": [
                {"className": "ModuleScript", "name": "Utils", "filePaths": ["src/Server/Utils.luau"]},
                {"className": "Script", "name": "Consumer", "filePaths": ["src/Server/Consumer.luau"]},
            ],
        }],
    }), encoding="utf-8")

    suffix = ".exe" if os.name == "nt" else ""
    quoted = str(f).replace("\\", "\\\\")
    _write_fake_bin(fake_toolchain / ".luaudit" / "bin", f"luau-lsp{suffix}", f"""#!/bin/sh
has_sm=0
for arg in "$@"; do
  case "$arg" in
    --sourcemap=*) has_sm=1;;
  esac
done
if [ "$has_sm" = "1" ]; then
  echo "{quoted} [game/ServerScriptService/Consumer]:8:27-29: (W0) TypeError: Expected this to be 'number', but got 'string'"
fi
exit 0
""")

    r = subprocess.run(
        [sys.executable, str(ENGINE), "check", "--json", str(f)],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(fake_toolchain), "PYTHON": sys.executable},
    )
    assert r.returncode == 1, r.stderr
    data = json.loads(r.stdout)
    errors = [d for d in data["diagnostics"] if d["severity"] == "error"]
    assert len(errors) == 1, f"expected the cross-file TypeError, got: {data['diagnostics']}"
    e = errors[0]
    assert e["code"] == "TypeError"
    assert e["line"] == 8 and e["column"] == 27
    assert e["file"] == os.path.abspath(str(f)) or e["file"] == str(f)


def test_engine_sourcemap_autogen_from_rojo_project(fake_toolchain, tmp_path):
    """When only default.project.json exists (barebones rojo), the engine runs
    `rojo sourcemap` to generate sourcemap.json, then uses it."""
    project = tmp_path / "rojoproj"
    f = _write_sample(project, "src/Server/Consumer.luau",
                      "--!strict\nlocal S = game:GetService('ServerScriptService')\nlocal U = require(S.Utils)\nlocal p = U.makePoint('x','y')\nprint(p)\n")
    (project / "default.project.json").write_text(json.dumps({
        "name": "test", "tree": {"$className": "DataModel"},
    }), encoding="utf-8")

    suffix = ".exe" if os.name == "nt" else ""
    # fake rojo: writes a real-looking sourcemap.json next to the project
    _write_fake_bin(project, f"rojo{suffix}", f"""#!/bin/sh
cat > "$(dirname "$1")/sourcemap.json" <<'EOF'
{{
  "name": "test",
  "className": "DataModel",
  "children": [
    {{
      "className": "ServerScriptService",
      "name": "ServerScriptService",
      "children": [
        {{"className": "ModuleScript", "name": "Utils", "filePaths": ["src/Server/Utils.luau"]}},
        {{"className": "Script", "name": "Consumer", "filePaths": ["src/Server/Consumer.luau"]}}
      ]
    }}
  ]
}}
EOF
exit 0
""")
    # fake luau-lsp: emit cross-file error only when --sourcemap is passed
    quoted = str(f).replace("\\", "\\\\")
    _write_fake_bin(fake_toolchain / ".luaudit" / "bin", f"luau-lsp{suffix}", f"""#!/bin/sh
has_sm=0
for arg in "$@"; do
  case "$arg" in
    --sourcemap=*) has_sm=1;;
  esac
done
if [ "$has_sm" = "1" ]; then
  echo "{quoted} [game/ServerScriptService/Consumer]:8:27-29: (W0) TypeError: Expected this to be 'number', but got 'string'"
fi
exit 0
""")
    # put fake rojo on PATH for the engine's shutil.which lookup
    env = {**os.environ, "HOME": str(fake_toolchain), "PYTHON": sys.executable,
           "PATH": str(project) + os.pathsep + os.environ.get("PATH", "")}
    assert (project / f"rojo{suffix}").exists()

    r = subprocess.run(
        [sys.executable, str(ENGINE), "check", "--json", str(f)],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 1, f"expected cross-file error, got stderr: {r.stderr}"
    data = json.loads(r.stdout)
    errors = [d for d in data["diagnostics"] if d["severity"] == "error"]
    assert len(errors) == 1, f"expected 1 cross-file TypeError, got: {data['diagnostics']}"
    assert errors[0]["code"] == "TypeError"
    # the engine generated sourcemap.json next to the project
    assert (project / "sourcemap.json").exists()


def test_engine_mirror_materializes_and_checks(fake_toolchain, tmp_path):
    """mirror mode reads the Studio plugin settings.json payload, materializes
    files + sourcemap into ~/.luaudit/mirror/, and checks them."""
    # fake Studio settings.json with a luaudit mirror payload
    local = tmp_path / "AppData" / "Local"
    settings_dir = local / "Roblox" / "12345" / "InstalledPlugins" / "0"
    settings_dir.mkdir(parents=True)
    payload = {
        "sources": {
            "ServerScriptService/Utils.luau": "--!strict\nlocal Utils = {}\nfunction Utils.makePoint(x: number, y: number) return {x=x, y=y} end\nreturn Utils\n",
            "ServerScriptService/Consumer.luau": "--!strict\nlocal S = game:GetService('ServerScriptService')\nlocal U = require(S.Utils)\nlocal p = U.makePoint('x', 'y')\nprint(p)\n",
        },
        "tree": {
            "name": "test-place", "className": "DataModel",
            "children": [{
                "className": "ServerScriptService", "name": "ServerScriptService",
                "children": [
                    {"className": "ModuleScript", "name": "Utils", "filePaths": ["ServerScriptService/Utils.luau"]},
                    {"className": "Script", "name": "Consumer", "filePaths": ["ServerScriptService/Consumer.luau"]},
                ],
            }],
        },
    }
    settings = {"luaudit-mirror-v1": json.dumps(payload)}
    (settings_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    # fake luau-lsp: emit cross-file error only when --sourcemap is passed
    suffix = ".exe" if os.name == "nt" else ""
    mirror_path = fake_toolchain / ".luaudit" / "mirror" / "ServerScriptService" / "Consumer.luau"
    quoted = str(mirror_path).replace("\\", "\\\\")
    _write_fake_bin(fake_toolchain / ".luaudit" / "bin", f"luau-lsp{suffix}", f"""#!/bin/sh
has_sm=0
for arg in "$@"; do
  case "$arg" in
    --sourcemap=*) has_sm=1;;
  esac
done
if [ "$has_sm" = "1" ]; then
  echo "{quoted} [game/ServerScriptService/Consumer]:8:27-29: (W0) TypeError: Expected this to be 'number', but got 'string'"
fi
exit 0
""")

    env = {**os.environ, "HOME": str(fake_toolchain), "PYTHON": sys.executable,
           "LOCALAPPDATA": str(local)}
    r = subprocess.run(
        [sys.executable, str(ENGINE), "mirror", "--json", "--check-all"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 1, f"expected cross-file error, stderr: {r.stderr}"
    data = json.loads(r.stdout)
    errors = [d for d in data["diagnostics"] if d["severity"] == "error"]
    assert len(errors) == 1, f"expected 1 TypeError, got: {data['diagnostics']}"
    assert errors[0]["code"] == "TypeError"
    # files materialized into the mirror dir
    assert (fake_toolchain / ".luaudit" / "mirror" / "sourcemap.json").exists()
    assert (fake_toolchain / ".luaudit" / "mirror" / "ServerScriptService" / "Consumer.luau").exists()


def test_engine_mcp_edit_routes_to_mirror(fake_toolchain, tmp_path):
    """An MCP Studio-bridge edit tool (edit_script) routes to mirror mode and
    emits the PostToolUse contract with mirrored-tree diagnostics."""
    # fake Studio settings.json with a mirror payload containing a cross-file error
    local = tmp_path / "AppData" / "Local"
    settings_dir = local / "Roblox" / "12345" / "InstalledPlugins" / "0"
    settings_dir.mkdir(parents=True)
    payload = {
        "sources": {
            "ServerScriptService/Utils.luau": "--!strict\nlocal Utils = {}\nfunction Utils.makePoint(x: number, y: number) return {x=x, y=y} end\nreturn Utils\n",
            "ServerScriptService/Consumer.luau": "--!strict\nlocal S = game:GetService('ServerScriptService')\nlocal U = require(S.Utils)\nlocal p = U.makePoint('x', 'y')\nprint(p)\n",
        },
        "tree": {
            "name": "test-place", "className": "DataModel",
            "children": [{
                "className": "ServerScriptService", "name": "ServerScriptService",
                "children": [
                    {"className": "ModuleScript", "name": "Utils", "filePaths": ["ServerScriptService/Utils.luau"]},
                    {"className": "Script", "name": "Consumer", "filePaths": ["ServerScriptService/Consumer.luau"]},
                ],
            }],
        },
    }
    (settings_dir / "settings.json").write_text(json.dumps({"luaudit-mirror-v1": json.dumps(payload)}), encoding="utf-8")

    suffix = ".exe" if os.name == "nt" else ""
    mirror_path = fake_toolchain / ".luaudit" / "mirror" / "ServerScriptService" / "Consumer.luau"
    quoted = str(mirror_path).replace("\\", "\\\\")
    _write_fake_bin(fake_toolchain / ".luaudit" / "bin", f"luau-lsp{suffix}", f"""#!/bin/sh
has_sm=0
for arg in "$@"; do
  case "$arg" in
    --sourcemap=*) has_sm=1;;
  esac
done
if [ "$has_sm" = "1" ]; then
  echo "{quoted} [game/ServerScriptService/Consumer]:8:27-29: (W0) TypeError: Expected this to be 'number', but got 'string'"
fi
exit 0
""")

    env = {**os.environ, "HOME": str(fake_toolchain), "PYTHON": sys.executable,
           "LOCALAPPDATA": str(local)}
    r = _run_hook(fake_toolchain, {"tool_name": "edit_script", "tool_input": {"path": "ServerScriptService/Consumer"}}, env_extra={"LOCALAPPDATA": str(local)})
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "luaudit diagnostics:" in ctx
    assert "TypeError" in ctx


def test_engine_mirror_empty_sources_list(fake_toolchain, tmp_path):
    """Luau's JSONEncode turns an empty sources table into [] (not {}). The
    engine must normalize a list-shaped sources (empty or pairs) to a dict
    and still materialize the tree + sourcemap."""
    mirror_dir = tmp_path / "mirror"
    payload = {
        "sources": [],  # Luau empty-table encoding
        "tree": {
            "name": "place", "className": "DataModel",
            "children": [
                {"name": "ServerScriptService", "className": "ServerScriptService",
                 "children": [{"name": "Utils", "className": "ModuleScript",
                               "filePaths": ["ServerScriptService/Utils.luau"]}]}
            ],
        },
    }
    sm = engine.materialize_mirror(payload, mirror_dir)
    assert sm.exists()
    sm_data = json.loads(sm.read_text())
    assert sm_data["className"] == "DataModel"
    assert sm_data["children"][0]["name"] == "ServerScriptService"
    # list-of-pairs shape also normalizes
    payload2 = {"sources": [["ServerScriptService/Utils.luau", "return 1"]], "tree": payload["tree"]}
    sm2 = engine.materialize_mirror(payload2, mirror_dir)
    assert (mirror_dir / "ServerScriptService" / "Utils.luau").exists()
    assert (mirror_dir / "ServerScriptService" / "Utils.luau").read_text() == "return 1"


def test_engine_mirror_prefers_payload_user_dir(fake_toolchain, tmp_path, monkeypatch):
    """With multiple Roblox user dirs, _find_studio_settings must pick the one
    carrying the mirror payload, not the first/oldest (the '0' dir is a stale
    account that sorts first but has no payload)."""
    roblox = tmp_path / "Roblox"
    (roblox / "0" / "InstalledPlugins" / "0").mkdir(parents=True)
    (roblox / "154032452" / "InstalledPlugins" / "0").mkdir(parents=True)
    # stale '0' dir: no mirror payload, old mtime
    stale = roblox / "0" / "InstalledPlugins" / "0" / "settings.json"
    stale.write_text(json.dumps({"SomeKey": 1}))
    os.utime(stale, (1000, 1000))
    # active user dir: has the payload, newer mtime
    live = roblox / "154032452" / "InstalledPlugins" / "0" / "settings.json"
    payload = json.dumps({"luaudit-mirror-v1": json.dumps({"sources": {}, "tree": {"className": "DataModel"}})})
    live.write_text(payload)
    os.utime(live, (2000, 2000))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert engine._find_studio_settings() == str(live)


def test_engine_check_clean_ok(fake_toolchain, tmp_path):
    f = _write_sample(tmp_path, "clean.luau", "local x: number = 1\n")
    r = subprocess.run(
        [sys.executable, str(ENGINE), "check", str(f)],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(fake_toolchain), "PYTHON": sys.executable},
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "", f"clean check should be silent, got: {r.stdout!r}"


def test_engine_check_nonexistent_target_error(fake_toolchain):
    r = subprocess.run(
        [sys.executable, str(ENGINE), "check", "/no/such/dir"],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(fake_toolchain), "PYTHON": sys.executable},
    )
    assert r.returncode == 1
    assert "NoSuchFile" in r.stdout


def test_engine_hook_ignores_other_tools(fake_toolchain, tmp_path):
    """Hooks on non-write tools are ignored even if a file path is set."""
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\n")
    for tool in ("Read", "Glob"):
        r = _run_hook(fake_toolchain, {"tool_name": tool, "tool_input": {"file_path": str(f)}})
        assert r.stdout.strip() == "", f"{tool} should be ignored"


def test_engine_hook_bash_write_redirection_emits_contract(fake_toolchain, tmp_path):
    """Codex writes via Bash commands (printf > file.luau); the hook must
    extract the written path from the command and emit diagnostics."""
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\n")
    cmd = f"printf 'local x = 1\\n' > {f} && cat {f}"
    r = _run_hook(fake_toolchain, {"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "luaudit diagnostics" in out["hookSpecificOutput"]["additionalContext"]


def test_engine_hook_bash_append_and_sed(fake_toolchain, tmp_path):
    """Append (>>) and sed -i forms also yield the written Luau path."""
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\n")
    r1 = _run_hook(fake_toolchain, {"tool_name": "Bash", "tool_input": {"command": f"echo x >> {f}"}})
    assert r1.stdout.strip() != "", "append should produce diagnostics"
    r2 = _run_hook(fake_toolchain, {"tool_name": "Bash", "tool_input": {"command": f"sed -i 's/a/b/' {f}"}})
    assert r2.stdout.strip() != "", "sed -i should produce diagnostics"


def test_engine_hook_bash_quoted_spaced_path(fake_toolchain, tmp_path):
    """Quoted paths containing spaces (printf > \"my file.luau\") must fire."""
    d = tmp_path / "with space"
    d.mkdir()
    f = d / "bad.luau"
    f.write_text("local x: number = 's'\n", encoding="utf-8")
    r = _run_hook(fake_toolchain, {"tool_name": "Bash", "tool_input": {"command": f"printf 'x' > \"{f}\""}})
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert "luaudit diagnostics" in out["hookSpecificOutput"]["additionalContext"]
    # single-quoted variant too
    r2 = _run_hook(fake_toolchain, {"tool_name": "Bash", "tool_input": {"command": f"printf 'x' > '{f}'"}})
    assert r2.stdout.strip() != "", "single-quoted path should fire"


def test_engine_hook_bash_stderr_redirect_silent(fake_toolchain, tmp_path):
    """2> (stderr) redirection must NOT be treated as a write, even if the
    stderr target is a .luau file that exists."""
    f = _write_sample(tmp_path, "err.luau", "local x: number = 1\n")
    r = _run_hook(fake_toolchain, {"tool_name": "Bash", "tool_input": {"command": f"ls > /dev/null 2> {f}"}})
    assert r.stdout.strip() == "", "2> stderr redirect must be silent"


def test_engine_hook_bash_allstream_and_noclobber(fake_toolchain, tmp_path):
    """&> (all-stream) and >| (noclobber force) writes MUST fire (N1/N2
    regressions)."""
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\n")
    r1 = _run_hook(fake_toolchain, {"tool_name": "Bash", "tool_input": {"command": f"somecmd &> {f}"}})
    assert r1.stdout.strip() != "", "&> all-stream write should fire"
    r2 = _run_hook(fake_toolchain, {"tool_name": "Bash", "tool_input": {"command": f"somecmd >| {f}"}})
    assert r2.stdout.strip() != "", ">| noclobber-force write should fire"


def test_engine_hook_bash_lua_read_only_silent(fake_toolchain, tmp_path):
    """Read-only .lua tokens (cat/head/git diff) must NOT fire (F2 regression)."""
    f = _write_sample(tmp_path, "clean.lua", "local x: number = 's'\n")
    for cmd in (f"cat {f}", f"head -5 {f}", f"git diff {f}"):
        r = _run_hook(fake_toolchain, {"tool_name": "Bash", "tool_input": {"command": cmd}})
        assert r.stdout.strip() == "", f"read-only {cmd!r} must be silent"


def test_engine_hook_bash_sed_lua_guarded(fake_toolchain, tmp_path):
    """sed -i fires for .lua too, but a bare .lua token without sed guard is
    not a write."""
    f = _write_sample(tmp_path, "bad.lua", "local x: number = 's'\n")
    r = _run_hook(fake_toolchain, {"tool_name": "Bash", "tool_input": {"command": f"sed -i 's/a/b/' {f}"}})
    assert r.stdout.strip() != "", "sed -i on .lua should fire"
    r2 = _run_hook(fake_toolchain, {"tool_name": "Bash", "tool_input": {"command": f"echo hi {f}"}})
    assert r2.stdout.strip() == "", "bare .lua token must be silent"


def test_engine_bootstrap_failure_reports_internal_error(tmp_path):
    """F1 regression: a broken/offline toolchain must surface an InternalError
    diagnostic, NEVER a silent 'clean'."""
    home = tmp_path / "empty-home"
    home.mkdir()
    f = tmp_path / "bad.luau"
    f.write_text("local x: number = 's'\n", encoding="utf-8")
    env = {**os.environ, "HOME": str(home), "PYTHON": sys.executable,
           "LUAUDIT_HOME": str(home / ".luaudit"),
           # block all network so bootstrap fails deterministically
           "https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"}
    r = subprocess.run(
        [sys.executable, str(ENGINE)],
        input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(f)}}),
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert r.returncode == 0
    out = json.loads(r.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "InternalError" in ctx, f"expected InternalError, got: {ctx!r}"
    assert "toolchain" in ctx


def test_engine_hook_shell_command_set_content_emits_contract(fake_toolchain, tmp_path):
    """codex 0.147 on Windows writes via shell_command (PowerShell Set-Content);
    the hook must extract the path and emit diagnostics (cache-staleness fix)."""
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\n")
    cmd = f"Set-Content -Path '{f}' -Value 'local x = 1'"
    r = _run_hook(fake_toolchain, {"tool_name": "shell_command", "tool_input": {"command": cmd}})
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "luaudit diagnostics" in out["hookSpecificOutput"]["additionalContext"]


def test_engine_hook_shell_command_outfile_and_addcontent(fake_toolchain, tmp_path):
    """Out-File and Add-Content PowerShell writes must fire."""
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\n")
    r1 = _run_hook(fake_toolchain, {"tool_name": "shell_command", "tool_input": {"command": f"Out-File -FilePath '{f}' -Value 'x'"}})
    assert r1.stdout.strip() != "", "Out-File should produce diagnostics"
    r2 = _run_hook(fake_toolchain, {"tool_name": "shell_command", "tool_input": {"command": f"Add-Content -Path '{f}' -Value 'x'"}})
    assert r2.stdout.strip() != "", "Add-Content should produce diagnostics"


def test_engine_hook_shell_command_dotnet_file_write(fake_toolchain, tmp_path):
    """[System.IO.File]::WriteAllText writes must fire."""
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\n")
    cmd = f"[System.IO.File]::WriteAllText('{f}', 'local x = 1')"
    r = _run_hook(fake_toolchain, {"tool_name": "shell_command", "tool_input": {"command": cmd}})
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert "luaudit diagnostics" in out["hookSpecificOutput"]["additionalContext"]


def test_engine_hook_shell_command_read_silent(fake_toolchain, tmp_path):
    """A read-only shell_command (Get-Content) must stay silent."""
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\n")
    r = _run_hook(fake_toolchain, {"tool_name": "shell_command", "tool_input": {"command": f"Get-Content -Path '{f}'"}})
    assert r.stdout.strip() == "", "Get-Content read must be silent"


def test_engine_hook_cmd_tool_name_emits(fake_toolchain, tmp_path):
    """Cmd/PowerShell tool names route through the same extraction."""
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\n")
    for tool in ("PowerShell", "Cmd", "cmd"):
        r = _run_hook(fake_toolchain, {"tool_name": tool, "tool_input": {"command": f"Set-Content -Path '{f}' -Value 'x'"}})
        assert r.stdout.strip() != "", f"{tool} should produce diagnostics"


def test_engine_hook_bash_read_only_silent(fake_toolchain, tmp_path):
    """cat/read-only Bash commands on .luau files must stay silent."""
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\n")
    for cmd in (f"cat {f}", f"head -5 {f}", f"ls -la {f}"):
        r = _run_hook(fake_toolchain, {"tool_name": "Bash", "tool_input": {"command": cmd}})
        assert r.stdout.strip() == "", f"read-only {cmd!r} should be silent"


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="bash + python3 required to exercise the shipped .sh hook",
)
def test_sh_hook_script_end_to_end(fake_toolchain, tmp_path):
    """The shipped bash hook (what hooks.json actually invokes) drives the
    engine: event in -> contract JSON out, silent on clean."""
    f = _write_sample(tmp_path, "bad.luau", "local x: number = 's'\n")
    env = {**os.environ, "HOME": str(fake_toolchain), "PYTHON": "python3"}
    r = subprocess.run(
        ["bash", str(PLUGIN_DIR / "scripts" / "luaudit-hook.sh")],
        input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(f)}}),
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "[ERROR]" in out["hookSpecificOutput"]["additionalContext"]

    clean = _write_sample(tmp_path, "clean.luau", "local x: number = 1\n")
    r2 = subprocess.run(
        ["bash", str(PLUGIN_DIR / "scripts" / "luaudit-hook.sh")],
        input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(clean)}}),
        capture_output=True, text=True, env=env,
    )
    assert r2.returncode == 0
    assert r2.stdout.strip() == "", "clean file must be silent through the .sh hook"

