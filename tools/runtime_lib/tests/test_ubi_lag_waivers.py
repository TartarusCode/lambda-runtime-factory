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
_COMPLIANCE = _REPO_ROOT / "tools" / "runtime_lib" / "compliance.py"
_POAM = _REPO_ROOT / "tools" / "compliance" / "ubi-lag-waivers.json"
_BUILD_CONTAINER = _REPO_ROOT / "tools" / "bin" / "build-container"

KRB5_INSTALLED = "1.21.1-10.el9_8.1"
KRB5_FIXED = "1.21.1-10.el9_9.2"
TODAY = "2026-11-16"

# The fixture is deliberately a literal, not the shipped file. An empty
# `{"waivers": []}` is the POA&M's valid end state — the retainer leaves it behind
# once the fixed RPM reaches UBI9 — so a test that read the repository's copy would
# go red on the day the waiver it describes was retired, which is the day the
# mechanism worked.
SAMPLE_WAIVER = {
    "advisory": "RHSA-2026:99715",
    "package": "krb5-libs",
    "fixed_evr": "0:1.21.1-10.el9_9.2",
    "expires_on": "2026-11-30",
    "reason": (
        "RHEL 9 advisory RHSA-2026:99715 published 2026-11-12; the fixed RPM "
        "0:1.21.1-10.el9_9.2 is not yet published to UBI9 BaseOS repodata or "
        "Iron Bank ubi9-minimal, so no base image and no build-time upgrade can "
        "install it."
    ),
    "cves": [
        "CVE-2026-107364",
        "CVE-2026-112315",
        "CVE-2026-112316",
        "CVE-2026-112317",
        "CVE-2026-112318",
        "CVE-2026-112319",
        "CVE-2026-112320",
        "CVE-2026-112321",
        "CVE-2026-112322",
        "CVE-2026-112323",
        "CVE-2026-118380",
        "CVE-2026-119079",
        "CVE-2026-71199",
        "CVE-2026-71200",
    ],
}


def sample_waiver() -> dict:
    return json.loads(json.dumps(SAMPLE_WAIVER))


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
    waivers.write_text(json.dumps({"waivers": [waiver or sample_waiver()]}), encoding="utf-8")
    return report, spdx, waivers


def _gate(
    tmp_path: Path,
    report: Path,
    spdx: Path,
    waivers: Path,
    today: str = TODAY,
    *,
    enforcement: str = "enforced",
    base_source: str = "ironbank",
):
    gate_path = tmp_path / "gate.json"
    result = subprocess.run(
        [
            sys.executable,
            str(_COMPLIANCE),
            "gate",
            "--report",
            str(report),
            "--waivers",
            str(waivers),
            "--gate-out",
            str(gate_path),
            "--spdx",
            str(spdx),
            "--today",
            today,
            "--runtime",
            "pypy311",
            "--arch",
            "x86_64",
            "--enforcement",
            enforcement,
            "--base-source",
            base_source,
        ],
        capture_output=True,
        text=True,
    )
    gate = json.loads(gate_path.read_text(encoding="utf-8")) if gate_path.exists() else {}
    return result, gate


def test_a_fixable_high_without_a_waiver_still_fails_the_gate(tmp_path: Path) -> None:
    """The gate has to keep its teeth: the waiver mechanism is not a way past it."""
    waiver = sample_waiver()
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
    waiver = sample_waiver()
    waiver["expires_on"] = "2026-11-15"
    report, spdx, waivers = _fixture(tmp_path, [_match()], waiver=waiver)

    result, gate = _gate(tmp_path, report, spdx, waivers)

    assert result.returncode == 2, result.stderr
    assert gate["poam"][0]["state"] == "expired"
    assert gate["waived"] == []
    assert len(gate["blocking"]) == 1


