"""Behavior tests for runtime line detection, roll-off, and matrix bounding."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

import bump_version
import container_image
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
    ("release-runtime.yml", "publish-containers"),
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


def test_build_env_installs_the_scan_tooling_in_every_cell() -> None:
    """The image scan is part of the bundle, so there is no cached-payload path
    that skips Grype: an artifact with no SBOM is not a publishable artifact."""
    action = yaml.safe_load(
        (_REPO_ROOT / ".github/actions/runtime-build-env/action.yml").read_text(encoding="utf-8")
    )

    assert "grype" not in action["inputs"], "there is nothing to switch off"
    install = next(
        step for step in action["runs"]["steps"] if step.get("name") == "Install Grype"
    )
    assert "if" not in install, "Grype is installed unconditionally"


def test_every_build_env_call_site_leaves_the_scan_enabled() -> None:
    sites = _build_env_call_sites()

    assert sites, "no workflow uses the build-env action"
    for site, inputs in sites.items():
        assert "grype" not in inputs, f"{site[0]}:{site[1]} must not gate the scan"




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

        assert steps[0].get("uses") == "actions/checkout@v7", (
            f"{workflow}:prepare-matrix must check out before any local action"
        )
        assert steps[1].get("uses") == _MATRIX_ACTION
        assert steps[1]["id"] == "matrix", "the job output must read steps.matrix.outputs.matrix"
        # Every following matrix generated in the same job must use the same
        # generator: a hand-rolled second matrix is how a runner label or the
        # publish filter drifts.
        for step in steps[2:]:
            assert step.get("uses") == _MATRIX_ACTION, (
                f"{workflow}:prepare-matrix may only add matrix steps from the shared action"
            )
            assert step.get("id"), "each extra matrix needs an id for its job output"


def test_the_matrix_action_excludes_deprecated_runtimes_by_default() -> None:
    action = _action("runtime-matrix")

    assert action["inputs"]["include-deprecated"]["default"] == "false"
    assert action["outputs"]["matrix"]["value"] == "${{ steps.matrix.outputs.matrix }}"


def test_ci_matrix_gates_the_compliance_step_on_the_container_flag() -> None:
    """Nothing selects the scan separately any more: a cell that builds the image
    always generates its bundle, which is what makes the artifact reviewable."""
    call = _call_site("ci.yml", "runtime-checks", _BUILD_ACTION)

    assert call["container"] == "true"
    assert "audit" not in call, "the scan has no separate switch"
    assert _named_step("build-and-audit-runtime", "Generate the compliance bundle")["if"] == (
        "inputs.container == 'true'"
    )




def test_the_scheduled_audit_runs_the_same_sequence_as_a_pr() -> None:
    call = _call_site("audit-runtimes.yml", "audit", _BUILD_ACTION)

    assert call["container"] == "true", "the image and its bundle are the output"
    assert "skip-build" not in call, "the payload is staged before the image builds"


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
    "release-runtime.yml": ("grype-db", "publish-containers"),
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
    can restore, and evicts the download and tool caches under the 10GB budget."""
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


# --- Cache contract ------------------------------------------------------------
#
# Every cell runs the build, so nothing caches the built payload: a restored
# artifacts/ directory would be overwritten at best and leave a stale file behind
# at worst, now that no cache-hit signal reaches the build step. What stays cached
# is the expensive, input-independent part — the upstream downloads and the scan
# tooling — plus the Buildx layer cache in the build action.

_DOWNLOAD_INPUTS = (
    # A version bump rewrites both, so a key without them would serve the previous
    # release's tarball to a build pinned to the new one.
    "runtimes/{0}/runtime.json",
    "runtimes/{0}/checksums/**",
)

_BUILT_ARTIFACT_CACHE = "Cache built runtime package"


def test_nothing_caches_the_built_payload() -> None:
    step = _action_step("Cache runtime downloads")

    assert step["uses"] == "actions/cache@v6"
    assert step["with"]["path"] == "${{ env.DOWNLOAD_CACHE_DIR }}"
    for source in _DOWNLOAD_INPUTS:
        assert source in step["with"]["key"], f"the download key must cover {source}"
    assert step["with"].get("restore-keys") is None, (
        "a tarball fetched for another pin must never be reused"
    )

    action = _build_env_action()
    assert "outputs" not in action, "no cache-hit signal is published"
    assert all(s.get("name") != _BUILT_ARTIFACT_CACHE for s in action["runs"]["steps"]), (
        "the built payload is produced fresh in every cell"
    )
    assert "artifacts" not in yaml.safe_dump(action["runs"]["steps"]), (
        "no cache step may restore the built payload"
    )






def test_the_build_sequence_is_uniform_across_every_call_site() -> None:
    """`build-runtime` always runs first: `build-container` consumes the staged
    payload and stages it itself when absent, so a "skip the build" switch would
    only move that work one step later while pretending the cell was cheaper."""
    action = yaml.safe_load(
        (_REPO_ROOT / ".github/actions/build-and-audit-runtime/action.yml").read_text(encoding="utf-8")
    )
    assert "skip-build" not in action["inputs"]

    call = _call_site("ci.yml", "runtime-checks", _BUILD_ACTION)
    assert "skip-build" not in call

    build = _named_step("build-and-audit-runtime", "Build runtime package")
    assert "if" not in build, "no cell skips the build step"
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
    """A role that cannot be assumed must fail once, not in every cell: the
    credentials action retries for ~51s per cell before it gives up."""
    doc = _workflow("release-runtime.yml")
    preflight = doc["jobs"]["preflight"]

    assert "preflight" in doc["jobs"]["publish-containers"]["needs"]
    assert doc["jobs"]["publish-containers"]["environment"] == "release"

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




def test_the_release_record_names_the_container_images() -> None:
    """A record that listed only the layers would omit the toolchain runtimes."""
    steps = _workflow("release-runtime.yml")["jobs"]["record-release"]["steps"]
    by_name = {step.get("name"): step for step in steps}

    images = by_name["Collect the published container images"]
    # Naming an image the merge job never pushed is worse than omitting it.
    assert images["if"] == "${{ needs.merge-containers.result == 'success' }}"
    assert "container-matrix" in images["env"]["MATRIX"]
    assert "CONTAINER_IMAGE" in images["run"] and "CONTAINER_TAG" in images["run"]
    # The notes are attached to the tag, so the image list has to land in the
    # same file the attach step reads.
    assert "release-notes.md" in images["run"]


