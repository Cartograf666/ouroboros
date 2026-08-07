"""Exact, immutable Claudexor runtime delivery for packaged Ouroboros."""

from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import tarfile

import pytest

from ouroboros import claudexor_runtime as runtime


BUILD_SHA = "1" * 40
OLD_BUILD_SHA = "2" * 40
NODE_VERSION = "24.16.0"
NODE_PLATFORMS = (
    "darwin-arm64",
    "darwin-x64",
    "linux-arm64",
    "linux-x64",
    "win32-x64",
)


def _archive(path: pathlib.Path, *, entrypoint: str = "dist/claudexord.js") -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"console.log('fixture')\n"
    with tarfile.open(path, "w:gz") as bundle:
        member = tarfile.TarInfo(entrypoint)
        member.size = len(payload)
        member.mode = 0o644
        bundle.addfile(member, io.BytesIO(payload))
    return path


def _node_artifacts(
    *, exact_key: str = "", exact_archive: pathlib.Path | None = None
) -> dict[str, runtime.NodeRuntimeArtifact]:
    artifacts = {}
    for key in NODE_PLATFORMS:
        suffix = ".zip" if key.startswith("win32-") else ".tar.gz"
        distribution = key.replace("win32-", "win-")
        name = f"node-v{NODE_VERSION}-{distribution}"
        archive = f"{name}{suffix}"
        executable = f"{name}/node.exe" if key.startswith("win32-") else f"{name}/bin/node"
        artifacts[key] = runtime.NodeRuntimeArtifact(
            archive_url=f"https://node.example.test/{archive}",
            sha256="a" * 64,
            size_bytes=1,
            executable=executable,
        )
    if exact_key and exact_archive is not None:
        current = artifacts[exact_key]
        artifacts[exact_key] = runtime.NodeRuntimeArtifact(
            archive_url=f"https://node.example.test/{exact_archive.name}",
            sha256=hashlib.sha256(exact_archive.read_bytes()).hexdigest(),
            size_bytes=exact_archive.stat().st_size,
            executable=current.executable,
        )
    return artifacts


def _pin(
    archive: pathlib.Path,
    *,
    version: str = "3.4.0",
    node_artifacts: dict[str, runtime.NodeRuntimeArtifact] | None = None,
) -> runtime.ClaudexorRuntimePin:
    return runtime.ClaudexorRuntimePin(
        version=version,
        build_sha=BUILD_SHA,
        protocol_major=3,
        archive_url=f"https://example.test/releases/{archive.name}",
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        size_bytes=archive.stat().st_size,
        node_version=NODE_VERSION,
        node_artifacts=node_artifacts or _node_artifacts(),
        entrypoint="dist/claudexord.js",
    )


def _metadata(
    root: pathlib.Path,
    *,
    version: str,
    build_sha: str,
    source: str = "cache",
) -> None:
    entrypoint = root / "dist" / "claudexord.js"
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    entrypoint.write_text("fixture\n", encoding="utf-8")
    (root / "managed-runtime.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": version,
                "build_sha": build_sha,
                "protocol_major": 3,
                "archive_sha256": "a" * 64,
                "archive_size": 123,
                "node_version": NODE_VERSION,
                "entrypoint": "dist/claudexord.js",
                "archive_source": source,
            }
        ),
        encoding="utf-8",
    )


