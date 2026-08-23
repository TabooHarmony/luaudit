"""Persistent delta store: remembers what the agent has already been shown.

The per-edit hook used to re-inject identical diagnostics every turn, which
is how luaudit earned its "noise" reputation. This module splits each check
result into NEW findings (worth interrupting for) and REPEATS (collapse to
a count).

Identity rules (deliberate, see the v1.1 noise debate):
- Zero judgment: no opinion about which warnings matter. A finding is a
  repeat when its fingerprint (code + normalized message) was surfaced
  before, regardless of line shifts or unrelated edits elsewhere in the
  file. This is what stops the "persistent warning re-injected on every
  save" spam.
- A fingerprint ABSENT from a pass is forgotten (fixed or rewritten away),
  so fix -> regress reads as new again and gets injected.
- Fail open: any error reading/writing state means everything counts as
  new. The hook must never crash or lose diagnostics over bookkeeping.
- Hygiene built in: file entries expire after STATE_MAX_AGE_DAYS unused,
  the file table caps at STATE_MAX_FILES entries, and per-file
  fingerprints cap at STATE_MAX_FINDINGS. State lives under
  ~/.luaudit/state/ (respects LUAUDIT_HOME).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

DELTA_SCHEMA = 1
STATE_MAX_FILES = 256
STATE_MAX_AGE_DAYS = 14
STATE_MAX_FINDINGS = 64

_WS_RE = re.compile(r"\s+")


def default_state_dir() -> Path:
    home = Path(os.environ.get("LUAUDIT_HOME", str(Path.home() / ".luaudit")))
    return home / "state"


def normalize_key(filepath: str) -> str:
    """Stable per-OS key so Windows drive-casing variants dedupe."""
    try:
        return os.path.normcase(os.path.abspath(filepath))
    except (TypeError, ValueError):
        return str(filepath)


def fingerprint(diag: dict[str, Any]) -> str:
    """Stable identity of a diagnostic across line shifts and edits."""
    code = str(diag.get("code", ""))
    message = _WS_RE.sub(" ", str(diag.get("message", ""))).strip()
    return f"{code}|{message}"


def _prune(data: dict, now: float) -> None:
    """Enforce age, file-count, and findings-count caps. Mutates data."""
    cutoff = now - STATE_MAX_AGE_DAYS * 86400
    files = data.get("files", {})
    for key in list(files.keys()):
        entry = files[key]
        if float(entry.get("last_seen", 0)) < cutoff:
            del files[key]
    # LRU cap: drop least-recently-seen files beyond the cap.
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
    """JSON-backed store classifying diagnostics into new vs repeat."""

    def __init__(self, state_dir: str | Path | None = None):
        self.state_dir = Path(state_dir) if state_dir else default_state_dir()
        self.path = self.state_dir / "delta.json"

    # -- storage ------------------------------------------------------------

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
            pass  # fail open: losing state degrades to "everything is new"

    @staticmethod
    def _empty_file_entry() -> dict:
        return {"hash": None, "findings": {}, "last_seen": 0.0}

    # -- core API -----------------------------------------------------------

    def classify(
        self, filepath: str, diagnostics: list[dict], now: float | None = None
    ) -> dict:
        """Split diagnostics into new vs repeats for one file.

        Returns {"new": [diag...], "repeats": [diag...], "repeat_count": int,
        "suppressed": int}. "suppressed" counts diagnostics whose fingerprint
        is muted (Phase 4 consumes this; until then it is always 0).
        Never raises.
        """
        now = time.time() if now is None else now
        result: dict = {"new": [], "repeats": [], "repeat_count": 0, "suppressed": 0}
        try:
            data = self._load()
            key = normalize_key(filepath)
            entry = data["files"].get(key) or self._empty_file_entry()
            findings: dict = entry.get("findings", {})
            muted: dict = data.get("muted", {})

            seen_fps: set[str] = set()
            for diag in diagnostics:
                fp = fingerprint(diag)
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

            # Forget fingerprints that vanished this pass: fixed or rewritten
            # away. Their next appearance reads as new again.
            for gone in [fp for fp in findings if fp not in seen_fps]:
                del findings[gone]

            entry["findings"] = findings
            entry.pop("hash", None)
            entry["last_seen"] = now
            data["files"][key] = entry
            _prune(data, now)
            self._save(data)
        except Exception:
            # Absolute fail-open guarantee: bookkeeping must never break the
            # hook. If anything above misbehaves, report everything as new.
            result = {
                "new": list(diagnostics),
                "repeat_count": 0,
                "suppressed": 0,
            }
        return result

    def surface_counts(self, filepath: str) -> dict[str, int]:
        """Consecutive-surface counters per fingerprint, for mute policy."""
        try:
            data = self._load()
            entry = data["files"].get(normalize_key(filepath))
            if not entry:
                return {}
            return {
                fp: int(rec.get("n", 0)) for fp, rec in entry.get("findings", {}).items()
            }
        except Exception:
            return {}

    # -- muted fingerprints (Phase 4 consumes; stored here for hygiene) -----

    def muted(self) -> dict:
        try:
            return dict(self._load().get("muted", {}))
        except Exception:
            return {}

    def mute(self, fp: str, sample_message: str = "", now: float | None = None) -> bool:
        """Record a muted fingerprint. Returns True only on FIRST insert so
        callers can announce the mute exactly once."""
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

    def unmute(self, fp: str | None = None) -> int:
        """Remove one or all muted fingerprints. Returns how many removed.

        Also resets the removed fingerprints' surface counters so a restored
        warning starts counting from zero instead of insta-re-muting."""
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

    # -- session-dirty markers (turn-end sweep consumes in Phase 3) ---------

    def mark_dirty(self, root: str, now: float | None = None) -> None:
        try:
            now = time.time() if now is None else now
            data = self._load()
            data.setdefault("dirty", {})[normalize_key(root)] = now
            self._save(data)
        except Exception:
            pass

    def pop_dirty(self, root: str) -> bool:
        """True when the root saw an edit since the last pop. Clears it."""
        try:
            data = self._load()
            key = normalize_key(root)
            dirty = data.get("dirty", {})
            was = key in dirty
            dirty.pop(key, None)
            self._save(data)
            return was
        except Exception:
            return False

    def pop_dirty_all(self) -> list[str]:
        """Return and clear every dirty root. Used by the turn-end sweep."""
        try:
            data = self._load()
            dirty = data.get("dirty", {})
            data["dirty"] = {}
            self._save(data)
            return list(dirty.keys())
        except Exception:
            return []


def new_store(state_dir: str | Path | None = None) -> DeltaStore:
    return DeltaStore(state_dir)
