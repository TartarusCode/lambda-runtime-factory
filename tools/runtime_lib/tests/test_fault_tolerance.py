"""Behavior tests for fault tolerance and PyPy archive resolution in bump_version."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bump_version


def _pypy_versions_payload() -> str:
    """A trimmed versions.json with an out-of-order, mixed-extension release set."""
    return json.dumps(
        [
            {
                "pypy_version": "7.3.23",
                "python_version": "3.11.15",
                "stable": True,
                "files": [
                    {"filename": "pypy3.11-v7.3.23-linux64.tar.bz2", "platform": "linux", "arch": "x64"},
                    {"filename": "pypy3.11-v7.3.23-aarch64.tar.bz2", "platform": "linux", "arch": "aarch64"},
                ],
            },
            {
                "pypy_version": "8.0.0",
                "python_version": "3.12.14",
                "stable": True,
                "files": [
                    {"filename": "pypy3.12-v8.0.0-linux64.tar.gz", "platform": "linux", "arch": "x64"},
                    {"filename": "pypy3.12-v8.0.0-aarch64.tar.gz", "platform": "linux", "arch": "aarch64"},
                    {"filename": "pypy3.12-v8.0.0-macos_arm64.tar.gz", "platform": "darwin", "arch": "arm64"},
                ],
            },
            {
                "pypy_version": "8.0.0",
                "python_version": "3.11.16",
                "stable": True,
                "files": [
                    {"filename": "pypy3.11-v8.0.0-linux64.tar.gz", "platform": "linux", "arch": "x64"},
                    {"filename": "pypy3.11-v8.0.0-aarch64.tar.gz", "platform": "linux", "arch": "aarch64"},
                    {"filename": "pypy3.11-v8.0.0-macos_arm64.tar.gz", "platform": "darwin", "arch": "arm64"},
                ],
            },
            {
                "pypy_version": "9.9.9",
                "python_version": "3.11.99",
                "stable": False,
                "files": [
                    {"filename": "pypy3.11-v9.9.9-linux64.tar.gz", "platform": "linux", "arch": "x64"},
                ],
            },
        ]
    )


def test_check_latest_pypy_picks_numeric_max_and_ignores_unstable(
    mocker: pytest.MockFixture,
) -> None:
    """Ordering must not matter, and unstable/prerelease entries must be skipped."""
    mocker.patch.object(bump_version, "_http_get_text", return_value=_pypy_versions_payload())

    assert bump_version.check_latest_pypy() == "pypy3.11-v8.0.0"


def test_resolve_pypy_archive_name_uses_upstream_filename(
    mocker: pytest.MockFixture,
) -> None:
    """The archive extension is read from versions.json, not a hardcoded template."""
    mocker.patch.object(bump_version, "_http_get_text", return_value=_pypy_versions_payload())

    assert (
        bump_version.resolve_archive_name("portable-pypy", "pypy3.11-v8.0.0", "x86_64")
        == "pypy3.11-v8.0.0-linux64.tar.gz"
    )
    assert (
        bump_version.resolve_archive_name("portable-pypy", "pypy3.11-v8.0.0", "arm64")
        == "pypy3.11-v8.0.0-aarch64.tar.gz"
    )
    # Older release still resolves to its own extension.
    assert (
        bump_version.resolve_archive_name("portable-pypy", "pypy3.11-v7.3.23", "x86_64")
        == "pypy3.11-v7.3.23-linux64.tar.bz2"
    )
    # Python version is taken from the distribution string, so the 3.12 build is
    # never selected for the 3.11 runtime.
    assert "3.11" in bump_version.resolve_archive_name(
        "portable-pypy", "pypy3.11-v8.0.0", "x86_64"
    )


def test_resolve_pypy_archive_name_missing_arch_raises(
    mocker: pytest.MockFixture,
) -> None:
    """An unknown release must fail loudly rather than producing a guessed name."""
    mocker.patch.object(bump_version, "_http_get_text", return_value=_pypy_versions_payload())

    with pytest.raises(ValueError):
        bump_version.resolve_archive_name("portable-pypy", "pypy3.11-v6.0.0", "x86_64")


def _write_pypy_manifest(root: Path, version: str) -> None:
    manifest = {
        "runtime_id": "pypy311",
        "runtime_family": "portable-pypy",
        "display_name": "PyPy 3.11",
        "distribution_version": version,
        "artifact": {"checksum_file": "checksums/pypy.sha256"},
        "release": {"regions": ["eu-central-1"]},
    }
    path = root / "pypy311" / "runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def test_bump_runtime_persists_pypy_archive_ext(
    mocker: pytest.MockFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bumping PyPy records the resolved extension so the build uses the real file."""
    import runtime_manifest

    monkeypatch.setattr(runtime_manifest, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(bump_version, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(
        bump_version, "runtime_manifest_path", lambda rid: tmp_path / rid / "runtime.json"
    )
    mocker.patch.object(
        bump_version,
        "fetch_checksums",
        return_value=[
            ("aa11", "pypy3.11-v8.0.0-linux64.tar.gz"),
            ("bb22", "pypy3.11-v8.0.0-aarch64.tar.gz"),
        ],
    )

    _write_pypy_manifest(tmp_path, "pypy3.11-v7.3.23")
    bump_version.bump_runtime("pypy311", "pypy3.11-v8.0.0")

    manifest = json.loads((tmp_path / "pypy311" / "runtime.json").read_text(encoding="utf-8"))
    assert manifest["distribution_version"] == "pypy3.11-v8.0.0"
    assert manifest["archive_ext"] == ".tar.gz"

    checksum = (tmp_path / "pypy311" / "checksums" / "pypy.sha256").read_text(encoding="utf-8")
    assert "pypy3.11-v8.0.0-linux64.tar.gz" in checksum


def test_bump_runtime_failure_leaves_manifest_untouched(
    mocker: pytest.MockFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checksum fetch failure must not write a bumped manifest."""
    import runtime_manifest

    monkeypatch.setattr(runtime_manifest, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(bump_version, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(
        bump_version, "runtime_manifest_path", lambda rid: tmp_path / rid / "runtime.json"
    )
    mocker.patch.object(
        bump_version, "fetch_checksums", side_effect=RuntimeError("HTTP 404 Not Found")
    )

    _write_pypy_manifest(tmp_path, "pypy3.11-v7.3.23")
    with pytest.raises(RuntimeError):
        bump_version.bump_runtime("pypy311", "pypy3.11-v8.0.0")

    manifest = json.loads((tmp_path / "pypy311" / "runtime.json").read_text(encoding="utf-8"))
    assert manifest["distribution_version"] == "pypy3.11-v7.3.23"
    assert "archive_ext" not in manifest
    assert not (tmp_path / "pypy311" / "checksums" / "pypy.sha256").exists()


def test_bump_latest_all_isolates_per_runtime_failures(
    mocker: pytest.MockFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failing runtime must not stop the others, and failures are reported."""
    monkeypatch.setattr(bump_version, "check_and_report_new_lines", lambda runtime_ids=None: (
        {
            "bun14": ("1.4.0", "1.4.2"),
            "pypy311": ("pypy3.11-v7.3.23", "pypy3.11-v8.0.0"),
            "go126": ("1.26.7", "1.26.8"),
        },
        {},
    ))

    attempted: list[str] = []

    def fake_bump(runtime_id: str, version: str, *, dry_run: bool = False) -> None:
        attempted.append(runtime_id)
        if runtime_id == "pypy311":
            raise RuntimeError("HTTP 404 Not Found")

    mocker.patch.object(bump_version, "bump_runtime", side_effect=fake_bump)

    failures = bump_version.bump_latest_all()

    assert attempted == ["bun14", "pypy311", "go126"]
    assert failures == [("pypy311", "RuntimeError: HTTP 404 Not Found")]


def test_add_runtime_line_failure_leaves_no_partial_directory(
    mocker: pytest.MockFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checksum failure during a new-line add must not leave a cloned directory."""
    import runtime_manifest

    monkeypatch.setattr(runtime_manifest, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(bump_version, "runtimes_root", lambda: tmp_path)

    source = tmp_path / "bun13"
    source.mkdir(parents=True)
    (source / "runtime.json").write_text(
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
    monkeypatch.setattr(bump_version, "list_runtime_ids", lambda: ["bun13"])

    mocker.patch.object(
        bump_version, "fetch_checksums", side_effect=RuntimeError("upstream unavailable")
    )

    with pytest.raises(RuntimeError):
        bump_version.add_runtime_line("bun", "1.4.0")

    assert not (tmp_path / "bun14").exists()


def test_github_releases_sends_token_and_paginates(
    mocker: pytest.MockFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Releases are fetched with auth (when available) and paginated past page 1."""
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    full_page = [{"tag_name": f"bun-v1.4.{i}"} for i in range(100)]
    short_page = [{"tag_name": "bun-v1.3.14"}]

    seen: list[tuple[str, dict]] = []

    def fake_get(url, *, accept="*/*", headers=None):
        seen.append((url, headers or {}))
        payload = full_page if url.endswith("page=1") else short_page
        return json.dumps(payload).encode("utf-8")

    mocker.patch.object(bump_version, "_http_get", side_effect=fake_get)

    releases = bump_version._github_releases("oven-sh/bun")

    assert len(releases) == 101
    assert len(seen) == 2
    assert all(h.get("Authorization") == "Bearer secret-token" for _, h in seen)


def test_github_releases_without_token_has_no_auth_header(
    mocker: pytest.MockFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    seen: list[dict] = []

    def fake_get(url, *, accept="*/*", headers=None):
        seen.append(headers or {})
        return json.dumps([]).encode("utf-8")

    mocker.patch.object(bump_version, "_http_get", side_effect=fake_get)

    bump_version._github_releases("oven-sh/bun")

    assert seen and "Authorization" not in seen[0]


def _graalpy_releases_payload() -> bytes:
    """Releases where the tag (graal-25.3.4) differs from the asset version (25.3.4.1)."""
    return json.dumps(
        [
            {
                "tag_name": "graal-25.4.4",
                "assets": [
                    {"name": "graalpy3.13-25.4.4-linux-amd64.tar.gz"},
                    {"name": "graalpy3.13-25.4.4-linux-aarch64.tar.gz"},
                    {"name": "graalpy3.13-community-25.4.4-linux-amd64.tar.gz"},
                ],
            },
            {
                "tag_name": "graal-25.3.4",
                "assets": [
                    {"name": "graalpy3.13-25.3.4.1-linux-amd64.tar.gz"},
                    {"name": "graalpy3.13-25.3.4.1-linux-aarch64.tar.gz"},
                ],
            },
            {
                "tag_name": "graal-25.2.4",
                "assets": [
                    {"name": "graalpy3.12-25.2.4-linux-amd64.tar.gz"},
                    {"name": "graalpy3.12-25.2.4-linux-aarch64.tar.gz"},
                ],
            },
        ]
    ).encode("utf-8")


def test_graalpy_asset_version_ignores_community_and_jvm_assets() -> None:
    names = [
        "graalpy3.13-25.4.4-linux-amd64.tar.gz",
        "graalpy3.13-community-25.4.4-linux-amd64.tar.gz",
        "graalpy3.13-jvm-25.4.4-linux-amd64.tar.gz",
        "graalpy3.13-25.4.4-windows-amd64.zip",
    ]
    assert bump_version._graalpy_asset_version(names, "3.13") == "25.4.4"
    assert bump_version._graalpy_asset_version(["README.md"], "3.13") is None


def test_resolve_graalpy_archive_name_and_tag(mocker: pytest.MockFixture) -> None:
    """The asset name comes from the index and the tag is resolved separately."""
    mocker.patch.object(bump_version, "_http_get", return_value=_graalpy_releases_payload())

    name = bump_version.resolve_archive_name("graalpy", "25.3.4.1", "x86_64", python_version="3.13")
    assert name == "graalpy3.13-25.3.4.1-linux-amd64.tar.gz"
    # The tag differs from the distribution version.
    assert bump_version._resolve_graalpy_release_tag("25.3.4.1", name) == "25.3.4"


def test_check_latest_graalpy_uses_asset_version_not_tag(mocker: pytest.MockFixture) -> None:
    """Tag graal-25.3.4 must report distribution version 25.3.4.1, and 25.4.4 as latest."""
    mocker.patch.object(bump_version, "_http_get", return_value=_graalpy_releases_payload())

    assert bump_version.check_latest_graalpy("3.13") == "25.4.4"
    assert bump_version.check_latest_graalpy("3.12") == "25.2.4"


def test_graalpy_missing_release_tag_raises(mocker: pytest.MockFixture) -> None:
    mocker.patch.object(bump_version, "_http_get", return_value=_graalpy_releases_payload())

    with pytest.raises(ValueError):
        bump_version._resolve_graalpy_release_tag("9.9.9", "graalpy3.13-9.9.9-linux-amd64.tar.gz")


def _write_graalpy_manifest(root: Path, version: str, release_tag: str | None = None) -> None:
    manifest = {
        "runtime_id": "graalpy313",
        "runtime_family": "graalpy",
        "display_name": "GraalPy 3.13",
        "distribution_version": version,
        "python_version": "3.13",
        "artifact": {"checksum_file": "checksums/graalpy.sha256"},
        "release": {"regions": ["eu-central-1"]},
    }
    if release_tag is not None:
        manifest["release_tag"] = release_tag
    path = root / "graalpy313" / "runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _graalpy_bump_fixture(
    mocker: pytest.MockFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import runtime_manifest

    monkeypatch.setattr(runtime_manifest, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(bump_version, "runtimes_root", lambda: tmp_path)
    monkeypatch.setattr(
        bump_version, "runtime_manifest_path", lambda rid: tmp_path / rid / "runtime.json"
    )
    mocker.patch.object(
        bump_version,
        "fetch_checksums",
        return_value=[
            ("aa11", "graalpy3.13-25.3.4.1-linux-amd64.tar.gz"),
            ("bb22", "graalpy3.13-25.3.4.1-linux-aarch64.tar.gz"),
        ],
    )


def test_bump_runtime_persists_graalpy_release_tag(
    mocker: pytest.MockFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tag that differs from the version must be recorded so the build URL works."""
    _graalpy_bump_fixture(mocker, tmp_path, monkeypatch)
    monkeypatch.setattr(
        bump_version, "RELEASE_TAG_RESOLVERS", {"graalpy": lambda version, archive: "25.3.4"}
    )

    _write_graalpy_manifest(tmp_path, "25.2.4")
    bump_version.bump_runtime("graalpy313", "25.3.4.1")

    manifest = json.loads((tmp_path / "graalpy313" / "runtime.json").read_text(encoding="utf-8"))
    assert manifest["distribution_version"] == "25.3.4.1"
    assert manifest["release_tag"] == "25.3.4"


def test_bump_runtime_drops_stale_graalpy_release_tag(
    mocker: pytest.MockFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the tag equals the version, a previously recorded tag must be removed."""
    _graalpy_bump_fixture(mocker, tmp_path, monkeypatch)
    monkeypatch.setattr(
        bump_version, "RELEASE_TAG_RESOLVERS", {"graalpy": lambda version, archive: version}
    )

    _write_graalpy_manifest(tmp_path, "25.3.4.1", release_tag="25.3.4")
    bump_version.bump_runtime("graalpy313", "25.4.4")

    manifest = json.loads((tmp_path / "graalpy313" / "runtime.json").read_text(encoding="utf-8"))
    assert manifest["distribution_version"] == "25.4.4"
    assert "release_tag" not in manifest
