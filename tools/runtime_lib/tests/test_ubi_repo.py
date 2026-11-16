"""UBI9 repository reading, EVR ordering, and waiver retirement.

The waiver file exists because a fix can be published to RHEL days before the UBI9
repository carries the RPM. That window has an end, and these tests pin both halves
of how the pipeline notices it: the comparison that decides an RPM is at or past
the fixed build, and the retainer that deletes the acknowledgement once it is.
"""

from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path

import yaml

from bump_version import prune_resolved_waivers, waiver_file
from ubi_repo import available_evrs, compare_evr, is_at_least, normalize_evr

_REPO_ROOT = Path(__file__).resolve().parents[3]
_POAM = _REPO_ROOT / "tools" / "compliance" / "ubi-lag-waivers.json"

FIXED = "1.21.1-10.el9_9.2"
INSTALLED = "1.21.1-10.el9_8.1"

REPOMD = b"""<?xml version="1.0" encoding="UTF-8"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
  <data type="primary">
    <location href="repodata/abc-primary.xml.gz"/>
  </data>
  <data type="filelists">
    <location href="repodata/abc-filelists.xml.gz"/>
  </data>
</repomd>
"""

PRIMARY = b"""<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://linux.duke.edu/metadata/common">
  <package type="rpm">
    <name>krb5-libs</name>
    <arch>x86_64</arch>
    <version epoch="0" ver="1.21.1" rel="10.el9_8.1"/>
  </package>
  <package type="rpm">
    <name>krb5-libs</name>
    <arch>x86_64</arch>
    <version epoch="0" ver="1.21.1" rel="10.el9_9.2"/>
  </package>
  <package type="rpm">
    <name>krb5-libs</name>
    <arch>aarch64</arch>
    <version epoch="0" ver="1.21.1" rel="10.el9_7.4"/>
  </package>
  <package type="rpm">
    <name>zlib</name>
    <arch>x86_64</arch>
    <version epoch="0" ver="1.2.11" rel="40.el9_8"/>
  </package>
  <package type="rpm">
    <name>krb5-libs</name>
    <arch>noarch</arch>
    <version epoch="0" ver="1.21.1" rel="10.el9_9.3"/>
  </package>
</metadata>
"""


def _fake_fetch(payloads: dict[str, bytes]):
    def fetch(url: str, timeout: int = 0) -> bytes:
        for suffix, body in payloads.items():
            if url.endswith(suffix):
                return body
        raise OSError(f"no fixture for {url}")

    return fetch


def test_evr_ordering_follows_rpm_rules() -> None:
    # The advisory's fixed build is newer than what the image carries.
    assert compare_evr(FIXED, INSTALLED) > 0
    assert is_at_least(FIXED, INSTALLED)
    assert not is_at_least(INSTALLED, FIXED)

    # A later rebuild of the same release stream, and the double-digit case that a
    # string comparison gets wrong ("el9_9.10" < "el9_9.2" lexically).
    assert compare_evr("1.21.1-10.el9_9.10", "1.21.1-10.el9_9.2") > 0
    assert compare_evr("1.21.1-10.el9_9.2", "1.21.1-10.el9_9.10") < 0

    # Epoch, arch suffix and the "0:" spellings all name the same build.
    assert compare_evr("0:" + FIXED, FIXED) == 0
    assert compare_evr(FIXED + ".x86_64", FIXED) == 0
    assert compare_evr("1:" + FIXED, FIXED) > 0

    # A digit run is newer than an alphabetic one, as rpm says.
    assert compare_evr("1.0.1", "1.0a") > 0
    assert compare_evr("2.0", "2.0.1") < 0
    assert compare_evr("2.0", "2.0-1") < 0, "a missing release sorts before any release"


def test_normalize_evr_keeps_a_real_epoch() -> None:
    assert normalize_evr("0:1.21.1-10.el9_8.1") == "1.21.1-10.el9_8.1"
    assert normalize_evr("1.21.1-10.el9_8.1.aarch64") == "1.21.1-10.el9_8.1"
    assert normalize_evr("1:1.21.1-10.el9_8.1") == "1:1.21.1-10.el9_8.1"


def test_the_repository_is_read_from_repodata_not_a_container() -> None:
    fetch = _fake_fetch(
        {
            "baseos/os/repodata/repomd.xml": REPOMD,
            "baseos/os/repodata/abc-primary.xml.gz": gzip.compress(PRIMARY),
        }
    )
    evrs = available_evrs("krb5-libs", "x86_64", repos=("baseos",), fetch=fetch)

    assert evrs == {INSTALLED, FIXED, "1.21.1-10.el9_9.3"}, (
        "the aarch64 build must not answer for x86_64, and noarch must"
    )


def test_a_repository_that_fails_is_skipped_rather_than_fatal() -> None:
    fetch = _fake_fetch({"baseos/os/repodata/repomd.xml": REPOMD,
                         "baseos/os/repodata/abc-primary.xml.gz": gzip.compress(PRIMARY)})
    evrs = available_evrs("krb5-libs", "x86_64", repos=("appstream", "baseos"), fetch=fetch)
    assert FIXED in evrs


