"""Turn-end sweep tests: cross-file detection at Stop, strictly informational,
delta-aware, budget-capped, and incapable of raising."""

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
    monkeypatch.setenv("LUAUDIT_HOME", str(tmp_path / "home"))
    workdir = tmp_path / "proj"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return _load_plugin()


def _fake_check(p, by_file):
    """by_file: {abs_path: [diag dicts]} (matched case-insensitively; Windows
    drive casing may differ between getcwd() and walked paths)."""
    norm = lambda s: __import__("os").path.normcase(__import__("os").path.abspath(s))
    table = {norm(k): v for k, v in by_file.items()}

    def fake(paths, cwd="."):
        diags = []
        for t in paths:
            diags.extend(table.get(norm(t), []))
        return {"diagnostics": diags,
                "summary": {"errors": sum(1 for d in diags if d["severity"] == "error"),
                            "warnings": sum(1 for d in diags if d["severity"] == "warning"),
                            "total": len(diags)}}
    p.check_paths = fake


def _diag(path, code="TypeError", severity="error", message="x is not a number", line=2):
    return {"file": path, "line": line, "column": 1, "severity": severity,
            "code": code, "message": message, "source": "luau-lsp"}


def _run_sweep(p):
    buf = io.StringIO()
    orig = sys.stdout
    sys.stdout = buf
    try:
        rc = p.run_stop_hook()
    finally:
        sys.stdout = orig
    out = buf.getvalue().strip()
    return rc, (json.loads(out) if out else None)


# -- behavior ------------------------------------------------------------------

def test_no_dirty_roots_is_silent(p):
    rc, payload = _run_sweep(p)
    assert rc == 0 and payload is None


def test_reports_error_in_untouched_file(p):
    a = str(Path(p.os.getcwd()) / "a.luau")
    b = str(Path(p.os.getcwd()) / "b.luau")
    for f in (a, b):
        Path(f).write_text("local x = 1\n")
    # The agent edits b; a gets broken out-of-band (e.g. signature change).
    _fake_check(p, {a: [_diag(a)], b: []})
    p.DeltaStore().mark_dirty(p.os.getcwd())
    rc, payload = _run_sweep(p)
    assert rc == 0 and payload is not None
    assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "TypeError" in ctx and a in ctx
    assert "file(s) checked" in ctx


def test_repeat_warnings_collapse_to_quiet_line(p):
    a = str(Path(p.os.getcwd()) / "a.luau")
    Path(a).write_text("local x = 1\n")
    warn = _diag(a, code="UnusedVariable", severity="warning")
    _fake_check(p, {a: [warn]})
    st = p.DeltaStore()
    st.mark_dirty(p.os.getcwd())
    rc, payload = _run_sweep(p)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "new warnings" in ctx and "UnusedVariable" in ctx

    # Second turn, same warning still there: quiet count only.
    st.mark_dirty(p.os.getcwd())
    rc, payload = _run_sweep(p)
    ctx = payload["hookSpecificOutput"]["adaptive" if False else "additionalContext"]
    assert "sweep clean" in ctx
    assert "1 known warning(s) unchanged" in ctx
    assert "UnusedVariable" not in ctx


def test_budget_exhaustion_stays_silent(p):
    a = str(Path(p.os.getcwd()) / "a.luau")
    Path(a).write_text("local x = 1\n")
    _fake_check(p, {a: [_diag(a)]})
    p.SWEEP_BUDGET_SECONDS = -1.0  # budget already spent
    p.DeltaStore().mark_dirty(p.os.getcwd())
    rc, payload = _run_sweep(p)
    assert rc == 0 and payload is None  # partial info beats wrong info: silence


def test_never_raises_even_if_store_explodes(p):
    class Boom:
        def pop_dirty_all(self):
            raise RuntimeError("disk on fire")
    p.DeltaStore = Boom
    rc, payload = _run_sweep(p)
    assert rc == 0 and payload is None


def test_cli_dispatch_stop_hook(p):
    rc = p.main(["stop-hook"])
    assert rc == 0


def test_missing_dirty_root_skipped(p):
    st = p.DeltaStore()
    st.mark_dirty(str(Path(p.os.getcwd()) / "deleted-folder"))
    rc, payload = _run_sweep(p)
    assert rc == 0 and payload is None
