"""Tests for bootstrap: config writes, platform detection, init_configs."""

from __future__ import annotations

import hashlib
import os
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


# ---------------------------------------------------------------------------
# format_files: honest reporting (regression: the demo run's "nothing to
# format" while the file was genuinely unformatted -- the agent's shell ate
# the backslashes out of the Windows path and the old code skipped silently).
# ---------------------------------------------------------------------------

class _FakeStylua:
    """Stand-in for subprocess.run on the stylua binary."""

    def __init__(self, mode: str):
        self.mode = mode  # "rewrite" | "noop" | "fail"
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        target = cmd[-1]

        class R:
            returncode = 0
            stderr = "stylua blew up"

        if self.mode == "fail":
            R.returncode = 1
        elif self.mode == "rewrite":
            with open(target, "wb") as fh:
                fh.write(b"local x = 1\n")
        return R()


def _fmt_setup(tmp_path, monkeypatch, mode: str) -> _FakeStylua:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / bootstrap._exe("stylua")).write_text("#!/bin/sh\n")
    monkeypatch.setattr(bootstrap, "BIN_DIR", bin_dir)
    fake = _FakeStylua(mode)
    fake_mod = type("sub", (), {"run": fake, "SubprocessError": Exception})
    monkeypatch.setattr(bootstrap, "subprocess", fake_mod)
    return fake


def test_format_files_reports_missing_not_skips(tmp_path, monkeypatch):
    fake = _fmt_setup(tmp_path, monkeypatch, "noop")
    res = bootstrap.format_files(["C:UsersAdmindemoutils.luau"], cwd=str(tmp_path))
    # The caller's own mangled token comes back verbatim...
    assert res["missing"] == ["C:UsersAdmindemoutils.luau"]
    # ...and nothing ran.
    assert fake.calls == []


def test_format_files_changed_vs_clean(tmp_path, monkeypatch):
    messy = tmp_path / "messy.luau"
    messy.write_bytes(b"local   x=1\n")
    tidy = tmp_path / "tidy.luau"
    tidy.write_bytes(b"local x = 1\n")

    fake = _fmt_setup(tmp_path, monkeypatch, "rewrite")
    res = bootstrap.format_files([str(messy)], cwd=str(tmp_path))
    assert res["changed"] == [str(messy)] and not res["clean"] and not res["missing"]

    fake = _fmt_setup(tmp_path, monkeypatch, "noop")
    res = bootstrap.format_files([str(tidy)], cwd=str(tmp_path))
    assert res["clean"] == [str(tidy)] and not res["changed"]


def test_format_files_walks_directories(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    (pkg / "inner").mkdir(parents=True)
    (pkg / "a.luau").write_bytes(b"x = 1\n")
    (pkg / "inner" / "b.lua").write_bytes(b"y = 2\n")
    (pkg / "ignore.txt").write_text("nope")
    fake = _fmt_setup(tmp_path, monkeypatch, "noop")
    res = bootstrap.format_files([str(pkg)], cwd=str(tmp_path))
    assert sorted(os.path.basename(c[-1]) for c in fake.calls) == ["a.luau", "b.lua"]
    assert not res["missing"]


def test_format_files_stylua_failure_is_visible(tmp_path, monkeypatch):
    f = tmp_path / "x.luau"
    f.write_bytes(b"local x = 1\n")
    _fmt_setup(tmp_path, monkeypatch, "fail")
    res = bootstrap.format_files([str(f)], cwd=str(tmp_path))
    assert res["failed"] == [str(f)]
    assert not res["changed"] and not res["clean"]


def test_cli_format_missing_path_exits_2(tmp_path, monkeypatch, capsys):
    """The demo failure end-to-end: a path the shell mangled must NOT be
    reported as 'nothing to format' with exit 0."""
    monkeypatch.setattr(bootstrap, "ensure_tools", lambda: None)
    monkeypatch.setattr(bootstrap, "has_stylua", lambda: True)
    _fmt_setup(tmp_path, monkeypatch, "noop")
    rc = cli.main(["format", "C:UsersAdmindemoutils.luau", "--cwd", str(tmp_path)])
    cap = capsys.readouterr()
    assert rc == 2
    assert "no such file or directory" in cap.err
    assert "nothing to format" not in cap.out + cap.err


def test_cli_format_all_clean_is_explicit(tmp_path, monkeypatch, capsys):
    tidy = tmp_path / "tidy.luau"
    tidy.write_bytes(b"local x = 1\n")
    monkeypatch.setattr(bootstrap, "ensure_tools", lambda: None)
    monkeypatch.setattr(bootstrap, "has_stylua", lambda: True)
    _fmt_setup(tmp_path, monkeypatch, "noop")
    rc = cli.main(["format", str(tidy), "--cwd", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "already formatted" in out