def test_every_job_has_a_timeout() -> None:
    """The default is six hours: a hung download would bill the wall clock."""
    for path in sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        jobs = yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}

        for job_name, job in jobs.items():
            assert job.get("timeout-minutes"), f"{path.name}:{job_name} needs timeout-minutes"




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


# --- Manifest naming convention -------------------------------------------------
#
# `display_name` is what the published layer description is built from
# ("{display_name} Lambda Runtime"), so a stale one ships to AWS. bun13, go126 and
# rust194 still read "Bun 1.x", "Go 1.x" and "Rust 1.x" while every line added since
# is named after its version line by _new_display_name().

_FAMILY_LABELS = {
    "bun": "Bun",
    "deno": "Deno",
    "go-toolchain": "Go",
    "rust-musl": "Rust",
    "portable-pypy": "PyPy",
    "graalpy": "GraalPy",
}


def test_display_names_follow_the_family_and_line_convention() -> None:
    for runtime_id in runtime_manifest.list_runtime_ids():
        manifest = json.loads(
            runtime_manifest.runtime_manifest_path(runtime_id).read_text(encoding="utf-8")
        )
        line = manifest.get("version_line") or manifest.get("python_version")
        expected = f"{_FAMILY_LABELS[manifest['runtime_family']]} {line}"

        assert manifest["display_name"] == expected, (
            f"{runtime_id}: display_name is '{manifest['display_name']}', expected '{expected}' — "
            "it becomes the published layer description"
        )


# --- Deployment settings are repo-level ------------------------------------------
#
# Regions and the S3 bucket base name live in REPO_DEFAULTS. A runtime manifest is merged
# last, so a per-runtime copy of either one silently wins over the global value — which is
# exactly what happened: every manifest carried its own region list.


def test_deployment_regions_are_a_repo_level_setting() -> None:
    assert runtime_manifest.REPO_DEFAULTS["release"]["regions"] == ["us-east-1"]

    for runtime_id in runtime_manifest.list_runtime_ids():
        raw = json.loads(
            runtime_manifest.runtime_manifest_path(runtime_id).read_text(encoding="utf-8")
        )
        assert "release" not in raw, (
            f"{runtime_id}: runtime.json must not redeclare release settings — regions and the "
            "bucket base name are repo-level (see REPO_DEFAULTS)"
        )


# --- Release visibility ----------------------------------------------------------
#
# A tag push carries no workflow inputs, so `github.event.inputs.publicize` is empty
# on the one path that must never disagree with the README's "layers are public".
# Comparing it against 'true' selected the private branch silently, and nothing in
# the run said so.




# --- Layer delivery limits -------------------------------------------------------
#
# A deployment package is capped at 250 MB unzipped, and that covers the function
# plus every layer attached to it, so a runtime shipping a whole upstream toolchain
# cannot be delivered as a layer however it is split. Those runtimes are built and
# audited but kept out of the publish matrix. Both halves matter: dropping them from
# CI as well would let their packages rot unnoticed.




def test_the_release_matrix_asks_for_publishable_runtimes_only() -> None:
    steps = yaml.safe_load(_workflow_text("release-runtime.yml"))["jobs"]["prepare-matrix"]["steps"]
    matrix_step = next(step for step in steps if step.get("id") == "matrix")

    # One artifact per runtime: there is no layer-only subset to exclude.
    assert "layer-publishable-only" not in matrix_step["with"]


def test_an_empty_publish_matrix_is_refused_not_silently_green() -> None:
    steps = yaml.safe_load(_workflow_text("release-runtime.yml"))["jobs"]["preflight"]["steps"]
    refuse = next((step for step in steps if step.get("name") == "Refuse an empty publish matrix"), None)

    assert refuse is not None, "a release with nothing to publish must fail, not report success"
    assert "needs.prepare-matrix.outputs.matrix" in refuse["env"]["MATRIX"]
    assert "-eq 0" in refuse["run"]


# --- Deterministic packages ------------------------------------------------------
#
# The zip's SHA-256 is the S3 object key *and* the layer-reuse key
# (Content.CodeSha256). A build that embeds "now" mints a new layer version on every
# release and the reuse path can never fire — which is exactly what happened: the
# same runtime and arch produced aff7a46c… and b0dd53dd… from identical inputs.




# --- Deployment-package budget ---------------------------------------------------
#
# AWS caps a deployment package at 250 MB unzipped, and that ceiling covers the
# function plus every layer attached to it. The build measures the package and
# refuses to call an oversized one publishable; the publish matrix skips what the
# build would refuse. Measured on graalpy312 before slimming: 252.7 MB unzipped —
# 2.7 MB over, which is exactly the failure AWS reported.






def test_layer_slimming_never_prunes_package_metadata() -> None:
    """Version identification reads metadata inside the package, so slimming may
    only prune things it does not read."""
    script = (_REPO_ROOT / "tools" / "bin" / "build-runtime").read_text(encoding="utf-8")
    slim = script.split("slim_layer()", 1)[1].split("\n}\n", 1)[0]

    for metadata in ("dist-info", "egg-info", "METADATA"):
        assert metadata not in slim, (
            f"slimming must not remove {metadata}: that is how a component version is identified"
        )
    assert "SLIM_LAYER" in slim, "the A/B switch is how the before/after byte counts are compared"






def test_the_unpacked_size_is_summed_from_the_rootfs_listing() -> None:
    """The recorded size must not depend on the daemon's image store.

    `docker image inspect .Size` is the compressed size on a containerd store, so
    the same image reads about three times smaller there than on the classic
    overlay2 store. The rootfs export is measured instead, and only regular files
    carry bytes: a directory and a symlink (whose listing ends with its target,
    not with its own path) contribute nothing.
    """
    listing = "\n".join(
        [
            "drwxrwxrwt 0/0 0 2026-01-01 00:00 tmp/",
            "-rw-r--r-- 0/0 1024 2026-01-01 00:00 opt/go/bin/go",
            "lrwxrwxrwx 0/0 0 2026-01-01 00:00 usr/lib/libz.so.1 -> libz.so.1.2.13",
            "-rw-r--r-- 0/0 512 2026-01-01 00:00 usr/lib/libz.so.1.2.13",
        ]
    )

    summary = container_image.parse_rootfs_listing(listing)

    assert summary["unpacked_bytes"] == 1536
    assert summary["entries"]["tmp"] == "drwxrwxrwt"
    assert summary["entries"]["opt/go/bin/go"] == "-rw-r--r--"
    assert summary["entries"]["usr/lib/libz.so.1"] == "lrwxrwxrwx"
    assert summary["entries"]["usr/lib/libz.so.1.2.13"] == "-rw-r--r--"


