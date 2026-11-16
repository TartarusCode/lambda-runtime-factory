"""Digest lineage on a tag refresh, and the bottom-up pruner that follows it.

A security refresh pushes new bytes over `<version>-<arch>`, so the digest that was
there becomes untagged but stays pullable — along with the five referrers attached to
it. These tests pin the two halves of that lifecycle: the provenance quoting what the
build replaced, and the pruner removing only what nothing protects, referrers first.
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
_GENERATOR = _TOOLS / "generate-compliance-artifacts"
_PRUNER = _TOOLS / "prune-superseded-digests"
_RELEASE = _REPO_ROOT / ".github/workflows/release-runtime.yml"

_ACTIVE_TAG = "sha256:img"
_INDEX = "sha256:idx"
_INDEX_CHILD = "sha256:child"
_OLD = "sha256:old"


def _provenance(tmp_path: Path, container: dict) -> dict:
    """Run the provenance block the build runs, with this container.json."""
    stem = tmp_path / "pypy311-x86_64"
    for name in ("spdx", "cdx"):
        (tmp_path / f"pypy311-x86_64.{name}.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pypy311-x86_64.sbom-stats.json").write_text(
        json.dumps({"total": 10, "rpm": 8, "runtime": 2}), encoding="utf-8"
    )
    payload = tmp_path / "runtime-manifest.json"
    payload.write_text(
        json.dumps({"runtime_id": "pypy311", "arch": "x86_64", "distribution_version": "x"}),
        encoding="utf-8",
    )
    (tmp_path / "container.json").write_text(json.dumps(container), encoding="utf-8")

    blocks = re.findall(
        r"<<'PYEOF'\n(.*?)\nPYEOF", _GENERATOR.read_text(encoding="utf-8"), flags=re.S
    )
    assert len(blocks) >= 3
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            blocks[2],
            str(stem),
            str(payload),
            str(tmp_path),
            "example.invalid/pypy311:3.11",
            "false",
            "",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(Path(f"{stem}.provenance.json").read_text(encoding="utf-8"))


def test_the_provenance_quotes_the_digest_the_build_supersedes(tmp_path: Path) -> None:
    provenance = _provenance(
        tmp_path,
        {
            "base_image_ref": "registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal:latest",
            "supersedes_digest": _OLD,
            "supersedes_digests": {
                "public.ecr.aws/alias": {"reference": "public.ecr.aws/alias/pypy311:3.11-x86_64", "digest": _OLD}
            },
        },
    )
    assert provenance["supersedes_digest"] == _OLD


def test_the_provenance_records_null_when_nothing_was_replaced(tmp_path: Path) -> None:
    """A new tag and an unchanged image both mean "nothing replaced", and the key is
    present as null rather than missing: an auditor reading the attestation must be
    able to tell that the question was asked."""
    provenance = _provenance(
        tmp_path,
        {"base_image_ref": "registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal:latest"},
    )
    assert "supersedes_digest" in provenance
    assert provenance["supersedes_digest"] is None


def _stub_registry(tmp_path: Path) -> tuple[Path, Path]:
    """A registry that answers tags, manifests and referrers, and logs every delete."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "deletes.log"
    oras = bin_dir / "oras"
    oras.write_text(
        f"""#!/bin/sh
log="{log}"
case "$1" in
  repo)
    [ "$2" = "tags" ] && printf '1.0-amd64\\n1.0\\nlatest\\n'
    ;;
  manifest)
    case "$2" in
      fetch)
        for arg in "$@"; do ref="$arg"; done
        case "$ref" in
          *:1.0-amd64) echo '{{"digest":"{_ACTIVE_TAG}"}}' ;;
          *:1.0|*:latest) echo '{{"digest":"{_INDEX}"}}' ;;
          *@{_INDEX}) echo '{{"manifests":[{{"digest":"{_INDEX_CHILD}"}}]}}' ;;
          *) echo '{{}}' ;;
        esac
        ;;
      delete)
        for arg in "$@"; do last="$arg"; done
        echo "$last" >> "$log"
        ;;
    esac
    ;;
  discover)
    for arg in "$@"; do ref="$arg"; done
    case "$ref" in
      *@{_INDEX}) echo '{{"referrers":[{{"digest":"sha256:ref-index"}}]}}' ;;
      *@{_INDEX_CHILD}) echo '{{"referrers":[{{"digest":"sha256:ref-child"}}]}}' ;;
      *@{_OLD}) echo '{{"referrers":[{{"digest":"sha256:ref-old-1"}},{{"digest":"sha256:ref-old-2"}}]}}' ;;
      *) echo '{{}}' ;;
    esac
    ;;
esac
""",
        encoding="utf-8",
    )
    oras.chmod(0o755)
    return bin_dir, log


