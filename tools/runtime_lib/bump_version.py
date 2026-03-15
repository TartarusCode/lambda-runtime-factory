"""Bump a runtime's distribution version and refresh checksums from upstream."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from runtime_manifest import (
    DEFAULT_ARCH,
    RUNTIME_FAMILY_DEFAULTS,
    SUPPORTED_ARCHS,
    _apply_defaults,
    list_runtime_ids,
    load_runtime,
    runtime_manifest_path,
    runtimes_root,
)


def _http_get(url: str, *, accept: str = "*/*") -> bytes:
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "lambda-runtime-bump/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _http_get_text(url: str) -> str:
    return _http_get(url).decode("utf-8").strip()


def _sha256_from_download(url: str) -> str:
    """Download a file and compute its SHA-256 hash."""
    sha = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "lambda-runtime-bump/1.0"})
    with urllib.request.urlopen(request, timeout=300) as response:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            sha.update(chunk)
    return sha.hexdigest()


def _fetch_checksum_go(version: str, archive_name: str) -> str:
    data = json.loads(_http_get_text("https://go.dev/dl/?mode=json"))
    for release in data:
        if release["version"] != f"go{version}":
            continue
        for file_info in release["files"]:
            if file_info["filename"] == archive_name:
                return file_info["sha256"]
    raise ValueError(f"Checksum not found for {archive_name} in Go {version} release metadata")


def _fetch_checksum_bun(version: str, archive_name: str) -> str:
    shasums = _http_get_text(
        f"https://github.com/oven-sh/bun/releases/download/bun-v{version}/SHASUMS256.txt"
    )
    for line in shasums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == archive_name:
            return parts[0]
    raise ValueError(f"Checksum not found for {archive_name} in Bun v{version} SHASUMS256.txt")


def _fetch_checksum_graalpy(version: str, archive_name: str) -> str:
    sha_url = (
        f"https://github.com/oracle/graalpython/releases/download/"
        f"graal-{version}/{archive_name}.sha256"
    )
    content = _http_get_text(sha_url)
    return content.split()[0]


def _fetch_checksum_rust(version: str, archive_name: str) -> str:
    sha_url = f"https://static.rust-lang.org/dist/{archive_name}.sha256"
    content = _http_get_text(sha_url)
    return content.split()[0]


def _fetch_checksum_pypy(version: str, archive_name: str) -> str:
    """Download the PyPy archive and compute its SHA-256 (no central checksum index)."""
    url = f"https://downloads.python.org/pypy/{archive_name}"
    print(f"  Downloading {archive_name} to compute SHA-256 (no upstream checksum index)...")
    return _sha256_from_download(url)


CHECKSUM_FETCHERS = {
    "go-toolchain": _fetch_checksum_go,
    "bun": _fetch_checksum_bun,
    "graalpy": _fetch_checksum_graalpy,
    "rust-musl": _fetch_checksum_rust,
    "portable-pypy": _fetch_checksum_pypy,
}


def resolve_archive_name(runtime_family: str, version: str, arch: str) -> str:
    """Build the archive filename for a given family, version, and architecture."""
    family = RUNTIME_FAMILY_DEFAULTS[runtime_family]
    arch_slug = family["arch_map"][arch]
    template = family["artifact"]["checksum_name"]
    return template.format(distribution_version=version, arch_slug=arch_slug)


def fetch_checksums(
    runtime_family: str, version: str
) -> List[Tuple[str, str]]:
    """Fetch checksums for all architectures. Returns [(hash, archive_name), ...]."""
    fetcher = CHECKSUM_FETCHERS.get(runtime_family)
    if fetcher is None:
        raise ValueError(f"No checksum fetcher for runtime family '{runtime_family}'")

    results = []
    for arch in SUPPORTED_ARCHS:
        archive_name = resolve_archive_name(runtime_family, version, arch)
        print(f"  Fetching checksum for {archive_name} ...")
        sha = fetcher(version, archive_name)
        results.append((sha, archive_name))
        print(f"    {sha}  {archive_name}")
    return results


def bump_runtime(runtime_id: str, new_version: str, *, dry_run: bool = False) -> None:
    manifest_path = runtime_manifest_path(runtime_id)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Runtime manifest not found: {manifest_path}")

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_version = raw["distribution_version"]
    runtime_family = raw["runtime_family"]

    if old_version == new_version:
        print(f"{runtime_id}: already at version {new_version}")
        return

    print(f"{runtime_id}: {old_version} -> {new_version}")
    checksums = fetch_checksums(runtime_family, new_version)

    if dry_run:
        print("  (dry run — no files modified)")
        return

    raw["distribution_version"] = new_version
    manifest_path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"  Updated {manifest_path}")

    resolved = _apply_defaults(runtime_id, raw)
    checksum_rel = resolved["artifact"]["checksum_file"]
    checksum_path = runtimes_root() / runtime_id / checksum_rel
    checksum_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{sha}  {name}\n" for sha, name in checksums]
    checksum_path.write_text("".join(lines), encoding="utf-8")
    print(f"  Updated {checksum_path}")


def check_latest_go() -> Optional[str]:
    data = json.loads(_http_get_text("https://go.dev/dl/?mode=json"))
    for release in data:
        if release["stable"]:
            return release["version"].removeprefix("go")
    return None


def check_latest_bun() -> Optional[str]:
    data = json.loads(_http_get(
        "https://api.github.com/repos/oven-sh/bun/releases/latest",
        accept="application/vnd.github+json",
    ))
    tag = data.get("tag_name", "")
    return tag.removeprefix("bun-v") if tag else None


def check_latest_graalpy() -> Optional[str]:
    data = json.loads(_http_get(
        "https://api.github.com/repos/oracle/graalpython/releases/latest",
        accept="application/vnd.github+json",
    ))
    tag = data.get("tag_name", "")
    return tag.removeprefix("graal-") if tag else None


def check_latest_rust() -> Optional[str]:
    channel = _http_get_text("https://static.rust-lang.org/dist/channel-rust-stable.toml")
    in_pkg_rust = False
    for line in channel.splitlines():
        stripped = line.strip()
        if stripped == "[pkg.rust]":
            in_pkg_rust = True
            continue
        if in_pkg_rust and stripped.startswith("version"):
            raw = stripped.split("=", 1)[1].strip().strip('"')
            return raw.split()[0]
        if stripped.startswith("[") and in_pkg_rust:
            break
    return None


def check_latest_pypy() -> Optional[str]:
    data = json.loads(_http_get_text("https://downloads.python.org/pypy/versions.json"))
    for release in data:
        if release.get("stable") and release.get("python_version", "").startswith("3.11"):
            return f"pypy3.11-v{release['pypy_version']}"
    return None


LATEST_CHECKERS = {
    "go-toolchain": check_latest_go,
    "bun": check_latest_bun,
    "graalpy": check_latest_graalpy,
    "rust-musl": check_latest_rust,
    "portable-pypy": check_latest_pypy,
}


def check_updates(runtime_ids: Optional[List[str]] = None) -> Dict[str, Tuple[str, str]]:
    """Check for available updates. Returns {runtime_id: (current, latest)} for outdated runtimes."""
    if runtime_ids is None:
        runtime_ids = list_runtime_ids()

    outdated: Dict[str, Tuple[str, str]] = {}
    for runtime_id in runtime_ids:
        manifest_path = runtime_manifest_path(runtime_id)
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        family = raw["runtime_family"]
        current = raw["distribution_version"]

        checker = LATEST_CHECKERS.get(family)
        if checker is None:
            print(f"{runtime_id}: no update checker for family '{family}'")
            continue

        print(f"{runtime_id}: checking for updates (current: {current}) ...")
        try:
            latest = checker()
        except Exception as exc:
            print(f"  Failed to check: {exc}")
            continue

        if latest is None:
            print(f"  Could not determine latest version")
            continue

        if latest == current:
            print(f"  Up to date ({current})")
        else:
            print(f"  Update available: {current} -> {latest}")
            outdated[runtime_id] = (current, latest)

    return outdated


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bump runtime distribution versions and refresh checksums."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bump_parser = subparsers.add_parser("bump", help="Bump a runtime to a new version")
    bump_parser.add_argument("runtime", help="Runtime ID (e.g. go126)")
    bump_parser.add_argument("version", help="New distribution version (e.g. 1.26.2)")
    bump_parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")

    check_parser = subparsers.add_parser("check", help="Check all runtimes for available updates")
    check_parser.add_argument("--runtime", help="Check a specific runtime only")
    check_parser.add_argument("--json", action="store_true", help="Output as JSON")

    bump_all_parser = subparsers.add_parser("bump-latest", help="Bump all outdated runtimes to latest")
    bump_all_parser.add_argument("--runtime", help="Only bump a specific runtime")
    bump_all_parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "bump":
        bump_runtime(args.runtime, args.version, dry_run=args.dry_run)
        return

    if args.command == "check":
        runtime_ids = [args.runtime] if args.runtime else None
        outdated = check_updates(runtime_ids)
        if args.json:
            print(json.dumps(
                {rid: {"current": cur, "latest": lat} for rid, (cur, lat) in outdated.items()},
                indent=2,
            ))
        if not outdated:
            print("\nAll runtimes are up to date.")
        else:
            print(f"\n{len(outdated)} runtime(s) have updates available.")
        return

    if args.command == "bump-latest":
        runtime_ids = [args.runtime] if args.runtime else None
        outdated = check_updates(runtime_ids)
        if not outdated:
            print("\nNothing to bump.")
            return
        for runtime_id, (current, latest) in outdated.items():
            print(f"\nBumping {runtime_id} ...")
            bump_runtime(runtime_id, latest, dry_run=args.dry_run)
        return

    parser.error("Unhandled command")


if __name__ == "__main__":
    main()