def _data_plane(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    import ouroboros.config as config

    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    return data


def test_pin_loader_distinguishes_unpublished_from_partial(tmp_path):
    pin_file = tmp_path / "pin.json"
    pin_file.write_text('{"schema_version":1,"release":null}\n', encoding="utf-8")
    assert runtime.load_runtime_pin(pin_file) is None

    pin_file.write_text(
        json.dumps({"schema_version": 1, "release": {"version": "3.4.0"}}),
        encoding="utf-8",
    )
    with pytest.raises(runtime.ClaudexorRuntimeError) as excinfo:
        runtime.load_runtime_pin(pin_file)
    assert excinfo.value.code == "runtime_pin_invalid"
    assert "missing fields" in str(excinfo.value)


def test_managed_runtime_layout_stays_inside_legacy_windows_path_budget(
    tmp_path, monkeypatch
):
    _data_plane(monkeypatch, tmp_path)
    archive = _archive(tmp_path / "runtime.tar.gz")
    pin = _pin(archive)

    assert pin.install_name == f"3.4.0-{BUILD_SHA[:12]}"
    assert runtime.managed_runtime_dir(pin).relative_to(
        runtime.managed_runtime_root()
    ) == pathlib.Path(pin.install_name)

    # The current public closure's measured longest relative member is 170
    # characters.  A conservative default Windows profile still stays below
    # legacy MAX_PATH with the shared compact layout; no platform fork needed.
    windows_data_root = pathlib.PureWindowsPath(
        r"C:\Users\twenty-character-user\Ouroboros\data"
    )
    longest_member = (
        "browser-mcp-runtime/node_modules/.pnpm/"
        "playwright-core@1.62.0-alpha-1783623505000/node_modules/"
        "playwright-core/lib/vite/traceViewer/assets/defaultSettingsView-CT5oRh4X.js"
    )
    assert len(longest_member) == 170
    candidate = windows_data_root / "state" / "cx" / pin.install_name / longest_member
    assert len(str(candidate)) < 260


def test_archive_verification_binds_both_size_and_digest(tmp_path):
    archive = _archive(tmp_path / "runtime.tar.gz")
    pin = _pin(archive)
    assert runtime.verify_runtime_archive(archive, pin) == archive

    archive.write_bytes(archive.read_bytes() + b"x")
    with pytest.raises(runtime.ClaudexorRuntimeError) as excinfo:
        runtime.verify_runtime_archive(archive, pin)
    assert excinfo.value.code == "runtime_archive_size_mismatch"


def test_fetch_existing_valid_archive_wins_without_network(tmp_path, monkeypatch):
    archive = _archive(tmp_path / "runtime.tar.gz")
    pin = _pin(archive)

    class NoNetwork:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("network must not be opened for a valid durable cache")

    import httpx

    monkeypatch.setattr(httpx, "Client", NoNetwork)
    assert runtime.fetch_runtime_archive(pin, archive) == archive


@pytest.mark.parametrize("kind", ["traversal", "symlink", "device"])
def test_extraction_rejects_escape_links_and_special_files(tmp_path, kind):
    archive = tmp_path / f"{kind}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("../escape" if kind == "traversal" else "entry")
        if kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "outside"
        elif kind == "device":
            member.type = tarfile.CHRTYPE
        else:
            member.size = 1
        bundle.addfile(member, io.BytesIO(b"x") if member.size else None)
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises(runtime.ClaudexorRuntimeError) as excinfo:
        runtime.ClaudexorRuntimeManager._extract_archive(archive, destination)
    assert excinfo.value.code == "runtime_archive_unsafe"
    assert not (tmp_path / "escape").exists()


def test_extraction_accepts_a_normal_tar_root_entry(tmp_path):
    archive = tmp_path / "rooted.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        bundle.addfile(root)
        payload = b"ok\n"
        member = tarfile.TarInfo("dist/claudexord.js")
        member.size = len(payload)
        member.mode = 0o644
        bundle.addfile(member, io.BytesIO(payload))
    destination = tmp_path / "out"
    destination.mkdir()
    runtime.ClaudexorRuntimeManager._extract_archive(archive, destination)
    assert (destination / "dist" / "claudexord.js").read_bytes() == b"ok\n"


def test_seed_install_uses_bundled_node_and_records_real_source(tmp_path, monkeypatch):
    _data_plane(monkeypatch, tmp_path)
    seed = _archive(tmp_path / "bundle" / "claudexor-runtime" / "runtime.tar.gz")
    pin = _pin(seed)
    node = tmp_path / "bundle" / "node-standalone" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("node fixture\n", encoding="utf-8")

    import ouroboros.platform_layer as platform

    monkeypatch.setattr(platform, "bundled_resource_bases", lambda: [tmp_path / "bundle"])
    monkeypatch.setattr(
        platform,
        "embedded_node_candidates",
        lambda base: [pathlib.Path(base) / "node-standalone" / "bin" / "node"],
    )
    monkeypatch.setattr(
        platform,
        "probe_node_version",
        lambda candidate: NODE_VERSION if candidate == str(node) else "",
    )
    manager = runtime.ClaudexorRuntimeManager(pin)
    probes = []
    monkeypatch.setattr(
        manager,
        "_probe",
        lambda command, exact_pin: probes.append((list(command), exact_pin)) or NODE_VERSION,
    )

    command = manager.ensure()
    target = runtime.managed_runtime_dir(pin)
    assert command == [str(node), str(target / pin.entrypoint)]
    assert probes and all(probe_pin == pin for _, probe_pin in probes)
    metadata = json.loads((target / "managed-runtime.json").read_text(encoding="utf-8"))
    assert metadata["archive_source"] == "bundle_seed"
    assert metadata["build_sha"] == BUILD_SHA
    assert manager.status()["source"] == "bundle_seed"


def test_clean_source_install_fetches_exact_managed_node_in_the_same_ensure(
    tmp_path, monkeypatch
):
    _data_plane(monkeypatch, tmp_path)
    source_root = tmp_path / "clean-source"
    closure = _archive(source_root / "claudexor-runtime" / "runtime.tar.gz")
    node_archive = tmp_path / f"node-v{NODE_VERSION}-linux-x64.tar.gz"
    node_member = f"node-v{NODE_VERSION}-linux-x64/bin/node"
    payload = b"managed node fixture\n"
    with tarfile.open(node_archive, "w:gz") as bundle:
        member = tarfile.TarInfo(node_member)
        member.size = len(payload)
        member.mode = 0o755
        bundle.addfile(member, io.BytesIO(payload))
    pin = _pin(
        closure,
        node_artifacts=_node_artifacts(
            exact_key="linux-x64", exact_archive=node_archive
        ),
    )

    import ouroboros.platform_layer as platform

    monkeypatch.setattr(platform, "bundled_resource_bases", lambda: [source_root])
    monkeypatch.setattr(platform, "node_distribution_platform", lambda: "linux-x64")
    monkeypatch.setattr(
        platform,
        "embedded_node_candidates",
        lambda base: [pathlib.Path(base) / "node-standalone" / "bin" / "node"],
    )
    monkeypatch.setattr(
        platform,
        "probe_node_version",
        lambda candidate: NODE_VERSION if pathlib.Path(candidate).is_file() else "",
    )

    def fetch_fixture(artifact, destination):
        pathlib.Path(destination).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(node_archive, destination)
        return runtime.verify_node_archive(destination, artifact)

    monkeypatch.setattr(runtime, "fetch_node_archive", fetch_fixture)
    manager = runtime.ClaudexorRuntimeManager(pin)
    monkeypatch.setattr(manager, "_probe", lambda _command, _pin: NODE_VERSION)

    command = manager.ensure()

    assert pathlib.Path(command[0]).is_relative_to(runtime.managed_runtime_root())
    assert pathlib.Path(command[0]).parts[-3:] == ("node-standalone", "bin", "node")
    assert not (source_root / "node-standalone").exists()
    metadata = json.loads(
        (
            runtime.managed_node_dir(pin, "linux-x64")
            / "managed-node.json"
        ).read_text(encoding="utf-8")
    )
    assert metadata["archive_sha256"] == pin.node_artifacts["linux-x64"].sha256
    assert metadata["version"] == NODE_VERSION


def test_managed_pin_never_falls_back_to_random_path_binary(tmp_path, monkeypatch):
    archive = _archive(tmp_path / "runtime.tar.gz")
    pin = _pin(archive)
    external = tmp_path / "claudexord"
    external.write_text("external\n", encoding="utf-8")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: str(external))
    manager = runtime.ClaudexorRuntimeManager(pin)
    assert manager.resolve_command() == []

    def fail_install(*_args, **_kwargs):
        raise runtime.ClaudexorRuntimeError("runtime_download_failed", "offline")

    monkeypatch.setattr(manager, "_install", fail_install)
    with pytest.raises(runtime.ClaudexorRuntimeError) as excinfo:
        manager.ensure()
    assert excinfo.value.code == "runtime_download_failed"
    assert manager.status()["state"] == "error"

    compatibility = runtime.ClaudexorRuntimeManager(None)
    assert compatibility.resolve_command() == [str(external)]