def test_a_resolved_waiver_is_a_notice_not_a_failure(tmp_path: Path) -> None:
    """The lag window closing is not an error. The image carries the fixed build, so
    the acknowledgement is dead, but nothing is being released: the cell says so with
    a notice and the weekly retainer deletes the entry."""
    report, spdx, waivers = _fixture(tmp_path, [], installed=KRB5_FIXED)

    result, gate = _gate(tmp_path, report, spdx, waivers)

    assert result.returncode == 0, result.stderr
    assert gate["result"] == "pass"
    assert gate["blocking"] == [], "there is no finding left to block on"
    assert gate["poam"][0]["state"] == "resolved"
    assert "::notice::" in result.stderr
    assert "resolved upstream" in result.stderr
    assert "time to clean up tools/compliance/ubi-lag-waivers.json" in result.stderr
    assert "::error::" not in result.stderr, "a resolved waiver must not fail the cell"


def test_a_build_newer_than_the_fixed_one_is_resolved_too(tmp_path: Path) -> None:
    """Red Hat will rebuild the package again; the waiver must not need a fresh edit
    for every release after the one it named."""
    report, spdx, waivers = _fixture(tmp_path, [], installed="1.21.1-10.el9_9.10")

    result, gate = _gate(tmp_path, report, spdx, waivers)

    assert result.returncode == 0, result.stderr
    assert gate["poam"][0]["state"] == "resolved"


def test_a_fallback_build_records_blocking_findings_without_failing(tmp_path: Path) -> None:
    """The Red Hat fallback exists so an unauthenticated build can still be exercised.
    It is not a release artifact, so its verdict is recorded — blocking list,
    provenance, step log — but the cell succeeds instead of going red over a base
    nobody publishes."""
    report, spdx, waivers = _fixture(tmp_path, [_match()])
    waivers.write_text(json.dumps({"waivers": []}), encoding="utf-8")

    result, gate = _gate(
        tmp_path, report, spdx, waivers, enforcement="advisory", base_source="fallback"
    )

    assert result.returncode == 0, result.stderr
    assert gate["result"] == "fail", "the verdict stays a failure; only enforcement changes"
    assert gate["enforcement"] == "advisory"
    assert gate["base_source"] == "fallback"
    assert len(gate["blocking"]) == 1
    assert "did not enforce it" in result.stderr
    assert "fallback base" in result.stderr
    assert "::warning::" in result.stderr
    assert "::error::" not in result.stderr


def test_an_iron_bank_build_enforces_the_same_verdict(tmp_path: Path) -> None:
    """The exception is scoped to the fallback: the identical report on an Iron Bank
    build is still a hard failure."""
    report, spdx, waivers = _fixture(tmp_path, [_match()])
    waivers.write_text(json.dumps({"waivers": []}), encoding="utf-8")

    result, gate = _gate(
        tmp_path, report, spdx, waivers, enforcement="enforced", base_source="ironbank"
    )

    assert result.returncode == 2, result.stderr
    assert gate["enforcement"] == "enforced"
    assert gate["base_source"] == "ironbank"
    assert "::error::" in result.stderr


def test_a_release_requires_iron_bank_credentials() -> None:
    """The fallback's advisory verdict must never reach a published image. The release
    preflight is the boundary: without the token the workflow stops before it builds,
    so an unenforced scan cannot be published by a dispatch that lost its secret."""
    release = yaml.safe_load(
        (_REPO_ROOT / ".github" / "workflows" / "release-runtime.yml").read_text(encoding="utf-8")
    )
    steps = release["jobs"]["preflight"]["steps"]
    guard = next(s for s in steps if s.get("name") == "Require Iron Bank credentials")

    assert guard["env"]["IRONBANK_TOKEN"] == "${{ secrets.IRONBANK_TOKEN }}"
    assert "exit 1" in guard["run"], "an empty token has to fail the preflight"


