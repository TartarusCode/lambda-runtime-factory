"""Behavior tests for GraalPy version bumping and archive naming."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bump_version
import runtime_manifest
from runtime_manifest import _apply_defaults

_TESTS_DIR = Path(__file__).resolve().parent
_RUNTIME_LIB_DIR = _TESTS_DIR.parent
_REPO_ROOT = _RUNTIME_LIB_DIR.parent.parent


def _write_manifest(runtimes_root: Path, python_version: str, version: str) -> None:
    runtime_id = "graalpy313" if python_version == "3.13" else "graalpy312"
    manifest = {
        "runtime_id": runtime_id,
        "runtime_family": "graalpy",
        "display_name": "GraalPy 3.13" if python_version == "3.13" else "GraalPy 3.12",
        "distribution_version": version,
        "python_version": python_version,
        "artifact": {
            "checksum_file": "checksums/graalpy.sha256",
        },
        "release": {
            "regions": ["eu-central-1", "us-west-1"],
        },
    }
    path = runtimes_root / runtime_id / "runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def test_latest_graalpy_filters_by_python_version(
    mocker: pytest.MockFixture, graalpy_releases: list[dict[str, object]]
) -> None:
    payload = json.dumps(graalpy_releases).encode("utf-8")
    mocker.patch.object(bump_version, "_http_get", return_value=payload)

    assert bump_version.check_latest_graalpy("3.12") == "25.2.4"
    assert bump_version.check_latest_graalpy("3.13") == "25.3.4.1"


def test_graalpy_python_version_inferred_from_asset_names() -> None:
    assert bump_version._graalpy_python_version(
        ["graalpy3.13-25.3.4.1-linux-amd64.tar.gz"]
    ) == "3.13"
    assert bump_version._graalpy_python_version(
        ["graalpy3.12-25.2.4-linux-amd64.tar.gz"]
    ) == "3.12"
    assert bump_version._graalpy_python_version(
        ["graalpy-25.0.3-linux-amd64.tar.gz"]
    ) == "3.12"


def test_graalpy_check_reports_correct_latest_versions(
    mocker: pytest.MockFixture,
    graalpy_releases: list[dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(graalpy_releases).encode("utf-8")
    mocker.patch.object(bump_version, "_http_get", return_value=payload)
    monkeypatch.setattr(runtime_manifest, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(bump_version, "runtimes_root", lambda: tmp_path)

    _write_manifest(tmp_path, "3.12", "25.0.3")
    _write_manifest(tmp_path, "3.13", "25.2.4")

    outdated = bump_version.check_updates(["graalpy312", "graalpy313"])
    assert outdated["graalpy312"] == ("25.0.3", "25.2.4")
    assert outdated["graalpy313"] == ("25.2.4", "25.3.4.1")


def test_graalpy313_archive_names_use_python_version() -> None:
    raw = {
        "runtime_id": "graalpy313",
        "runtime_family": "graalpy",
        "distribution_version": "25.3.4.1",
        "python_version": "3.13",
    }
    resolved = _apply_defaults("graalpy313", raw)

    assert resolved["artifact"]["archive_name"] == "graalpy3.13-25.3.4.1-linux-amd64.tar.gz"
    assert resolved["artifact"]["checksum_name"] == "graalpy3.13-25.3.4.1-linux-amd64.tar.gz"
    assert resolved["layout"]["helper_install_dir"] == "graalpy/lib/python3.13/site-packages"


def test_graalpy312_archive_names_use_python_version() -> None:
    raw = {
        "runtime_id": "graalpy312",
        "runtime_family": "graalpy",
        "distribution_version": "25.2.4",
        "python_version": "3.12",
    }
    resolved = _apply_defaults("graalpy312", raw)

    assert resolved["artifact"]["archive_name"] == "graalpy3.12-25.2.4-linux-amd64.tar.gz"
    assert resolved["artifact"]["checksum_name"] == "graalpy3.12-25.2.4-linux-amd64.tar.gz"
    assert resolved["layout"]["helper_install_dir"] == "graalpy/lib/python3.12/site-packages"


def test_bump_runtime_writes_python_versioned_checksum_names(
    mocker: pytest.MockFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_manifest, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(bump_version, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(bump_version, "runtime_manifest_path", lambda rid: tmp_path / rid / "runtime.json")

    _write_manifest(tmp_path, "3.12", "25.0.3")
    mocker.patch.object(
        bump_version,
        "fetch_checksums",
        return_value=[
            ("aa11bb22cc33dd44ee55ff66778899aabbccddeeff0011223344", "graalpy3.12-25.2.4-linux-amd64.tar.gz"),
            ("bb11bb22cc33dd44ee55ff66778899aabbccddeeff0011223344", "graalpy3.12-25.2.4-linux-aarch64.tar.gz"),
        ],
    )

    bump_version.bump_runtime("graalpy312", "25.2.4")

    manifest = json.loads((tmp_path / "graalpy312" / "runtime.json").read_text(encoding="utf-8"))
    assert manifest["distribution_version"] == "25.2.4"
    checksum = (tmp_path / "graalpy312" / "checksums" / "graalpy.sha256").read_text(encoding="utf-8")
    assert "graalpy3.12-25.2.4-linux-amd64.tar.gz" in checksum
    assert "graalpy3.12-25.2.4-linux-aarch64.tar.gz" in checksum


def test_real_graalpy_runtimes_validate() -> None:
    from runtime_manifest import validate_runtime

    validate_runtime("graalpy312", "x86_64")
    validate_runtime("graalpy313", "x86_64")
    validate_runtime("graalpy313", "arm64")