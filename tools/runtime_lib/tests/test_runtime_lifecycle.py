"""Behavior tests for runtime line detection, roll-off, and matrix bounding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

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


def test_validate_archive_urls_reports_only_missing(
    mocker: pytest.MockFixture, fake_root: Path
) -> None:
    """404/410 are failures; transient errors and 200s are not."""
    _write_manifest(fake_root, "bun13", "bun", "1.3.14", version_line="1.3")
    _write_manifest(fake_root, "bun14", "bun", "1.4.2", version_line="1.4")

    mocker.patch.object(
        runtime_manifest,
        "_head_status",
        side_effect=lambda url: 404 if "1.3.14" in url else (None if "1.4.2" in url else 200),
    )

    problems = runtime_manifest.validate_archive_urls(["bun13", "bun14"])

    # One problem per architecture for the missing runtime; the 200/None ones pass.
    assert len(problems) == 2
    assert all("bun13/" in problem and "404" in problem for problem in problems)
    assert {problem.split("/")[1].split(":")[0] for problem in problems} == {"x86_64", "arm64"}


def test_validate_archive_urls_skips_deprecated_by_default(
    mocker: pytest.MockFixture, fake_root: Path
) -> None:
    _write_manifest(fake_root, "bun13", "bun", "1.3.14", version_line="1.3", deprecated=True)
    _write_manifest(fake_root, "bun14", "bun", "1.4.2", version_line="1.4")
    mocker.patch.object(runtime_manifest, "_head_status", return_value=200)

    # Deprecated runtimes are not built, so they must not red the online check.
    assert runtime_manifest.validate_archive_urls() == []


def test_manifest_matrix_filters_runtime_and_arch(fake_root: Path) -> None:
    _write_manifest(fake_root, "bun13", "bun", "1.3.14", version_line="1.3")
    _write_manifest(fake_root, "deno29", "deno", "2.9.7", version_line="2.9")

    matrix = runtime_manifest.manifest_matrix(["deno29"], ["arm64"])
    entries = matrix["include"]

    assert len(entries) == 1
    assert entries[0]["runtime"] == "deno29" and entries[0]["arch"] == "arm64"


def test_manifest_matrix_can_include_deprecated(fake_root: Path) -> None:
    _write_manifest(fake_root, "bun13", "bun", "1.3.14", version_line="1.3", deprecated=True)

    assert runtime_manifest.manifest_matrix()["include"] == []
    assert len(runtime_manifest.manifest_matrix(include_deprecated=True)["include"]) == 2


def test_runner_labels_are_pinned_not_floating() -> None:
    """`ubuntu-latest` migrates OS releases on GitHub's schedule; never float."""
    labels = set(runtime_manifest.ARCH_RUNNERS.values()) | {runtime_manifest.DEFAULT_RUNNER}

    assert labels, "runner labels must be defined"
    assert all(label.startswith("ubuntu-") for label in labels)
    assert not any("latest" in label for label in labels)


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


# --- CI gating contract --------------------------------------------------------
#
# The Grype scan is skipped for cells whose pinned payload did not change, so the
# call sites that MUST always scan cannot rely on a default. These tests encode
# that invariant so it cannot be lost by editing a workflow.

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUILD_ENV_ACTION = "./.github/actions/runtime-build-env"

# (workflow file, job) pairs whose entire purpose is the scan.
_AUDIT_CRITICAL = {
    ("ci.yml", "grype-db"),
    ("release-runtime.yml", "publish"),
    ("audit-runtimes.yml", "audit"),
}


def _build_env_call_sites() -> dict[tuple[str, str], dict]:
    """Map every (workflow, job) that uses the build-env action to its inputs."""
    sites: dict[tuple[str, str], dict] = {}
    for path in sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_name, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if step.get("uses") == _BUILD_ENV_ACTION:
                    sites[(path.name, job_name)] = step.get("with") or {}
    return sites


