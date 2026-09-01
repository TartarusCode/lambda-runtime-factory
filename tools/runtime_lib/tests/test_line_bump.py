"""Behavior tests for line-aware runtime version checks (bun/deno/go/rust)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bump_version


@pytest.fixture()
def go_dl_json() -> bytes:
    """Fake go.dev/dl/?mode=json payload."""
    return json.dumps(
        [
            {"version": "go1.27.0", "stable": True},
            {"version": "go1.26.5", "stable": True},
            {"version": "go1.26.4", "stable": True},
        ]
    ).encode("utf-8")


def test_check_latest_bun_filters_to_line(
    mocker: pytest.MockFixture, bun_releases: list[dict[str, object]]
) -> None:
    payload = json.dumps(bun_releases).encode("utf-8")
    mocker.patch.object(bump_version, "_http_get", return_value=payload)

    assert bump_version.check_latest_bun("1.3") == "1.3.14"
    assert bump_version.check_latest_bun("1.4") == "1.4.0"


def test_check_latest_deno_filters_to_line(
    mocker: pytest.MockFixture, deno_releases: list[dict[str, object]]
) -> None:
    payload = json.dumps(deno_releases).encode("utf-8")
    mocker.patch.object(bump_version, "_http_get", return_value=payload)

    assert bump_version.check_latest_deno("2.9") == "2.9.6"
    assert bump_version.check_latest_deno("2.8") == "2.8.2"


def test_check_latest_go_filters_to_line(
    mocker: pytest.MockFixture, go_dl_json: bytes
) -> None:
    mocker.patch.object(bump_version, "_http_get_text", return_value=go_dl_json.decode("utf-8"))

    assert bump_version.check_latest_go("1.26") == "1.26.5"
    assert bump_version.check_latest_go("1.27") == "1.27.0"


def test_check_latest_rust_filters_to_line(
    mocker: pytest.MockFixture,
) -> None:
    channel = "\n".join(
        [
            "[pkg.rust]",
            'version = "1.98.0 (2026-08-20)"',
            "",
            "[pkg.rust.x86_64-unknown-linux-musl]",
            "version = 1.98.0",
        ]
    )
    mocker.patch.object(bump_version, "_http_get_text", return_value=channel)

    # 1.96 line: only newer patch is 1.97.x line — so nothing on 1.96 is available
    assert bump_version.check_latest_rust("1.98") == "1.98.0"
    assert bump_version.check_latest_rust("1.96") is None


def test_bump_latest_with_line_skips_offline_updates(
    mocker: pytest.MockFixture,
    bun_releases: list[dict[str, object]],
    deno_releases: list[dict[str, object]],
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime on a line with no newer patch must not be bumped to another line."""
    import runtime_manifest

    monkeypatch.setattr(runtime_manifest, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(bump_version, "runtimes_root", lambda: tmp_path)
    mocker.patch.object(
        bump_version,
        "_http_get",
        side_effect=[
            json.dumps(bun_releases).encode("utf-8"),
            json.dumps(deno_releases).encode("utf-8"),
        ],
    )

    bun_manifest = tmp_path / "bun13" / "runtime.json"
    bun_manifest.parent.mkdir(parents=True)
    bun_manifest.write_text(
        json.dumps(
            {
                "runtime_id": "bun13",
                "runtime_family": "bun",
                "distribution_version": "1.3.14",
                "version_line": "1.3",
                "artifact": {"checksum_file": "checksums/bun.sha256"},
                "release": {"regions": ["eu-central-1"]},
            }
        ),
        encoding="utf-8",
    )

    outdated = bump_version.check_updates(["bun13"])
    assert "bun13" not in outdated  # 1.4.0 is off the 1.3 line


@pytest.fixture()
def fake_runtimes_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Runtimes root backed by tmp_path, shared by runtime_manifest and bump_version."""
    import runtime_manifest

    monkeypatch.setattr(runtime_manifest, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(bump_version, "runtimes_root", lambda: tmp_path)
    return tmp_path


def _write_runtime_manifest(root: Path, runtime_id: str, family: str, version: str, version_line: str) -> None:
    manifest = {
        "runtime_id": runtime_id,
        "runtime_family": family,
        "display_name": runtime_id,
        "distribution_version": version,
        "version_line": version_line,
        "artifact": {"checksum_file": "checksums/bun.sha256"},
        "release": {"regions": ["eu-central-1"]},
    }
    path = root / runtime_id / "runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def test_new_line_id_and_display_name() -> None:
    assert bump_version._new_line_id("bun", "1.4.0") == "bun14"
    assert bump_version._new_line_id("deno", "2.9.0") == "deno29"
    assert bump_version._new_line_id("go-toolchain", "1.27.0") == "go127"
    assert bump_version._new_line_id("rust-musl", "1.98.0") == "rust198"

    assert bump_version._new_display_name("bun", "1.4.0") == "Bun 1.4"
    assert bump_version._new_display_name("deno", "2.9.0") == "Deno 2.9"
    assert bump_version._new_display_name("go-toolchain", "1.27.0") == "Go 1.27"
    assert bump_version._new_display_name("rust-musl", "1.98.0") == "Rust 1.98"


def test_detect_new_lines_reports_only_untracked_lines(
    mocker: pytest.MockFixture,
    bun_releases: list[dict[str, object]],
    rust_stable_channel: str,
    fake_runtimes_root: Path,
) -> None:
    """bun 1.4 exists (untracked) while bun 1.3 is tracked; rust 1.98 exists and is untracked."""
    payload = json.dumps(bun_releases).encode("utf-8")
    mocker.patch.object(bump_version, "_http_get", return_value=payload)
    mocker.patch.object(bump_version, "_http_get_text", return_value=rust_stable_channel)

    _write_runtime_manifest(fake_runtimes_root, "bun13", "bun", "1.3.14", "1.3")

    new_lines = bump_version.detect_new_lines()
    assert "bun" in new_lines
    assert new_lines["bun"] == {"id": "bun14", "line": "1.4", "version": "1.4.0"}
    assert "rust-musl" in new_lines
    assert new_lines["rust-musl"] == {
        "id": "rust198",
        "line": "1.98",
        "version": "1.98.0",
    }


def test_detect_new_lines_is_idempotent_when_line_exists(
    mocker: pytest.MockFixture,
    bun_releases: list[dict[str, object]],
    fake_runtimes_root: Path,
) -> None:
    """Once bun14 exists, bun is no longer reported as a new line."""
    payload = json.dumps(bun_releases).encode("utf-8")
    mocker.patch.object(bump_version, "_http_get", return_value=payload)

    _write_runtime_manifest(fake_runtimes_root, "bun13", "bun", "1.3.14", "1.3")
    _write_runtime_manifest(fake_runtimes_root, "bun14", "bun", "1.4.0", "1.4")

    assert "bun" not in bump_version.detect_new_lines()


def test_add_runtime_line_clones_and_rewrites(
    mocker: pytest.MockFixture, fake_runtimes_root: Path
) -> None:
    """Cloning bun13 creates bun14 with rewritten runtime.json and checksums."""
    _write_runtime_manifest(fake_runtimes_root, "bun13", "bun", "1.3.14", "1.3")
    (fake_runtimes_root / "bun13" / "bootstrap").mkdir(parents=True)
    (fake_runtimes_root / "bun13" / "bootstrap" / "bootstrap").write_text(
        "#!/bin/sh\nexec /opt/bun/bun ...\n", encoding="utf-8"
    )
    (fake_runtimes_root / "bun13" / "examples" / "sls").mkdir(parents=True)
    (fake_runtimes_root / "bun13" / "examples" / "sls" / "serverless.yml").write_text(
        "service: hello-bun13\nfunctions:\n  hello-bun13:\n    layers: [bun13]\n",
        encoding="utf-8",
    )

    mocker.patch.object(
        bump_version,
        "fetch_checksums",
        return_value=[
            ("aa11bb22cc33dd44ee55ff66778899aabbccddeeff00112233445566778899aa11", "bun-linux-x64.zip"),
            ("bb11bb22cc33dd44ee55ff66778899aabbccddeeff00112233445566778899bb11", "bun-linux-aarch64.zip"),
        ],
    )

    new_id = bump_version.add_runtime_line("bun", "1.4.0")
    assert new_id == "bun14"

    new_manifest = json.loads(
        (fake_runtimes_root / "bun14" / "runtime.json").read_text(encoding="utf-8")
    )
    assert new_manifest["runtime_id"] == "bun14"
    assert new_manifest["distribution_version"] == "1.4.0"
    assert new_manifest["version_line"] == "1.4"

    sls = (fake_runtimes_root / "bun14" / "examples" / "sls" / "serverless.yml").read_text(
        encoding="utf-8"
    )
    assert "hello-bun14" in sls and "bun13" not in sls

    checksum = (fake_runtimes_root / "bun14" / "checksums" / "bun.sha256").read_text(
        encoding="utf-8"
    )
    assert "bun-linux-x64.zip" in checksum


def test_bump_latest_adds_new_lines(
    mocker: pytest.MockFixture,
    monkeypatch: pytest.MonkeyPatch,
    bun_releases: list[dict[str, object]],
    fake_runtimes_root: Path,
) -> None:
    """bump-latest adds a runtime for an untracked new line."""
    monkeypatch.setattr(bump_version, "list_runtime_ids", lambda: ["bun13"])
    monkeypatch.setattr(bump_version, "runtime_manifest_path", lambda rid: fake_runtimes_root / rid / "runtime.json")

    payload = json.dumps(bun_releases).encode("utf-8")
    mocker.patch.object(bump_version, "_http_get", return_value=payload)
    mocker.patch.object(bump_version, "_http_get_text", return_value="[pkg.rust]\n")
    mocker.patch.object(bump_version, "fetch_checksums", return_value=[])
    mocker.patch.object(bump_version, "add_runtime_line", return_value="bun14")

    _write_runtime_manifest(fake_runtimes_root, "bun13", "bun", "1.3.14", "1.3")

    bump_version.bump_latest_all()

    bump_version.add_runtime_line.assert_called_once_with("bun", "1.4.0", dry_run=False)


def test_add_runtime_line_dry_run_does_not_create(
    mocker: pytest.MockFixture, fake_runtimes_root: Path
) -> None:
    """--dry-run must not create the directory or fetch checksums."""
    _write_runtime_manifest(fake_runtimes_root, "bun13", "bun", "1.3.14", "1.3")
    fetch = mocker.patch.object(bump_version, "fetch_checksums", return_value=[])

    new_id = bump_version.add_runtime_line("bun", "1.4.0", dry_run=True)

    assert new_id == "bun14"
    assert not (fake_runtimes_root / "bun14").exists()
    fetch.assert_not_called()


def test_check_latest_rust_ignores_component_version_entries(
    mocker: pytest.MockFixture,
) -> None:
    """A `version = ""` or component version entry outside [pkg.rust] must not crash."""
    channel = "\n".join(
        [
            "[pkg.cargo]",
            'version = "0.99.0"',
            "",
            "[pkg.rust]",
            'version = "1.98.0 (88d9e12ae 2026-08-18)"',
            "",
            "[pkg.rust.target.aarch64-apple-darwin]",
            "available = true",
            "",
            "[[pkg.rust.target.aarch64-apple-darwin.components]]",
            'version = ""',
        ]
    )
    mocker.patch.object(bump_version, "_http_get_text", return_value=channel)

    assert bump_version.check_latest_rust("") == "1.98.0"
    assert bump_version.check_latest_rust("1.98") == "1.98.0"


def test_check_latest_skips_non_numeric_tags(
    mocker: pytest.MockFixture,
) -> None:
    """A canary/RC tag on the releases list must not crash global-latest detection."""
    releases = [
        {"tag_name": "bun-v1.4.0-canary.1+abc123"},
        {"tag_name": "bun-v1.4.0"},
    ]
    payload = json.dumps(releases).encode("utf-8")
    mocker.patch.object(bump_version, "_http_get", return_value=payload)

    assert bump_version.check_latest_bun("") == "1.4.0"