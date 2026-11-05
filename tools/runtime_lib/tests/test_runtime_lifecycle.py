"""Behavior tests for runtime line detection, roll-off, and matrix bounding."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
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
_MATRIX_ACTION = "./.github/actions/runtime-matrix"
_BUILD_ACTION = "./.github/actions/build-and-audit-runtime"


def _workflow(workflow: str) -> dict:
    """A parsed workflow document, by file name."""
    return yaml.safe_load(_workflow_text(workflow))


def _triggers(workflow: str) -> dict:
    """The `on:` block. PyYAML reads a bare `on` key as the boolean True."""
    doc = _workflow(workflow)
    return doc.get("on") or doc.get(True) or {}


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


def _call_site(workflow: str, job: str, action: str) -> dict:
    """The `with:` inputs of a job's call to a repository-local action."""
    steps = _workflow(workflow)["jobs"][job]["steps"]
    return next(step for step in steps if step.get("uses") == action).get("with") or {}


def _workflow_text(workflow: str) -> str:
    return (_REPO_ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")


def _action(action_name: str) -> dict:
    return yaml.safe_load(
        (_REPO_ROOT / ".github" / "actions" / action_name / "action.yml").read_text(encoding="utf-8")
    )


def _named_step(action_name: str, step_name: str) -> dict:
    return next(
        step for step in _action(action_name)["runs"]["steps"] if step.get("name") == step_name
    )


def test_no_workflow_generates_the_matrix_outside_the_action() -> None:
    """A second hand-rolled generator is exactly how a runner label or the
    deprecated-runtime filter drifts away from the rest of the pipeline."""
    for path in sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        assert "runtime_manifest.py matrix" not in path.read_text(encoding="utf-8"), (
            f"{path.name} must take its matrix from the runtime-matrix action"
        )


def test_no_workflow_builds_or_audits_outside_the_shared_action() -> None:
    """The build and the scan are one unit of behaviour in every workflow."""
    for path in sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        assert "make build" not in text, f"{path.name} must build via build-and-audit-runtime"
        assert "bash tools/bin/audit-runtime" not in text, (
            f"{path.name} must scan via build-and-audit-runtime"
        )


def test_every_matrix_job_checks_out_before_generating_the_matrix() -> None:
    """A repository-local action is resolved from the workspace, so it cannot
    check the repo out itself."""
    for workflow in ("ci.yml", "release-runtime.yml", "audit-runtimes.yml"):
        steps = _workflow(workflow)["jobs"]["prepare-matrix"]["steps"]

        assert [step.get("uses") for step in steps] == ["actions/checkout@v7", _MATRIX_ACTION], (
            f"{workflow}:prepare-matrix must check out first, then generate the matrix"
        )
        assert steps[1]["id"] == "matrix", "the job output must read steps.matrix.outputs.matrix"


def test_the_matrix_action_excludes_deprecated_runtimes_by_default() -> None:
    action = _action("runtime-matrix")

    assert action["inputs"]["include-deprecated"]["default"] == "false"
    assert action["outputs"]["matrix"]["value"] == "${{ steps.matrix.outputs.matrix }}"


def test_ci_matrix_audit_step_is_gated_on_the_decision() -> None:
    """The gate and the action output must stay wired to each other."""
    call = _call_site("ci.yml", "runtime-checks", _BUILD_ACTION)

    assert call["audit"] == "${{ steps.build-env.outputs.audit }}"
    assert _named_step("build-and-audit-runtime", "Audit runtime package")["if"] == (
        "inputs.audit == 'true'"
    )


def test_a_release_publishes_only_something_that_was_scanned() -> None:
    """Reusing the cached package is allowed; skipping the scan never is."""
    call = _call_site("release-runtime.yml", "publish", _BUILD_ACTION)

    assert call["audit"] == "true"
    assert call["skip-build"] == "${{ steps.build-env.outputs.package-cache-hit == 'true' }}"


def test_the_scheduled_audit_needs_no_separate_build_step() -> None:
    call = _call_site("audit-runtimes.yml", "audit", _BUILD_ACTION)

    assert call["audit"] == "true"
    assert call["skip-build"] == "true", "audit-runtime builds the layer on demand"


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


def test_the_database_refresh_runs_the_binary_not_a_quoted_command() -> None:
    """`"${GRYPE_INSTALL_DIR}/grype db update"` quotes the arguments into the
    path, so bash looks for a file named "grype db update", the refresh dies
    with ENOENT, and the `if` still reports success — the week's database is
    never written and nothing goes red."""
    run = _action_step("Update vulnerability database")["run"]

    assert '"${GRYPE_INSTALL_DIR}/grype" db update' in run
    assert '"${GRYPE_INSTALL_DIR}/grype db update"' not in run
    assert "::error::" in run, (
        "a refresh that can never succeed must not be a silent warning"
    )


def test_only_the_default_branch_writes_the_shared_database_entry() -> None:
    """A cache entry is only readable outside its own ref when it was created on
    the default branch. A PR run that saves one writes ~458MB that nothing else
    can restore, and evicts the package cache under the 10GB budget."""
    default_branch = "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"

    for name in ("Update vulnerability database", "Save vulnerability database"):
        assert default_branch in _action_step(name)["if"], (
            f"{name} must be limited to the default branch"
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
    # Sourced by build-runtime; a change here that alters the layer layout would
    # otherwise reuse a package built by the old helpers.
    "tools/bin/_common.sh",
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
    call = _call_site("ci.yml", "runtime-checks", _BUILD_ACTION)

    assert call["skip-build"] == "${{ steps.build-env.outputs.package-cache-hit == 'true' }}"
    build = _named_step("build-and-audit-runtime", "Build runtime package")
    assert build["if"] == "inputs.skip-build != 'true'"
    assert build["run"].startswith("timeout "), (
        "a throttled upstream download must fail fast, and a composite step cannot "
        "carry timeout-minutes, so the command bounds itself"
    )


# A composite action's step accepts a much smaller key set than a workflow step.
# GitHub rejects the whole action manifest on an unknown key ("Unexpected value
# 'timeout-minutes'") and the job fails before running anything — which is
# invisible to `bash -n`, to the build, and to actionlint. Keep it static.
_COMPOSITE_STEP_KEYS = {
    "name",
    "id",
    "if",
    "uses",
    "run",
    "shell",
    "env",
    "with",
    "working-directory",
    "continue-on-error",
}


def test_composite_action_steps_only_use_supported_keys() -> None:
    for path in sorted((_REPO_ROOT / ".github" / "actions").glob("*/action.yml")):
        action = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert action["runs"]["using"] == "composite", f"{path.parent.name} must stay composite"

        for step in action["runs"]["steps"]:
            unsupported = sorted(set(step) - _COMPOSITE_STEP_KEYS)
            assert not unsupported, (
                f"{path.parent.name}: step '{step.get('name') or step.get('uses')}' uses "
                f"unsupported key(s) {unsupported}; GitHub will refuse to load the action"
            )
            if "run" in step:
                assert step.get("shell") == "bash", (
                    f"{path.parent.name}: every run step needs shell: bash"
                )


# --- Release trigger and provenance contract ----------------------------------
#
# A release may start from a tag or a dispatch, but the workflow must never push
# its own tag: a tag pushed with GITHUB_TOKEN does not trigger workflows, so
# "tag then publish" and "publish then tag" cannot both work. The tag is the
# input, the OIDC subject must not depend on which entry point was used, and the
# published layer versions have to stay traceable to the commit that produced
# them.

_RELEASE_TAG_PATTERN = "^release-[0-9]{4}\\.[0-9]{2}\\.[0-9]{2}(-[0-9]+)?$"


def test_the_release_can_be_triggered_by_a_release_tag() -> None:
    triggers = _triggers("release-runtime.yml")

    assert triggers["push"]["tags"] == ["release-*"]
    assert "workflow_dispatch" in triggers, "an ad-hoc or partial republish must stay possible"


def test_the_release_never_pushes_its_own_tag() -> None:
    text = _workflow_text("release-runtime.yml")

    assert "git tag" not in text and "git push" not in text, (
        "a tag pushed with GITHUB_TOKEN cannot trigger a workflow; the tag comes from outside"
    )


def test_the_release_validates_the_tag_before_publishing_anything() -> None:
    preflight = _workflow("release-runtime.yml")["jobs"]["preflight"]["steps"]
    step = next(s for s in preflight if s.get("name") == "Validate the release tag")

    assert step["if"] == "github.ref_type == 'tag'"
    assert _RELEASE_TAG_PATTERN in step["run"], "release-YYYY.MM.DD is the contract"


def test_the_release_requires_the_credential_preflight() -> None:
    """A role that cannot be assumed must fail once, not in all 24 cells: the
    credentials action retries for ~51s per cell before it gives up."""
    doc = _workflow("release-runtime.yml")
    preflight = doc["jobs"]["preflight"]

    assert "preflight" in doc["jobs"]["publish"]["needs"]
    assert doc["jobs"]["publish"]["environment"] == "release"

    secret_step = next(
        s for s in preflight["steps"] if s.get("name") == "Validate the release role secret"
    )
    assert secret_step["env"]["AWS_RELEASE_ROLE_ARN"] == "${{ secrets.AWS_RELEASE_ROLE_ARN }}"
    assert "aws-actions/configure-aws-credentials@v6" in [
        s.get("uses") for s in preflight["steps"]
    ]


def test_every_job_that_assumes_the_role_declares_the_environment() -> None:
    """The environment is part of the OIDC subject. Leaving it off a cell would
    make its subject depend on the ref, so a branch dispatch and a tag would
    present different identities to the same role."""
    doc = _workflow("release-runtime.yml")

    for job_name, job in doc["jobs"].items():
        assumes_role = any(
            step.get("uses") == "aws-actions/configure-aws-credentials@v6"
            for step in job.get("steps") or []
        )
        if assumes_role:
            assert job.get("environment") == "release", (
                f"{job_name} must declare the release environment"
            )


def test_the_preflight_states_the_oidc_subject_it_will_present() -> None:
    """Naming the subject is what turns a trust-policy mismatch into a one-line
    diagnosis instead of 'credentials could not be loaded'."""
    assert "repo:${GITHUB_REPOSITORY}:environment:release" in _workflow_text(
        "release-runtime.yml"
    )


def test_the_release_uses_oidc_and_holds_no_static_aws_keys() -> None:
    text = _workflow_text("release-runtime.yml")

    assert "${{ secrets.AWS_RELEASE_ROLE_ARN }}" in text
    assert "aws-access-key-id" not in text and "aws-secret-access-key" not in text
    assert _workflow("release-runtime.yml")["permissions"]["id-token"] == "write"


def test_the_release_can_read_the_commits_ci_conclusion() -> None:
    """The publish gate reads the Actions API, which needs its own scope."""
    permissions = _workflow("release-runtime.yml")["permissions"]

    assert permissions["actions"] == "read"


def test_the_release_records_what_it_published() -> None:
    record = _workflow("release-runtime.yml")["jobs"]["record-release"]

    assert record["needs"] == ["prepare-matrix", "publish"]
    assert record["if"] == (
        "${{ always() && needs.publish.result == 'success' && github.event.inputs.dry_run != 'true' }}"
    )
    # Job-level permissions replace the workflow-level map, so the OIDC scope has
    # to be repeated or the credential step in this job fails.
    assert record["permissions"] == {"contents": "write", "id-token": "write"}


def test_every_job_has_a_timeout() -> None:
    """The default is six hours: a hung download would bill the wall clock."""
    for path in sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        jobs = yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}

        for job_name, job in jobs.items():
            assert job.get("timeout-minutes"), f"{path.name}:{job_name} needs timeout-minutes"


def test_a_release_can_be_dry_run_without_publishing_anything() -> None:
    """The preflight's secret and role checks sit behind the tag gate, so a dry
    run is the only way to reach them without creating a layer version."""
    triggers = _triggers("release-runtime.yml")
    assert "dry_run" in triggers["workflow_dispatch"]["inputs"]

    doc = _workflow("release-runtime.yml")
    by_name = {step.get("name"): step for step in doc["jobs"]["publish"]["steps"]}

    for name in (
        "Upload runtime artifact to S3",
        "Publish Lambda layer",
        "Publish and publicize Lambda layer",
        "Summarize latest layer arns",
    ):
        assert "github.event.inputs.dry_run != 'true'" in by_name[name]["if"], name

    assert by_name["Report the dry run"]["if"] == "${{ github.event.inputs.dry_run == 'true' }}"
    assert "github.event.inputs.dry_run != 'true'" in doc["jobs"]["record-release"]["if"], (
        "a dry run must not create a GitHub Release"
    )


# --- Layer publish contract ----------------------------------------------------
#
# Publishing a layer version is not idempotent, so a re-run of a release used to
# mint another version for bytes that were already published. The content hash
# decides reuse. These tests run the real script against a stubbed aws CLI, so
# they cover the behaviour rather than the text.

_FAKE_AWS = '''#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_AWS_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")

command = args[:2]
if command == ["lambda", "list-layer-versions"]:
    existing = os.environ.get("FAKE_AWS_EXISTING_VERSION", "")
    if existing:
        print(existing)
        sys.exit(0)
    sys.exit(254)  # the CLI's ResourceNotFoundException path
if command == ["lambda", "publish-layer-version"]:
    print("99")
    sys.exit(0)
if command == ["lambda", "add-layer-version-permission"]:
    error = os.environ.get("FAKE_AWS_PERMISSION_ERROR", "")
    if error:
        sys.stderr.write(
            "An error occurred (" + error + ") when calling the "
            "AddLayerVersionPermission operation: " + error + "\\n"
        )
        sys.exit(255)
    sys.exit(0)
sys.exit(2)
'''


def _publish_package_path(tmp_path: Path) -> Path:
    """Where the build would put the package, straight from the manifest CLI."""
    output = subprocess.run(
        [
            sys.executable,
            "tools/runtime_lib/runtime_manifest.py",
            "env",
            "--runtime",
            "pypy311",
            "--arch",
            "x86_64",
        ],
        cwd=_REPO_ROOT,
        env=dict(os.environ, BUILD_ROOT=str(tmp_path)),
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    for line in output.splitlines():
        if line.startswith("PACKAGE_PATH="):
            return Path(shlex.split(line.split("=", 1)[1])[0])

    raise AssertionError("runtime_manifest env did not report PACKAGE_PATH")


def _run_publish_runtime(
    tmp_path: Path,
    *,
    publicize: bool = False,
    existing_version: str = "",
    permission_error: str = "",
) -> tuple[subprocess.CompletedProcess, str]:
    """Run `publish-runtime` with a stubbed aws CLI. Returns (result, aws calls)."""
    package = _publish_package_path(tmp_path)
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_bytes(b"layer bytes\n")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    aws = fake_bin / "aws"
    aws.write_text(_FAKE_AWS, encoding="utf-8")
    aws.chmod(0o755)

    log = tmp_path / "aws-calls.log"
    env = dict(
        os.environ,
        PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        # The package is already on disk; do not let the helper build it.
        RUNTIME_BUILD_IN_PROGRESS="1",
        BUILD_ROOT=str(tmp_path),
        FAKE_AWS_LOG=str(log),
        FAKE_AWS_EXISTING_VERSION=existing_version,
        FAKE_AWS_PERMISSION_ERROR=permission_error,
    )

    args = ["bash", "tools/bin/publish-runtime"]
    if publicize:
        args.append("--publicize")
    args += ["pypy311", "x86_64"]

    result = subprocess.run(args, cwd=_REPO_ROOT, env=env, capture_output=True, text=True)
    return result, log.read_text(encoding="utf-8") if log.exists() else ""


def test_republishing_unchanged_bytes_reuses_the_existing_version(tmp_path: Path) -> None:
    """The layer itself already carries these bytes, so nothing is published."""
    result, calls = _run_publish_runtime(tmp_path, existing_version="7")

    assert result.returncode == 0, result.stderr
    assert "reusing it" in result.stdout
    assert "publish-layer-version" not in calls, (
        "a re-run of a release must not mint another layer version"
    )


def test_a_layer_that_does_not_exist_yet_is_published(tmp_path: Path) -> None:
    """The content-hash lookup fails when the layer has no versions; that is not
    fatal, and the version is published."""
    result, calls = _run_publish_runtime(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "list-layer-versions" in calls
    assert "publish-layer-version" in calls
    assert "Content.CodeSha256" in calls, "reuse must be decided by the content hash"


def test_publicizing_an_already_public_version_is_not_a_failure(tmp_path: Path) -> None:
    result, _ = _run_publish_runtime(
        tmp_path, publicize=True, existing_version="7", permission_error="ResourceConflictException"
    )

    assert result.returncode == 0, result.stderr
    assert "already set" in result.stdout


def test_a_real_permission_error_still_fails(tmp_path: Path) -> None:
    """Only the duplicate-statement conflict is tolerated."""
    result, _ = _run_publish_runtime(
        tmp_path, publicize=True, existing_version="7", permission_error="AccessDeniedException"
    )

    assert result.returncode != 0
    assert "AccessDeniedException" in result.stderr


# --- Documentation contract ----------------------------------------------------
#
# The README's runtime table is written by hand, so it drifts the moment a line is
# added: rust199 was in the build matrix while the README's list still ended at
# rust198. The manifests are the source of truth.


def test_the_readme_lists_every_active_runtime() -> None:
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")

    missing = [
        runtime_id
        for runtime_id in runtime_manifest.list_active_runtime_ids()
        if f"`{runtime_id}`" not in readme
    ]

    assert not missing, f"README.md does not mention {missing}"


def test_the_development_documentation_is_linked_and_present() -> None:
    """The README is the entry point; the build, CI and release half lives next door."""
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    contributing = _REPO_ROOT / "CONTRIBUTING.md"

    assert contributing.exists()
    assert "CONTRIBUTING.md" in readme

    for target in ("make build", "make local-invoke", "roll-off"):
        assert target in contributing.read_text(encoding="utf-8"), (
            f"CONTRIBUTING.md should document {target}"
        )