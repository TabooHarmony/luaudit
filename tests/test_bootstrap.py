"""Tests for bootstrap: config writes, platform detection, init_configs."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from luaudit import bootstrap, cli


def test_init_configs_writes_once(tmp_path: Path):
    wrote = bootstrap.init_configs(tmp_path)
    assert sorted(wrote) == [".luaurc", "selene.toml"]
    assert (tmp_path / "selene.toml").read_text() == bootstrap.SELENE_TOML
    assert (tmp_path / ".luaurc").read_text().strip() == '{\n  "languageMode": "strict"\n}'

    # second call writes nothing
    assert bootstrap.init_configs(tmp_path) == []


def test_init_configs_idempotent_content(tmp_path: Path):
    (tmp_path / "selene.toml").write_text('std = "roblox"\n')
    wrote = bootstrap.init_configs(tmp_path)
    # pre-existing file untouched; only missing .luaurc is created
    assert wrote == [".luaurc"]
    assert (tmp_path / "selene.toml").read_text() == 'std = "roblox"\n'


def test_cli_init_writes_configs(tmp_path: Path, monkeypatch, capsys):
    """Regression: `luaudit init` used to crash with AttributeError because
    the subparser never declared its `dir` argument. This pins the real CLI
    path (parser -> handler), not just bootstrap.init_configs."""
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["init"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "wrote configs" in out
    assert (tmp_path / "selene.toml").exists()
    assert (tmp_path / ".luaurc").exists()

    # second run reports already-present and still exits 0
    rc = cli.main(["init"])
    assert rc == 0
    assert "already present" in capsys.readouterr().out


def test_init_never_downloads_toolchain(tmp_path: Path, monkeypatch):
    """init only writes config files; it must not pull ~40MB of binaries."""
    def boom():
        raise AssertionError("init must not call ensure_tools()")
    monkeypatch.setattr(bootstrap, "ensure_tools", boom)
    rc = cli.main(["init", str(tmp_path / "proj")])
    assert rc == 0
    assert (tmp_path / "proj" / "selene.toml").exists()


def test_zip_extraction_rejects_slip_members(tmp_path: Path, monkeypatch):
    """The package bootstrapper's zip-slip guard mirrors the plugin engine."""
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escapes.luau", "x")

    def fake_download(url, dest, timeout=60):
        Path(dest).write_bytes(evil.read_bytes())

    monkeypatch.setattr(bootstrap, "_download", fake_download)
    monkeypatch.setitem(
        bootstrap.SHA256_PINS,
        "https://evil.test/x.zip",
        hashlib.sha256(evil.read_bytes()).hexdigest(),
    )
    try:
        bootstrap._download_and_extract_zip("https://evil.test/x.zip", tmp_path / "out")
    except ValueError as e:
        assert "unsafe zip member" in str(e)
    else:
        raise AssertionError("zip-slip member was extracted without complaint")
    assert not (tmp_path / "escapes.luau").exists()


def test_has_selene_false_on_linux_arm64(monkeypatch, tmp_path: Path):
    """No native selene arm64 build exists; has_selene must say so rather
    than let an x86_64 binary produce 'Exec format error' per file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / bootstrap._exe("selene")).write_text("#!/bin/sh\n")
    monkeypatch.setattr(bootstrap, "BIN_DIR", bin_dir)

    monkeypatch.setattr(bootstrap, "_get_platform", lambda: ("linux", "arm64"))
    assert bootstrap.has_selene() is False

    monkeypatch.setattr(bootstrap, "_get_platform", lambda: ("linux", "x86_64"))
    assert bootstrap.has_selene() is True


def test_platform_shape():
    os_name, arch = bootstrap._get_platform()
    assert os_name in ("windows", "darwin", "linux")
    assert arch in ("x86_64", "arm64")


def test_urls_shape():
    urls = bootstrap._get_urls()
    assert set(urls) == {"luau-lsp", "selene", "stylua"}
    for u in urls.values():
        assert u.startswith("https://")
