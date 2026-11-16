"""The severity gate, and the UBI-lag POA&M it consults.

Grype's own `--fail-on` cannot tell "this image is vulnerable" from "the fix exists
in RHEL's feed but no UBI9 repository carries the RPM yet", and a `.grype.yaml`
ignore would delete the finding from the report an auditor reads. So the scan runs
unfiltered and the gate is applied here — over the report, against an expiring
waiver file. These tests run the snippet the build runs, extracted from the script
itself, against fixture reports.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "tools" / "bin" / "generate-compliance-artifacts"
_POAM = _REPO_ROOT / "tools" / "compliance" / "ubi-lag-waivers.json"
_BUILD_CONTAINER = _REPO_ROOT / "tools" / "bin" / "build-container"

KRB5_INSTALLED = "1.21.1-10.el9_8.1"
KRB5_FIXED = "1.21.1-10.el9_9.2"
TODAY = "2026-11-16"

# The POA&M block is the second heredoc; the SBOM assertion above it is the first.
_GATE_BLOCK = 1
_PROVENANCE_BLOCK = 2


def _block(index: int) -> str:
    text = _SCRIPT.read_text(encoding="utf-8")
    blocks = re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF", text, flags=re.S)
    assert len(blocks) >= 4, "the SBOM check, the gate, the provenance and the row are all blocks"
    return blocks[index]


def shipped_waiver() -> dict:
    return json.loads(_POAM.read_text(encoding="utf-8"))["waivers"][0]


def _match(
    cve: str = "CVE-2026-112321",
    severity: str = "High",
    package: str = "krb5-libs",
    installed: str = KRB5_INSTALLED,
    fixed: str = KRB5_FIXED,
    state: str = "fixed",
) -> dict:
    return {
        "vulnerability": {
            "id": cve,
            "severity": severity,
            "fix": {"state": state, "versions": [fixed] if fixed else []},
        },
        "artifact": {"name": package, "version": installed},
    }


def _fixture(
    tmp_path: Path,
    matches: list[dict],
    installed: str = KRB5_INSTALLED,
    waiver: dict | None = None,
) -> tuple[Path, Path, Path]:
    """Write a Grype report, the image's SPDX document and a waiver file."""
    report = tmp_path / "pypy311-x86_64.grype.json"
    report.write_text(json.dumps({"matches": matches}), encoding="utf-8")

    spdx = tmp_path / "pypy311-x86_64.spdx.json"
    spdx.write_text(
        json.dumps(
            {
                "packages": [
                    {"name": "krb5-libs", "versionInfo": installed},
                    {"name": "glibc", "versionInfo": "2.34-100.el9_8.1"},
                ]
            }
        ),
        encoding="utf-8",
    )

    waivers = tmp_path / "ubi-lag-waivers.json"
    waivers.write_text(json.dumps({"waivers": [waiver or shipped_waiver()]}), encoding="utf-8")
    return report, spdx, waivers


def _gate(tmp_path: Path, report: Path, spdx: Path, waivers: Path, today: str = TODAY):
    gate_path = tmp_path / "gate.json"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _block(_GATE_BLOCK),
            str(report),
            str(waivers),
            str(gate_path),
            str(spdx),
            today,
            "pypy311",
            "x86_64",
        ],
        capture_output=True,
        text=True,
    )
    gate = json.loads(gate_path.read_text(encoding="utf-8")) if gate_path.exists() else {}
    return result, gate


def test_a_fixable_high_without_a_waiver_still_fails_the_gate(tmp_path: Path) -> None:
    """The gate has to keep its teeth: the waiver mechanism is not a way past it."""
    waiver = shipped_waiver()
    waiver["cves"] = ["CVE-2026-999999"]
    report, spdx, waivers = _fixture(tmp_path, [_match()], waiver=waiver)

    result, gate = _gate(tmp_path, report, spdx, waivers)

    assert result.returncode == 2, result.stderr
    assert gate["result"] == "fail"
    assert [item["cve"] for item in gate["blocking"]] == ["CVE-2026-112321"]
    assert gate["waived"] == []
    assert "severity gate failed" in result.stderr