def test_build_env_defaults_to_the_cheap_path() -> None:
    """The optimization must not be opt-in via a default that pays 2.8GB."""
    action = yaml.safe_load(
        (_REPO_ROOT / ".github/actions/runtime-build-env/action.yml").read_text(encoding="utf-8")
    )

    assert action["inputs"]["grype"]["default"] == "when-changed"


def test_every_build_env_call_site_declares_grype_explicitly() -> None:
    sites = _build_env_call_sites()

    assert sites, "no workflow uses the build-env action"
    for site, inputs in sites.items():
        assert inputs.get("grype") in {"always", "when-changed"}, (
            f"{site[0]}:{site[1]} must pass `grype:` explicitly"
        )


def test_audit_critical_jobs_always_prepare_grype() -> None:
    sites = _build_env_call_sites()

    for site in sorted(_AUDIT_CRITICAL):
        assert sites.get(site, {}).get("grype") == "always", (
            f"{site[0]}:{site[1]} must always prepare Grype"
        )


def test_ci_matrix_audit_step_is_gated_on_the_decision() -> None:
    """The gate and the action output must stay wired to each other."""
    doc = yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = doc["jobs"]["runtime-checks"]["steps"]

    call = next(step for step in steps if step.get("uses") == _BUILD_ENV_ACTION)
    audit = next(step for step in steps if step.get("name") == "Audit runtime package")

    assert call.get("id") == "build-env"
    assert audit.get("if") == "steps.build-env.outputs.audit == 'true'"


# --- Grype database cache contract --------------------------------------------
#
# actions/cache entries are immutable: a restore whose key already exists is never
# saved back ("Cache hit occurred on the primary key ..., not saving cache"). Two
# invariants follow, and these tests keep both from being lost by editing a
# workflow:
#   1. only the job that refreshed the database may write the week's entry, or the
#      first restorer would freeze the previous week's database under the new key;
#   2. the refresh is skipped when the entry exists, because it could only be
#      discarded at the end of the job.

_BUILD_ENV_ACTION_PATH = _REPO_ROOT / ".github" / "actions" / "runtime-build-env" / "action.yml"
_DB_CACHE_PATH = "~/.cache/grype"

# (workflow, job that refreshes the database, job that consumes the entry)
_DATABASE_PRIMERS = {
    "ci.yml": ("grype-db", "runtime-checks"),
    "audit-runtimes.yml": ("grype-db", "audit"),
    "release-runtime.yml": ("grype-db", "publish"),
}


def _build_env_action() -> dict:
    return yaml.safe_load(_BUILD_ENV_ACTION_PATH.read_text(encoding="utf-8"))


def _action_step(name: str) -> dict:
    return next(
        step for step in _build_env_action()["runs"]["steps"] if step.get("name") == name
    )


def test_the_database_cache_is_only_written_by_the_refresher() -> None:
    steps = [s for s in _build_env_action()["runs"]["steps"] if (s.get("with") or {}).get("path") == _DB_CACHE_PATH]

    assert [s["name"] for s in steps] == ["Restore Grype database", "Save vulnerability database"], (
        "the database cache must be split into a restore and an explicit save; "
        "the combined action saves whatever ran first"
    )
    assert steps[0]["uses"] == "actions/cache/restore@v6"
    assert steps[1]["uses"] == "actions/cache/save@v6"


def test_the_database_refresh_is_skipped_when_the_week_is_already_cached() -> None:
    """An existing key is never re-saved, so a refresh on a hit is pure waste."""
    for name in ("Update vulnerability database", "Save vulnerability database"):
        condition = _action_step(name)["if"]
        for clause in ("inputs.db-refresh == 'true'", "steps.dbcache.outputs.cache-hit != 'true'"):
            assert clause in condition, f"{name} must gate on {clause}"

    assert "steps.dbupdate.outputs.refreshed == 'true'" in _action_step("Save vulnerability database")["if"], (
        "a failed refresh must not be cached: it would freeze the restored database "
        "under the new week's key"
    )