def test_go_and_rust_get_a_compiled_launcher_not_a_script() -> None:
    script = (_REPO_ROOT / "tools" / "bin" / "build-runtime").read_text(encoding="utf-8")
    launcher = (_REPO_ROOT / "tools" / "launcher" / "bootstrap.c").read_text(encoding="utf-8")

    assert "install_native_bootstrap" in script
    assert "go-toolchain | rust-musl" in script, "the launcher belongs to both shell families"
    assert "execv(" in launcher
    # The compiled launcher must not start with a shebang: that is exactly the
    # thing this replaces.
    assert not launcher.lstrip().startswith("#!")
    assert "RUNTIME_DIR_NAME" in launcher, "the launcher must resolve each runtime's own bin/"


def test_the_javascript_helpers_initialize_the_environment_themselves() -> None:
    """The shell bootstrap is gone in the image, so HOME/DENO_DIR must be set by
    the helper — otherwise Bun or Deno guesses and writes to a read-only home."""
    bun = (
        _REPO_ROOT / "runtimes" / "bun13" / "helpers" / "lambda_runtime_bun" / "runtime.ts"
    ).read_text(encoding="utf-8")
    bun14 = (
        _REPO_ROOT / "runtimes" / "bun14" / "helpers" / "lambda_runtime_bun" / "runtime.ts"
    ).read_text(encoding="utf-8")
    deno = (
        _REPO_ROOT / "runtimes" / "deno29" / "helpers" / "lambda_runtime_deno" / "runtime.ts"
    ).read_text(encoding="utf-8")

    for text in (bun, bun14):
        assert 'process.env.HOME ??= "/tmp"' in text
    assert 'process.env.DENO_DIR ??= "/tmp/.deno"' in deno
    assert 'process.env.HOME ??= "/tmp"' in deno




def test_soci_publishing_is_opt_in_and_needs_no_containerd() -> None:
    """SOCI is a Lambda Managed Instances optimisation, not part of a working
    image, and the conversion has to run without a containerd: `soci create` and
    `soci push` resolve the image through the containerd image service, which a
    runner that builds with Docker does not have. `soci convert --standalone`
    reads an OCI layout instead, and oras moves it."""
    release = _workflow("release-runtime.yml")
    inputs = _triggers("release-runtime.yml")["workflow_dispatch"]["inputs"]
    assert inputs["publish_soci"]["default"] is False

    publish = {step.get("name"): step for step in release["jobs"]["publish-containers"]["steps"]}
    assert "inputs.publish_soci" in publish["Publish the SOCI-enabled copy"]["if"]

    merge = {step.get("name"): step for step in release["jobs"]["merge-containers"]["steps"]}
    assert "--soci" in merge["Create the multi-arch tags"]["run"]

    script = (_REPO_ROOT / "tools" / "bin" / "publish-soci-index").read_text(encoding="utf-8")
    assert "convert --standalone" in script, "standalone mode is what removes the containerd dependency"
    assert "oras" in script
    assert "containerd.sock" not in script and "images import" not in script

    # A SOCI Index Manifest v2 image is immutable: a moving `-soci` tag would let a
    # release change what a function runs, which is exactly what v2 forbids.
    merge_script = (_REPO_ROOT / "tools" / "bin" / "merge-container-tags").read_text(encoding="utf-8")
    assert "latest-soci" not in merge_script


def test_the_ecr_alias_can_be_pointed_at_the_live_one(monkeypatch) -> None:
    """A custom ECR Public alias can sit pending while the account's default alias
    works, and a push into a non-active alias goes nowhere. The manifest names the
    alias the project wants; `ECR_PUBLIC_ALIAS` names the one that is live, so
    flipping it is a repository variable and not a code change."""
    monkeypatch.setenv("ECR_PUBLIC_ALIAS", "q7s5j4e1")
    env = runtime_manifest.runtime_env("go126", "x86_64")
    registries = json.loads(env["CONTAINER_REGISTRIES"])

    assert "public.ecr.aws/q7s5j4e1/lambda-runtime-factory" in registries
    assert not any("tartaruscode" in item for item in registries if item.startswith("public.ecr.aws/"))
    # The primary registry is not ECR and must not be rewritten.
    assert env["CONTAINER_REGISTRY"] == "ghcr.io/tartaruscode/lambda-runtime-factory"

    monkeypatch.setenv("ECR_PUBLIC_ALIAS", "Bad_Alias!")
    with pytest.raises(ValueError, match="not a valid ECR Public alias"):
        runtime_manifest.runtime_env("go126", "x86_64")


def test_every_runtime_publishes_to_the_primary_registry_and_ecr_public(monkeypatch) -> None:
    # The tests must not depend on the caller's shell: ECR_PUBLIC_ALIAS rewrites
    # the alias the release publishes under, and CI sets it.
    monkeypatch.delenv("ECR_PUBLIC_ALIAS", raising=False)
    for runtime_id in runtime_manifest.list_active_runtime_ids():
        env = runtime_manifest.runtime_env(runtime_id, "x86_64")
        registries = json.loads(env["CONTAINER_REGISTRIES"])

        assert env["CONTAINER_REGISTRY"] in registries, runtime_id
        assert env["CONTAINER_IMAGE"].startswith(env["CONTAINER_REGISTRY"] + "/"), runtime_id
        assert (
            f"public.ecr.aws/{runtime_manifest.DEFAULT_ECR_PUBLIC_ALIAS}/lambda-runtime-factory"
            in registries
        ), runtime_id


def test_the_live_ecr_alias_is_the_default_and_the_environment_overrides_it(monkeypatch) -> None:
    """An unset ECR_PUBLIC_ALIAS must resolve to an alias that answers, so a local
    lookup or a script run without the variable finds the published image rather
    than a 404 under the alias the project intends to move to."""
    monkeypatch.delenv("ECR_PUBLIC_ALIAS", raising=False)
    assert runtime_manifest.DEFAULT_ECR_PUBLIC_ALIAS == "q7s5j4e1"
    registries = json.loads(runtime_manifest.runtime_env("pypy311", "x86_64")["CONTAINER_REGISTRIES"])
    assert "public.ecr.aws/q7s5j4e1/lambda-runtime-factory" in registries

    monkeypatch.setenv("ECR_PUBLIC_ALIAS", "anotheralias")
    registries = json.loads(runtime_manifest.runtime_env("pypy311", "x86_64")["CONTAINER_REGISTRIES"])
    assert "public.ecr.aws/anotheralias/lambda-runtime-factory" in registries
    assert not any("q7s5j4e1" in registry for registry in registries), "the override wins"


