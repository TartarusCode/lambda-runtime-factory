"""Contract tests for the supply-chain bundle.

The compliance artifacts are the product now, so their shape is pinned here rather
than left to whichever script happened to write them last. Each test runs the real
snippet the build runs — extracted from the script itself — so a change to the
generator cannot drift away from the manifest an auditor reads.
"""

from __future__ import annotations

import json
import os
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
    assert 'oras attach --artifact-type "$2" "${reference}" "$1:$2"' in attach
    for name in ("spdx", "cdx", "grype", "provenance"):
        assert f'attach_if_missing "${{runtime_id}}-${{arch}}.{name}.json" ' in attach


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


def test_every_published_reference_gets_its_own_signature() -> None:
    """A consumer verifies the digest it actually pulls. The SOCI copy and the two
    merged indexes are different digests from the per-arch image, so each needs its
    own signature — a signed manifest underneath an unsigned index proves nothing
    to someone pulling the tag."""
    script = (_REPO_ROOT / "tools" / "bin" / "attest-container").read_text(encoding="utf-8")
    assert "--target" in script and "--sign-only" in script
    for target in ("standard", "soci", "merged", "merged-soci"):
        assert f"{target}) tag=" in script, f"attest-container cannot target {target}"
    assert "CONTAINER_SOCI_SUFFIX" in script

    workflow = yaml.safe_load(
        (_REPO_ROOT / ".github/workflows/release-runtime.yml").read_text(encoding="utf-8")
    )
    publish_steps = workflow["jobs"]["publish-containers"]["steps"]
    sign = next(s for s in publish_steps if s.get("name") == "Sign and attest the image")
    assert "--target soci" in sign["run"], "the SOCI copy is published unsigned"
    assert "inputs.publish_soci" in sign["run"], "only sign a copy this run published"

    merge_steps = workflow["jobs"]["merge-containers"]["steps"]
    merge_sign = next(s for s in merge_steps if s.get("name") == "Sign the multi-arch indexes")
    assert "--target merged --sign-only" in merge_sign["run"]
    assert "--target merged-soci" in merge_sign["run"]
    assert "inputs.publish_soci" in merge_sign["run"]

    # The tools have to be on PATH before the signing step, and the tags have to
    # exist before the digests can be resolved.
    tools_at = next(
        index
        for index, step in enumerate(merge_steps)
        if step.get("uses") == "./.github/actions/compliance-tools"
    )
    assert tools_at < merge_steps.index(merge_sign), "cosign and oras must be installed first"
    tags_at = next(index for index, step in enumerate(merge_steps)
                   if step.get("name") == "Create the multi-arch tags")
    assert tags_at < merge_steps.index(merge_sign), "the index must exist before it is signed"


def test_signing_an_index_attaches_no_bundle() -> None:
    """The multi-arch index describes no single SBOM: each platform manifest
    carries its own. `--sign-only` skips both the attachment and the requirement
    that the artifacts be on disk, which is what lets the merge job sign."""
    script = (_REPO_ROOT / "tools" / "bin" / "attest-container").read_text(encoding="utf-8")

    assert "if (( ! sign_only )); then" in script
    guarded = script.split("if (( ! sign_only )); then")
    assert len(guarded) == 3, "both the artifact check and the attach block are gated"
    assert "oras attach" in guarded[2], "the attach block sits behind the gate"


def test_the_compliance_tools_match_the_runner_architecture() -> None:
    """Release assets are named for the CPU. An arm64 runner that downloads the
    x86_64 asset installs a binary it cannot exec, and nothing notices until the
    signing step calls it — inside a release, after the image is already pushed."""
    action = yaml.safe_load(
        (_REPO_ROOT / ".github/actions/compliance-tools/action.yml").read_text(encoding="utf-8")
    )
    step = next(
        s for s in action["runs"]["steps"] if s.get("name") == "Install Syft, Cosign and Oras"
    )
    run = step["run"]

    assert "uname -m" in run, "the asset arch comes from the runner, not a constant"
    assert "cosign-linux-${tool_arch}" in run
    assert "linux_${tool_arch}.tar.gz" in run
    assert "cosign-linux-amd64" not in run, "a hardcoded arch silently breaks the other runner"
    assert "_amd64.tar.gz" not in run

    cache_step = next(
        s for s in action["runs"]["steps"] if s.get("name") == "Cache Syft, Cosign and Oras"
    )
    assert cache_step["with"]["key"].startswith("compliance-tools-v2-"), (
        "a v1 entry may hold a binary for another CPU"
    )


