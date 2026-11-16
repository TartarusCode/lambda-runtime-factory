"""What the public UBI9 repositories publish, and whether a build is at or past it.

Two questions the compliance pipeline cannot answer from the image alone:

* which EVR of a package the UBI9 mirror serves right now — so a UBI-lag waiver can
  retire itself in the week the patched RPM lands, instead of a human reading a cell
  log or running `repoquery`;
* whether one EVR is at least another — so a waiver whose fix the image already
  carries is recognized as stale rather than being renewed.

The repository is read over HTTPS from the CDN the images' own yum config points at
(`/ubi9/9/<arch>/<repo>/os`), so no container, credential or entitlement is
involved: UBI content is public. `available_evrs` takes its fetcher as an argument,
which is what lets the tests run the parser and the pruning decision offline.
"""

from __future__ import annotations

import gzip
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
from typing import Callable, Iterable, Set

BASE_URL = "https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi9/9/{arch}/{repo}/os"
# The two repositories the runtime images enable. A package can live in either:
# the library floor already pulls libxcrypt-compat from appstream.
REPOS = ("baseos", "appstream")
# rpm's own naming, which is what `$basearch` expands to in the image's repo config.
ARCHES = {"x86_64": "x86_64", "arm64": "aarch64"}
# What Syft appends to an RPM's version, and what a hand-written EVR may carry.
ARCH_SUFFIXES = (".x86_64", ".aarch64", ".i686", ".noarch", ".s390x", ".ppc64le")
TIMEOUT = 60


def normalize_evr(value: str) -> str:
    """One spelling per build.

    `0:1.21.1-10.el9_8.1`, `1.21.1-10.el9_8.1` and `1.21.1-10.el9_8.1.x86_64` name
    the same package; a comparison must not fail on the epoch or the arch suffix. A
    non-zero epoch is preserved, because it is part of the identity.
    """
    text = str(value or "").strip()
    epoch, separator, rest = text.partition(":")
    if separator and epoch.strip().isdigit():
        epoch = epoch.strip()
        text = rest if epoch == "0" else f"{epoch}:{rest}"
    for suffix in ARCH_SUFFIXES:
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _split(evr: str) -> tuple[str, str, str]:
    """(epoch, version, release); a missing epoch is 0 and a missing release is ''."""
    text = normalize_evr(evr)
    epoch = "0"
    if ":" in text:
        head, _, rest = text.partition(":")
        if head.isdigit():
            epoch, text = head, rest
    version, separator, release = text.rpartition("-")
    if not separator:
        return epoch, text, ""
    return epoch, version, release


def _run(text: str, index: int) -> tuple[str, int]:
    start = index
    if text[index].isdigit():
        while index < len(text) and text[index].isdigit():
            index += 1
    else:
        while index < len(text) and text[index].isalpha():
            index += 1
    return text[start:index], index


def _segment_compare(left: str, right: str) -> int:
    """rpm's segment comparison: digit runs compare as numbers, and a digit run is
    newer than any alphabetic run (`1.0.1` beats `1.0a`)."""
    if left == right:
        return 0
    i = j = 0
    while i < len(left) and j < len(right):
        while i < len(left) and not left[i].isalnum():
            i += 1
        while j < len(right) and not right[j].isalnum():
            j += 1
        if i >= len(left) or j >= len(right):
            break
        left_run, i = _run(left, i)
        right_run, j = _run(right, j)
        if left_run.isdigit() and right_run.isdigit():
            left_digits = left_run.lstrip("0") or "0"
            right_digits = right_run.lstrip("0") or "0"
            if len(left_digits) != len(right_digits):
                return 1 if len(left_digits) > len(right_digits) else -1
            if left_digits != right_digits:
                return 1 if left_digits > right_digits else -1
        elif left_run.isdigit():
            return 1
        elif right_run.isdigit():
            return -1
        elif left_run != right_run:
            return 1 if left_run > right_run else -1

    while i < len(left) and not left[i].isalnum():
        i += 1
    while j < len(right) and not right[j].isalnum():
        j += 1
    if i < len(left):
        return 1
    if j < len(right):
        return -1
    return 0


def compare_evr(left: str, right: str) -> int:
    """-1, 0 or 1 as `left` is older than, equal to, or newer than `right`."""
    left_epoch, left_version, left_release = _split(left)
    right_epoch, right_version, right_release = _split(right)

    left_epoch_value = int(left_epoch) if left_epoch.isdigit() else 0
    right_epoch_value = int(right_epoch) if right_epoch.isdigit() else 0
    if left_epoch_value != right_epoch_value:
        return 1 if left_epoch_value > right_epoch_value else -1

    version = _segment_compare(left_version, right_version)
    if version:
        return version
    return _segment_compare(left_release, right_release)


def is_at_least(evr: str, target: str) -> bool:
    return compare_evr(evr, target) >= 0


def _fetch(url: str, timeout: int = TIMEOUT) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "lambda-runtime-compliance/1.0"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _primary_href(repomd: bytes) -> str:
    """The primary-metadata file a repository's repomd.xml points at."""
    root = ElementTree.fromstring(repomd)
    for data in root:
        if _local_name(data.tag) != "data" or data.get("type") != "primary":
            continue
        for child in data:
            if _local_name(child.tag) == "location" and child.get("href"):
                return str(child.get("href"))
    return ""


def _package_evrs(primary: bytes, package: str, arch: str) -> Set[str]:
    root = ElementTree.fromstring(primary)
    wanted = package.lower()
    evrs: Set[str] = set()
    for element in root:
        if _local_name(element.tag) != "package":
            continue
        fields = {_local_name(child.tag): child for child in element}
        name = fields.get("name")
        if name is None or (name.text or "").strip().lower() != wanted:
            continue
        entry_arch = (fields["arch"].text or "").strip() if "arch" in fields else ""
        if entry_arch and entry_arch != arch and entry_arch != "noarch":
            continue
        version = fields.get("version")
        if version is None:
            continue
        epoch = (version.get("epoch") or "0").strip()
        ver = (version.get("ver") or "").strip()
        rel = (version.get("rel") or "").strip()
        evrs.add(f"{epoch}:{ver}-{rel}" if epoch != "0" else f"{ver}-{rel}")
    return evrs


def available_evrs(
    package: str,
    arch: str,
    *,
    repos: Iterable[str] = REPOS,
    base_url: str = BASE_URL,
    fetch: Callable[..., bytes] = _fetch,
) -> Set[str]:
    """Every EVR of `package` the UBI9 mirror publishes for `arch`.

    A repository that does not serve this architecture, or answers with an error, is
    skipped: an empty set then means "nothing found", and the caller decides what
    that means — never "the fix is there".
    """
    rpm_arch = ARCHES.get(arch, arch)
    found: Set[str] = set()
    for repo in repos:
        base = base_url.format(arch=rpm_arch, repo=repo)
        try:
            repomd = fetch(f"{base}/repodata/repomd.xml")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        href = _primary_href(repomd)
        if not href:
            continue
        try:
            payload = fetch(f"{base}/{href}")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        try:
            primary = gzip.decompress(payload)
        except OSError:
            primary = payload
        try:
            found |= _package_evrs(primary, package, rpm_arch)
        except ElementTree.ParseError:
            continue
    return found