def test_every_build_env_call_site_declares_its_database_role_explicitly() -> None:
    sites = _build_env_call_sites()

    for site, inputs in sites.items():
        assert inputs.get("db-refresh") in {"true", "false"}, (
            f"{site[0]}:{site[1]} must pass `db-refresh:` explicitly"
        )


def test_one_database_refresher_per_scanning_workflow() -> None:
    sites = _build_env_call_sites()
    refreshers = {site for site, inputs in sites.items() if inputs.get("db-refresh") == "true"}

    assert refreshers == {(workflow, primer) for workflow, (primer, _) in _DATABASE_PRIMERS.items()}

    for workflow, (primer, scanner) in _DATABASE_PRIMERS.items():
        jobs = yaml.safe_load(
            (_REPO_ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
        )["jobs"]
        needs = jobs[scanner].get("needs")
        needs = needs if isinstance(needs, list) else [needs]

        assert primer in needs, f"{workflow}: {scanner} must wait for the database refresher"


def test_no_workflow_refreshes_the_database_outside_the_action() -> None:
    """The update must stay next to the cache gate that decides whether to save it."""
    for path in sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        assert "grype db update" not in path.read_text(encoding="utf-8"), (
            f"{path.name} must let the build-env action gate the database refresh"
        )


# --- Built-package cache contract ---------------------------------------------
#
# The package cache may only ever hold the package this build would produce, so its
# key covers every build input and it must not fall back to a prefix match: a stale
# package restored from another revision would ship without a rebuild.

_PACKAGE_INPUTS = (
    "runtime.json",
    "checksums/**",
    "bootstrap/**",
    "helpers/**",
    "tools/bin/build-runtime",
    "tools/runtime_lib/runtime_manifest.py",
)


def test_the_package_cache_is_keyed_on_every_build_input() -> None:
    step = _action_step("Cache built runtime package")
    key = step["with"]["key"]

    assert step["uses"] == "actions/cache@v6"
    assert step["with"]["path"].endswith("/artifacts"), step["with"]["path"]
    for build_input in _PACKAGE_INPUTS:
        assert build_input in key, f"the package cache key must cover {build_input}"
    assert step["with"].get("restore-keys") is None, (
        "a package built from other inputs must never be restored"
    )

    assert _build_env_action()["outputs"]["package-cache-hit"]["value"] == (
        "${{ steps.package.outputs.cache-hit }}"
    )


def test_the_sam_image_is_warmed_at_the_tag_sam_resolves() -> None:
    """Bare `:latest` is a multi-arch list and a different image from the arch tag
    SAM pulls, so warming it only fetches layers SAM never uses."""
    doc = yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = doc["jobs"]["runtime-checks"]["steps"]

    warm = next(step for step in steps if step.get("name") == "Warm SAM build image")

    assert "public.ecr.aws/sam/build-provided.al2023:latest-${{ matrix.arch }}" in warm["run"]


def test_local_invoke_asserts_the_handler_answered() -> None:
    """A green verify step must mean the handler ran, not just that no known failure
    string appeared, and it must not rebuild what the previous step built."""
    script = (_REPO_ROOT / "tools" / "bin" / "local-invoke-runtime").read_text(encoding="utf-8")

    assert "|| true" not in script, "the sam local invoke exit status must not be discarded"
    assert "invoke_status" in script, "the exit status must be checked"
    assert "statusCode" in script and "hello world" in script, (
        "the response must be asserted, not only the absence of failure text"
    )
    assert "LOCAL_BUILD_TEMPLATE" in script, (
        "the SAM build must be skipped when `make local-build` already produced it"
    )


def test_the_ci_build_is_skipped_when_the_package_is_cached() -> None:
    doc = yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = doc["jobs"]["runtime-checks"]["steps"]

    hold = next(step for step in steps if step.get("uses") == _BUILD_ENV_ACTION)
    build = next(step for step in steps if step.get("name") == "Build runtime package")

    assert hold.get("id") == "build-env"
    assert build.get("if") == "steps.build-env.outputs.package-cache-hit != 'true'"