def test_invalid_tracked_pin_never_degrades_to_path_compatibility(tmp_path, monkeypatch):
    external = tmp_path / "claudexord"
    external.write_text("external\n", encoding="utf-8")
    monkeypatch.setattr(runtime.shutil, "which", lambda _name: str(external))

    def invalid_pin():
        raise runtime.ClaudexorRuntimeError("runtime_pin_invalid", "partial release")

    monkeypatch.setattr(runtime, "load_runtime_pin", invalid_pin)
    manager = runtime.ClaudexorRuntimeManager()
    assert manager.resolve_command() == []
    assert manager.status()["state"] == "error"
    with pytest.raises(runtime.ClaudexorRuntimeError) as excinfo:
        manager.ensure()
    assert excinfo.value.code == "runtime_pin_invalid"


def test_status_is_read_only_and_exposes_update_before_and_after_staging(
    tmp_path, monkeypatch
):
    _data_plane(monkeypatch, tmp_path)
    archive = _archive(tmp_path / "runtime.tar.gz")
    pin = _pin(archive)
    manager = runtime.ClaudexorRuntimeManager(pin)

    before = manager.status(
        running=True,
        engine_version="3.2.1",
        engine_build_sha=OLD_BUILD_SHA,
    )
    assert before == {
        "state": "update_available",
        "version": "3.2.1",
        "target_version": "3.4.0",
        "staged_version": "",
        "build_sha": OLD_BUILD_SHA,
        "source": "",
        "node_version": "",
        "last_error": None,
    }
    assert not runtime.managed_runtime_root().exists()

    old_root = runtime.managed_runtime_root() / f"3.2.1-{OLD_BUILD_SHA[:12]}"
    _metadata(old_root, version="3.2.1", build_sha=OLD_BUILD_SHA, source="cache")
    preserved = manager.status()
    assert preserved["state"] == "update_available"
    assert (preserved["version"], preserved["source"]) == ("3.2.1", "cache")

    target = runtime.managed_runtime_dir(pin)
    _metadata(target, version=pin.version, build_sha=pin.build_sha, source="download")
    # Target metadata is exact only when its archive identity also matches the pin.
    target_meta = json.loads((target / "managed-runtime.json").read_text(encoding="utf-8"))
    target_meta.update(archive_sha256=pin.sha256, archive_size=pin.size_bytes)
    (target / "managed-runtime.json").write_text(json.dumps(target_meta), encoding="utf-8")

    staged = manager.status(
        running=True,
        engine_version="3.2.1",
        engine_build_sha=OLD_BUILD_SHA,
    )
    assert staged["state"] == "update_staged"
    assert staged["staged_version"] == pin.version
    assert staged["source"] == "download"
    ready = manager.status(
        running=True,
        engine_version=pin.version,
        engine_build_sha=pin.build_sha,
    )
    assert ready["state"] == "ready"


