"""Behavior tests for runtime line detection, roll-off, and matrix bounding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bump_version
import runtime_manifest


def _pypy_versions_payload() -> str:
    """Upstream index with several Python lines alive at once."""
    return json.dumps(
        [
            {"pypy_version": "8.0.0", "python_version": "3.12.14", "stable": True,
             "files": [{"filename": "pypy3.12-v8.0.0-linux64.tar.gz", "platform": "linux", "arch": "x64"},
                       {"filename": "pypy3.12-v8.0.0-aarch64.tar.gz", "platform": "linux", "arch": "aarch64"}]},
            {"pypy_version": "8.0.0", "python_version": "3.11.16", "stable": True,
             "files": [{"filename": "pypy3.11-v8.0.0-linux64.tar.gz", "platform": "linux", "arch": "x64"},
                       {"filename": "pypy3.11-v8.0.0-aarch64.tar.gz", "platform": "linux", "arch": "aarch64"}]},
            {"pypy_version": "7.3.23", "python_version": "3.11.15", "stable": True,
             "files": [{"filename": "pypy3.11-v7.3.23-linux64.tar.bz2", "platform": "linux", "arch": "x64"}]},
            {"pypy_version": "7.3.19", "python_version": "3.10.16", "stable": True,
             "files": [{"filename": "pypy3.10-v7.3.19-linux64.tar.bz2", "platform": "linux", "arch": "x64"}]},
            {"pypy_version": "8.0.0", "python_version": "2.7.18", "stable": True,
             "files": [{"filename": "pypy2.7-v8.0.0-linux64.tar.gz", "platform": "linux", "arch": "x64"}]},
            {"pypy_version": "9.9.9", "python_version": "3.99.0", "stable": False,
             "files": [{"filename": "pypy3.99-v9.9.9-linux64.tar.gz", "platform": "linux", "arch": "x64"}]},
        ]
    )


@pytest.fixture()
def fake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Runtimes root shared by runtime_manifest and bump_version."""
    monkeypatch.setattr(runtime_manifest, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(bump_version, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(
        runtime_manifest, "runtime_manifest_path", lambda rid: tmp_path / rid / "runtime.json"
    )
    monkeypatch.setattr(
        bump_version, "runtime_manifest_path", lambda rid: tmp_path / rid / "runtime.json"
    )
    return tmp_path


def _write_manifest(root: Path, runtime_id: str, family: str, version: str, **extra: object) -> None:
    manifest = {
        "runtime_id": runtime_id,
        "runtime_family": family,
        "display_name": runtime_id,
        "distribution_version": version,
        "artifact": {"checksum_file": "checksums/x.sha256"},
        "release": {"regions": ["eu-central-1"]},
    }
    manifest.update(extra)
    path = root / runtime_id / "runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


# --- PyPy Python-version lines -------------------------------------------------


def test_check_latest_pypy_is_per_python_line(mocker: pytest.MockFixture) -> None:
    mocker.patch.object(bump_version, "_http_get_text", return_value=_pypy_versions_payload())

    assert bump_version.check_latest_pypy("3.11") == "pypy3.11-v8.0.0"
    assert bump_version.check_latest_pypy("3.12") == "pypy3.12-v8.0.0"
    assert bump_version.check_latest_pypy("3.10") == "pypy3.10-v7.3.19"


def test_pypy_python_lines_picks_newest_build_per_line(mocker: pytest.MockFixture) -> None:
    mocker.patch.object(bump_version, "_http_get_text", return_value=_pypy_versions_payload())

    lines = bump_version._pypy_python_lines()

    assert lines["3.11"] == "pypy3.11-v8.0.0"  # 8.0.0 beats 7.3.23
    assert lines["3.12"] == "pypy3.12-v8.0.0"
    assert "3.99" not in lines  # unstable release excluded


def test_new_line_identifiers_for_pypy() -> None:
    version = "pypy3.12-v8.0.0"

    assert bump_version._new_line_id("portable-pypy", version) == "pypy312"
    assert bump_version._new_display_name("portable-pypy", version) == "PyPy 3.12"
    assert bump_version._line_for("portable-pypy", version) == "3.12"
    # Sanity: the semver families still behave.
    assert bump_version._new_line_id("go-toolchain", "1.27.0") == "go127"


def test_detect_new_lines_reports_new_pypy_python_line(
    mocker: pytest.MockFixture, fake_root: Path
) -> None:
    """A new Python line upstream must surface as pypy312, not be silently ignored."""
    mocker.patch.object(bump_version, "_http_get_text", return_value=_pypy_versions_payload())
    # Drop the semver checkers so this test never touches the network.
    mocker.patch.dict(bump_version.LATEST_CHECKERS, {}, clear=True)
    _write_manifest(fake_root, "pypy311", "portable-pypy", "pypy3.11-v8.0.0", python_version="3.11")

    new_lines = bump_version.detect_new_lines()

    assert new_lines["portable-pypy"] == {
        "id": "pypy312",
        "line": "3.12",
        "version": "pypy3.12-v8.0.0",
    }


def test_detect_new_lines_does_not_backfill_old_pypy_lines(
    mocker: pytest.MockFixture, fake_root: Path
) -> None:
    """Older upstream lines (2.7, 3.10) must never be proposed."""
    mocker.patch.object(bump_version, "_http_get_text", return_value=_pypy_versions_payload())
    mocker.patch.dict(bump_version.LATEST_CHECKERS, {}, clear=True)
    _write_manifest(fake_root, "pypy312", "portable-pypy", "pypy3.12-v8.0.0", python_version="3.12")

    new_lines = bump_version.detect_new_lines()

    # 3.12 is the newest tracked line, so nothing newer exists -> no proposal.
    assert "portable-pypy" not in new_lines


def test_detect_new_lines_skips_tracked_pypy_line(
    mocker: pytest.MockFixture, fake_root: Path
) -> None:
    mocker.patch.object(bump_version, "_http_get_text", return_value=_pypy_versions_payload())
    mocker.patch.dict(bump_version.LATEST_CHECKERS, {}, clear=True)
    _write_manifest(fake_root, "pypy311", "portable-pypy", "pypy3.11-v8.0.0", python_version="3.11")
    _write_manifest(fake_root, "pypy312", "portable-pypy", "pypy3.12-v8.0.0", python_version="3.12")

    assert "portable-pypy" not in bump_version.detect_new_lines()


def test_add_runtime_line_for_pypy_sets_python_version_and_archive_ext(
    mocker: pytest.MockFixture, fake_root: Path
) -> None:
    """Cloning pypy311 for 3.12 must record the Python line and the .tar.gz extension."""
    _write_manifest(fake_root, "pypy311", "portable-pypy", "pypy3.11-v7.3.23", python_version="3.11")
    (fake_root / "pypy311" / "examples" / "sls").mkdir(parents=True)
    (fake_root / "pypy311" / "examples" / "sls" / "serverless.yml").write_text(
        "service: hello-pypy311\n", encoding="utf-8"
    )
    mocker.patch.object(
        bump_version,
        "fetch_checksums",
        return_value=[
            ("aa11", "pypy3.12-v8.0.0-linux64.tar.gz"),
            ("bb22", "pypy3.12-v8.0.0-aarch64.tar.gz"),
        ],
    )

    new_id = bump_version.add_runtime_line("portable-pypy", "pypy3.12-v8.0.0")

    assert new_id == "pypy312"
    manifest = json.loads((fake_root / "pypy312" / "runtime.json").read_text(encoding="utf-8"))
    assert manifest["runtime_id"] == "pypy312"
    assert manifest["distribution_version"] == "pypy3.12-v8.0.0"
    assert manifest["python_version"] == "3.12"
    assert manifest["version_line"] == "3.12"
    assert manifest["display_name"] == "PyPy 3.12"
    assert manifest["archive_ext"] == ".tar.gz"
    assert "hello-pypy312" in (fake_root / "pypy312" / "examples" / "sls" / "serverless.yml").read_text(
        encoding="utf-8"
    )


# --- Roll-off / matrix bounding ------------------------------------------------


def test_plan_roll_off_keeps_newest_lines_per_family(fake_root: Path) -> None:
    _write_manifest(fake_root, "bun13", "bun", "1.3.14", version_line="1.3")
    _write_manifest(fake_root, "bun14", "bun", "1.4.2", version_line="1.4")
    _write_manifest(fake_root, "rust194", "rust-musl", "1.94.0", version_line="1.94")
    _write_manifest(fake_root, "rust198", "rust-musl", "1.98.0", version_line="1.98")
    _write_manifest(fake_root, "rust199", "rust-musl", "1.99.0", version_line="1.99")

    plan = bump_version.plan_roll_off(keep=2)

    # bun has only 2 lines -> nothing; rust keeps 1.99+1.98 -> 1.94 rolled off.
    assert set(plan) == {"rust194"}
    assert "1.94" in plan["rust194"]


def test_plan_roll_off_uses_python_version_for_graalpy(fake_root: Path) -> None:
    """GraalPy manifests have no version_line; their Python version is the line."""
    _write_manifest(fake_root, "graalpy312", "graalpy", "25.2.4", python_version="3.12")
    _write_manifest(fake_root, "graalpy313", "graalpy", "25.4.4", python_version="3.13")

    assert set(bump_version.plan_roll_off(keep=2)) == set()
    assert set(bump_version.plan_roll_off(keep=1)) == {"graalpy312"}


def test_plan_roll_off_ignores_already_deprecated(fake_root: Path) -> None:
    _write_manifest(fake_root, "bun13", "bun", "1.3.14", version_line="1.3", deprecated=True)
    _write_manifest(fake_root, "bun14", "bun", "1.4.2", version_line="1.4")

    assert set(bump_version.plan_roll_off(keep=1)) == set()


def test_roll_off_dry_run_does_not_write(fake_root: Path) -> None:
    _write_manifest(fake_root, "bun13", "bun", "1.3.14", version_line="1.3")
    _write_manifest(fake_root, "bun14", "bun", "1.4.2", version_line="1.4")

    plan = bump_version.roll_off(keep=1, apply=False)

    assert set(plan) == {"bun13"}
    raw = json.loads((fake_root / "bun13" / "runtime.json").read_text(encoding="utf-8"))
    assert "deprecated" not in raw


def test_roll_off_apply_marks_and_prune_deletes(fake_root: Path) -> None:
    _write_manifest(fake_root, "bun13", "bun", "1.3.14", version_line="1.3")
    _write_manifest(fake_root, "bun14", "bun", "1.4.2", version_line="1.4")

    bump_version.roll_off(keep=1, apply=True)
    raw = json.loads((fake_root / "bun13" / "runtime.json").read_text(encoding="utf-8"))
    assert raw["deprecated"] is True

    bump_version.roll_off(keep=1, apply=True, prune=True)
    assert not (fake_root / "bun13").exists()
    assert (fake_root / "bun14").exists()


def test_roll_off_refuses_to_prune_last_runtime_in_family(fake_root: Path) -> None:
    """Pruning must never empty a family entirely."""
    _write_manifest(fake_root, "bun13", "bun", "1.3.14", version_line="1.3", deprecated=True)
    _write_manifest(fake_root, "deno29", "deno", "2.9.7", version_line="2.9")

    bump_version.roll_off(keep=1, apply=True, prune=True)

    # bun13 is the only `bun` runtime, so it is kept despite being deprecated.
    assert (fake_root / "bun13").exists()
    assert (fake_root / "deno29").exists()


def test_roll_off_prune_requires_apply(fake_root: Path) -> None:
    _write_manifest(fake_root, "bun13", "bun", "1.3.14", version_line="1.3", deprecated=True)
    _write_manifest(fake_root, "bun14", "bun", "1.4.2", version_line="1.4")

    with pytest.raises(ValueError):
        bump_version.roll_off(keep=1, apply=False, prune=True)

    assert (fake_root / "bun13").exists()


def test_manifest_matrix_excludes_deprecated(fake_root: Path) -> None:
    _write_manifest(fake_root, "bun13", "bun", "1.3.14", version_line="1.3", deprecated=True)
    _write_manifest(fake_root, "bun14", "bun", "1.4.2", version_line="1.4")

    matrix = runtime_manifest.manifest_matrix()
    runtimes = {entry["runtime"] for entry in matrix["include"]}

    assert runtimes == {"bun14"}
    assert runtime_manifest.list_active_runtime_ids() == ["bun14"]
    assert runtime_manifest.list_runtime_ids() == ["bun13", "bun14"]


def test_check_updates_skips_deprecated_unless_named(
    mocker: pytest.MockFixture, fake_root: Path
) -> None:
    _write_manifest(fake_root, "bun13", "bun", "1.3.14", version_line="1.3", deprecated=True)
    _write_manifest(fake_root, "bun14", "bun", "1.4.0", version_line="1.4")
    # check_updates resolves the checker through LATEST_CHECKERS, so patch that.
    monkeypatch_checkers = {"bun": lambda version_line="": {"1.3": "1.3.20", "1.4": "1.4.2"}.get(version_line)}
    mocker.patch.dict(bump_version.LATEST_CHECKERS, monkeypatch_checkers)

    assert "bun13" not in bump_version.check_updates()
    # An explicit request still checks a deprecated runtime.
    assert "bun13" in bump_version.check_updates(["bun13"])