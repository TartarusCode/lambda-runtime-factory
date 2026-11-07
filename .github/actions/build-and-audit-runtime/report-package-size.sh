#!/bin/bash
# Append this cell's row to the job summary.
#
# Reads what the build wrote rather than re-measuring anything: container.json
# from build-container, and the provenance manifest from
# generate-compliance-artifacts. The heading and table header are written once
# per summary, so a job that reports several cells gets one table instead of one
# broken table per cell.
set -euo pipefail

runtime_id=$1
arch=$2

python3 - "${runtime_id}" "${arch}" "${GITHUB_STEP_SUMMARY:-/dev/null}" <<'PYEOF'
import json
import os
import sys

runtime_id, arch, summary_path = sys.argv[1:4]
artifact_dir = os.environ.get("ARTIFACT_DIR", "")

HEADING = "## Runtime image"
HEADER = (
    "| Runtime | Arch | Base image | Unpacked image (MiB) "
    "| SBOM packages (RPM + runtime) | CVEs (C/H/M/L) | Smoke test |"
)
SEPARATOR = "| --- | --- | --- | --- | --- | --- | --- |"


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


container = load(os.path.join(artifact_dir, "container.json")) if artifact_dir else {}
provenance = load(
    os.path.join(artifact_dir, "compliance", f"{runtime_id}-{arch}.provenance.json")
) if artifact_dir else {}

# Nothing recorded is not a row: a table of dashes reads like a cell that
# produced empty artifacts, which is a different claim from "it never got here".
if not container and not provenance:
    block = (
        f"\n_{runtime_id} ({arch}): no image record was written, so the cell "
        "stopped before the image existed._\n"
    )
else:
    base_ref = container.get("base_image_ref") or provenance.get("base_image_ref") or ""
    if "registry1.dso.mil" in base_ref:
        base = "Iron Bank"
    elif base_ref:
        base = "UBI fallback"
    else:
        base = "-"

    size = container.get("unpacked_size_bytes") or container.get("unpacked_bytes") or 0
    size_cell = f"{size / 1048576:.1f}" if size else "-"

    counts = provenance.get("grype_summary") or {}
    cves = (
        f"{counts.get('Critical', 0)}/{counts.get('High', 0)}/"
        f"{counts.get('Medium', 0)}/{counts.get('Low', 0)}"
        if counts else "-"
    )

    packages = (provenance.get("sbom") or {}).get("packages")
    packages_cell = f"{packages.get('rpm', 0)} + {packages.get('runtime', 0)}" if packages else "-"

    verified = container.get("read_only_rootfs_verified")
    smoke = "pass" if verified else ("not verified" if container else "-")

    row = (
        f"| `{runtime_id}` | `{arch}` | {base} | {size_cell} "
        f"| {packages_cell} | {cves} | {smoke} |"
    )

    try:
        with open(summary_path, encoding="utf-8") as handle:
            existing = handle.read()
    except OSError:
        existing = ""

    # A markdown table needs a blank line before it and its header exactly once;
    # a second cell adds a row, not another table.
    if HEADER in existing:
        block = row + "\n"
    else:
        block = "\n" + HEADING + "\n\n" + HEADER + "\n" + SEPARATOR + "\n" + row + "\n"

with open(summary_path, "a", encoding="utf-8") as handle:
    handle.write(block)
PYEOF