def test_only_ecr_registries_carry_a_soci_copy() -> None:
    """Lambda reads SOCI indexes from ECR, so a copy elsewhere is bytes nothing
    consumes. The rule lives in one helper rather than in each script."""
    environment = dict(os.environ)
    environment.pop("SOCI_REGISTRIES", None)
    environment.pop("CONTAINER_REGISTRIES_OVERRIDE", None)
    environment["CONTAINER_REGISTRIES"] = json.dumps(
        [
            "ghcr.io/example/project",
            "public.ecr.aws/alias/project",
            "123456789.dkr.ecr.us-east-1.amazonaws.com/project",
        ]
    )
    result = subprocess.run(
        ["bash", "-c", "source tools/bin/_common.sh; common::soci_registries"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        "public.ecr.aws/alias/project",
        "123456789.dkr.ecr.us-east-1.amazonaws.com/project",
    ]


def test_soci_attestation_follows_the_soci_publication_policy() -> None:
    """Attesting a SOCI digest on a registry that never received the copy fails
    with "Cannot resolve ... to a digest". Both scripts ask the same helper."""
    publish = (_REPO_ROOT / "tools/bin/publish-soci-index").read_text(encoding="utf-8")
    attest = (_REPO_ROOT / "tools/bin/attest-container").read_text(encoding="utf-8")
    for script in (publish, attest):
        assert "common::soci_registries" in script, "one rule, both callers"

    body = attest.split('case "${target}" in', 1)[1]
    soci_branch = body.split("soci | merged-soci)", 1)[1].split(";;", 1)[0]
    other_branch = body.split("*)", 1)[1].split(";;", 1)[0]
    assert "common::soci_registries" in soci_branch
    assert "common::soci_registries" not in other_branch, (
        "a plain image is published everywhere and must be attested everywhere"
    )


def test_a_transient_registry_answer_is_retried(tmp_path) -> None:
    """A tag resolves before every layer is readable, so a pull moments after a
    push can answer `not found` for a blob. Retrying separates that from a
    genuinely missing image, and gives up rather than looping."""
    script = """
source tools/bin/_common.sh
export RETRY_DELAY=0
attempts=0
flaky() { attempts=$((attempts + 1)); [ "$attempts" -ge 3 ]; }
common::retry 5 flaky && echo "recovered after $attempts"
common::retry 2 false || echo "gave up"
"""
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, cwd=_REPO_ROOT
    )
    assert result.returncode == 0, result.stderr
    assert "recovered after 3" in result.stdout
    assert "gave up" in result.stdout

    soci = (_REPO_ROOT / "tools/bin/publish-soci-index").read_text(encoding="utf-8")
    assert soci.count("common::retry 5") == 2, "the pull and the read-back both retry"