def test_an_empty_waiver_file_is_the_valid_end_state(tmp_path: Path) -> None:
    """The retainer deletes the last entry and leaves the file behind. A gate that
    rejected an empty list would turn the fix into a red pipeline."""
    report, spdx, waivers = _fixture(tmp_path, [], installed=KRB5_INSTALLED)
    waivers.write_text(json.dumps({"waivers": []}), encoding="utf-8")

    result, gate = _gate(tmp_path, report, spdx, waivers)

    assert result.returncode == 0, result.stderr
    assert gate["result"] == "pass"
    assert gate["waived"] == [] and gate["blocking"] == []
    assert gate["poam"] == []


def test_the_active_rhsa_waiver_passes_while_the_finding_stays_in_the_report(
    tmp_path: Path,
) -> None:
    """The whole point: 14 fixable High findings, zero blocking, and the report still
    names all 14 — a waiver that removed them would leave an artifact claiming the
    image is clean."""
    cves = sample_waiver()["cves"]
    report, spdx, waivers = _fixture(
        tmp_path, [_match(cve=cve) for cve in cves], installed=KRB5_INSTALLED
    )

    result, gate = _gate(tmp_path, report, spdx, waivers)

    assert result.returncode == 0, result.stderr
    assert gate["result"] == "pass"
    assert gate["blocking"] == []
    assert len(gate["waived"]) == len(cves)
    assert gate["waived_high"] == len(cves)
    assert gate["active_advisories"] == [sample_waiver()["advisory"]]
    assert gate["poam"][0]["state"] == "active"

    # And the report the gate read is untouched: the findings are still in it.
    kept = json.loads(report.read_text(encoding="utf-8"))["matches"]
    assert len(kept) == len(cves)


def test_a_waiver_matches_on_cve_and_package_not_the_installed_build(tmp_path: Path) -> None:
    """A POA&M acknowledges a CVE for a package until a date. The old exact-build
    match made the entry fail whenever either base patched on a different day, which
    is the whole problem a single waiver list cannot solve."""
    waiver = sample_waiver()
    waiver["installed_evr"] = "0:9.9.9-1.el9"  # an extra field must not affect matching
    report, spdx, waivers = _fixture(tmp_path, [_match()], waiver=waiver)

    result, gate = _gate(tmp_path, report, spdx, waivers)

    assert result.returncode == 0, result.stderr
    assert len(gate["waived"]) == 1
    assert gate["blocking"] == []


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


def test_a_malformed_poam_fails_instead_of_waiving_nothing(tmp_path: Path) -> None:
    """A missing list is a broken file. An empty list is not — that is the end state
    the retainer leaves behind."""
    report, spdx, waivers = _fixture(tmp_path, [_match()])

    waivers.write_text(json.dumps({"entries": []}), encoding="utf-8")
    result, _gate_doc = _gate(tmp_path, report, spdx, waivers)
    assert result.returncode == 2, result.stderr

    waivers.write_text(json.dumps({"waivers": "not-a-list"}), encoding="utf-8")
    result, _gate_doc = _gate(tmp_path, report, spdx, waivers)
    assert result.returncode == 2, result.stderr

    waivers.write_text("{not json", encoding="utf-8")
    result, _gate_doc = _gate(tmp_path, report, spdx, waivers)
    assert result.returncode == 2, result.stderr


def test_the_provenance_keeps_raw_counts_and_adds_the_poam_verdict(tmp_path: Path) -> None:
    """An auditor reads `high=14` and `0 unwaived`, not `high=0`: the severity counts
    stay what Grype found, and the waiver is reported beside them."""
    cves = sample_waiver()["cves"]
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
            str(_COMPLIANCE),
            "provenance",
            "--stem",
            str(stem),
            "--payload-manifest",
            str(payload),
            "--artifact-dir",
            str(tmp_path),
            "--image",
            "example.invalid/pypy311:3.11",
            "--gate",
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
    assert provenance["active_poam_advisories"] == [sample_waiver()["advisory"]]
    assert provenance["cve_gate"] == "pass"
    assert provenance["cve_gate_enforcement"] == "enforced"
    assert provenance["cve_gate_base_source"] == "ironbank"


