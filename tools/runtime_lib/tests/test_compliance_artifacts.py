"""Contract tests for the supply-chain bundle.

The compliance artifacts are the product now, so their shape is pinned here rather
than left to whichever script happened to write them last. Each test runs the real
snippet the build runs — extracted from the script itself — so a change to the
generator cannot drift away from the manifest an auditor reads.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOLS = _REPO_ROOT / "tools" / "bin"


def _heredoc(script: Path, index: int = 0) -> str:
    """The index-th embedded Python block a shell script pipes to `python3 -`."""
    text = script.read_text(encoding="utf-8")
    blocks = re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF", text, flags=re.S)
    assert blocks, f"{script} has no PYEOF block"
    return blocks[index]


def _run(code: str, args: list[str]) -> subprocess.CompletedProcess:
    # `python3 -c code a b` puts a, b at sys.argv[1:] — the same offsets the
    # scripts see when they pipe the block to `python3 -`.
    return subprocess.run(
        [sys.executable, "-c", code, *args], capture_output=True, text=True
    )


def test_container_json_carries_what_the_provenance_manifest_reads(tmp_path: Path) -> None:
    record = tmp_path / "container.json"
    result = _run(
        _heredoc(_TOOLS / "build-container"),
        [
            str(record),
            "pypy311",
            "x86_64",
            "example/pypy311:3.11-x86_64",
            "sha256:" + "a" * 64,
            "registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal:latest",
            "sha256:" + "b" * 64,
            "1234567",
            "1001:1001",
            "/var/task",
            '["/opt/bootstrap"]',
        ],
    )
    assert result.returncode == 0, result.stderr

    doc = json.loads(record.read_text(encoding="utf-8"))
    for key in (
        "runtime_id",
        "arch",
        "image_tag",
        "image_digest",
        "base_image_ref",
        "base_image_digest",
        "user",
        "workdir",
        "entrypoint",
        "package_managers_removed",
        "rpm_db_present",
    ):
        assert key in doc, key
    assert doc["entrypoint"] == ["/opt/bootstrap"]
    assert doc["user"] == "1001:1001"
    assert doc["unpacked_size_bytes"] == 1234567
    # Both spellings: the directive names one, older callers read the other.
    assert doc["unpacked_bytes"] == doc["unpacked_size_bytes"]


def test_the_provenance_manifest_records_the_evidence_a_cato_asks_for(tmp_path: Path) -> None:
    stem = tmp_path / "pypy311-x86_64"
    (tmp_path / "pypy311-x86_64.spdx.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pypy311-x86_64.cdx.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pypy311-x86_64.grype.json").write_text(
        json.dumps(
            {
                "matches": [
                    {"vulnerability": {"id": "CVE-1", "severity": "High"}},
                    {"vulnerability": {"id": "CVE-2", "severity": "Low"}},
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "pypy311-x86_64.sbom-stats.json").write_text(
        json.dumps({"total": 10, "rpm": 8, "runtime": 2}), encoding="utf-8"
    )
    payload = tmp_path / "runtime-manifest.json"
    payload.write_text(
        json.dumps(
            {
                "runtime_id": "pypy311",
                "arch": "x86_64",
                "distribution_version": "pypy3.11-v8.0.1",
                "upstream_archive_url": "https://example.invalid/pypy.tar.bz2",
                "upstream_sha256": "c" * 64,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "container.json").write_text(
        json.dumps(
            {
                "base_image_ref": "registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal:latest",
                "base_image_digest": "sha256:" + "d" * 64,
                "unpacked_size_bytes": 4096,
                "user": "1001:1001",
                "package_managers_removed": True,
                "rpm_db_present": True,
                "read_only_rootfs_verified": True,
            }
        ),
        encoding="utf-8",
    )

    result = _run(
        # Blocks in order: the SBOM assertion, the CVE detail, this manifest, the row.
        _heredoc(_TOOLS / "generate-compliance-artifacts", index=2),
        [str(stem), str(payload), str(tmp_path), "example.invalid/pypy311:3.11"],
    )
    assert result.returncode == 0, result.stderr

    provenance = json.loads(Path(f"{stem}.provenance.json").read_text(encoding="utf-8"))
    assert provenance["runtime_id"] == "pypy311"
    assert provenance["upstream_sha256"] == "c" * 64
    assert provenance["base_image_digest"] == "sha256:" + "d" * 64
    assert provenance["grype_summary"]["High"] == 1
    assert provenance["grype_summary"]["Low"] == 1
    assert provenance["sbom"]["packages"] == {"total": 10, "rpm": 8, "runtime": 2}
    assert set(provenance["stig_verification"]) == {
        "user",
        "package_managers_removed",
        "rpm_db_present",
        "read_only_rootfs_verified",
    }
    # The digests are of the documents that are actually published.
    assert len(provenance["sbom_sha256"]["spdx"]) == 64
    assert len(provenance["sbom_sha256"]["cdx"]) == 64


def test_the_job_summary_row_reads_the_bundle_it_reports(tmp_path: Path) -> None:
    (tmp_path / "container.json").write_text(
        json.dumps(
            {
                "base_image_ref": "registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal:latest",
                "unpacked_size_bytes": 4 * 1048576,
                "read_only_rootfs_verified": True,
            }
        ),
        encoding="utf-8",
    )
    compliance = tmp_path / "compliance"
    compliance.mkdir()
    (compliance / "pypy311-x86_64.provenance.json").write_text(
        json.dumps(
            {
                "base_image_ref": "registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal:latest",
                "grype_summary": {"Critical": 0, "High": 1, "Medium": 14, "Low": 1},
                "sbom": {"packages": {"total": 132, "rpm": 124, "runtime": 8}},
            }
        ),
        encoding="utf-8",
    )
    summary = tmp_path / "summary.md"

    script = _REPO_ROOT / ".github" / "actions" / "build-and-audit-runtime" / "report-package-size.sh"
    result = subprocess.run(
        ["bash", str(script), "pypy311", "x86_64"],
        capture_output=True,
        text=True,
        env={"ARTIFACT_DIR": str(tmp_path), "GITHUB_STEP_SUMMARY": str(summary), "PATH": "/usr/bin:/bin"},
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr

    text = summary.read_text(encoding="utf-8")
    assert text.count("| Runtime | Arch |") == 1, "a table header per row fragments the table"
    assert text.count("| --- | --- | --- | --- | --- | --- | --- |") == 1, "one separator per table"
    assert "`pypy311`" in text and "`x86_64`" in text
    assert "Iron Bank" in text, "the row must say which base was published"
    assert "4.0" in text, "the unpacked size is binary megabytes"
    assert "124 + 8" in text, "the SBOM package split is the compliance evidence"
    assert "0/1/14/1" in text
    assert "| pass |" in text, "the smoke test is the one claim that must never be assumed"


def test_every_cell_adds_a_row_to_the_same_table(tmp_path: Path) -> None:
    """A job that reports several cells appends rows to one table: a second
    header would render as a second table, and a markdown table needs its blank
    line and separator exactly once."""
    (tmp_path / "container.json").write_text(
        json.dumps({"base_image_ref": "registry.access.redhat.com/ubi9/ubi-minimal:latest",
                    "unpacked_size_bytes": 1048576, "read_only_rootfs_verified": True}),
        encoding="utf-8",
    )
    summary = tmp_path / "summary.md"
    script = _REPO_ROOT / ".github" / "actions" / "build-and-audit-runtime" / "report-package-size.sh"

    def report(runtime: str, arch: str, artifact_dir: Path) -> None:
        result = subprocess.run(
            ["bash", str(script), runtime, arch],
            capture_output=True,
            text=True,
            env={"ARTIFACT_DIR": str(artifact_dir), "GITHUB_STEP_SUMMARY": str(summary),
                 "PATH": "/usr/bin:/bin"},
            cwd=_REPO_ROOT,
        )
        assert result.returncode == 0, result.stderr

    report("pypy311", "x86_64", tmp_path)
    report("pypy311", "arm64", tmp_path)
    report("rust199", "arm64", tmp_path / "absent")

    text = summary.read_text(encoding="utf-8")
    assert text.count("| Runtime | Arch |") == 1
    assert text.count("| --- | --- | --- | --- | --- | --- | --- |") == 1, "one separator per table"
    assert text.count("| `pypy311` |") == 2, "one row per reported cell"
    assert "UBI fallback" in text, "the fallback base is named as such"
    assert "| `rust199` |" not in text, "an unbuilt cell is not a row of dashes"
    assert "rust199 (arm64)" in text and "no image record" in text


def test_no_workflow_or_script_references_the_removed_layer_tools() -> None:
    """A stale call site is how a release dies minutes in, per cell. The tokens are
    full script paths so that legitimate names — `audit-runtimes.yml`, the
    `build-and-audit-runtime` action — cannot trip the check."""
    removed = (
        "tools/bin/package_size",
        "tools/bin/audit-runtime",
        "tools/bin/latest-runtime",
        "tools/bin/upload-runtime",
        "tools/bin/publish-runtime",
        "tools/bin/unpublish-runtime",
        "tools/bin/create-buckets",
        "tools/bin/local-build-runtime",
        "tools/bin/local-invoke-runtime",
        "runtime_lib/package_size.py",
        "Dockerfile.ironbank",
        "layer_publishable",
    )
    hits = []
    for root in (_REPO_ROOT / ".github", _REPO_ROOT / "tools"):
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".yml", ".sh", ".py"}:
                continue
            # The tests name these on purpose; scanning them would only find this list.
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for token in removed:
                if token in text:
                    hits.append(f"{path.relative_to(_REPO_ROOT)}: {token}")
    assert not hits, "\n".join(hits)


def test_the_compliance_bundle_is_generated_after_the_smoke_test() -> None:
    """Provenance cites the STIG check the smoke test performs, so the order is
    part of the contract: generating first would record an unproven claim."""
    action = yaml.safe_load(
        (_REPO_ROOT / ".github" / "actions" / "build-and-audit-runtime" / "action.yml").read_text(
            encoding="utf-8"
        )
    )
    names = [step.get("name") for step in action["runs"]["steps"]]
    generator = names.index("Generate the compliance bundle")
    assert generator > names.index("Smoke-test the container image")
    assert generator > names.index("Check the image contract")

    step = next(s for s in action["runs"]["steps"] if s.get("name") == "Generate the compliance bundle")
    assert "generate-compliance-artifacts" in step["run"]
    assert step["if"] == "inputs.container == 'true'"


def test_the_image_builds_its_lineage_assertion_into_the_layer() -> None:
    dockerfile = (_REPO_ROOT / "tools" / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert "/var/lib/rpm" in dockerfile and "/usr/lib/sysimage/rpm" in dockerfile
    assert "/etc/os-release" in dockerfile
    assert "os-release present" in dockerfile, "the assertion must be observable in the build log"
    assert "rm -f /usr/bin/microdnf /usr/bin/dnf /usr/bin/yum" in dockerfile
    assert "microdnf install -y --nodocs --setopt=install_weak_deps=0" in dockerfile


def test_attaching_the_bundle_uses_paths_oras_accepts() -> None:
    """`oras attach` refuses an absolute path by default, because the reference
    must not describe the host that produced it. A release that passed the
    artifact's full path failed with "absolute file paths are insecure" after the
    image had already been signed, so the attach runs from the bundle directory
    with bare filenames."""
    script = (_REPO_ROOT / "tools" / "bin" / "attest-container").read_text(encoding="utf-8")

    assert 'cd "${compliance_dir}"' in script
    assert "--disable-path-validation" not in script, "the path check is the guard, not an obstacle"

    # The existence check above the loop may use the absolute stem; the attach
    # itself may not.
    attach = script.split('cd "${compliance_dir}"', 1)[1]
    assert "${stem}" not in attach, "oras gets the absolute stem, which it rejects"
    for name in ("spdx", "cdx", "grype", "provenance"):
        assert f"\"${{runtime_id}}-${{arch}}.{name}.json:" in attach


def test_signing_and_attaching_pin_the_digest_not_the_tag() -> None:
    """A tag can be repointed, so a signature over it does not cover the bytes a
    consumer later pulls — cosign says so itself when handed a tag. The digest is
    resolved from the registry once and both the signature and every attachment
    name that digest; the tag stays the human-facing handle."""
    script = (_REPO_ROOT / "tools" / "bin" / "attest-container").read_text(encoding="utf-8")

    assert "oras manifest fetch --descriptor" in script, "the digest must come from the registry"
    assert 'reference="${repository}@${digest}"' in script

    body = script.split('for registry in "${registries[@]}"; do', 1)[1]
    # Join the `\` continuations so each registry call is one logical command.
    joined = body.replace("\\\n", " ")
    pinned = [
        line.strip()
        for line in joined.splitlines()
        if line.strip().startswith(("cosign", "oras attach"))
    ]
    assert pinned, "the loop must sign and attach something"
    for line in pinned:
        assert "tag_reference" not in line, f"a tag reached the registry call: {line}"
        assert '"${reference}"' in line, f"not pinned to the digest: {line}"
