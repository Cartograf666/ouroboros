from __future__ import annotations

import os
import pathlib
import subprocess


REPO = pathlib.Path(__file__).resolve().parents[1]


def test_appdir_layout_wraps_the_existing_pyinstaller_payload(tmp_path: pathlib.Path):
    dist = tmp_path / "dist"
    payload = dist / "Ouroboros"
    payload.mkdir(parents=True)
    launcher = payload / "Ouroboros"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    internal = payload / "_internal"
    internal.mkdir()
    (internal / "repo.bundle").write_bytes(b"bundle")

    appdir = tmp_path / "Ouroboros.AppDir"
    env = os.environ.copy()
    env["OUROBOROS_DIST_DIR"] = str(dist)
    env["OUROBOROS_APPDIR"] = str(appdir)
    subprocess.run(
        ["bash", "scripts/build_appimage.sh", "--appdir-only"],
        cwd=REPO,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert (appdir / "AppRun").stat().st_mode & 0o111
    assert (appdir / "ouroboros.desktop").is_file()
    assert (appdir / "ouroboros.png").is_file()
    assert (appdir / "usr/lib/ouroboros/Ouroboros").is_file()
    assert (appdir / "usr/lib/ouroboros/_internal/repo.bundle").is_file()
    desktop = (appdir / "ouroboros.desktop").read_text(encoding="utf-8")
    assert "Exec=Ouroboros" in desktop
    assert "Icon=ouroboros" in desktop


def test_apprun_exposes_cli_without_writing_to_the_mount(tmp_path: pathlib.Path):
    appdir = tmp_path / "AppDir"
    cli = appdir / "usr/lib/ouroboros/bin/ouroboros"
    cli.parent.mkdir(parents=True)
    cli.write_text('#!/bin/sh\nprintf "%s\\n" "$OUROBOROS_BUNDLE_DIR"\n', encoding="utf-8")
    cli.chmod(0o755)
    launcher = appdir / "usr/lib/ouroboros/Ouroboros"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    apprun = appdir / "AppRun"
    apprun.write_bytes((REPO / "packaging/appimage/AppRun").read_bytes())
    apprun.chmod(0o755)

    result = subprocess.run(
        [str(apprun), "--cli", "--help"],
        env={**os.environ, "APPDIR": str(appdir)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == str(appdir / "usr/lib/ouroboros/_internal")