def test_an_expired_waiver_stops_covering_its_findings(tmp_path: Path) -> None:
    """`expires_on` is the last day the entry waives anything, so the acknowledgement
    cannot outlive the lag window on its own."""
    waiver = shipped_waiver()
    waiver["expires_on"] = "2026-11-15"
    report, spdx, waivers = _fixture(tmp_path, [_match()], waiver=waiver)

    result, gate = _gate(tmp_path, report, spdx, waivers)

    assert result.returncode == 2, result.stderr
    assert gate["poam"][0]["state"] == "expired"
    assert gate["waived"] == []
    assert len(gate["blocking"]) == 1


def test_a_waiver_for_a_build_the_image_no_longer_carries_fails(tmp_path: Path) -> None:
    """The record must describe the image. An installed build that matches neither
    the waived one nor the fixed one means the POA&M is stale in a way a reader
    cannot see from the CVE list alone — so the run fails even with no finding left
    to block on."""
    report, spdx, waivers = _fixture(tmp_path, [], installed="1.21.1-10.el9_8.2")

    result, gate = _gate(tmp_path, report, spdx, waivers)

    assert gate["blocking"] == [], "there is nothing left to block on"
    assert gate["poam"][0]["state"] == "mismatched"
    assert "1.21.1-10.el9_8.2" in gate["poam"][0]["detail"]
    assert result.returncode == 2, result.stderr


def test_a_waiver_is_reported_for_removal_once_the_fixed_build_is_installed(
    tmp_path: Path,
) -> None:
    """The lag window closing is the expected end: the finding is gone, the gate
    passes, and the entry is named so the next commit deletes it instead of leaving
    a waiver in the tree that describes a build nobody ships."""
    report, spdx, waivers = _fixture(tmp_path, [], installed=KRB5_FIXED)

    result, gate = _gate(tmp_path, report, spdx, waivers)

    assert result.returncode == 0, result.stderr
    assert gate["result"] == "pass"
    assert gate["poam"][0]["state"] == "resolved"
    assert "remove tools/compliance/ubi-lag-waivers.json" in result.stderr


def test_the_active_rhsa_waiver_passes_while_the_finding_stays_in_the_report(
    tmp_path: Path,
) -> None:
    """The whole point: 14 fixable High findings, zero blocking, and the report still
    names all 14 — a waiver that removed them would leave an artifact claiming the
    image is clean."""
    cves = shipped_waiver()["cves"]
    report, spdx, waivers = _fixture(
        tmp_path, [_match(cve=cve) for cve in cves], installed=KRB5_INSTALLED
    )

    result, gate = _gate(tmp_path, report, spdx, waivers)

    assert result.returncode == 0, result.stderr
    assert gate["result"] == "pass"
    assert gate["blocking"] == []
    assert len(gate["waived"]) == len(cves)
    assert gate["waived_high"] == len(cves)
    assert gate["active_advisories"] == [shipped_waiver()["advisory"]]
    assert gate["poam"][0]["state"] == "active"

    # And the report the gate read is untouched: the findings are still in it.
    kept = json.loads(report.read_text(encoding="utf-8"))["matches"]
    assert len(kept) == len(cves)


def test_a_finding_without_a_published_fix_does_not_block(tmp_path: Path) -> None:
    """`--only-fixed` used to express this; the gate has to carry it now, or every
    wont-fix and unfixed advisory in the base turns the pipeline red."""
    report, spdx, waivers = _fixture(
        tmp_path, [_match(fixed="", state="not-fixed")], installed=KRB5_INSTALLED
    )

    result, gate = _gate(tmp_path, report, spdx, waivers)

    assert result.returncode == 0, result.stderr
    assert gate["blocking"] == []
    assert gate["waived"] == []


def test_a_critical_finding_blocks_even_while_a_high_is_waived(tmp_path: Path) -> None:
    """A waiver covers the CVEs it names and nothing else: a new advisory on the same
    package is the case that must not ride an existing entry."""
    report, spdx, waivers = _fixture(
        tmp_path,
        [_match(severity="Critical", cve="CVE-2026-999999"), _match(cve="CVE-2026-112321")],
        installed=KRB5_INSTALLED,
    )

    result, gate = _gate(tmp_path, report, spdx, waivers)

    assert result.returncode == 2, result.stderr
    assert [item["cve"] for item in gate["blocking"]] == ["CVE-2026-999999"]
    assert gate["waived_high"] == 1
    assert gate["waived_critical"] == 0