def _prune(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess:
    bin_dir, _log = _stub_registry(tmp_path)
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "RUNNER_TEMP": str(tmp_path),
            "CONTAINER_REGISTRIES_OVERRIDE": '["registry.example"]',
        }
    )
    return subprocess.run(
        [
            "bash",
            str(_PRUNER),
            "pypy311",
            "x86_64",
            "--registry",
            "registry.example",
            *arguments,
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=_REPO_ROOT,
    )


def test_an_active_tag_index_child_or_referrer_is_never_a_candidate(tmp_path: Path) -> None:
    """A tag is the published contract, an index's children are what a pull resolves
    to, and their referrers are the evidence attached to them. Naming any of them must
    not remove anything."""
    for protected in (_ACTIVE_TAG, _INDEX, _INDEX_CHILD, "sha256:ref-index"):
        result = _prune(tmp_path, "--superseded-digest", protected, "--delete")
        assert result.returncode == 0, result.stderr
        assert f"keep   {protected}" in result.stdout, result.stdout
        log = tmp_path / "deletes.log"
        assert not log.exists() or not log.read_text(encoding="utf-8").strip(), (
            f"{protected} is protected, and nothing may be deleted"
        )
        if log.exists():
            log.unlink()


def test_a_superseded_digest_loses_its_referrers_before_the_subject(tmp_path: Path) -> None:
    """Deleting the subject first leaves its referrers unreachable by discovery, so
    they can never be found to remove afterwards."""
    result = _prune(tmp_path, "--superseded-digest", _OLD, "--delete")
    assert result.returncode == 0, result.stderr

    run_log = tmp_path / "deletes.log"
    deleted = [line.rsplit("@", 1)[-1] for line in run_log.read_text(encoding="utf-8").splitlines()]

    assert deleted[-1] == _OLD, f"the subject has to go last: {deleted}"
    assert sorted(deleted[:2]) == ["sha256:ref-old-1", "sha256:ref-old-2"], deleted
    assert set(deleted) == {_OLD, "sha256:ref-old-1", "sha256:ref-old-2"}, (
        "nothing that a tag, an index or another referrer protects may be touched"
    )


def test_a_dry_run_removes_nothing(tmp_path: Path) -> None:
    result = _prune(tmp_path, "--superseded-digest", _OLD)
    assert result.returncode == 0, result.stderr
    assert f"delete {_OLD}" in result.stdout, "the plan still says what would go"
    assert "Dry run" in result.stdout
    log = tmp_path / "deletes.log"
    assert not log.exists(), "no manifest delete may be issued without --delete"


def test_the_release_workflow_records_the_lineage_and_can_prune() -> None:
    workflow = yaml.safe_load(_RELEASE.read_text(encoding="utf-8"))
    inputs = workflow[True]["workflow_dispatch"]["inputs"]

    assert inputs["prune_superseded"]["default"] is False, "deletion stays opt-in"
    assert inputs["prune_superseded"]["type"] == "boolean"

    steps = workflow["jobs"]["publish-containers"]["steps"]
    lineage = next(step for step in steps if step.get("name") == "Record the digest lineage")
    assert "supersedes_digest" in lineage["run"]
    assert "Digest lineage" in lineage["run"], "the mapping belongs in the release summary"
    assert "prune-superseded-digests" in lineage["run"]
    assert "--delete" in lineage["run"] and "--superseded-digest" in lineage["run"]
    assert "PRUNE_SUPERSEDED" in lineage["run"], "the input decides whether it runs"