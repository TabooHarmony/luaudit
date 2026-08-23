"""End-to-end hook policy tests: drive run_hook through its real path with a
faked toolchain, asserting the v1.1 anti-noise behavior:

- errors inject inline every time
- new warnings inject once with detail
- repeat warnings collapse to one count line
- clean passes go silent AND forget stale fingerprints
- fix -> regress reads as new again
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_ENGINE = REPO / "plugins" / "luaudit" / "scripts" / "luaudit_hook.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("luaudit_plugin_engine", PLUGIN_ENGINE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def p(tmp_path, monkeypatch):
    """Fresh engine instance with isolated state and cwd."""
    monkeypatch.setenv("LUAUDIT_HOME", str(tmp_path / "home"))
    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return _load_plugin()


def _diag(p, code="UnusedVariable", severity="warning", message="x is never used", line=3):
    return {"file": str(p), "line": line, "column": 1, "severity": severity,
            "code": code, "message": message, "source": "selene"}


def _fake_check(p, diags):
    def fake(paths, cwd="."):
        return {"diagnostics": list(diags),
                "summary": {"errors": sum(1 for d in diags if d["severity"] == "error"),
                            "warnings": sum(1 for d in diags if d["severity"] == "warning"),
                            "total": len(diags)}}
    p.check_paths = fake


def _run_edit_event(p, filepath):
    """Feed a Claude-style Edit event through real stdin handling."""
    event = {"tool_name": "Edit", "tool_input": {"file_path": filepath}}
    p.__dict__.setdefault("_orig_stdin", sys.stdin)
    sys.stdin = io.StringIO(json.dumps(event))
    try:
        rc = p.run_hook()
    finally:
        sys.stdin = p.__dict__["_orig_stdin"]
    return rc


def _capture_stdout(p, fn):
    buf = io.StringIO()
    orig = sys.stdout
    sys.stdout = buf
    try:
        rc = fn()
    finally:
        sys.stdout = orig
    out = buf.getvalue().strip()
    parsed = json.loads(out) if out else None
    return rc, parsed


def _luau_file(p, name="a.luau"):
    f = Path(p.os.getcwd()) / name
    f.write_text("local x = 1\n")
    return str(f)


# -- behavior ------------------------------------------------------------------

def test_error_injects_inline_every_time(p):
    f = _luau_file(p)
    for i in range(3):  # same error three edits in a row
        _fake_check(p, [_diag(f, code="TypeError", severity="error")])
        rc, payload = _capture_stdout(p, lambda: _run_edit_event(p, f))
        assert rc == 0 and payload is not None
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        assert "TypeError" in ctx and "[ERROR]" in ctx
        assert "still present" not in ctx  # errors are never collapsed


def test_new_warning_injects_once_then_collapses(p):
    f = _luau_file(p)
    _fake_check(p, [_diag(f)])
    _, payload = _capture_stdout(p, lambda: _run_edit_event(p, f))
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "UnusedVariable" in ctx          # first sighting: full detail
    assert "still present" not in ctx

    _fake_check(p, [_diag(f)])
    _, payload = _capture_stdout(p, lambda: _run_edit_event(p, f))
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "UnusedVariable" not in ctx      # no re-injection...
    assert "1 previously reported warning(s) still present" in ctx  # ...one count line
    assert "luaudit check" in ctx           # escape hatch is advertised


def test_warning_plus_error_still_injects_the_error_when_warning_repeats(p):
    f = _luau_file(p)
    _fake_check(p, [_diag(f)])
    _capture_stdout(p, lambda: _run_edit_event(p, f))  # warning seen once
    # next edit: same warning + a NEW error
    _fake_check(p, [_diag(f), _diag(f, code="TypeError", severity="error", message="bad types", line=9)])
    _, payload = _capture_stdout(p, lambda: _run_edit_event(p, f))
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "TypeError" in ctx                                   # error inline
    assert "1 previously reported warning(s) still present" in ctx  # repeat collapsed


def test_clean_pass_silent_and_forgets(p):
    f = _luau_file(p)
    _fake_check(p, [_diag(f)])
    _capture_stdout(p, lambda: _run_edit_event(p, f))
    _fake_check(p, [])  # fixed
    rc, payload = _capture_stdout(p, lambda: _run_edit_event(p, f))
    assert rc == 0 and payload is None      # silent on clean
    _fake_check(p, [_diag(f)])  # regressed
    _, payload = _capture_stdout(p, lambda: _run_edit_event(p, f))
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "UnusedVariable" in ctx          # regression reads as NEW again


def test_changed_line_same_warning_is_a_repeat(p):
    """Line shifts must not make an old warning look new."""
    f = _luau_file(p)
    _fake_check(p, [_diag(f, line=3)])
    _capture_stdout(p, lambda: _run_edit_event(p, f))
    _fake_check(p, [_diag(f, line=42)])     # agent edited elsewhere; warning shifted
    _, payload = _capture_stdout(p, lambda: _run_edit_event(p, f))
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "still present" in ctx


def test_dirty_marker_set_even_on_clean_pass(p):
    f = _luau_file(p)
    _fake_check(p, [])
    _capture_stdout(p, lambda: _run_edit_event(p, f))
    store = p.DeltaStore()
    assert store.pop_dirty(str(Path(f).parent)) is True


def test_non_luau_and_missing_files_never_touch_state(p):
    rc, payload = _capture_stdout(p, lambda: _run_edit_event(p, "readme.md"))
    assert rc == 0 and payload is None
    rc, payload = _capture_stdout(p, lambda: _run_edit_event(p, "nope.luau"))
    assert rc == 0 and payload is None
    store = p.DeltaStore()
    assert store.pop_dirty_all() == []


def test_two_warnings_repeat_together_in_one_count_line(p):
    f = _luau_file(p)
    _fake_check(p, [_diag(f, code="A"), _diag(f, code="B")])
    _capture_stdout(p, lambda: _run_edit_event(p, f))
    _fake_check(p, [_diag(f, code="A"), _diag(f, code="B"), _diag(f, code="C")])
    _, payload = _capture_stdout(p, lambda: _run_edit_event(p, f))
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "C " in ctx or "C:" in ctx                       # new one detailed
    assert "2 previously reported warning(s) still present" in ctx