def _attest_calls(tmp_path: Path, present: tuple[str, ...]) -> list[str]:
    """Run attest-container against a stubbed registry; return the calls it made.

    The stub answers `oras discover` with the referrers listed in `present`, so the
    test drives the idempotency decision rather than reading the script for it.
    """
    artifacts = tmp_path / "lambda-runtime-monorepo" / "pypy311" / "x86_64" / "artifacts"
    compliance = artifacts / "compliance"
    compliance.mkdir(parents=True)
    for name in ("spdx", "cdx", "grype", "provenance"):
        (compliance / f"pypy311-x86_64.{name}.json").write_text("{}", encoding="utf-8")

    referrers = tmp_path / "referrers.json"
    referrers.write_text(
        json.dumps({"referrers": [{"artifactType": kind} for kind in present]}), encoding="utf-8"
    )
    calls = tmp_path / "calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    oras = bin_dir / "oras"
    oras.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  discover) cat "{referrers}" ;;\n'
        """  manifest) echo '{"digest":"sha256:deadbeef"}' ;;\n"""
        f'  attach) echo "ATTACH $3" >> "{calls}" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    oras.chmod(0o755)
    cosign = bin_dir / "cosign"
    cosign.write_text(f'#!/bin/sh\necho "COSIGN" >> "{calls}"\n', encoding="utf-8")
    cosign.chmod(0o755)

    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            # _common.sh derives ARTIFACT_DIR from the build root, so point the
            # root at the fixture instead of trying to set the derived path.
            "RUNNER_TEMP": str(tmp_path),
            "SIGSTORE_ID_TOKEN": "stub",
            "CONTAINER_REGISTRIES_OVERRIDE": '["public.ecr.aws/alias/project"]',
        }
    )
    result = subprocess.run(
        ["bash", str(_REPO_ROOT / "tools/bin/attest-container"), "pypy311", "x86_64"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    return calls.read_text(encoding="utf-8").split() if calls.exists() else []


SIGNATURE = "application/vnd.dev.sigstore.bundle.v0.3+json"
ARTIFACT_TYPES = (
    "application/spdx+json",
    "application/vnd.cyclonedx+json",
    "application/vnd.grype.report.v1+json",
    "application/vnd.lambda-runtime.provenance.v1+json",
)


def test_a_fresh_digest_gets_signed_and_fully_attested(tmp_path: Path) -> None:
    calls = _attest_calls(tmp_path, present=())

    assert calls.count("COSIGN") == 1
    attached = [call for call, _kind in zip(calls, calls[1:]) if call == "ATTACH"]
    assert sorted(calls[i + 1] for i, call in enumerate(calls) if call == "ATTACH") == sorted(
        ARTIFACT_TYPES
    )
    assert len(attached) == 4


def test_re_attesting_the_same_digest_writes_nothing(tmp_path: Path) -> None:
    """A second release over an unchanged image resolves to the same digest, and
    both oras and cosign append — so a re-run doubles every referrer."""
    calls = _attest_calls(tmp_path, present=(*ARTIFACT_TYPES, SIGNATURE))

    assert "COSIGN" not in calls
    assert "ATTACH" not in calls


def test_a_partial_previous_run_is_completed_not_repeated(tmp_path: Path) -> None:
    """Self-healing: the two artifacts and the signature already on the digest are
    left alone, the two missing ones are written."""
    calls = _attest_calls(
        tmp_path, present=(ARTIFACT_TYPES[0], ARTIFACT_TYPES[1], SIGNATURE)
    )

    assert "COSIGN" not in calls
    attached = sorted(calls[i + 1] for i, call in enumerate(calls) if call == "ATTACH")
    assert attached == [ARTIFACT_TYPES[2], ARTIFACT_TYPES[3]]


def test_pruning_keeps_one_referrer_of_each_type_and_only_deletes_on_request(
    tmp_path: Path,
) -> None:
    """Releases used to append a second copy of every referrer on an unchanged
    digest. Pruning keeps the oldest of each type — the one existing records point
    at — and removes nothing at all unless `--delete` is passed."""
    referrers = tmp_path / "referrers.json"
    referrers.write_text(
        json.dumps(
            {
                "referrers": [
                    {
                        "artifactType": "application/spdx+json",
                        "digest": "sha256:old",
                        "annotations": {
                            "org.opencontainers.image.created": "2026-11-08T03:00:00Z"
                        },
                    },
                    {
                        "artifactType": "application/spdx+json",
                        "digest": "sha256:new",
                        "annotations": {
                            "org.opencontainers.image.created": "2026-11-08T04:00:00Z"
                        },
                    },
                    {"artifactType": SIGNATURE, "digest": "sha256:only"},
                ]
            }
        ),
        encoding="utf-8",
    )
    deleted = tmp_path / "deleted.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    oras = bin_dir / "oras"
    oras.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "manifest" ] && [ "$2" = "delete" ]; then echo "$3" >> "{deleted}"; exit 0; fi\n'
        """if [ "$1" = "manifest" ]; then echo '{"digest":"sha256:deadbeef"}'; exit 0; fi\n"""
        f'if [ "$1" = "discover" ]; then cat "{referrers}"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    oras.chmod(0o755)

    def run(*arguments: str):
        environment = dict(os.environ)
        environment.update(
            {
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "RUNNER_TEMP": str(tmp_path),
                "CONTAINER_REGISTRIES_OVERRIDE": '["public.ecr.aws/alias/project"]',
            }
        )
        return subprocess.run(
            [
                "bash",
                str(_REPO_ROOT / "tools/bin/prune-duplicate-referrers"),
                "pypy311",
                "x86_64",
                *arguments,
            ],
            capture_output=True,
            text=True,
            env=environment,
            cwd=_REPO_ROOT,
        )

    dry = run()
    assert dry.returncode == 0, dry.stderr
    assert "keep   sha256:old" in dry.stdout
    assert "delete sha256:new" in dry.stdout
    assert "1 duplicate(s) would be removed" in dry.stdout
    assert not deleted.exists(), "a dry run must not delete anything"

    real = run("--delete")
    assert real.returncode == 0, real.stderr
    assert "removed sha256:new" in real.stdout
    removals = deleted.read_text(encoding="utf-8").split()
    assert len(removals) == 1, "the only signature on the digest must survive"
    assert removals[0].endswith("@sha256:new")