def test_the_shipped_poam_is_a_usable_record() -> None:
    """Every entry the file holds has to be readable by the gate. The file is
    allowed to hold none: emptying the list is what the retainer does when the
    lag window closes, and that end state must not fail the build."""
    document = json.loads(_POAM.read_text(encoding="utf-8"))
    assert isinstance(document.get("waivers"), list), (
        "the file must carry the waivers key"
    )

    for entry in document["waivers"]:
        assert {
            "advisory",
            "package",
            "fixed_evr",
            "expires_on",
            "reason",
            "cves",
        } <= set(entry)
        assert re.fullmatch(r"RHSA-\d{4}:\d+", entry["advisory"]), entry["advisory"]
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


def test_the_provenance_step_is_handed_the_gate_verdict() -> None:
    """The gate file is a named argument, and dropping it fails silently: the
    provenance would simply carry no waiver record, so the artifact would stop saying
    which findings the gate released and under which advisory."""
    script = _SCRIPT.read_text(encoding="utf-8")
    invocation = script.split('python3 "${compliance_cli}" provenance', 1)[1]
    invocation = invocation.split("\n\n", 1)[0]

    assert '"${gate_file}"' in invocation, "the provenance step never sees the verdict"


def test_a_compliance_change_builds_the_canary_cell() -> None:
    """Never an empty matrix: a job with no cells is green, built nothing, and reads as
    a passing build. The waiver file is the input that decides whether every cell
    passes, so changing it has to build one."""
    from runtime_manifest import CANARY_ARCH, CANARY_RUNTIME, build_plan

    plan = build_plan(["tools/compliance/ubi-lag-waivers.json"])
    assert plan["mode"] == "canary"
    assert plan["runtimes"] == [CANARY_RUNTIME]
    assert plan["archs"] == [CANARY_ARCH]
    assert "compliance waiver changed" in plan["reason"]

    # Tooling and docs still build something rather than nothing.
    assert build_plan(["README.md", "tools/bin/check-container"])["runtimes"] == [CANARY_RUNTIME]
    assert build_plan([])["runtimes"] == [CANARY_RUNTIME], "an empty diff is still a canary"

    # A runtime change narrows the matrix, and the waiver change adds the canary to it.
    plan = build_plan(["runtimes/go127/runtime.yaml"])
    assert plan["mode"] == "narrowed" and plan["runtimes"] == ["go127"]

    plan = build_plan(["runtimes/go127/runtime.yaml", "tools/compliance/ubi-lag-waivers.json"])
    assert plan["mode"] == "narrowed"
    assert plan["runtimes"] == sorted([CANARY_RUNTIME, "go127"])

    # A path that only looks like a runtime directory is not one.
    assert build_plan(["runtimes/not-a-runtime/notes.txt"])["mode"] == "canary"


def test_the_matrix_action_classifies_through_the_generator() -> None:
    """The classification is one function, so it can be tested without a checkout. A
    grep in the composite action would be a second implementation that drifts."""
    action = yaml.safe_load(
        (_REPO_ROOT / ".github/actions/runtime-matrix/action.yml").read_text(encoding="utf-8")
    )
    step = next(s for s in action["runs"]["steps"] if s.get("id") == "matrix")

    assert "runtime_manifest.py changed --base" in step["run"]
    assert "--shell" in step["run"], "the plan has to reach the matrix inputs"
    assert "grep '^runtimes/'" not in step["run"], "the generator owns the classification"


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
        if s.get("name") == "Build, smoke-test and scan the image"
    )
    assert publish["uses"] == "./.github/actions/build-and-audit-runtime", (
        "the release must use the same build path as CI and the audit"
    )

    build_container = _BUILD_CONTAINER.read_text(encoding="utf-8")
    assert "BUILD_UBI_RPM_EPOCH=${ubi_rpm_epoch}" in build_container
    assert '--build-arg "UBI_RPM_EPOCH=${ubi_rpm_epoch}"' in build_container
    assert 'ubi_rpm_epoch="${UBI_RPM_EPOCH:-$(date -u +%Y-%m-%d)}"' in build_container