def test_status_distinguishes_fresh_missing_from_a_corrupt_target(tmp_path, monkeypatch):
    """Fresh absence means Install; a broken immutable target means Fix."""
    _data_plane(monkeypatch, tmp_path)
    archive = _archive(tmp_path / "runtime.tar.gz")
    pin = _pin(archive)
    manager = runtime.ClaudexorRuntimeManager(pin)

    fresh = manager.status()
    assert fresh["state"] == "missing"
    assert fresh["last_error"] is None

    target = runtime.managed_runtime_dir(pin)
    target.mkdir(parents=True)
    (target / "managed-runtime.json").write_text("{broken", encoding="utf-8")

    corrupt = manager.status()
    assert corrupt["state"] == "error"
    assert corrupt["last_error"] == "managed runtime files are incomplete or fail identity checks"


def test_probe_requires_exact_bundled_node_and_stamped_identity(tmp_path, monkeypatch):
    archive = _archive(tmp_path / "runtime.tar.gz")
    pin = _pin(archive)
    manager = runtime.ClaudexorRuntimeManager(pin)

    import ouroboros.platform_layer as platform

    monkeypatch.setattr(platform, "probe_node_version", lambda _path: "24.15.0")
    with pytest.raises(runtime.ClaudexorRuntimeError) as excinfo:
        manager._probe(["/bundle/node", "/runtime/daemon.js"], pin)
    assert excinfo.value.code == "runtime_node_version_mismatch"

    monkeypatch.setattr(platform, "probe_node_version", lambda _path: NODE_VERSION)
    monkeypatch.setenv("CLAUDEXOR_BUILD_SHA", BUILD_SHA)
    seen = {}

    def fake_run(*_args, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"version": pin.version, "buildSha": OLD_BUILD_SHA}) + "\n"
        )

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    with pytest.raises(runtime.ClaudexorRuntimeError) as excinfo:
        manager._probe(["/bundle/node", "/runtime/daemon.js"], pin)
    assert excinfo.value.code == "runtime_probe_identity_mismatch"
    assert "CLAUDEXOR_BUILD_SHA" not in seen["env"]


