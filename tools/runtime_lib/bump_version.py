"""Bump a runtime's distribution version and refresh checksums from upstream."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
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


def _fetch_checksum_deno(version: str, archive_name: str) -> str:
    sha_url = (
        f"https://github.com/denoland/deno/releases/download/v{version}/"
        f"{archive_name}.sha256sum"
    )
    content = _http_get_text(sha_url)
    for line in content.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == archive_name:
            return parts[0]
    raise ValueError(f"Checksum not found for {archive_name} in Deno v{version} release")


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
    "deno": _fetch_checksum_deno,
    "graalpy": _fetch_checksum_graalpy,
    "rust-musl": _fetch_checksum_rust,
    "portable-pypy": _fetch_checksum_pypy,
}


def resolve_archive_name(
    runtime_family: str, version: str, arch: str, python_version: str = ""
) -> str:
    """Build the archive filename for a given family, version, and architecture."""
    family = RUNTIME_FAMILY_DEFAULTS[runtime_family]
    arch_slug = family["arch_map"][arch]
    template = family["artifact"]["checksum_name"]
    return template.format(
        distribution_version=version,
        arch_slug=arch_slug,
        python_version=python_version,
    )


def fetch_checksums(
    runtime_family: str, version: str, python_version: str = ""
) -> List[Tuple[str, str]]:
    """Fetch checksums for all architectures. Returns [(hash, archive_name), ...]."""
    fetcher = CHECKSUM_FETCHERS.get(runtime_family)
    if fetcher is None:
        raise ValueError(f"No checksum fetcher for runtime family '{runtime_family}'")

    results = []
    for arch in SUPPORTED_ARCHS:
        archive_name = resolve_archive_name(
            runtime_family, version, arch, python_version=python_version
        )
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
    python_version = raw.get("python_version", "")

    if old_version == new_version:
        print(f"{runtime_id}: already at version {new_version}")
        return

    print(f"{runtime_id}: {old_version} -> {new_version}")
    checksums = fetch_checksums(
        runtime_family, new_version, python_version=python_version
    )

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


def _version_tuple(version: str) -> Optional[List[int]]:
    """Parse a dotted version string into ints, or None if malformed."""
    parts = version.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return [int(part) for part in parts]


def _same_line(version: str, line: str) -> bool:
    """Return whether version belongs to the major.minor line."""
    version_parts = _version_tuple(version)
    line_parts = _version_tuple(line)
    if version_parts is None or line_parts is None:
        return False
    return version_parts[0:2] == line_parts[0:2]


def _version_line_of(version: str) -> str:
    """Return the major.minor line for a dotted version, or '' if malformed."""
    parts = _version_tuple(version)
    if parts is None or len(parts) < 2:
        return ""
    return f"{parts[0]}.{parts[1]}"


LINE_FAMILIES: Dict[str, str] = {
    "bun": "bun",
    "deno": "deno",
    "go-toolchain": "go",
    "rust-musl": "rust",
}


def _new_line_id(family: str, version: str) -> str:
    """Derive a runtime id for a new version line, e.g. ('go', '1.27.0') -> 'go127'."""
    prefix = LINE_FAMILIES[family]
    line = _version_line_of(version)
    if not line:
        raise ValueError(f"Cannot derive line id from version '{version}' for family '{family}'")
    return f"{prefix}{line.replace('.', '')}"


def _new_display_name(family: str, version: str) -> str:
    """Derive a display name for a new version line, e.g. ('go-toolchain', '1.27.0') -> 'Go 1.27'."""
    prefix = LINE_FAMILIES[family]
    label = "Go" if prefix == "go" else prefix.capitalize()
    line = _version_line_of(version)
    if not line:
        raise ValueError(f"Cannot derive display name from version '{version}' for family '{family}'")
    return f"{label} {line}"


def detect_new_lines() -> Dict[str, Dict[str, str]]:
    """Return new version lines available upstream but not tracked by any runtime.

    Returns {runtime_family: {'id', 'line', 'version'}}. Only line-based families
    (bun/deno/go/rust) are considered; graalpy and pypy use their own schemes.
    """
    existing_lines = {
        (raw["runtime_family"], raw.get("version_line", ""))
        for runtime_id in list_runtime_ids()
        for raw in [json.loads(runtime_manifest_path(runtime_id).read_text(encoding="utf-8"))]
        if raw.get("version_line")
    }

    new_lines: Dict[str, Dict[str, str]] = {}
    for family in LINE_FAMILIES:
        checker = LATEST_CHECKERS.get(family)
        if checker is None:
            continue
        try:
            latest = checker("")
        except Exception as exc:
            print(f"{family}: failed to check latest: {exc}")
            continue
        if not latest:
            continue
        line = _version_line_of(latest)
        if not line or (family, line) in existing_lines:
            continue
        new_lines[family] = {
            "id": _new_line_id(family, latest),
            "line": line,
            "version": latest,
        }
    return new_lines


def add_runtime_line(family: str, version: str, *, dry_run: bool = False) -> str:
    """Create a runtime directory for a new version line by cloning the newest same-family runtime."""
    new_id = _new_line_id(family, version)
    dest = runtimes_root() / new_id
    if dest.exists():
        print(f"{new_id}: already exists")
        return new_id

    if dry_run:
        print(f"  (dry run) would create {new_id} at version {version} (line {_version_line_of(version)})")
        return new_id

    family_runtimes = [
        (runtime_id, json.loads(runtime_manifest_path(runtime_id).read_text(encoding="utf-8")))
        for runtime_id in list_runtime_ids()
        if json.loads(runtime_manifest_path(runtime_id).read_text(encoding="utf-8")).get("runtime_family") == family
    ]
    if not family_runtimes:
        raise ValueError(f"No existing runtime for family '{family}' to clone from")

    source_id = max(family_runtimes, key=lambda item: _version_tuple(item[1]["distribution_version"]) or [0])[0]

    shutil.copytree(runtimes_root() / source_id, dest)
    print(f"{source_id} -> {new_id}: cloned runtime directory")

    manifest_path = dest / "runtime.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime_id"] = new_id
    manifest["distribution_version"] = version
    manifest["version_line"] = _version_line_of(version)
    manifest["display_name"] = _new_display_name(family, version)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for rel in ("examples/sam/Makefile", "examples/sls/serverless.yml"):
        path = dest / rel
        if path.exists():
            text = path.read_text(encoding="utf-8").replace(source_id, new_id)
            path.write_text(text, encoding="utf-8")

    checksums = fetch_checksums(family, version)
    checksum_rel = manifest.get("artifact", {}).get("checksum_file", "")
    if not checksum_rel:
        raise ValueError(f"Manifest for {new_id} has no artifact.checksum_file")
    checksum_path = dest / checksum_rel
    checksum_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{sha}  {name}\n" for sha, name in checksums]
    checksum_path.write_text("".join(lines), encoding="utf-8")

    print(f"  Created {new_id} at version {version} (line {_version_line_of(version)})")
    return new_id


def check_latest_go(version_line: str = "") -> Optional[str]:
    data = json.loads(_http_get_text("https://go.dev/dl/?mode=json"))
    recent: Optional[str] = None
    recent_tuple: Optional[List[int]] = None
    for release in data:
        if not release["stable"]:
            continue
        version = release["version"].removeprefix("go")
        version_parts = _version_tuple(version)
        if version_parts is None:
            continue
        if version_line and not _same_line(version, version_line):
            continue
        if recent_tuple is None or version_parts > recent_tuple:
            recent = version
            recent_tuple = version_parts
    return recent


def check_latest_bun(version_line: str = "") -> Optional[str]:
    data = json.loads(_http_get(
        "https://api.github.com/repos/oven-sh/bun/releases?per_page=100",
        accept="application/vnd.github+json",
    ))
    recent: Optional[str] = None
    recent_tuple: Optional[List[int]] = None
    for release in data:
        tag = release.get("tag_name", "")
        if not tag.startswith("bun-v"):
            continue
        version = tag.removeprefix("bun-v")
        version_parts = _version_tuple(version)
        if version_parts is None:
            continue
        if version_line and not _same_line(version, version_line):
            continue
        if recent_tuple is None or version_parts > recent_tuple:
            recent = version
            recent_tuple = version_parts
    return recent


def check_latest_deno(version_line: str = "") -> Optional[str]:
    data = json.loads(_http_get(
        "https://api.github.com/repos/denoland/deno/releases?per_page=100",
        accept="application/vnd.github+json",
    ))
    recent: Optional[str] = None
    recent_tuple: Optional[List[int]] = None
    for release in data:
        tag = release.get("tag_name", "")
        if not tag.startswith("v"):
            continue
        version = tag.removeprefix("v")
        version_parts = _version_tuple(version)
        if version_parts is None:
            continue
        if version_line and not _same_line(version, version_line):
            continue
        if recent_tuple is None or version_parts > recent_tuple:
            recent = version
            recent_tuple = version_parts
    return recent


def _graalpy_python_version(asset_names: List[str]) -> Optional[str]:
    """Infer the GraalPy Python version from release asset names."""
    for name in asset_names:
        if name.startswith("graalpy3."):
            return name[len("graalpy") :].split("-", 1)[0]
        if name.startswith("graalpy-"):
            return "3.12"
    return None


def _graalpy_is_newer(version: str, latest: str) -> bool:
    """Return whether version is newer than latest using numeric comparison."""
    try:
        version_parts = tuple(int(p) for p in version.split("."))
        latest_parts = tuple(int(p) for p in latest.split("."))
    except ValueError:
        return False
    return version_parts > latest_parts


def check_latest_graalpy(python_version: str) -> Optional[str]:
    data = json.loads(_http_get(
        "https://api.github.com/repos/oracle/graalpython/releases?per_page=100",
        accept="application/vnd.github+json",
    ))
    latest: Optional[str] = None
    for release in data:
        tag = release.get("tag_name", "")
        if not tag.startswith("graal-"):
            continue
        asset_names = [asset.get("name", "") for asset in release.get("assets", [])]
        if _graalpy_python_version(asset_names) != python_version:
            continue
        version = tag.removeprefix("graal-")
        if latest is None or _graalpy_is_newer(version, latest):
            latest = version
    return latest


def check_latest_rust(version_line: str = "") -> Optional[str]:
    channel = _http_get_text("https://static.rust-lang.org/dist/channel-rust-stable.toml")
    latest: Optional[str] = None
    latest_tuple: Optional[List[int]] = None
    in_rust = False
    for raw_line in channel.splitlines():
        line = raw_line.strip()
        if line == "[pkg.rust]":
            in_rust = True
            continue
        if line.startswith("[pkg.rust.") or line.startswith("[[pkg.rust."):
            in_rust = False
            continue
        if in_rust and line.startswith("version"):
            raw = line.split("=", 1)[1].strip().strip('"')
            version = raw.split()[0]
            version_parts = _version_tuple(version)
            if version_parts is None:
                continue
            if version_line and not _same_line(version, version_line):
                continue
            if latest_tuple is None or version_parts > latest_tuple:
                latest = version
                latest_tuple = version_parts
    return latest


def check_latest_pypy() -> Optional[str]:
    data = json.loads(_http_get_text("https://downloads.python.org/pypy/versions.json"))
    for release in data:
        if release.get("stable") and release.get("python_version", "").startswith("3.11"):
            return f"pypy3.11-v{release['pypy_version']}"
    return None


LATEST_CHECKERS = {
    "go-toolchain": check_latest_go,
    "bun": check_latest_bun,
    "deno": check_latest_deno,
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
            version_line = raw.get("version_line", "")
            if family == "graalpy":
                latest = check_latest_graalpy(raw.get("python_version", ""))
            elif family == "portable-pypy":
                latest = checker()
            else:
                latest = checker(version_line)
        except Exception as exc:
            print(f"  Failed to check: {exc}")
            continue

        if latest is None:
            if version_line:
                print(
                    f"  No newer version on line {version_line} "
                    f"(current: {current}) — new major/minor requires a new runtime"
                )
            else:
                print(f"  Could not determine latest version")
            continue

        if latest == current:
            print(f"  Up to date ({current})")
        else:
            print(f"  Update available: {current} -> {latest}")
            outdated[runtime_id] = (current, latest)

    return outdated


def check_and_report_new_lines(
    runtime_ids: Optional[List[str]] = None,
) -> Tuple[Dict[str, Tuple[str, str]], Dict[str, Dict[str, str]]]:
    """Return (outdated, new_lines) for the given runtimes."""
    outdated = check_updates(runtime_ids)
    if runtime_ids is not None:
        new_lines: Dict[str, Dict[str, str]] = {}
    else:
        new_lines = detect_new_lines()
    return outdated, new_lines


def bump_latest_all(runtime_ids: Optional[List[str]] = None, *, dry_run: bool = False) -> None:
    """Bump all outdated runtimes and add any new version lines."""
    outdated, new_lines = check_and_report_new_lines(runtime_ids)

    if not outdated and not new_lines:
        print("\nNothing to bump.")
        return

    for runtime_id, (current, latest) in outdated.items():
        print(f"\nBumping {runtime_id} ...")
        bump_runtime(runtime_id, latest, dry_run=dry_run)

    for family, info in new_lines.items():
        print(f"\nAdding new line {info['id']} ({family} {info['line']}) ...")
        add_runtime_line(family, info["version"], dry_run=dry_run)


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
        outdated, new_lines = check_and_report_new_lines(runtime_ids)
        if args.json:
            print(json.dumps(
                {
                    "outdated": {
                        rid: {"current": cur, "latest": lat}
                        for rid, (cur, lat) in outdated.items()
                    },
                    "new_lines": new_lines,
                },
                indent=2,
            ))
        if not outdated and not new_lines:
            print("\nAll runtimes are up to date.")
        else:
            if outdated:
                print(f"\n{len(outdated)} runtime(s) have updates available.")
            if new_lines:
                print(f"\n{len(new_lines)} new runtime line(s) available.")
        return

    if args.command == "bump-latest":
        runtime_ids = [args.runtime] if args.runtime else None
        bump_latest_all(runtime_ids, dry_run=args.dry_run)
        return

    parser.error("Unhandled command")


if __name__ == "__main__":
    main()