def _waiver_file(tmp_path: Path) -> Path:
    path = tmp_path / "ubi-lag-waivers.json"
    path.write_text(_POAM.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_a_waiver_is_retired_only_when_the_fixed_evr_is_published(tmp_path: Path) -> None:
    path = _waiver_file(tmp_path)
    original = path.read_text(encoding="utf-8")

    # The repository serves the build the waiver already names: the window is open.
    retired = prune_resolved_waivers(
        path=path, available=lambda package, arch: {INSTALLED} if arch == "x86_64" else {INSTALLED}
    )
    assert retired == []
    assert path.read_text(encoding="utf-8") == original, "an unresolved waiver must not be rewritten"

    # The fixed build appears for both architectures: the window has closed.
    retired = prune_resolved_waivers(
        path=path, available=lambda package, arch: {INSTALLED, "0:" + FIXED}
    )
    assert [item["advisory"] for item in retired] == ["RHSA-2026:99715"]
    assert retired[0]["package"] == "krb5-libs"
    assert retired[0]["fixed_evr"] == "0:" + FIXED
    assert retired[0]["published_evrs"] == sorted(["0:" + FIXED])

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["waivers"] == [], "the entry is deleted, and the file remains a valid record"


def test_a_waiver_survives_a_fix_that_only_one_architecture_can_get(tmp_path: Path) -> None:
    """Pruning on the x86_64 answer alone leaves the arm64 cells waiving a finding
    nothing they can reach will fix, so every built architecture has to have it."""
    path = _waiver_file(tmp_path)

    retired = prune_resolved_waivers(
        path=path, available=lambda package, arch: {FIXED} if arch == "x86_64" else {INSTALLED}
    )

    assert retired == []
    assert json.loads(path.read_text(encoding="utf-8"))["waivers"], "the entry stays"


def test_a_dry_run_reports_without_writing(tmp_path: Path) -> None:
    path = _waiver_file(tmp_path)
    original = path.read_text(encoding="utf-8")

    retired = prune_resolved_waivers(
        path=path, available=lambda package, arch: {"1.21.1-10.el9_9.3"}, dry_run=True
    )

    assert retired and retired[0]["published_evrs"] == ["1.21.1-10.el9_9.3"]
    assert path.read_text(encoding="utf-8") == original


def test_the_check_command_carries_the_retirement_into_the_update_report() -> None:
    """`check --prune-waivers` is what the weekly workflow runs; the retirement has to
    be visible to it, or a week with no tarball change opens no pull request."""
    from bump_version import build_parser

    parsed = build_parser().parse_args(["check", "--json", "--prune-waivers"])
    assert parsed.prune_waivers is True

    parsed = build_parser().parse_args(["prune-waivers", "--dry-run"])
    assert parsed.command == "prune-waivers" and parsed.dry_run is True


def test_the_shipped_waiver_file_is_where_the_retainer_writes() -> None:
    assert waiver_file() == _POAM
    document = json.loads(_POAM.read_text(encoding="utf-8"))
    assert "waivers" in document
    for entry in document["waivers"]:
        # The retainer needs these keys to decide anything at all.
        assert entry["package"] and entry["fixed_evr"]
        date.fromisoformat(entry["expires_on"])


def test_the_update_workflow_prunes_and_carries_the_change_into_its_pull_request() -> None:
    workflow = yaml.safe_load(
        (_REPO_ROOT / ".github/workflows/check-updates.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["check-and-bump"]["steps"]
    check = next(step for step in steps if step.get("id") == "check")

    assert "--prune-waivers" in check["run"], "the weekly run has to retire, not just report"
    assert "retired_waivers" in check["run"], "the report has to name what it retired"
    assert "updates_found=" in check["run"], (
        "a retired waiver is an update even when no runtime tarball moved"
    )

    pull_request = next(
        step for step in steps if step.get("uses", "").startswith("peter-evans/create-pull-request")
    )
    assert "tools/compliance/ubi-lag-waivers.json" in pull_request["with"]["add-paths"], (
        "the deletion has to be in the pull request's commit paths"
    )
    assert "runtimes/**" in pull_request["with"]["add-paths"]
    assert "release-required" in pull_request["with"]["labels"]
    assert pull_request["with"]["title"] == "${{ steps.title.outputs.title }}"

    title = next(step for step in steps if step.get("id") == "title")
    assert "[Security: Retire" in title["run"], "a reviewer sees the retirement in the title"

    label = next(
        step for step in steps if step.get("name") == "Ensure the release-required label exists"
    )
    assert "gh label create release-required" in label["run"]
    assert "--force" in label["run"], "the step has to be idempotent"

    body = next(step for step in steps if step.get("name") == "Build PR description")
    assert "Release Required After Merge" in body["run"]
    assert "git tag -s" in body["run"], "the body carries the exact release command"


def test_ci_notes_that_a_retired_waiver_needs_a_release() -> None:
    workflow = yaml.safe_load(
        (_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["repo-checks"]["steps"]
    notice = next(step for step in steps if step.get("name") == "Note a retired UBI-lag waiver")

    assert "tools/compliance/ubi-lag-waivers.json" in notice["run"]
    assert "github.event.before" in notice["if"], "the diff needs both ends of the push"
    assert "GITHUB_STEP_SUMMARY" in notice["run"], "a green run is where this has to be visible"
    assert "release-runtime.yml" in notice["run"]