def test_the_ecr_repository_name_excludes_the_account_alias() -> None:
    """ECR Public's alias is the registry namespace, not part of the repository
    name — creating `tartarusgroup/lambda-runtime-factory/pypy311` would make a
    repository two levels deeper than the image path that pushes to it."""
    # The registry list can be overridden in the environment (that is how the
    # merge and push scripts are pointed at a throwaway registry), so the check
    # runs without it rather than depending on the caller's shell.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("CONTAINER_REGISTRIES_OVERRIDE", "ECR_PUBLIC_ALIAS")
    }
    output = subprocess.run(
        [
            "bash",
            str(_REPO_ROOT / "tools" / "bin" / "ensure-container-repos"),
            "pypy311",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    ).stdout

    assert "(repository name lambda-runtime-factory/pypy311)" in output
    assert (
        f"public.ecr.aws/{runtime_manifest.DEFAULT_ECR_PUBLIC_ALIAS}"
        "/lambda-runtime-factory/pypy311"
        in output
    )


def test_a_container_can_pass_the_handler_as_cmd() -> None:
    """Lambda runs a container image as ENTRYPOINT + CMD, so the handler reaches
    the runtime as an argument when `_HANDLER` is not set. Supporting both is what
    makes `CMD ["app.handler"]` work in a function image."""
    for runtime_id in ("pypy311", "pypy312", "graalpy313"):
        bootstrap = (
            _REPO_ROOT / "runtimes" / runtime_id / "bootstrap" / "bootstrap.py3"
        ).read_text(encoding="utf-8")
        assert "len(sys.argv) > 1" in bootstrap, runtime_id
        assert 'os.environ["_HANDLER"] = handler' in bootstrap, runtime_id

    bun = (
        _REPO_ROOT / "runtimes" / "bun13" / "helpers" / "lambda_runtime_bun" / "runtime.ts"
    ).read_text(encoding="utf-8")
    assert "process.argv.slice(2)[0]" in bun

    deno = (
        _REPO_ROOT / "runtimes" / "deno29" / "helpers" / "lambda_runtime_deno" / "runtime.ts"
    ).read_text(encoding="utf-8")
    assert "Deno.args[0]" in deno

    launcher = (_REPO_ROOT / "tools" / "launcher" / "bootstrap.c").read_text(encoding="utf-8")
    assert "argc > 1" in launcher
    assert 'setenv("_HANDLER", handler, 1)' in launcher


def test_slimming_precompiles_bytecode_and_keeps_the_sbom_metadata() -> None:
    script = (_REPO_ROOT / "tools" / "bin" / "build-runtime").read_text(encoding="utf-8")

    # Bytecode is compiled by the payload's own interpreter, with hash-based
    # invalidation so a stale .pyc can never win over an edited source.
    assert "compileall -b -q -f --invalidation-mode=unchecked-hash" in script
    # ...and it is opt-in: measured on pypy311 it cost 16 MB and 20 ms of init.
    assert "PRECOMPILE_BYTECODE:-0" in script
    # Package metadata is what vulnerability scanning reads; it is documented as
    # never removed and no delete rule targets it.
    assert "*.dist-info" in script
    assert "*dist-info*" not in script.split("slim_layer")[0]
    for line in script.splitlines():
        if "dist-info" in line:
            assert "-delete" not in line and "-exec rm" not in line, line


def test_the_example_dockerfiles_never_run_a_command_in_the_runtime_stage() -> None:
    examples = sorted((_REPO_ROOT / "examples" / "docker").glob("*/Dockerfile"))
    assert examples, "the consumer recipes are part of the deliverable"

    for path in examples:
        text = path.read_text(encoding="utf-8")
        assert ("CMD [" in text) or ("ENTRYPOINT [" in text), path
        # The runtime stage is the last FROM: a RUN there needs a shell the
        # runtime image deliberately does not have.
        tail = text[text.rindex("\nFROM "):]
        assert "RUN " not in tail, f"{path}: the runtime stage cannot run a command"








def test_the_container_build_documents_a_consumer_dockerfile() -> None:
    """The image is only useful if a consumer can copy it into their own
    Dockerfile; the example is part of the contract, not decoration."""
    readme = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    contributing = (_REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert "FROM ghcr.io/tartaruscode/lambda-runtime-factory/" in readme
    assert "FROM ghcr.io/tartaruscode/lambda-runtime-factory/" in contributing








def test_the_iron_bank_template_carries_the_stig_invariants() -> None:
    """The canonical template is the one file that has to be right: the final
    stage runs non-root, writable only under /tmp, and carries no package
    manager."""
    template = (
        _REPO_ROOT / "tools" / "docker" / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert 'ENTRYPOINT ["/opt/bootstrap"]' in template, "the STIG entrypoint is fixed"
    assert "USER ${NONROOT_UID}:${NONROOT_GID}" in template
    assert "WORKDIR @@WORKDIR@@" in template
    # The DoD root CAs are injected and the trust store is extracted.
    assert "/etc/pki/ca-trust/source/anchors/" in template
    assert "update-ca-trust extract" in template
    assert "COPY --from=builder /etc/pki/ca-trust/extracted" in template
    # TLS stacks all read the same bundle.
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "DENO_CERT", "NODE_EXTRA_CA_CERTS"):
        assert key in template, key
    # Runtime dependencies are installed by the base's own package manager...
    assert "microdnf install -y --nodocs --setopt=install_weak_deps=0" in template
    # ...and never cherry-picked as bare shared objects, which would leave
    # /var/lib/rpm without the owning package and /etc/ld.so.cache stale.
    assert "cp -a" not in template
    assert "find /lib" not in template and "find /usr/lib" not in template
    assert "ldd" not in template
    assert "STAGED_SONAMES" not in template
    # rpm stays: Grype reads its database.
    assert "rm -rf /var/lib/rpm" not in template
    assert "rpm -q" in template


def test_the_iron_bank_package_map_names_packages_not_files() -> None:
    # What the base ships needs no install at all.
    assert container_image.required_ubi_packages(["libc.so.6", "libz.so.1"]) == []
    # A soname the base lacks maps to its owning RPM.
    assert container_image.required_ubi_packages(["libbz2.so.1.0"]) == ["bzip2-libs"]

    with pytest.raises(container_image.ContainerError, match="libunknown\\.so\\.9"):
        container_image.required_ubi_packages(["libunknown.so.9"])


def test_the_iron_bank_package_list_is_names_not_characters() -> None:
    """The manifest value is a JSON list; iterating the string would install
    packages named `"`, `[` and `a`."""
    packages = container_image.ironbank_packages(
        ["ca-certificates"], ["libc.so.6", "libbz2.so.1.0"]
    )

    assert packages == ["bzip2-libs", "ca-certificates"]
    assert all(len(name) > 1 for name in packages)


def test_local_invoke_never_bakes_the_emulator_into_the_image() -> None:
    for name in ("tools/docker/Dockerfile", "tools/runtime_lib/container_image.py"):
        text = (_REPO_ROOT / name).read_text(encoding="utf-8")
        assert "aws-lambda-rie" not in text, name


def test_build_runtime_normalizes_stig_permissions_before_staging() -> None:
    script = (_REPO_ROOT / "tools" / "bin" / "build-runtime").read_text(encoding="utf-8")

    chmod_index = script.index("chmod -R a+rX")
    manifest_index = script.index("runtime-manifest.json")
    assert chmod_index < manifest_index, "normalize before describing the payload"
    assert 'chmod a+rx "${LAYER_ROOT}/${BOOTSTRAP_OUTPUT}"' in script
    # The image is the artifact, so nothing here zips or publishes.
    assert "zip -qrX" not in script
    assert "PACKAGE_PATH" not in script






def test_the_soci_conversion_filters_the_pull_to_one_architecture() -> None:
    """buildx exports provenance attestations by default, so the pushed per-arch
    tag is an index holding an `unknown/unknown` manifest; `soci convert` refuses
    it. Filtering the oras pull drops that entry."""
    script = (_REPO_ROOT / "tools" / "bin" / "publish-soci-index").read_text(encoding="utf-8")

    assert '--platform "linux/${CONTAINER_ARCH}"' in script


# --- Two-tier CI gating -----------------------------------------------------------


def test_ci_has_a_fast_tier_and_a_ready_to_merge_tier() -> None:
    triggers = _triggers("ci.yml")

    assert "workflow_dispatch" in triggers, "an explicit full run must stay possible"
    pull_request = triggers["pull_request"]
    injected = set(pull_request["types"])
    assert {"opened", "synchronize", "reopened"} <= injected
    assert {"labeled", "unlabeled", "ready_for_review"} <= injected

    call = _call_site("ci.yml", "prepare-matrix", _MATRIX_ACTION)
    expression = call["changed-only"]
    assert "github.event_name == 'pull_request'" in expression
    assert "github.event.pull_request.draft == true" in expression
    assert "ready-to-merge" in expression


def test_the_matrix_action_can_narrow_to_changed_runtimes() -> None:
    action = _action("runtime-matrix")

    assert action["inputs"]["changed-only"]["default"] == "false"
    generate = next(
        step for step in action["runs"]["steps"] if step.get("id") == "matrix"
    )["run"]

    assert "git diff --name-only" in generate
    assert "'^runtimes/'" in generate
    # A tooling-only change still proves the shared plumbing builds.
    assert "pypy311" in generate and "canary" in generate.lower()


def test_the_matrix_generator_accepts_several_runtimes_at_once() -> None:
    """The fast tier passes the ids whose files changed, which is a list."""
    output = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "tools" / "runtime_lib" / "runtime_manifest.py"),
            "matrix", "--runtime", "pypy311,bun13", "--arch", "x86_64",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=_REPO_ROOT,
    ).stdout
    entries = json.loads(output)["include"]

    assert {entry["runtime"] for entry in entries} == {"pypy311", "bun13"}
    assert all(entry["arch"] == "x86_64" for entry in entries)


