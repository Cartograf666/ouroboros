from __future__ import annotations

import os
import pathlib
import subprocess

import pytest


REPO = pathlib.Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="AppImage packaging is POSIX-only"
)


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
    (internal / "VERSION").write_text("6.96.2\n", encoding="utf-8")
    embedded_python = internal / "python-standalone/bin/python3"
    embedded_python.parent.mkdir(parents=True)
    embedded_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

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
    assert (
        appdir / "usr/lib/ouroboros/_internal/python-standalone/bin/python3"
    ).is_file()
    desktop = (appdir / "ouroboros.desktop").read_text(encoding="utf-8")
    assert "Exec=Ouroboros" in desktop
    assert "Icon=ouroboros" in desktop

    version = subprocess.run(
        [str(appdir / "AppRun"), "--version"],
        env={**os.environ, "APPDIR": str(appdir)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert version.stdout.strip() == "Ouroboros 6.96.2"


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


@pytest.mark.parametrize("original_tmpdir", [None, "/caller/tmp"])
def test_apprun_restores_tmpdir_before_payload(tmp_path: pathlib.Path, original_tmpdir: str | None):
    appdir = tmp_path / "AppDir"
    cli = appdir / "usr/lib/ouroboros/bin/ouroboros"
    cli.parent.mkdir(parents=True)
    cli.write_text(
        "#!/bin/sh\n"
        "if [ \"${TMPDIR+x}\" = x ]; then printf 'set:%s\\n' \"$TMPDIR\"; else printf 'unset\\n'; fi\n"
        "if env | grep -q '^OUROBOROS_APPIMAGE_'; then exit 9; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    cli.chmod(0o755)
    launcher = appdir / "usr/lib/ouroboros/Ouroboros"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    apprun = appdir / "AppRun"
    apprun.write_bytes((REPO / "packaging/appimage/AppRun").read_bytes())
    apprun.chmod(0o755)
    env = {
        **os.environ,
        "APPDIR": str(appdir),
        "TMPDIR": "/private/extraction",
        "OUROBOROS_APPIMAGE_RESTORE_TMPDIR": "1",
        "OUROBOROS_APPIMAGE_ORIGINAL_TMPDIR_SET": "1" if original_tmpdir is not None else "0",
        "OUROBOROS_APPIMAGE_ORIGINAL_TMPDIR": original_tmpdir or "",
    }

    result = subprocess.run(
        [str(apprun), "--cli", "--help"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == (f"set:{original_tmpdir}" if original_tmpdir is not None else "unset")


def test_appimage_builder_pins_tool_and_embedded_runtime():
    script = (REPO / "scripts/build_appimage.sh").read_text(encoding="utf-8")

    assert "RUNTIME_VERSION=20251108" in script
    assert "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d" in script
    assert "00cbdfcf917cc6c0ff6d3347d59e0ca1f7f45a6df1a428a0d6d8a78664d87444" in script
    assert "releases/download/${RUNTIME_VERSION}/runtime-${TOOL_ARCH}" in script
    assert 'fetch_verified "$RUNTIME" "$RUNTIME_URL" "$RUNTIME_SHA256"' in script
    assert '"$TOOL" --runtime-file "$RUNTIME" "$APPDIR" "$OUTPUT"' in script
