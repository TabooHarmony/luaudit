"""Parity tests: the plugin engine must never drift from the package.

The plugin ships a self-contained copy of the engine so harnesses can run it
without installing the package. That copy is hand-synced, which has bitten us
before (--warnings existed in the CLI but not the plugin). These tests fail
the build the moment the two copies diverge in versions, download pins, or
observable behavior.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_ENGINE = REPO / "plugins" / "luaudit" / "scripts" / "luaudit_hook.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("luaudit_plugin_engine", PLUGIN_ENGINE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_package_bootstrap():
    sys.path.insert(0, str(REPO / "src"))
    import luaudit.bootstrap as bs

    return bs


# ---------------------------------------------------------------------------
# Constant parity
# ---------------------------------------------------------------------------

def test_versions_match() -> None:
    plugin = _load_plugin()
    bs = _load_package_bootstrap()
    assert plugin.LUAU_LSP_VERSION == bs.LUAU_LSP_VERSION
    assert plugin.SELENE_VERSION == bs.SELENE_VERSION
    assert plugin.STYLUA_VERSION == bs.STYLUA_VERSION


def test_sha256_pins_identical() -> None:
    plugin = _load_plugin()
    bs = _load_package_bootstrap()
    assert plugin.SHA256_PINS == bs.SHA256_PINS, (
        "plugin engine and package bootstrap disagree on artifact hashes; "
        "update both or bump only together"
    )


def test_default_selene_config_identical() -> None:
    """The curated default selene.toml must not drift between engines."""
    plugin = _load_plugin()
    bs = _load_package_bootstrap()
    assert plugin.SELENE_TOML == bs.SELENE_TOML
    # And it must keep the three noisy style lints off.
    for rule in ("multiple_statements", "parenthese_conditions", "shadowing"):
        assert f'{rule} = "allow"' in plugin.SELENE_TOML, rule


def test_every_download_url_is_pinned() -> None:
    plugin = _load_plugin()
    bs = _load_package_bootstrap()
    for mod in (plugin, bs):
        get_urls = getattr(mod, "_get_urls", None) or getattr(mod, "_urls")
        urls = set(get_urls().values())
        unpinned = urls - set(mod.SHA256_PINS)
        assert not unpinned, f"unpinned download URLs: {sorted(unpinned)}"


# ---------------------------------------------------------------------------
# Behavioral parity: edit detection (pure functions, no downloads)
# ---------------------------------------------------------------------------

def _detect(plugin, tool_name: str, command_or_input):
    if isinstance(command_or_input, dict):
        event = {"tool_name": tool_name, "tool_input": command_or_input}
    else:
        event = {"tool_name": tool_name, "tool_input": {"command": command_or_input}}
    return plugin._hook_event_file(event)


def test_detection_claude_tools(plugin=None) -> None:
    p = plugin or _load_plugin()
    assert _detect(p, "Write", {"file_path": "a/b.luau"}) == "a/b.luau"
    assert _detect(p, "Edit", {"file_path": "a/b.lua"}) == "a/b.lua"
    assert _detect(p, "MultiEdit", {"file_path": "a/b.luau"}) == "a/b.luau"


def test_detection_shell_writes(plugin=None) -> None:
    p = plugin or _load_plugin()
    assert _detect(p, "shell_command", "echo x > out.luau") == "out.luau"
    assert _detect(p, "Bash", "cat b.luau | tee -a app.luau >/dev/null") == "app.luau"
    assert _detect(p, "Bash", "sed -i 's/a/b/' fix.luau") == "fix.luau"
    assert _detect(p, "shell_command", "Set-Content -Path 'ok.luau' -Value x") == "ok.luau"
    assert _detect(
        p, "shell_command", '[System.IO.File]::WriteAllText("C:/w/n.luau", "x")'
    ) == "C:/w/n.luau"


def test_detection_codex_apply_patch(plugin=None) -> None:
    """codex's native edit format must trigger the hook."""
    p = plugin or _load_plugin()
    cmd = (
        "apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: src/game.luau\n"
        "@@\n-local a=1\n+local a=2\n*** End Patch\nPATCH"
    )
    assert _detect(p, "shell_command", cmd) == "src/game.luau"


def test_detection_python_writes(plugin=None) -> None:
    p = plugin or _load_plugin()
    assert _detect(p, "shell_command", "python -c \"open('a.luau','w').write('x')\"") == "a.luau"
    assert _detect(
        p, "Bash", "python3 -c \"from pathlib import Path; Path('b.luau').write_text('x')\""
    ) == "b.luau"


def test_non_writes_stay_silent(plugin=None) -> None:
    p = plugin or _load_plugin()
    assert _detect(p, "Bash", "grep foo bar.luau") is None
    assert _detect(p, "Bash", "lua run.luau 2> err.log") is None
    assert _detect(p, "shell_command", "python -c \"open('r.luau','r').read()\"") is None
    assert _detect(p, "Bash", "strings /usr/bin/tee | grep x.luau") is None


# ---------------------------------------------------------------------------
# Behavioral parity: strict mode (--warnings)
# ---------------------------------------------------------------------------

def _run_cli_with_fake_check(plugin, summary: dict, argv: list[str]) -> int:
    calls: list[list[str]] = []

    def fake_check_paths(paths, cwd="."):
        calls.append(list(paths))
        return {"diagnostics": [], "summary": summary}

    original = plugin.check_paths
    plugin.check_paths = fake_check_paths
    try:
        rc = plugin.run_cli(argv)
    finally:
        plugin.check_paths = original
    assert calls == [["x.luau"]], f"expected exactly one target, got {calls}"
    return rc


def test_strict_mode_semantics() -> None:
    p = _load_plugin()
    # errors always fail
    assert _run_cli_with_fake_check(p, {"errors": 1, "warnings": 0, "total": 1}, ["x.luau"]) == 1
    # warnings pass by default...
    assert _run_cli_with_fake_check(p, {"errors": 0, "warnings": 2, "total": 2}, ["x.luau"]) == 0
    # ...and fail under --warnings, wherever the flag appears
    assert _run_cli_with_fake_check(p, {"errors": 0, "warnings": 2, "total": 2}, ["--warnings", "x.luau"]) == 1
    assert _run_cli_with_fake_check(p, {"errors": 0, "warnings": 2, "total": 2}, ["x.luau", "--warnings"]) == 1
    # clean stays clean either way
    assert _run_cli_with_fake_check(p, {"errors": 0, "warnings": 0, "total": 0}, ["--warnings", "x.luau"]) == 0
