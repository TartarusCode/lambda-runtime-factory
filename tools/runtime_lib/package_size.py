"""Deployment-package size accounting against the Lambda layer budget.

AWS caps a deployment package at 250 MB unzipped, and that ceiling covers the
function plus every layer attached to it — which is why the six toolchain runtimes
cannot be delivered as a layer at all. The build measures the package it just
produced and refuses to call an oversized one publishable, so the failure lands in
CI instead of at ``PublishLayerVersion`` in the middle of a release.

Kept as a module so the build, the job summary and the tests all read the limit and
the arithmetic the same way.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runtime_manifest  # noqa: E402  (sibling module, resolved via sys.path above)

MAX_UNZIPPED_BYTES = 262144000
WARN_UNZIPPED_BYTES = 209715200
BYTES_PER_MB = 1048576


def measure(package_path: Path) -> Dict[str, int]:
    """Return the zipped and unzipped byte counts of a package.

    ``unzipped`` is the sum of the entry sizes, which is how Lambda accounts for a
    deployment package: a package measured at 265,000,000 bytes here is exactly the
    one AWS refused for exceeding 262,144,000.
    """
    path = Path(package_path)
    with zipfile.ZipFile(path) as archive:
        unzipped = sum(info.file_size for info in archive.infolist())
    return {"zipped_bytes": path.stat().st_size, "unzipped_bytes": unzipped}


def budget(
    stats: Dict[str, Any],
    *,
    max_unzipped_bytes: int = MAX_UNZIPPED_BYTES,
    warn_unzipped_bytes: int = WARN_UNZIPPED_BYTES,
    publishable: bool = True,
) -> Dict[str, Any]:
    """Judge a measured package against the budget.

    ``level`` is ``ok``, ``warning`` (publishable but leaving little of the quota for
    the function's own code) or ``error`` (over the ceiling while claiming to be
    publishable — the build fails on this). A runtime that is not publishable is
    never an error: its size is simply recorded.
    """
    unzipped = int(stats["unzipped_bytes"])
    headroom_mb = (max_unzipped_bytes - unzipped) / BYTES_PER_MB
    percent = 100.0 * unzipped / max_unzipped_bytes
    # A package one byte over the ceiling must not read as "100.0% of quota" in the
    # job summary, so past the limit the percentage rounds up instead of to nearest.
    if unzipped > max_unzipped_bytes:
        percent_of_quota = math.ceil(percent * 10) / 10
    else:
        percent_of_quota = round(percent, 1)
    runtime_id = stats.get("runtime_id", "")
    arch = stats.get("arch", "")

    if publishable and unzipped >= max_unzipped_bytes:
        level = "error"
        message = (
            f"{runtime_id} ({arch}) unzips to {unzipped} bytes, "
            f"{unzipped - max_unzipped_bytes} bytes over the {max_unzipped_bytes}-byte "
            "deployment-package limit. Slim the payload, or mark it "
            "layer_publishable: false and deliver it another way (see DELIVERY.md)."
        )
    elif publishable and unzipped > warn_unzipped_bytes:
        level = "warning"
        message = (
            f"{runtime_id} ({arch}) leaves only {headroom_mb:.1f} MB of unzipped headroom "
            "under the 250 MB limit, which the function's own code must share."
        )
    else:
        level = "ok"
        message = (
            f"{runtime_id} ({arch}): zipped {stats['zipped_bytes'] / BYTES_PER_MB:.1f} MB, "
            f"unzipped {unzipped / BYTES_PER_MB:.1f} MB — {percent_of_quota:.1f}% of the "
            f"{max_unzipped_bytes}-byte quota, {headroom_mb:.1f} MB left for function code"
        )

    return {
        "runtime_id": runtime_id,
        "arch": arch,
        "zipped_bytes": int(stats["zipped_bytes"]),
        "unzipped_bytes": unzipped,
        "max_unzipped_bytes": max_unzipped_bytes,
        "warn_unzipped_bytes": warn_unzipped_bytes,
        "layer_publishable": bool(publishable),
        "percent_of_quota": percent_of_quota,
        "headroom_mb": round(headroom_mb, 1),
        "level": level,
        "message": message,
        # The image size is measured after the package and stored in the same
        # sidecar; the row reports both halves of the artifact decision.
        "container_image_bytes": stats.get("container_image_bytes"),
        "container_image": stats.get("container_image"),
    }


def summary_row(stats: Dict[str, Any]) -> str:
    """Return the Markdown row used in the job summary."""
    container_bytes = stats.get("container_image_bytes")
    container_mb = (
        f"{int(container_bytes) / BYTES_PER_MB:.1f}" if container_bytes else "n/a"
    )
    return (
        "| Runtime | Arch | Zipped (MB) | Unzipped (Bytes / MB) | % of 250 MB Quota | "
        "Function Headroom (MB) | Layer Publishable | Container Image (MB) |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        f"| `{stats['runtime_id']}` | `{stats['arch']}` | "
        f"{stats['zipped_bytes'] / BYTES_PER_MB:.1f} | "
        f"{stats['unzipped_bytes']} / {stats['unzipped_bytes'] / BYTES_PER_MB:.1f} | "
        f"{stats['percent_of_quota']:.1f}% | {stats['headroom_mb']:.1f} | "
        f"{'yes' if stats['layer_publishable'] else 'no'} | {container_mb} |\n\n"
    )


def _package_in(artifact_dir: Path) -> Optional[Path]:
    if not artifact_dir.is_dir():
        return None
    packages = sorted(artifact_dir.glob("*.zip"), key=lambda path: path.stat().st_size)
    return packages[-1] if packages else None


def _stats_for(
    runtime_id: str, arch: str, artifact_dir: Path, publishable: bool
) -> Optional[Dict[str, Any]]:
    """Read build-stats.json, or measure the package when the build was cached."""
    stats_path = artifact_dir / "build-stats.json"
    if stats_path.exists():
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        stats.setdefault("runtime_id", runtime_id)
        stats.setdefault("arch", arch)
        stats.setdefault("layer_publishable", publishable)
        return stats

    package = _package_in(artifact_dir)
    if package is None:
        return None
    measured: Dict[str, Any] = dict(measure(package))
    measured.update(
        {"runtime_id": runtime_id, "arch": arch, "layer_publishable": publishable}
    )
    return measured


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Measure a package, write the sidecar and judge it")
    check.add_argument("--runtime", required=True)
    check.add_argument("--arch", required=True)
    check.add_argument("--package", required=True)
    check.add_argument("--stats-out", required=True)
    check.add_argument("--publishable", default="true")
    check.add_argument("--max-unzipped-bytes", type=int, default=MAX_UNZIPPED_BYTES)
    check.add_argument("--warn-unzipped-bytes", type=int, default=WARN_UNZIPPED_BYTES)

    summary = sub.add_parser("summary", help="Append this cell's budget row to the job summary")
    summary.add_argument("--runtime", required=True)
    summary.add_argument("--arch", required=True)
    summary.add_argument("--summary-file", default=None)

    args = parser.parse_args()

    if args.command == "check":
        measured = measure(Path(args.package))
        measured.update({"runtime_id": args.runtime, "arch": args.arch})
        verdict = budget(
            measured,
            max_unzipped_bytes=args.max_unzipped_bytes,
            warn_unzipped_bytes=args.warn_unzipped_bytes,
            publishable=args.publishable == "true",
        )
        Path(args.stats_out).write_text(
            json.dumps(
                {
                    "runtime_id": verdict["runtime_id"],
                    "arch": verdict["arch"],
                    "zipped_bytes": verdict["zipped_bytes"],
                    "unzipped_bytes": verdict["unzipped_bytes"],
                    "max_unzipped_bytes": verdict["max_unzipped_bytes"],
                    "warn_unzipped_bytes": verdict["warn_unzipped_bytes"],
                    "layer_publishable": verdict["layer_publishable"],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if verdict["level"] == "warning":
            print(f"::warning::{verdict['message']}")
        elif verdict["level"] == "error":
            print(f"::error::{verdict['message']}", file=sys.stderr)
        print(verdict["message"])
        raise SystemExit(1 if verdict["level"] == "error" else 0)

    # summary
    env = runtime_manifest.runtime_env(args.runtime, arch=args.arch)
    artifact_dir = Path(env["ARTIFACT_DIR"])
    stats = _stats_for(
        args.runtime, args.arch, artifact_dir, env["LAYER_PUBLISHABLE"] == "true"
    )
    if stats is None:
        # Nothing was built (cache hit without a sidecar, or the build failed
        # before writing one): the failure is already visible in the job.
        raise SystemExit(0)

    verdict = budget(
        stats,
        max_unzipped_bytes=int(stats.get("max_unzipped_bytes", MAX_UNZIPPED_BYTES)),
        warn_unzipped_bytes=int(stats.get("warn_unzipped_bytes", WARN_UNZIPPED_BYTES)),
        publishable=bool(stats.get("layer_publishable", True)),
    )
    row = summary_row(verdict)
    summary_file = args.summary_file or env.get("GITHUB_STEP_SUMMARY") or "/dev/null"
    with open(summary_file, "a", encoding="utf-8") as handle:
        handle.write(row)
    print(row.rstrip())


if __name__ == "__main__":
    main()