def test_failed_candidate_probe_preserves_existing_target(tmp_path, monkeypatch):
    _data_plane(monkeypatch, tmp_path)
    archive = _archive(tmp_path / "runtime.tar.gz")
    pin = _pin(archive)
    manager = runtime.ClaudexorRuntimeManager(pin)
    target = runtime.managed_runtime_dir(pin)
    target.mkdir(parents=True)
    sentinel = target / "old-tree"
    sentinel.write_text("keep", encoding="utf-8")

    import ouroboros.platform_layer as platform

    monkeypatch.setattr(platform, "bundled_resource_bases", lambda: [tmp_path / "bundle"])
    monkeypatch.setattr(
        platform,
        "embedded_node_candidates",
        lambda base: [pathlib.Path(base) / "node-standalone" / "bin" / "node"],
    )
    node = tmp_path / "bundle" / "node-standalone" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("fixture\n", encoding="utf-8")
    monkeypatch.setattr(platform, "probe_node_version", lambda _candidate: NODE_VERSION)

    def fail_probe(*_args, **_kwargs):
        raise runtime.ClaudexorRuntimeError("runtime_probe_failed", "candidate is bad")

    monkeypatch.setattr(manager, "_probe", fail_probe)
    with pytest.raises(runtime.ClaudexorRuntimeError):
        manager._promote_archive(pin, archive, "cache")
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_atomic_promote_mid_failure_restores_the_displaced_target(tmp_path, monkeypatch):
    """The claimed atomic promote, at its hardest point: the old target has
    ALREADY been displaced to ``.old-<nonce>`` when ``os.replace(staging, root)``
    fails.  The transaction must put the old tree back and raise typed."""
    _data_plane(monkeypatch, tmp_path)
    archive = _archive(tmp_path / "runtime.tar.gz")
    pin = _pin(archive)
    manager = runtime.ClaudexorRuntimeManager(pin)
    target = runtime.managed_runtime_dir(pin)
    target.mkdir(parents=True)
    sentinel = target / "old-tree"
    sentinel.write_text("keep", encoding="utf-8")

    import ouroboros.platform_layer as platform

    monkeypatch.setattr(platform, "bundled_resource_bases", lambda: [tmp_path / "bundle"])
    monkeypatch.setattr(
        platform,
        "embedded_node_candidates",
        lambda base: [pathlib.Path(base) / "node-standalone" / "bin" / "node"],
    )
    node = tmp_path / "bundle" / "node-standalone" / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("fixture\n", encoding="utf-8")
    monkeypatch.setattr(platform, "probe_node_version", lambda _candidate: NODE_VERSION)
    monkeypatch.setattr(manager, "_probe", lambda _command, _pin: NODE_VERSION)

    real_replace = os.replace

    def failing_promote_replace(src, dst):
        # Only the staging -> root promote fails; the preceding displacement
        # (root -> .old-<nonce>) and the recovery (.old-<nonce> -> root) work.
        if pathlib.Path(src).name.startswith(".tmp-") and pathlib.Path(dst) == target:
            raise OSError("simulated mid-promote failure")
        return real_replace(src, dst)

    monkeypatch.setattr(runtime.os, "replace", failing_promote_replace)
    with pytest.raises(runtime.ClaudexorRuntimeError) as excinfo:
        manager._promote_archive(pin, archive, "cache")
    assert excinfo.value.code == "runtime_install_failed"
    # The old target is back in place, byte-for-byte where it was.
    assert sentinel.read_text(encoding="utf-8") == "keep"
    # No transaction debris: neither staging nor a displaced orphan remains.
    leftovers = [p.name for p in target.parent.iterdir() if p.name != pin.install_name]
    assert leftovers == []


def test_old_launcher_interpreter_path_recovers_bundle_resources(tmp_path):
    from ouroboros.platform_layer import bundled_resource_ancestor_bases

    resources = tmp_path / "Ouroboros.app" / "Contents" / "Resources"
    interpreter = resources / "python-standalone" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("python fixture\n", encoding="utf-8")
    assert resources in bundled_resource_ancestor_bases(interpreter)
