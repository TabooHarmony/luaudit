"""Tests for runners: config discovery, check_files with mocked binaries."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from luaudit import bootstrap, runners


@pytest.fixture
def fake_toolchain(tmp_path: Path, monkeypatch):
    """Install fake luau-lsp/selene/stylua that emit scripted output."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    defs = tmp_path / "defs" / "globalTypes.d.luau"
    defs.parent.mkdir(parents=True)
    defs.write_text("declare class game {}\n")

    def write_fake(name: str, script: str) -> None:
        p = bin_dir / name
        p.write_text(script, encoding="utf-8")
        p.chmod(0o755)

    # luau-lsp: exit 1 with a type error diagnostic
    write_fake("luau-lsp", "#!/bin/sh\nprintf 'fake.luau:3:5-10: (W0) TypeError: fake error\\n'\nexit 1\n")
    # selene: JSON warning
    write_fake("selene", "#!/bin/sh\nprintf '%s\\n' '{\"severity\":\"Warning\",\"code\":\"w\",\"message\":\"lint me\",\"primary_label\":{\"filename\":\"fake.luau\",\"span\":{\"start_line\":0,\"start_column\":0,\"end_line\":0,\"end_column\":5}}}'\nexit 0\n")
    # stylua: clean (exit 0)
    write_fake("stylua", "#!/bin/sh\nexit 0\n")

    monkeypatch.setattr(bootstrap, "BIN_DIR", bin_dir)
    monkeypatch.setattr(bootstrap, "DEFS_DIR", tmp_path / "defs")
    monkeypatch.setattr(bootstrap, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(bootstrap, "_ready", True)
    monkeypatch.setattr(bootstrap, "DEFS_FILENAME", "globalTypes.d.luau")

    return {"bin": bin_dir, "defs": defs}


def test_find_config_walks_up(tmp_path: Path):
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    (tmp_path / "selene.toml").write_text("x")
    found = runners._find_config(str(sub), ("selene.toml",))
    assert found == str(tmp_path / "selene.toml")


def test_find_config_none(tmp_path: Path):
    assert runners._find_config(str(tmp_path), ("selene.toml",)) is None


def test_check_files_returns_diagnostics(fake_toolchain, tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "game.luau").write_text("local x: number = 'bad'\n")
    result = runners.check_files([str(src)], cwd=str(tmp_path))
    assert result["summary"]["total"] >= 1
    assert result["diagnostics"][0]["source"] in ("luau-lsp", "selene")


def test_check_files_empty_dir_clean(tmp_path: Path):
    result = runners.check_files([str(tmp_path)], cwd=str(tmp_path))
    assert result["summary"]["total"] == 0
    assert "note" in result
