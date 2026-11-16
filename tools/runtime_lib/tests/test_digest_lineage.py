"""Digest lineage on a tag refresh.

A security refresh pushes new bytes over `<version>-<arch>`, so the digest that was
there becomes untagged. These tests pin the one fact that outlives the tag: the
provenance names the digest the build replaced, and says so explicitly when nothing
was replaced.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GENERATOR = _REPO_ROOT / "tools" / "bin" / "generate-compliance-artifacts"
_RELEASE = _REPO_ROOT / ".github/workflows/release-runtime.yml"

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
                "public.ecr.aws/alias": {
                    "reference": "public.ecr.aws/alias/pypy311:3.11-x86_64",
                    "digest": _OLD,
                }
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


def test_the_release_workflow_records_the_lineage_without_a_pruner() -> None:
    """The provenance is the record; deletion is not part of the release path."""
    workflow = yaml.safe_load(_RELEASE.read_text(encoding="utf-8"))
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert "prune_superseded" not in inputs

    steps = workflow["jobs"]["publish-containers"]["steps"]
    lineage = next(step for step in steps if step.get("name") == "Record the digest lineage")
    assert "supersedes_digest" in lineage["run"]
    assert "Digest lineage" in lineage["run"], "the mapping belongs in the release summary"
    assert "prune-superseded" not in lineage["run"], "the release no longer deletes digests"
