#!/bin/bash
# Append this cell's artifact summary to the job summary.
#
# Reads what the build actually wrote rather than re-measuring anything: the
# container record from build-container, the provenance manifest from
# generate-compliance-artifacts, and the payload's own /opt/runtime-manifest.json.
# A cell that only built the payload (no image) still gets a row, with the image
# and SBOM columns empty rather than invented.
set -euo pipefail

runtime_id=$1
arch=$2

python3 - "${runtime_id}" "${arch}" "${GITHUB_STEP_SUMMARY:-/dev/null}" <<'PYEOF'
import json
import os
import sys

runtime_id, arch, summary_path = sys.argv[1:4]
artifact_dir = os.environ.get("ARTIFACT_DIR", "")

HEADER = (
    "| Runtime | Arch | Base Image | Unpacked Image (MB) | "
    "SBOM Packages (RPM + Runtime) | CVEs (C/H/M/L) | RIE Smoke Test |"
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

base_ref = container.get("base_image_ref") or provenance.get("base_image_ref") or ""
base = "IronBank" if "registry1.dso.mil" in base_ref else ("UBI-Fallback" if base_ref else "-")

size = container.get("unpacked_size_bytes") or container.get("unpacked_bytes") or 0
size_cell = f"{size / 1048576:.1f}" if size else "-"

counts = provenance.get("grype_summary") or {}
cves = (
    f"{counts.get('Critical', 0)}/{counts.get('High', 0)}/"
    f"{counts.get('Medium', 0)}/{counts.get('Low', 0)}"
    if counts else "-"
)

sbom = provenance.get("sbom") or {}
packages = sbom.get("packages")
packages_cell = f"{packages.get('rpm', 0)} + {packages.get('runtime', 0)}" if packages else "-"

smoke = "pass" if container.get("read_only_rootfs_verified") else ("built" if container else "-")

row = f"| `{runtime_id}` | `{arch}` | {base} | {size_cell} | {packages_cell} | {cves} | {smoke} |"

with open(summary_path, "a", encoding="utf-8") as handle:
    handle.write("\n" + HEADER + "\n" + SEPARATOR + "\n" + row + "\n")
PYEOF