def test_a_missing_or_malformed_poam_fails_instead_of_waiving_nothing(tmp_path: Path) -> None:
    report, spdx, waivers = _fixture(tmp_path, [_match()])
    waivers.write_text(json.dumps({"waivers": []}), encoding="utf-8")

    result, _gate_doc = _gate(tmp_path, report, spdx, waivers)
    assert result.returncode == 2, result.stderr

    waivers.write_text("{not json", encoding="utf-8")
    result, _gate_doc = _gate(tmp_path, report, spdx, waivers)
    assert result.returncode == 2, result.stderr


def test_the_provenance_keeps_raw_counts_and_adds_the_poam_verdict(tmp_path: Path) -> None:
    """An auditor reads `high=14` and `0 unwaived`, not `high=0`: the severity counts
    stay what Grype found, and the waiver is reported beside them."""
    cves = shipped_waiver()["cves"]
    report, spdx, waivers = _fixture(
        tmp_path, [_match(cve=cve) for cve in cves], installed=KRB5_INSTALLED
    )
    result, gate = _gate(tmp_path, report, spdx, waivers)
    assert result.returncode == 0, result.stderr
    gate_path = tmp_path / "gate.json"

    stem = tmp_path / "pypy311-x86_64"
    (tmp_path / "pypy311-x86_64.cdx.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pypy311-x86_64.sbom-stats.json").write_text(
        json.dumps({"total": 123, "rpm": 121, "runtime": 2}), encoding="utf-8"
    )
    payload = tmp_path / "runtime-manifest.json"
    payload.write_text(
        json.dumps({"runtime_id": "pypy311", "arch": "x86_64", "distribution_version": "x"}),
        encoding="utf-8",
    )
    (tmp_path / "container.json").write_text(
        json.dumps({"base_image_ref": "registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal:latest"}),
        encoding="utf-8",
    )

    run = subprocess.run(
        [
            sys.executable,
            "-c",
            _block(_PROVENANCE_BLOCK),
            str(stem),
            str(payload),
            str(tmp_path),
            "example.invalid/pypy311:3.11",
            "false",
            str(gate_path),
        ],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr

    provenance = json.loads(Path(f"{stem}.provenance.json").read_text(encoding="utf-8"))
    assert provenance["grype_summary"]["High"] == len(cves), "the raw counts stay raw"
    assert provenance["ubi_lag_waived_high"] == len(cves)
    assert provenance["unwaived_blocking_cves"] == 0
    assert provenance["unwaived_blocking_cve_ids"] == []
    assert sorted(provenance["waived_cve_ids"]) == sorted(cves)
    assert provenance["active_poam_advisories"] == [shipped_waiver()["advisory"]]
    assert provenance["cve_gate"] == "pass"


def test_the_shipped_poam_is_a_usable_record() -> None:
    document = json.loads(_POAM.read_text(encoding="utf-8"))
    assert document.get("waivers"), "the file must hold the acknowledgements the gate reads"

    for entry in document["waivers"]:
        assert {
            "advisory",
            "package",
            "installed_evr",
            "fixed_evr",
            "expires_on",
            "reason",
            "cves",
        } <= set(entry)
        assert re.fullmatch(r"RHSA-\d{4}:\d+", entry["advisory"]), entry["advisory"]
        assert entry["installed_evr"] != entry["fixed_evr"], "a waiver for an unchanged build is a no-op"
        try:
            date.fromisoformat(entry["expires_on"])
        except ValueError:
            raise AssertionError(f"expires_on must be YYYY-MM-DD: {entry['expires_on']!r}")
        assert entry["cves"], "an entry that waives nothing is not a record"
        assert len(entry["cves"]) == len(set(entry["cves"])), "a CVE listed twice hides nothing"
        for cve in entry["cves"]:
            assert re.fullmatch(r"CVE-\d{4}-\d{4,}", cve), cve
        assert len(entry["reason"]) > 40, "the reason is what an auditor reads"


def test_the_gate_decides_the_policy_not_grypes_fail_on() -> None:
    script = _SCRIPT.read_text(encoding="utf-8")
    joined = script.replace("\\\n", " ")
    # Comments are stripped first: the explanation of why the flags are absent names
    # them, and a substring check would read that comment as the call itself.
    commands = "\n".join(
        line for line in joined.splitlines() if not line.lstrip().startswith("#")
    )

    assert "--fail-on" not in commands, "Grype must not decide the policy; the gate does"
    assert "--only-fixed" in commands, (
        "the report and the counts taken from it describe the findings an operator can act on"
    )
    assert "tools/compliance/ubi-lag-waivers.json" in commands, "the POA&M path is the default"
    assert "UBI_LAG_WAIVERS" in commands, "a local run must be able to point at a scratch file"

    scan = [
        line.strip()
        for line in commands.splitlines()
        if line.strip().startswith("grype ")
    ]
    assert len(scan) == 1, scan
    assert "json=${stem}.grype.json" in scan[0], scan[0]
    assert "sarif=${stem}.grype.sarif" in scan[0], scan[0]


def test_the_os_layer_upgrades_the_base_and_is_restamped_by_every_build_call_site() -> None:
    """`microdnf upgrade` fetches remote repodata, and BuildKit does not track what a
    RUN fetched: without a stamp in the instruction the cached layer is replayed with
    the package set of the day it was first built, and the fixed RPM is never seen.
    A stamp that one call site forgets to pass is the same defect, silently."""
    dockerfile = (_REPO_ROOT / "tools" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    lines = dockerfile.splitlines()

    upgrade = next(
        index for index, line in enumerate(lines)
        if line.strip().startswith("microdnf upgrade -y --nodocs --setopt=install_weak_deps=0")
    )
    install = next(
        index for index, line in enumerate(lines)
        if line.strip().startswith("microdnf install -y --nodocs --setopt=install_weak_deps=0")
    )
    layer_start = max(index for index, line in enumerate(lines[:upgrade]) if line.startswith("RUN "))

    assert upgrade < install, "the upgrade has to run before the floor is installed"
    assert not [line for line in lines[layer_start + 1 : install] if line.startswith("RUN ")], (
        "the upgrade, the install and the stamp echo belong to one RUN, so they share a layer"
    )
    assert "${UBI_RPM_EPOCH}" in lines[layer_start], "the stamp must key this layer's cache"

    assert "ARG UBI_RPM_EPOCH" in dockerfile, "the stamp has to be a build arg"
    stage = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("FROM ") and "AS runtime" in line
    )
    stamp = next(
        index for index, line in enumerate(lines) if line.startswith("ARG UBI_RPM_EPOCH")
    )
    assert stage < stamp < upgrade, (
        "the stamp has to be declared inside the stage that runs the upgrade: an ARG "
        "above the first FROM is only in scope for FROM lines, so the RUN would expand "
        "it to the empty string and the cached layer would never be invalidated"
    )
    labels = next(index for index, line in enumerate(lines) if "@@LABELS@@" in line)
    assert labels > upgrade, "nothing runtime-specific may move above the floor"

    action = yaml.safe_load(
        (
            _REPO_ROOT / ".github/actions/build-and-audit-runtime/action.yml"
        ).read_text(encoding="utf-8")
    )
    build = next(s for s in action["runs"]["steps"] if s.get("name") == "Build the container image")
    assert "UBI_RPM_EPOCH=${{ env.BUILD_UBI_RPM_EPOCH }}" in build["with"]["build-args"]

    release = yaml.safe_load(
        (_REPO_ROOT / ".github/workflows/release-runtime.yml").read_text(encoding="utf-8")
    )
    publish = next(
        s
        for s in release["jobs"]["publish-containers"]["steps"]
        if s.get("name") == "Build the container image"
    )
    assert "UBI_RPM_EPOCH=${{ env.BUILD_UBI_RPM_EPOCH }}" in publish["with"]["build-args"]

    build_container = _BUILD_CONTAINER.read_text(encoding="utf-8")
    assert "BUILD_UBI_RPM_EPOCH=${ubi_rpm_epoch}" in build_container
    assert '--build-arg "UBI_RPM_EPOCH=${ubi_rpm_epoch}"' in build_container
    assert 'ubi_rpm_epoch="${UBI_RPM_EPOCH:-$(date -u +%Y-%m-%d)}"' in build_container