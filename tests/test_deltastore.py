"""Delta store: new-vs-repeat classification, hygiene, and fail-open behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from luaudit.deltastore import (
    STATE_MAX_FILES,
    STATE_MAX_FINDINGS,
    DeltaStore,
    fingerprint,
    normalize_key,
)


def _diag(code="TypeError", message="x is not a number", line=4):
    return {"file": "a.luau", "line": line, "column": 1, "severity": "error",
            "code": code, "message": message, "source": "luau-lsp"}


@pytest.fixture()
def tmp_file(tmp_path):
    f = tmp_path / "a.luau"
    f.write_text("local x = 1\n")
    return str(f)


def _store(tmp_path):
    return DeltaStore(tmp_path / "state")


# -- classification ---------------------------------------------------------

def test_first_sighting_is_new(tmp_path, tmp_file):
    st = _store(tmp_path)
    r = st.classify(tmp_file, [_diag()])
    assert len(r["new"]) == 1
    assert r["repeat_count"] == 0


def test_same_content_same_finding_is_repeat(tmp_path, tmp_file):
    st = _store(tmp_path)
    st.classify(tmp_file, [_diag()])
    r = st.classify(tmp_file, [_diag()])
    assert r["new"] == []
    assert r["repeat_count"] == 1


def test_same_fingerprint_survives_unrelated_edit_as_repeat(tmp_path):
    """The core anti-noise rule: a persistent warning is NOT re-injected
    just because some other part of the file changed."""
    st = _store(tmp_path)
    f = tmp_path / "a.luau"
    f.write_text("local x = 1\nreturn x\n")
    st.classify(str(f), [_diag()])
    f.write_text("local x = 2\n-- touched\nreturn x\n")
    r = st.classify(str(f), [_diag()])
    assert r["new"] == []
    assert r["repeat_count"] == 1


def test_fixed_then_regressed_is_new_again(tmp_path):
    """Finding disappears with a fix, reappears later => must inject again."""
    st = _store(tmp_path)
    f = tmp_path / "a.luau"
    f.write_text("bad\n")
    st.classify(str(f), [_diag()])
    f.write_text("good\n")
    st.classify(str(f), [])          # fixed: no diagnostics
    f.write_text("bad\n")            # regressed
    r = st.classify(str(f), [_diag()])
    assert len(r["new"]) == 1


def test_two_warnings_one_repeat_counts_individually(tmp_path, tmp_file):
    st = _store(tmp_path)
    st.classify(tmp_file, [_diag(), _diag(code="UnusedVariable")])
    r = st.classify(tmp_file, [_diag(), _diag(code="UnusedVariable"), _diag(code="NewLint")])
    assert [d["code"] for d in r["new"]] == ["NewLint"]
    assert r["repeat_count"] == 2


def test_fingerprint_ignores_position():
    assert fingerprint(_diag(line=4)) == fingerprint(_diag(line=40))
    assert fingerprint(_diag()) != fingerprint(_diag(message="different"))
    # whitespace inside messages normalizes
    assert fingerprint(_diag(message="x   is\nnot a number")) == fingerprint(_diag())


def test_unreadable_file_fails_open_as_new(tmp_path):
    st = _store(tmp_path)
    missing = str(tmp_path / "gone.luau")
    r = st.classify(missing, [_diag()])
    assert len(r["new"]) == 1


# -- fail open ---------------------------------------------------------------

def test_corrupt_state_file_fails_open(tmp_path, tmp_file):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "delta.json").write_text("{not json at all")
    st = DeltaStore(state_dir)
    r = st.classify(tmp_file, [_diag()])
    assert len(r["new"]) == 1


def test_wrong_schema_fails_open(tmp_path, tmp_file):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "delta.json").write_text(json.dumps({"schema": 99, "files": {}}))
    st = DeltaStore(state_dir)
    assert len(st.classify(tmp_file, [_diag()])["new"]) == 1


def test_readonly_state_dir_never_raises(tmp_path, tmp_file):
    ro = tmp_path / "ro"
    ro.mkdir()
    (ro / "delta.json").write_text("{}")
    import os, stat
    os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)  # read-only dir
    try:
        st = DeltaStore(ro)
        r = st.classify(tmp_file, [_diag()])  # save will fail silently
        assert len(r["new"]) == 1  # still reports everything new, no crash
        assert st.surface_counts(str(tmp_path / "never-seen.luau")) == {}
    finally:
        os.chmod(ro, stat.S_IRWXU)


# -- hygiene ------------------------------------------------------------------

def test_stale_entries_pruned_after_max_age(tmp_path):
    st = _store(tmp_path)
    old = 1_000_000_000.0
    fa = tmp_path / "a.luau"
    fb = tmp_path / "b.luau"
    fa.write_text("x\n")
    fb.write_text("y\n")
    st.classify(str(fa), [_diag()], now=old)
    # Touching only b two weeks later must evict untouched a.
    st.classify(str(fb), [_diag()], now=old + 15 * 86400)
    data = json.loads(st.path.read_text())
    assert normalize_key(str(fa)) not in data["files"]
    assert normalize_key(str(fb)) in data["files"]


def test_file_cap_evicts_least_recently_seen(tmp_path):
    st = _store(tmp_path)
    for i in range(STATE_MAX_FILES + 5):
        f = tmp_path / f"f{i}.luau"
        f.write_text("x\n")
        st.classify(str(f), [_diag()], now=float(i))
    data = json.loads(st.path.read_text())
    assert len(data["files"]) == STATE_MAX_FILES
    assert normalize_key(str(tmp_path / "f4.luau")) not in data["files"]      # oldest evicted
    assert normalize_key(str(tmp_path / f"f{STATE_MAX_FILES + 4}.luau")) in data["files"]


def test_findings_cap_per_file(tmp_path):
    st = _store(tmp_path)
    f = tmp_path / "big.luau"
    f.write_text("x\n")
    diags = [_diag(code=f"Code{i}", message=f"m{i}") for i in range(STATE_MAX_FINDINGS + 10)]
    r = st.classify(str(f), diags)
    assert len(r["new"]) == len(diags)          # reporting never truncated
    data = json.loads(st.path.read_text())
    entry = next(iter(data["files"].values()))
    assert len(entry["findings"]) <= STATE_MAX_FINDINGS
    # The cap evicted the OLDEST 10 fingerprints (Code0..9). On a second
    # identical pass those return as new; the surviving 64 repeat.
    r2 = st.classify(str(f), diags)
    assert r2["repeat_count"] == STATE_MAX_FINDINGS
    assert [d["code"] for d in r2["new"]] == [f"Code{i}" for i in range(10)]


# -- mute lifecycle -------------------------------------------------------------

def test_muted_fingerprints_are_suppressed_not_dropped_from_report(tmp_path, tmp_file):
    st = _store(tmp_path)
    fp = fingerprint(_diag())
    st.mute(fp, sample_message="unused variable")
    r = st.classify(tmp_file, [_diag()])
    assert r["new"] == [] and r["repeat_count"] == 0
    assert r["suppressed"] == 1


def test_unmute_restores(tmp_path, tmp_file):
    st = _store(tmp_path)
    fp = fingerprint(_diag())
    st.mute(fp)
    assert st.unmute(fp) == 1
    assert len(st.classify(tmp_file, [_diag()])["new"]) == 1


def test_unmute_all_and_noop(tmp_path, tmp_file):
    st = _store(tmp_path)
    st.mute("a|b")
    st.mute("c|d")
    assert st.unmute() == 2
    assert st.unmute() == 0
    assert st.unmute("missing|key") == 0


# -- dirty markers -----------------------------------------------------------

def test_dirty_roundtrip(tmp_path):
    st = _store(tmp_path)
    root = "/proj"
    assert st.pop_dirty(root) is False
    st.mark_dirty(root)
    assert st.pop_dirty(root) is True
    assert st.pop_dirty(root) is False


def test_dirty_survives_reopen(tmp_path):
    root = str(tmp_path / "proj")
    DeltaStore(tmp_path / "s1").mark_dirty(root)
    assert DeltaStore(tmp_path / "s1").pop_dirty(root) is True


# -- surface counts ------------------------------------------------------------

def test_surface_counts_track_consecutive_surfaces(tmp_path, tmp_file):
    st = _store(tmp_path)
    st.classify(tmp_file, [_diag()])
    st.classify(tmp_file, [_diag()])
    st.classify(tmp_file, [_diag()])
    counts = st.surface_counts(tmp_file)
    fp = fingerprint(_diag())
    assert counts.get(fp) == 3
