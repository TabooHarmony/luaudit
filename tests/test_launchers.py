"""Launchers must stay byte-identical after the shebang line, and both must
forward hook arguments (stop-hook dispatch) to the engine."""

from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "luaudit"


def _body(p: Path) -> str:
    text = p.read_text(encoding="utf-8")
    return "\n".join(text.splitlines()[1:]) + "\n"


def test_posix_launchers_are_identical():
    a = _body(PLUGIN_DIR / "scripts" / "luaudit-hook")
    b = _body(PLUGIN_DIR / "scripts" / "luaudit-hook.sh")
    assert a == b, "luaudit-hook and luaudit-hook.sh drifted; hooks.json uses luaudit-hook"


def test_launchers_forward_arguments():
    for name in ("luaudit-hook", "luaudit-hook.sh"):
        body = _body(PLUGIN_DIR / "scripts" / name)
        assert '"$@"' in body, f"{name} does not forward args; stop-hook dispatch would break"


def test_cmd_launcher_forwards_arguments():
    src = (PLUGIN_DIR / "scripts" / "luaudit-hook.cmd").read_text(encoding="utf-8")
    assert "%*" in src, "cmd launcher does not forward args; stop-hook dispatch would break"