def test_a_forgotten_placeholder_or_unknown_runtime_fails_loudly() -> None:
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        subprocess.run(
            [
                sys.executable,
                str(_REPO_ROOT / "tools" / "runtime_lib" / "runtime_manifest.py"),
                "matrix", "--runtime", "nope", "--arch", "x86_64",
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=_REPO_ROOT,
        )
    assert "Unknown runtime" in excinfo.value.stderr


# --- Rootless bootstrap contract --------------------------------------------------
#
# PID 1 has to forward SIGTERM, so every shell bootstrap replaces itself with the
# runtime (`exec`), leaving no intermediate shell. The compiled launcher does the
# same without a shell, and both resolve the handler from _HANDLER or argv[1].


def test_every_shell_bootstrap_execs_instead_of_forking() -> None:
    bootstraps = sorted((_REPO_ROOT / "runtimes").glob("*/bootstrap/bootstrap"))
    assert bootstraps, "the shell bootstrap families are part of the deliverable"

    for path in bootstraps:
        text = path.read_text(encoding="utf-8")
        assert text.splitlines()[0].startswith("#!"), path
        # Join backslash continuations first: the Deno bootstrap spans several
        # lines with `exec` at the start and its arguments below.
        joined = text.replace("\\\n", " ")
        statements = [
            line.strip()
            for line in joined.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        # The last statement must be the exec: a bootstrap that runs the runtime
        # as a child leaves a shell holding PID 1, which never forwards the Lambda
        # shutdown signal.
        assert statements[-1].startswith("exec "), f"{path}: last statement is {statements[-1]!r}"


def test_the_runtime_api_headers_are_read_case_insensitively() -> None:
    """Lambda's Runtime API sends `Lambda-Runtime-Aws-Request-Id`; the payloads
    name it in lower case. Both must resolve to the same header."""
    for path in sorted((_REPO_ROOT / "runtimes").glob("*/bootstrap/bootstrap.py3")):
        text = path.read_text(encoding="utf-8")
        assert "getheader(" in text, path
        assert "Lambda-Runtime-Aws-Request-Id" in text, path

    for helper in ("bun13", "bun14", "deno29"):
        text = (
            _REPO_ROOT / "runtimes" / helper / "helpers" / f"lambda_runtime_{helper.rstrip('0123456789')}" / "runtime.ts"
        ).read_text(encoding="utf-8")
        assert 'headers.get("lambda-runtime-aws-request-id")' in text, helper


# --- One container per runtime, on the Iron Bank base ---------------------------
#
# There is exactly one image per runtime, from the same ${LAYER_ROOT} the zip layer
# ships, on the DoD-hardened Iron Bank UBI9-minimal base. A second base would mean
# a second container matrix, a second set of tags, and a library-staging hack that
# leaves the image's RPM database unaware of libraries it carries. These tests hold
# that shape: one base, one entrypoint, one tag per version.


def test_every_runtime_has_one_container_target() -> None:
    for runtime_id in runtime_manifest.list_active_runtime_ids():
        for arch in runtime_manifest.SUPPORTED_ARCHS:
            container = runtime_manifest.load_runtime(runtime_id, arch=arch)["container"]

            assert container["enabled"] is True, runtime_id
            assert container["workdir"] == "/var/task", runtime_id
            # One entrypoint for every family: the bootstrap the zip layer ships.
            assert container["entrypoint"] == ["/opt/bootstrap"], runtime_id
            assert container["user"] == "1001:1001", runtime_id
            assert container["layer_root"] == "/opt", runtime_id


def test_the_single_container_base_is_iron_bank() -> None:
    container = runtime_manifest.REPO_DEFAULTS["container"]

    assert container["base_image"].startswith("registry1.dso.mil/ironbank/redhat/ubi/")
    assert container["fallback_base_image"] == (
        "registry.access.redhat.com/ubi9/ubi-minimal:latest"
    ), "the credential-free fallback must be the same UBI9 userland"
    assert container["nonroot_uid"] == container["nonroot_gid"] == 1001
    assert "base_image_digest" in container, "a digest pin must be possible"
    assert container["packages"][0] == "ca-certificates"
    assert {"zlib", "libffi", "expat", "libxcrypt-compat", "bzip2-libs", "xz-libs"} <= set(
        container["packages"]
    ), "the runtime library floor must be installed as RPMs"

    for runtime_id in runtime_manifest.list_active_runtime_ids():
        for arch in runtime_manifest.SUPPORTED_ARCHS:
            env = runtime_manifest.runtime_env(runtime_id, arch=arch)
            assert env["CONTAINER_BASE_IMAGE"].startswith("registry1.dso.mil/"), runtime_id
            assert env["CONTAINER_NONROOT_UID"] == "1001", runtime_id
            assert env["CONTAINER_USER"] == "1001:1001", runtime_id


def test_no_second_container_base_survives_anywhere() -> None:
    """The distroless base, the Debian `.so`-staging stage and the Alpine musl base
    are gone; a family that reintroduces one would rebuild the matrix by accident."""
    for runtime_id in runtime_manifest.list_active_runtime_ids():
        raw = json.loads(
            runtime_manifest.runtime_manifest_path(runtime_id).read_text(encoding="utf-8")
        )
        container = raw.get("container", {})
        for key in ("base_image", "fallback_base_image", "profile", "debug_base_image",
                    "libs_base_image", "links", "user", "entrypoint"):
            assert key not in container, (
                f"{runtime_id}: container.{key} belongs to the repo default, not a family"
            )

    family_defaults = json.dumps(runtime_manifest.RUNTIME_FAMILY_DEFAULTS)
    for forbidden in ("distroless", "gcr.io", "debian:", "alpine:"):
        assert forbidden not in family_defaults, forbidden

    package = runtime_manifest.load_runtime("graalpy313")["container"]
    assert "gcr.io" not in package["base_image"]


def test_the_container_coordinates_travel_to_the_builder() -> None:
    env = runtime_manifest.runtime_env("graalpy313", arch="arm64")

    assert env["CONTAINER_PLATFORM"] == "linux/arm64"
    assert env["CONTAINER_ARCH"] == "arm64"
    assert env["CONTAINER_WORKDIR"] == "/var/task"
    assert json.loads(env["CONTAINER_ENTRYPOINT"]) == ["/opt/bootstrap"]
    assert env["CONTAINER_IMAGE"].endswith("/graalpy313")
    assert env["CONTAINER_TAG"] == "25.5.5"
    assert env["VERSION_LINE"] == "3.13"
    assert env["CONTAINER_CACHE_REF"] == (
        "ghcr.io/tartaruscode/lambda-runtime-factory/graalpy313:buildcache-arm64"
    )
    # No variant coordinates exist any more: one image per runtime.
    for gone in ("CONTAINER_VARIANT", "CONTAINER_TAG_SUFFIX", "CONTAINER_PROFILE"):
        assert gone not in env, gone


def test_render_dockerfile_substitutes_every_placeholder() -> None:
    dockerfile = container_image.render_dockerfile(
        layer_root="/opt",
        workdir="/var/task",
        env={"HOME": "/tmp", "TMPDIR": "/tmp", "GOCACHE": "/tmp/.cache/go-build"},
        labels={"com.tartaruscode.lambda.runtime": "pypy311"},
    )

    assert not re.search(r"@@[A-Z_]+@@", dockerfile), "a placeholder was left unsubstituted"
    assert 'ENV HOME="/tmp"' in dockerfile
    assert 'LABEL com.tartaruscode.lambda.runtime="pypy311"' in dockerfile
    assert "WORKDIR /var/task" in dockerfile
    assert "COPY --chown=${NONROOT_UID}:${NONROOT_GID} layer/ /opt/" in dockerfile
    assert "chmod -R a+rX /opt" in dockerfile
    assert 'ENTRYPOINT ["/opt/bootstrap"]' in dockerfile
    assert "USER ${NONROOT_UID}:${NONROOT_GID}" in dockerfile


def test_the_toolchain_families_prove_themselves_without_a_handler() -> None:
    """An image with no handler cannot be exercised through RIE, so its smoke test
    is declared in the manifest and runs under the same STIG contract."""
    go = runtime_manifest.load_runtime("go127")["container"]["smoke_test"]
    assert go == {"argv": ["go", "version"], "expect_version": True}

    rust = runtime_manifest.load_runtime("rust199")["container"]["smoke_test"]
    assert rust["argv"] == ["/opt/bootstrap"]
    assert rust["expect_exit"] == 127
    assert "_HANDLER" in rust["expect_output"]

    for runtime_id in ("pypy311", "bun13", "deno29"):
        assert runtime_manifest.load_runtime(runtime_id)["container"]["smoke_test"] == {}


def test_a_malformed_smoke_test_is_refused() -> None:
    data = runtime_manifest.load_runtime("go127")
    data["container"]["smoke_test"] = {"argv": []}
    with pytest.raises(ValueError, match="smoke_test.argv"):
        runtime_manifest.validate_runtime_data("go127", data)

    data["container"]["smoke_test"] = ["go", "version"]
    with pytest.raises(ValueError, match="must be a mapping"):
        runtime_manifest.validate_runtime_data("go127", data)


def test_local_invoke_proves_the_stig_contract() -> None:
    script = (_REPO_ROOT / "tools" / "bin" / "local-invoke-container").read_text(encoding="utf-8")

    assert "--read-only" in script
    assert "uid=${CONTAINER_NONROOT_UID},gid=${CONTAINER_NONROOT_GID}" in script
    assert '--user "${CONTAINER_NONROOT_UID}:${CONTAINER_NONROOT_GID}"' in script
    # The smoke test for an image with no handler runs under the same flags, not
    # under a plain `docker run`.
    assert script.index("stig_args=(") < script.index("Running the declared smoke test")
    assert 'docker run --rm "${stig_args[@]}"' in script
    # RIE comes from the host cache, never from the production image.
    assert ":/tmp/aws-lambda-rie:ro" in script
    assert "aws-lambda-rie-" in script


def test_check_container_verifies_the_built_image() -> None:
    script = (_REPO_ROOT / "tools" / "bin" / "check-container").read_text(encoding="utf-8")

    # Everything it asserts is read back from the image, not from the manifest.
    assert "docker image inspect" in script
    assert '{{.Config.User}}' in script
    assert '{{json .Config.Entrypoint}}' in script
    assert "no-microdnf" in script and "no-dnf" in script and "no-yum" in script
    assert "rpmdb-present" in script
    assert "--read-only" in script


def test_build_container_has_one_base_path_and_no_variant_flags() -> None:
    """There is one base and one artifact, so nothing selects a variant."""
    script = (_REPO_ROOT / "tools" / "bin" / "build-container").read_text(encoding="utf-8")

    assert "--variant" not in script and "--debug" not in script
    assert "--ironbank" not in script
    assert "USE_FALLBACK_UBI" in script
    assert "IRONBANK_TOKEN" in script, "a CI build without the token must fall back"
    assert "BASE_IMAGE_DIGEST" in script
    assert "--cache-from" in script and "--cache-to" in script
    assert "type=gha" in script, "the cache backend needs no registry credentials"
    # No payload is re-derived in the image build: build-runtime owns it.
    assert "curl " not in script
    assert "sha256sum" not in script
    for unsupported in ("prepare-ironbank", "distroless", "LIB_PACKAGES", "STAGED_SONAMES"):
        assert unsupported not in script, unsupported


def test_ci_builds_scans_and_smoke_tests_the_container_image() -> None:
    call = _call_site("ci.yml", "runtime-checks", _BUILD_ACTION)

    assert call["container"] == "true"
    assert "ironbank" not in call, "one image per runtime; there is no second artifact"

    build = _named_step("build-and-audit-runtime", "Build the container image")
    audit = _named_step("build-and-audit-runtime", "Generate the compliance bundle")
    smoke = _named_step("build-and-audit-runtime", "Smoke-test the container image")
    contract = _named_step("build-and-audit-runtime", "Check the image contract")

    assert build["if"] == "inputs.container == 'true'"
    assert "build-container" in build["run"]
    assert audit["if"] == "inputs.container == 'true'"
    assert "generate-compliance-artifacts" in audit["run"]
    assert "local-invoke-container" in smoke["run"]
    assert "check-container" in contract["run"]
    assert "container-variants" not in _workflow_text("ci.yml")


def test_buildx_caching_is_wired_end_to_end() -> None:
    action = _action("build-and-audit-runtime")
    steps = {step.get("name"): step for step in action["runs"]["steps"]}

    setup = steps["Set up Docker Buildx"]
    assert setup["uses"] == "docker/setup-buildx-action@v3"
    # The `gha` cache backend only exists for the container driver.
    assert setup["with"]["driver"] == "docker-container"

    # The script owns the cache arguments now, because it is the only place that
    # knows the arch and the runtime id the scopes are built from.
    build = steps["Build the container image"]
    assert build["env"]["CONTAINER_BUILD_CACHE"] == "gha"
    assert "cache-from" not in build["run"] and "cache-to" not in build["run"]


def test_ci_does_not_need_registry_credentials() -> None:
    """The build cache is the Actions cache service (`type=gha`), and a CI cell
    pushes nothing, so a GHCR login there would be a credential grant with no
    consumer. The release still logs in, because it is the thing that pushes."""
    for workflow, job in (("ci.yml", "runtime-checks"), ("audit-runtimes.yml", "audit")):
        document = _workflow(workflow)["jobs"][job]
        names = [step.get("name") for step in document["steps"]]
        assert "Log in to GHCR" not in names, workflow
        assert document["permissions"].get("packages") != "write", workflow


def test_the_release_builds_scans_and_pushes_one_image_per_runtime() -> None:
    jobs = _workflow("release-runtime.yml")["jobs"]
    assert "publish-containers" in jobs and "merge-containers" in jobs
    publish = jobs["publish-containers"]
    steps = {step.get("name"): step for step in publish["steps"]}

    assert publish["permissions"]["packages"] == "write"
    assert steps["Log in to GHCR"]["with"]["registry"] == "ghcr.io"
    for name in ("Log in to GHCR", "Push the per-architecture image"):
        assert "github.event.inputs.dry_run != 'true'" in steps[name]["if"], name

    # Every image is built, scanned, smoke-tested and checked before it is pushed,
    # and there is no variant loop left to run twice.
    build = steps["Build, scan and smoke-test the image"]["run"]
    for command in ("build-container", "generate-compliance-artifacts", "local-invoke-container"):
        assert command in build, command
    assert "--variant" not in build
    assert "container-variants" not in _workflow_text("release-runtime.yml")
    assert "check-container" in steps["Check the image contract"]["run"]
    assert "--ironbank" not in steps["Push the per-architecture image"]["run"]

    merge = jobs["merge-containers"]
    merge_run = next(step for step in merge["steps"] if step.get("name") == "Create the multi-arch tags")["run"]
    assert "merge-container-tags" in merge_run
    assert "--ironbank" not in merge_run
    assert merge["permissions"]["packages"] == "write"

    # The tag arithmetic lives in the script, which is the part that has to be
    # right: one multi-arch tag per version, from the two per-arch images.
    script = (_REPO_ROOT / "tools" / "bin" / "merge-container-tags").read_text(encoding="utf-8")
    assert "docker buildx imagetools create" in script
    assert "-amd64" in script and "-arm64" in script
    assert "latest" in script
    # No variant or second-artifact tag suffix survives; `--ironbank` is only a
    # harmless no-op case.
    assert "${tag}-debug" not in script
    assert "${tag}-ironbank" not in script
    assert "${suffix}" not in script


def test_the_release_publishes_to_ecr_public_and_writes_a_soci_index() -> None:
    """Lambda functions cannot pull from GHCR, and a SOCI index is what makes a
    large image start lazily — both are part of the published artifact."""
    jobs = _workflow("release-runtime.yml")["jobs"]
    publish = {step.get("name"): step for step in jobs["publish-containers"]["steps"]}

    ecr_login = publish["Log in to Amazon ECR Public"]["run"]
    assert "aws ecr-public get-login-password" in ecr_login
    assert "docker login --username AWS" in ecr_login
    assert "ensure-container-repos" in publish["Ensure the image repositories exist"]["run"]
    assert "check-container" in publish["Check the image contract"]["run"]

    # SOCI runs beside the image it converts, not in a job that re-fetches it.
    soci = publish["Publish the SOCI-enabled copy"]
    assert "publish-soci-index" in soci["run"] and "--push" in soci["run"]
    assert "SOCI_VERSION" in soci["env"], "the soci CLI must be pinned"
    assert "ORAS_VERSION" in soci["env"], "oras moves the OCI layout, so it is pinned too"
    assert "publish-soci" not in jobs, "the separate SOCI job was folded into the image jobs"


def test_an_unsatisfied_soname_must_be_declared_and_justified() -> None:
    """An unsatisfied soname is a defect by default — the build fails with its
    name. A family can declare one instead, and only the musl toolchain does."""
    with pytest.raises(container_image.ContainerError, match=r"libc\.so"):
        container_image.required_ubi_packages(["libc.so"])

    assert container_image.required_ubi_packages(["libc.so"], unsatisfied=["libc.so"]) == []

    declared = {
        runtime_id: runtime_manifest.load_runtime(runtime_id)["container"]["unsatisfied_sonames"]
        for runtime_id in runtime_manifest.list_active_runtime_ids()
        if runtime_manifest.load_runtime(runtime_id)["container"]["unsatisfied_sonames"]
    }
    assert declared, "the rust family should declare its musl soname"
    assert set(declared) == {"rust194", "rust198", "rust199"}, declared
    for runtime_id, names in declared.items():
        assert names == ["libc.so"], runtime_id


# --- OIDC contract -------------------------------------------------------------
#
# A job-level `permissions:` map replaces the workflow-level one instead of merging
# with it, so a job that assumes an AWS role over OIDC has to grant `id-token:
# write` itself. The failure is not self-explanatory: the credentials action hints
# "Did you mean to set the `id-token` permission?" and then retries twelve times
# with "Could not load credentials from any providers", which reads like an account
# or role problem. One job lost that grant and the whole multi-arch merge stopped.

_AWS_CREDENTIALS_ACTION = "aws-actions/configure-aws-credentials"


def _assumes_an_aws_role(job: dict) -> bool:
    """Whether a job authenticates to AWS by assuming a role."""
    for step in job.get("steps") or []:
        if isinstance(step, dict) and _AWS_CREDENTIALS_ACTION in str(step.get("uses", "")):
            return True
    return False


def test_every_job_that_assumes_a_role_can_mint_an_oidc_token() -> None:
    checked = []
    for path in sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        workflow_permissions = doc.get("permissions") or {}
        for name, job in (doc.get("jobs") or {}).items():
            if not _assumes_an_aws_role(job):
                continue
            # A job-level map replaces the workflow map; a string grants
            # everything or nothing.
            effective = job.get("permissions")
            if effective is None:
                effective = workflow_permissions
            if isinstance(effective, str):
                assert effective == "write-all", (
                    f"{path.name}:{name} assumes an AWS role but is limited to "
                    f"`{effective}` permissions"
                )
            else:
                assert isinstance(effective, dict) and effective.get("id-token") == "write", (
                    f"{path.name}:{name} assumes an AWS role over OIDC but its effective "
                    f"permissions ({effective!r}) do not grant `id-token: write`"
                )
            checked.append((path.name, name))
    assert checked, "no job assumes an AWS role over OIDC"


def test_the_iron_bank_login_reads_what_the_fallback_check_reads() -> None:
    """A login step that reads a differently spelled secret than the fallback
    check can be skipped while the token is set — and the build then publishes the
    Red Hat fallback userland under the same tag, with nothing failing. Resolving
    the secret into one job variable, and logging in with that variable, is what
    makes the two impossible to disagree."""
    sites = []
    for path in sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, job in (doc.get("jobs") or {}).items():
            env = job.get("env") or {}
            for step in job.get("steps") or []:
                if not (
                    isinstance(step, dict)
                    and step.get("name") == "Log in to Iron Bank Registry"
                ):
                    continue
                sites.append((path.name, name))
                assert env.get("IRONBANK_TOKEN") == "${{ secrets.IRONBANK_TOKEN }}", (
                    f"{path.name}:{name} must resolve IRONBANK_TOKEN from the secret"
                )
                assert step.get("with", {}).get("password") in (
                    None,
                    "${{ env.IRONBANK_TOKEN }}",
                ), f"{path.name}:{name} must log in with the resolved IRONBANK_TOKEN"
                run = step.get("run", "")
                if run:
                    assert "${IRONBANK_TOKEN}" in run, (
                        f"{path.name}:{name} must log in with the resolved IRONBANK_TOKEN, "
                        "not a separately spelled secret name"
                    )
                assert "IRONBANK_TOKEN" in str(step.get("if", "")), (
                    f"{path.name}:{name} must gate the login on the same variable"
                )
    assert sites, "no workflow logs in to Iron Bank"


def test_the_iron_bank_login_is_retried_without_silently_changing_base() -> None:
    """registry1.dso.mil answers `context deadline exceeded` often enough that a
    single attempt fails an otherwise healthy cell. Retrying is safe; falling back
    to the Red Hat mirror is not, because the published image would then not be on
    the Iron Bank base the whole project exists to build on."""
    for name in ("ci.yml", "release-runtime.yml"):
        text = (_REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "Log in to Iron Bank Registry" in text, name
        block = text.split("Log in to Iron Bank Registry", 1)[1].split("- name:", 1)[0]

        assert "common::retry 4 ib_login" in block, name
        assert "registry1.dso.mil did not answer after 4 attempts" in block, name
        assert "USE_FALLBACK_UBI" not in block, (
            f"{name}: a retry must fail rather than publish on another base"
        )
