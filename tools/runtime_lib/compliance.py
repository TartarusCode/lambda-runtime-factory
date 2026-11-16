#!/usr/bin/env python3
"""The compliance bundle: SBOM assertions, the CVE gate and provenance.

This module owns the decisions that used to live in heredocs inside
``tools/bin/generate-compliance-artifacts``. The shell script still orchestrates
Syft, Grype and the registry calls; everything that reads or writes a document
lives here, where it can be imported by a test instead of extracted by regex.

Subcommands:

    sbom-check   assert the SPDX/CycloneDX pair describes the image that ships
    gate         apply the UBI-lag POA&M to a Grype report
    provenance   write <stem>.provenance.json from the payload and gate documents
    summary      append the GitHub job-summary row for a provenance document

The waiver file is deliberately small: an entry names an advisory, a package, the
fixed EVR the repository is waiting for, an expiry and the CVEs it releases. The
gate matches on CVE and package; ``fixed_evr`` belongs to the retirement job
(``bump_version.py prune-waivers``), not to finding matching. Retirement is the
only thing that removes an entry, which is why a resolved waiver is reported, not
enforced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, NoReturn, Sequence, Set, Tuple

import ubi_repo

SEVERITIES = ("critical", "high")
REQUIRED = ("advisory", "package", "fixed_evr", "expires_on", "reason", "cves")


def label(runtime_id: str, arch: str, text: str) -> str:
    return f"{runtime_id} ({arch}): {text}" if runtime_id else text


def _die(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _load(path: str, what: str) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as error:
        _die(f"{what}: {error}")
    except ValueError as error:
        _die(f"{what} is not valid JSON: {error}")


def load_waivers(path: str) -> List[Dict[str, Any]]:
    raw = _load(path, f"the UBI-lag POA&M file {path}")
    entries = raw.get("waivers") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        _die(f"{path} has no 'waivers' list")

    waivers: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries):
        where = f"{path} waiver #{index + 1}"
        missing = [key for key in REQUIRED if not entry.get(key)]
        if missing:
            _die(f"{where} is missing {', '.join(missing)}")
        cves = entry.get("cves")
        if not isinstance(cves, list) or not cves:
            _die(f"{where} lists no CVEs")
        try:
            expires = date.fromisoformat(str(entry["expires_on"]))
        except ValueError:
            _die(f"{where} has expires_on={entry['expires_on']!r}, which is not YYYY-MM-DD")
        waivers.append(
            {
                "advisory": str(entry["advisory"]),
                "package": str(entry["package"]),
                "fixed_evr": ubi_repo.normalize_evr(str(entry["fixed_evr"])),
                "expires_on": expires,
                "reason": str(entry["reason"]),
                "cves": {str(cve).upper() for cve in cves},
            }
        )
    return waivers


def _installed_versions(spdx_path: str, package: str) -> Set[str]:
    """What the image carries for one package, read from the SBOM it was scanned from."""
    if not spdx_path or not os.path.exists(spdx_path):
        return set()
    try:
        document = _load(spdx_path, spdx_path)
    except SystemExit:
        return set()
    wanted = package.lower()
    return {
        ubi_repo.normalize_evr(str(entry.get("versionInfo") or ""))
        for entry in (document.get("packages") or [])
        if str(entry.get("name") or "").lower() == wanted
    }


def _poam(
    waivers: Sequence[Dict[str, Any]], today: date, spdx_path: str
) -> List[Dict[str, Any]]:
    entries = []
    for waiver in waivers:
        versions = _installed_versions(spdx_path, waiver["package"])
        if waiver["expires_on"] < today:
            state = "expired"
        elif versions and any(ubi_repo.is_at_least(version, waiver["fixed_evr"]) for version in versions):
            state = "resolved"
        else:
            state = "active"
        entries.append(
            {
                "advisory": waiver["advisory"],
                "package": waiver["package"],
                "fixed_evr": waiver["fixed_evr"],
                "state": state,
                "expires_on": waiver["expires_on"].isoformat(),
                "cves": sorted(waiver["cves"]),
            }
        )
    return entries


def evaluate_gate(
    *,
    report_path: str,
    waivers_path: str,
    spdx_path: str,
    today_text: str,
    runtime_id: str,
    arch: str,
    enforcement: str,
    base_source: str,
) -> Tuple[Dict[str, Any], int]:
    """Apply the POA&M to the report and return (gate document, exit code)."""
    waivers = load_waivers(waivers_path)
    report = _load(report_path, f"the Grype report {report_path}")
    today = date.fromisoformat(today_text) if today_text else date.today()

    poam = _poam(waivers, today, spdx_path)
    blocking: List[Dict[str, str]] = []
    waived: List[Dict[str, str]] = []

    for match in report.get("matches") or []:
        vulnerability = match.get("vulnerability") or {}
        severity = str(vulnerability.get("severity") or "").lower()
        if severity not in SEVERITIES:
            continue
        fix = vulnerability.get("fix") or {}
        fixed_versions = [ubi_repo.normalize_evr(str(item)) for item in (fix.get("versions") or [])]
        if str(fix.get("state") or "").lower() != "fixed" or not fixed_versions:
            continue

        artifact = match.get("artifact") or {}
        package = str(artifact.get("name") or "")
        installed_version = ubi_repo.normalize_evr(str(artifact.get("version") or ""))
        cve = str(vulnerability.get("id") or "").upper()

        covering = next(
            (
                waiver
                for waiver in waivers
                if waiver["package"].lower() == package.lower()
                and cve in waiver["cves"]
                and waiver["expires_on"] >= today
            ),
            None,
        )
        record = {
            "cve": cve,
            "package": package,
            "installed_version": installed_version,
            "fixed_version": fixed_versions[0],
            "severity": severity,
        }
        if covering is None:
            blocking.append(record)
        else:
            waived.append(
                {
                    **record,
                    "advisory": covering["advisory"],
                    "expires_on": covering["expires_on"].isoformat(),
                }
            )

    result = "fail" if blocking else "pass"
    gate = {
        "runtime_id": runtime_id,
        "arch": arch,
        "today": today.isoformat(),
        "result": result,
        "enforcement": enforcement,
        "base_source": base_source,
        "policy": (
            "fixable High and Critical findings fail an Iron Bank build unless an active, "
            "unexpired UBI-lag POA&M entry names that CVE and package; a fallback build "
            "records the verdict without enforcing it"
        ),
        "blocking": blocking,
        "waived": waived,
        "waived_high": sum(1 for item in waived if item["severity"] == "high"),
        "waived_critical": sum(1 for item in waived if item["severity"] == "critical"),
        "active_advisories": sorted({item["advisory"] for item in waived}),
        "poam": poam,
    }

    for record in blocking:
        print(
            label(
                runtime_id,
                arch,
                f"  blocking {record['cve']} {record['package']} "
                f"{record['installed_version']} -> {record['fixed_version']}",
            ),
            file=sys.stderr,
        )
    for record in waived:
        print(
            label(
                runtime_id,
                arch,
                f"  waived {record['cve']} {record['package']} {record['installed_version']} "
                f"-> {record['fixed_version']} [{record['advisory']}]",
            ),
            file=sys.stderr,
        )
    for entry in poam:
        if entry["state"] == "active":
            print(
                label(
                    runtime_id,
                    arch,
                    f"POA&M {entry['advisory']}: {entry['package']} {entry['fixed_evr']} "
                    f"waived until {entry['expires_on']} ({len(entry['cves'])} CVE(s))",
                ),
                file=sys.stderr,
            )
        elif entry["state"] == "resolved":
            notice = label(
                runtime_id,
                arch,
                f"POA&M {entry['advisory']} is now resolved upstream — "
                "time to clean up tools/compliance/ubi-lag-waivers.json",
            )
            print(f"::notice::{notice}", file=sys.stderr)
        else:
            print(
                label(
                    runtime_id,
                    arch,
                    f"POA&M {entry['advisory']} for {entry['package']} expired on "
                    f"{entry['expires_on']} — remove it from tools/compliance/ubi-lag-waivers.json",
                ),
                file=sys.stderr,
            )

    summary = (
        f"{len(blocking)} unwaived fixable High/Critical CVE(s), "
        f"{len(waived)} waived ({gate['waived_high']} High, {gate['waived_critical']} Critical)"
    )
    if result == "fail" and enforcement == "advisory":
        message = label(
            runtime_id,
            arch,
            "the severity gate recorded a failure but did not enforce it: "
            f"this image was built from the {base_source} base, which is not a release "
            f"artifact; {summary}",
        )
        print(f"::warning::{message}", file=sys.stderr)
        return gate, 0
    if result == "fail":
        message = label(runtime_id, arch, f"the severity gate failed: {summary}")
        print(f"::error::{message}" if runtime_id else message, file=sys.stderr)
        return gate, 2
    print(label(runtime_id, arch, f"the severity gate passed: {summary}"), file=sys.stderr)
    return gate, 0


def check_sboms(spdx_path: str, runtime_id: str, stats_path: str, cdx_path: str = "") -> None:
    """One pass must describe the base, the payload and a non-empty CycloneDX set."""
    document = _load(spdx_path, f"{spdx_path}: the SBOM")
    packages = document.get("packages") or []
    if not packages:
        raise SystemExit(f"{spdx_path}: the SBOM has no packages at all")

    def purls(package: Dict[str, Any]) -> List[str]:
        return [
            ref.get("referenceLocator", "")
            for ref in (package.get("externalRefs") or [])
            if str(ref.get("referenceType", "")).lower() == "purl"
        ]

    rpm_packages = [p for p in packages if any(loc.startswith("pkg:rpm/") for loc in purls(p))]
    runtime_packages = [p for p in packages if p not in rpm_packages]

    base_names = ("glibc", "zlib", "openssl-libs", "ca-certificates")
    seen = {name: False for name in base_names}
    for package in packages:
        name = (package.get("name") or "").lower()
        for wanted in base_names:
            if name == wanted or name.startswith(wanted + "-"):
                seen[wanted] = True
    missing = [name for name, found in seen.items() if not found]
    if missing:
        raise SystemExit(
            f"{spdx_path}: no UBI9 base packages ({', '.join(missing)}) — the RPM database "
            "did not reach Syft, so this SBOM cannot carry RHEL backport lineage"
        )
    if not runtime_packages:
        raise SystemExit(f"{spdx_path}: no runtime packages — the SBOM describes the base but not /opt")

    if cdx_path:
        components = _load(cdx_path, f"{cdx_path}: the CycloneDX document").get("components") or []
        if not components:
            raise SystemExit(f"{cdx_path}: the CycloneDX document has no components")

    stats = {"total": len(packages), "rpm": len(rpm_packages), "runtime": len(runtime_packages)}
    Path(stats_path).write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"SBOM: {stats['total']} packages ({stats['rpm']} rpm, {stats['runtime']} runtime), base present"
    )


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _docker_format(template: str, reference: str) -> str:
    try:
        return subprocess.run(
            ["docker", "image", "inspect", "--format", template, reference],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return ""


def build_provenance(
    *,
    stem: str,
    payload_manifest: str,
    artifact_dir: str,
    image: str,
    gate_path: str = "",
) -> Dict[str, Any]:
    payload = _load(payload_manifest, f"{payload_manifest}: the payload manifest")
    container_record: Dict[str, Any] = {}
    record_path = os.path.join(artifact_dir, "container.json")
    if os.path.exists(record_path):
        container_record = _load(record_path, record_path)

    grype_path = stem + ".grype.json"
    counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0, "Negligible": 0, "Unknown": 0}
    if os.path.exists(grype_path):
        for match in (_load(grype_path, grype_path).get("matches") or []):
            severity = ((match.get("vulnerability") or {}).get("severity") or "Unknown").capitalize()
            counts[severity if severity in counts else "Unknown"] += 1

    provenance: Dict[str, Any] = {
        "runtime_id": payload.get("runtime_id"),
        "arch": payload.get("arch"),
        "distribution_version": payload.get("distribution_version"),
        "upstream_archive_url": payload.get("upstream_archive_url"),
        "upstream_sha256": payload.get("upstream_sha256"),
        "base_image_ref": container_record.get("base_image_ref", ""),
        "base_image_digest": container_record.get("base_image_digest", ""),
        "supersedes_digest": container_record.get("supersedes_digest"),
        "image_digest": _docker_format("{{.Id}}", image),
        "unpacked_size_bytes": container_record.get("unpacked_size_bytes", 0),
        "stig_verification": {
            "user": container_record.get("user", ""),
            "package_managers_removed": container_record.get("package_managers_removed", False),
            "rpm_db_present": container_record.get("rpm_db_present", False),
            "read_only_rootfs_verified": container_record.get("read_only_rootfs_verified", False),
        },
        "sbom": {
            "packages": _load(stem + ".sbom-stats.json", stem + ".sbom-stats.json")
            if os.path.exists(stem + ".sbom-stats.json")
            else {},
        },
        "sbom_sha256": {
            "spdx": _sha256(stem + ".spdx.json") if os.path.exists(stem + ".spdx.json") else "",
            "cdx": _sha256(stem + ".cdx.json") if os.path.exists(stem + ".cdx.json") else "",
        },
        "grype_summary": counts,
    }

    gate: Dict[str, Any] = {}
    if gate_path and os.path.exists(gate_path):
        gate = _load(gate_path, gate_path)
    if gate:
        provenance.update(
            {
                "cve_gate": gate.get("result", ""),
                "cve_gate_enforcement": gate.get("enforcement", "enforced"),
                "cve_gate_base_source": gate.get("base_source", "ironbank"),
                "ubi_lag_waived_high": gate.get("waived_high", 0),
                "ubi_lag_waived_critical": gate.get("waived_critical", 0),
                "waived_cve_ids": sorted(
                    item.get("cve", "") for item in (gate.get("waived") or [])
                ),
                "unwaived_blocking_cves": len(gate.get("blocking") or []),
                "unwaived_blocking_cve_ids": sorted(
                    item.get("cve", "") for item in (gate.get("blocking") or [])
                ),
                "active_poam_advisories": gate.get("active_advisories") or [],
            }
        )

    out = stem + ".provenance.json"
    Path(out).write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Provenance: {out}")
    print(
        f"CVEs: critical={counts['Critical']} high={counts['High']} "
        f"medium={counts['Medium']} low={counts['Low']}"
    )
    return provenance


def write_summary(provenance_path: str, summary_path: str) -> None:
    provenance = _load(provenance_path, provenance_path)
    counts = provenance["grype_summary"]
    base = "IronBank" if "registry1.dso.mil" in provenance["base_image_ref"] else "UBI-Fallback"
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write(
            "| {runtime_id} | {arch} | {base} | {size:.1f} | {sbom} | {c}/{h}/{m}/{l} |\n".format(
                runtime_id=provenance["runtime_id"],
                arch=provenance["arch"],
                base=base,
                size=(provenance["unpacked_size_bytes"] or 0) / 1048576,
                sbom="spdx+cdx",
                c=counts["Critical"],
                h=counts["High"],
                m=counts["Medium"],
                l=counts["Low"],
            )
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sbom = sub.add_parser("sbom-check")
    sbom.add_argument("--spdx", required=True)
    sbom.add_argument("--runtime", required=True)
    sbom.add_argument("--stats", required=True)
    sbom.add_argument("--cdx", default="")

    gate = sub.add_parser("gate")
    gate.add_argument("--report", required=True)
    gate.add_argument("--waivers", required=True)
    gate.add_argument("--gate-out", required=True)
    gate.add_argument("--spdx", required=True)
    gate.add_argument("--today", default="")
    gate.add_argument("--runtime", default="")
    gate.add_argument("--arch", default="")
    gate.add_argument("--enforcement", default="enforced")
    gate.add_argument("--base-source", default="ironbank")

    provenance = sub.add_parser("provenance")
    provenance.add_argument("--stem", required=True)
    provenance.add_argument("--payload-manifest", required=True)
    provenance.add_argument("--artifact-dir", required=True)
    provenance.add_argument("--image", required=True)
    provenance.add_argument("--gate", default="")

    summary = sub.add_parser("summary")
    summary.add_argument("--provenance", required=True)
    summary.add_argument("--summary", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "sbom-check":
        check_sboms(args.spdx, args.runtime, args.stats, args.cdx)
        return 0
    if args.command == "gate":
        gate, status = evaluate_gate(
            report_path=args.report,
            waivers_path=args.waivers,
            spdx_path=args.spdx,
            today_text=args.today,
            runtime_id=args.runtime,
            arch=args.arch,
            enforcement=args.enforcement,
            base_source=args.base_source,
        )
        Path(args.gate_out).write_text(
            json.dumps(gate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return status
    if args.command == "provenance":
        build_provenance(
            stem=args.stem,
            payload_manifest=args.payload_manifest,
            artifact_dir=args.artifact_dir,
            image=args.image,
            gate_path=args.gate,
        )
        return 0
    write_summary(args.provenance, args.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
