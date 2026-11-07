"""Render and stage the OCI image for a runtime.

Every runtime ships exactly one container, built from the same ``${LAYER_ROOT}``
payload as its Lambda zip layer, so there is one copy of every runtime and the two
artifacts cannot drift.

The base is the DoD-hardened Iron Bank UBI9-minimal, and there is deliberately no
second one. A distroless base would mean two container matrices, two tags per
version, and the ``debian:<N>-slim`` stage that copied resolved ``.so`` files into
an image whose RPM database never learned they existed — which is exactly what
makes a scanner report a patched library as vulnerable, and what leaves
``/etc/ld.so.cache`` stale. UBI9-minimal carries ``microdnf``, so the libraries the
payload needs are installed as *packages* and RPM keeps owning the files.

What the base does not give for free is handled in
``tools/docker/Dockerfile`` (a template this module renders per runtime):
the DoD/enterprise root CAs are injected and the trust store extracted, the payload
is copied to ``/opt`` owned by the runtime UID, permissions are normalized with
``chmod -R a+rX``, and the package manager is removed again before the image ships.
The Lambda filesystem contract and the STIG rootless rule are verified from the
exported rootfs rather than assumed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runtime_manifest  # noqa: E402  (sibling module, resolved via sys.path above)

ELF_MAGIC = b"\x7fELF"

# The canonical UBI9 stage layout. It is a template, not a Dockerfile: the
# @@...@@ placeholders carry the per-runtime environment, payload path and labels
# from the manifest, so the STIG invariants live in one reviewable file while the
# payload coordinates stay in the manifest.
CONTAINER_TEMPLATE = Path(__file__).resolve().parents[1] / "docker" / "Dockerfile"

# Sonames `registry1.dso.mil/.../ubi9-minimal` (and the Red Hat fallback, which
# serves the same userland) already provides. Verified against the image itself; a
# name missing from this set is installed as the package that owns it.
UBI_PROVIDED = {
    "ld-linux-aarch64.so.1",
    "ld-linux-x86-64.so.2",
    "libBrokenLocale.so.1",
    "libanl.so.1",
    "libbz2.so.1",
    "libc.so.6",
    "libc_malloc_debug.so.0",
    "libcrypto.so.3",
    "libcrypt.so.2",
    "libdl.so.2",
    "libffi.so.8",
    "libform.so.6",
    "libformw.so.6",
    "libgcc_s.so.1",
    "libgdbm.so.6",
    "libgdbm_compat.so.4",
    "liblzma.so.5",
    "libm.so.6",
    "libmenuw.so.6",
    "libmvec.so.1",
    "libncurses.so.6",
    "libncursesw.so.6",
    "libnss_compat.so.2",
    "libnss_dns.so.2",
    "libnss_files.so.2",
    "libpanel.so.6",
    "libpanelw.so.6",
    "libpthread.so.0",
    "libreadline.so.8",
    "libresolv.so.2",
    "librt.so.1",
    "libsqlite3.so.0",
    "libssl.so.3",
    "libstdc++.so.6",
    "libthread_db.so.1",
    "libtinfo.so.6",
    "libutil.so.1",
    "libuuid.so.1",
    "libyaml-0.so.2",
    "libz.so.1",
    "libzstd.so.1",
}

# UBI9 package that owns a soname the base does not ship. Unknown names fail the
# build with the soname in the message rather than producing an image that starts
# and then cannot dlopen.
PACKAGE_FOR_SONAME_UBI: Dict[str, str] = {
    "libbz2.so.1.0": "bzip2-libs",
    "libcrypt.so.1": "libxcrypt-compat",
    "libexpat.so.1": "expat",
    "libmpdec.so.3": "python3-libs",
    "libnsl.so.1": "libnsl",
    "libtinfo.so.5": "ncurses-compat-libs",
}


class ContainerError(RuntimeError):
    """A container build input is missing or a library cannot be resolved."""


def _readelf_needed(path: Path) -> List[str]:
    """Return the ``DT_NEEDED`` sonames of one file (empty for non-ELF)."""
    try:
        with path.open("rb") as handle:
            if handle.read(4) != ELF_MAGIC:
                return []
    except OSError:
        return []

    result = subprocess.run(
        ["readelf", "-d", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []

    needed: List[str] = []
    for line in result.stdout.splitlines():
        marker = "Shared library: ["
        if marker in line:
            needed.append(line.split(marker, 1)[1].rstrip("]").strip())
    return needed


def scan_payload_sonames(layer_root: Path) -> List[str]:
    """Union of every shared-library dependency in the payload.

    ``readelf`` understands foreign architectures, so an arm64 payload can be
    inspected on an x86_64 runner. Symlinks are skipped: the target is walked
    separately, and a dangling one would only produce noise.
    """
    sonames: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(layer_root):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            sonames.update(_readelf_needed(path))
    return sorted(sonames)


def payload_library_names(layer_root: Path) -> set[str]:
    """Basenames of libraries the payload ships itself.

    A dependency such as ``libpypy3.11-c.so`` is resolved through the payload's
    own ``$ORIGIN`` rpath, so it is already inside ``/opt`` and must not be
    installed from the base (no package owns it).
    """
    names: set[str] = set()
    for _dirpath, _dirnames, filenames in os.walk(layer_root):
        for name in filenames:
            if ".so" in name:
                names.add(name)
    return names


def required_ubi_packages(
    sonames: Iterable[str],
    *,
    provided_by_payload: Iterable[str] = (),
    unsatisfied: Iterable[str] = (),
) -> List[str]:
    """Map sonames to the UBI9 packages that provide what the base does not.

    ``unsatisfied`` names the sonames a family has explicitly declared this base
    cannot provide (see ``container.unsatisfied_sonames``). Anything else that is
    neither in the base nor shipped by the payload fails the build with the soname
    in the message, because that is a defect and not a decision.
    """
    payload = set(provided_by_payload)
    tolerated = set(unsatisfied)
    missing = [
        name
        for name in sonames
        if name not in UBI_PROVIDED and name not in payload and name not in tolerated
    ]
    unknown = [name for name in missing if name not in PACKAGE_FOR_SONAME_UBI]
    if unknown:
        raise ContainerError(
            "The UBI9-minimal base does not provide these shared libraries and no "
            f"UBI9 package is mapped for them: {', '.join(unknown)}. Add them to "
            "PACKAGE_FOR_SONAME_UBI in tools/runtime_lib/container_image.py, or "
            "declare them in container.unsatisfied_sonames with the reason."
        )
    return sorted({PACKAGE_FOR_SONAME_UBI[name] for name in missing})


def ironbank_packages(
    declared: Iterable[str],
    sonames: Iterable[str],
    *,
    provided_by_payload: Iterable[str] = (),
    unsatisfied: Iterable[str] = (),
) -> List[str]:
    """The UBI9 packages an image installs.

    The manifest's declared floor plus whatever the payload's ``DT_NEEDED`` scan
    finds the base does not ship. Only package *names* leave this function: the
    image installs them with ``microdnf``, so RPM keeps owning the files.
    """
    return sorted(
        set(declared)
        | set(
            required_ubi_packages(
                sonames,
                provided_by_payload=provided_by_payload,
                unsatisfied=unsatisfied,
            )
        )
    )


def _docker_env(env: Dict[str, str]) -> str:
    """Render ENV lines with Docker-escaped values."""
    lines = []
    for key in sorted(env):
        value = str(env[key]).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'ENV {key}="{value}"')
    return "\n".join(lines)


def render_dockerfile(
    *,
    layer_root: str,
    workdir: str,
    env: Dict[str, str],
    labels: Optional[Dict[str, str]] = None,
) -> str:
    """Render the canonical UBI9 template for one runtime.

    The stage layout, the CA injection, the STIG invariants and the entrypoint all
    come from ``tools/docker/Dockerfile``; only the payload path, the
    environment and the labels are substituted, so the two cannot drift. The
    entrypoint is deliberately not a placeholder: the STIG contract fixes it at
    ``/opt/bootstrap``, the one path every runtime family ships.
    """
    template = CONTAINER_TEMPLATE.read_text(encoding="utf-8")

    label_lines = "\n".join(
        f'LABEL {key}="{value}"' for key, value in sorted((labels or {}).items())
    )

    replacements = {
        "@@LAYER_ROOT@@": layer_root.rstrip("/"),
        "@@WORKDIR@@": workdir,
        "@@ENV@@": _docker_env(env),
        "@@LABELS@@": label_lines,
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)

    leftover = re.findall(r"@@[A-Z_]+@@", rendered)
    if leftover:
        raise ContainerError(
            f"{CONTAINER_TEMPLATE.name}: unsubstituted placeholders {sorted(set(leftover))}"
        )
    return rendered


def prepare(runtime_id: str, arch: str, *, force: bool = False) -> Dict[str, Any]:
    """Stage the container build context and return what the builder needs.

    The payload is the same ``${LAYER_ROOT}`` the zip layer ships, hardlinked into
    ``context/layer`` where the filesystem allows it, so a ~700 MB Rust toolchain
    is not copied twice just to satisfy Docker's context boundary. Nothing about the
    payload is re-derived here: build-runtime owns the download, the checksum, the
    extraction, the slimming and the STIG permissions.
    """
    env = runtime_manifest.runtime_env(runtime_id, arch=arch)
    if env["CONTAINER_ENABLED"] != "true":
        raise ContainerError(
            f"{runtime_id} ({arch}) has no container target (container.enabled=false)"
        )

    layer_root = Path(env["LAYER_ROOT"])
    if not layer_root.is_dir():
        raise ContainerError(
            f"{runtime_id} ({arch}): no payload at {layer_root}. "
            "Build the runtime package first (tools/bin/build-runtime)."
        )

    # The image entrypoints at the payload's bootstrap, so the payload must carry
    # one. It is the same file the zip layer ships.
    bootstrap = layer_root / env["BOOTSTRAP_OUTPUT"]
    if not bootstrap.exists():
        raise ContainerError(
            f"{runtime_id} ({arch}): the payload has no {bootstrap}; the image "
            "entrypoints at /opt/bootstrap, so build-runtime must ship it"
        )
    if not os.access(bootstrap, os.X_OK):
        raise ContainerError(
            f"{runtime_id} ({arch}): {bootstrap} is not executable, so the image "
            "could not start"
        )

    context_dir = Path(env["CONTAINER_CONTEXT_DIR"])
    if force or context_dir.exists():
        shutil.rmtree(context_dir, ignore_errors=True)
    context_dir.mkdir(parents=True, exist_ok=True)

    layer_link = context_dir / "layer"
    try:
        subprocess.run(
            ["cp", "-al", str(layer_root), str(layer_link)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        shutil.copytree(layer_root, layer_link, symlinks=True)

    # The template always COPYs certs/, so the directory must exist even when the
    # repository ships no extra root CAs.
    certs_dir = context_dir / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)
    repo_certs = runtime_manifest.repo_root() / "certs"
    if repo_certs.is_dir():
        for cert in sorted(repo_certs.glob("*.crt")):
            shutil.copy2(cert, certs_dir / cert.name)

    # Which UBI9 packages to install. The manifest declares an explicit floor
    # (ca-certificates); the payload's DT_NEEDED scan adds any soname the base does
    # not ship. The names go into a `microdnf install` build arg — no file is ever
    # cherry-picked, so RPM keeps owning /var/lib/rpm and ld.so.cache.
    sonames = scan_payload_sonames(layer_root)
    provided = payload_library_names(layer_root)
    declared = json.loads(env.get("CONTAINER_PACKAGES") or "[]")
    unsatisfied = json.loads(env.get("CONTAINER_UNSATISFIED_SONAMES") or "[]")
    packages = ironbank_packages(
        declared,
        sonames,
        provided_by_payload=provided,
        unsatisfied=unsatisfied,
    )

    labels = {
        "org.opencontainers.image.title": f"lambda-runtime-factory {runtime_id}",
        "org.opencontainers.image.description": (
            f"Iron Bank hardened AWS Lambda runtime image for {runtime_id} ({arch})"
        ),
        "org.opencontainers.image.source": "https://github.com/TartarusCode/lambda-runtime-factory",
        "org.opencontainers.image.version": env["CONTAINER_TAG"],
        "org.opencontainers.image.licenses": "Apache-2.0",
        "org.opencontainers.image.vendor": "TartarusCode",
        # The base this image was built on, named in the label set so a manifest
        # inspection answers the compliance question without pulling layers.
        "mil.dso.ironbank.os-type": "ubi9-minimal",
        "com.tartaruscode.lambda.runtime": runtime_id,
        "com.tartaruscode.lambda.arch": env["CONTAINER_ARCH"],
    }

    dockerfile = render_dockerfile(
        layer_root=env["CONTAINER_LAYER_ROOT"],
        workdir=env["CONTAINER_WORKDIR"],
        env=json.loads(env["CONTAINER_ENV"]),
        labels=labels,
    )
    (context_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8", newline="\n")

    staged = {
        "runtime_id": runtime_id,
        "arch": arch,
        "platform": env["CONTAINER_PLATFORM"],
        "base_image": env["CONTAINER_BASE_IMAGE"],
        "fallback_base_image": env["CONTAINER_FALLBACK_BASE_IMAGE"],
        "base_image_digest": env["CONTAINER_BASE_IMAGE_DIGEST"],
        "nonroot_uid": env["CONTAINER_NONROOT_UID"],
        "nonroot_gid": env["CONTAINER_NONROOT_GID"],
        "context_dir": str(context_dir),
        "dockerfile": str(context_dir / "Dockerfile"),
        "local_image": env["CONTAINER_LOCAL_IMAGE"],
        "tag": env["CONTAINER_TAG"],
        "entrypoint": json.loads(env["CONTAINER_ENTRYPOINT"]),
        "env": json.loads(env["CONTAINER_ENV"]),
        "smoke_test": json.loads(env["CONTAINER_SMOKE_TEST"]),
        "sonames": sonames,
        "packages": packages,
        "provided_by_base": [name for name in sonames if name in UBI_PROVIDED],
    }
    (context_dir / "container-spec.json").write_text(
        json.dumps(staged, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return staged


# `tar -tv` line: mode, owner, size, date, time, name — then `-> target` for a
# symlink. The name is captured non-greedily so a symlink keeps its own path
# rather than the target's, and a name containing spaces survives.
_ROOTFS_LINE = re.compile(
    r"^(?P<mode>[bcdlps-][rwxsStT-]{9})\s+\S+\s+(?P<size>\d+)\s+\S+\s+\S+\s+"
    r"(?P<name>.+?)(?:\s+->\s+.+)?$"
)


def parse_rootfs_listing(listing: str) -> Dict[str, Any]:
    """Summarize the ``tar -tv`` output of an exported rootfs.

    ``entries`` maps a path to its mode string. ``unpacked_bytes`` sums the
    regular files, which is the unpacked size of the image — the number Lambda
    counts against its 10 GB container limit, and the one a consumer pays in disk
    and pull-to-run time. ``docker image inspect .Size`` is *not* that number on
    every daemon: on a containerd image store it reports the compressed size, so
    the same image reads roughly three times smaller there than on the classic
    overlay2 store. Two runners with different stores would put two different
    numbers for one artifact into the summary; exporting the rootfs makes the
    recorded size mean one thing wherever it is measured.
    """
    entries: Dict[str, str] = {}
    unpacked_bytes = 0
    for line in listing.splitlines():
        match = _ROOTFS_LINE.match(line)
        if match is None:
            continue
        entries[match.group("name").rstrip("/")] = match.group("mode")
        if match.group("mode").startswith("-"):
            unpacked_bytes += int(match.group("size"))
    return {"entries": entries, "unpacked_bytes": unpacked_bytes}


def export_rootfs(image: str) -> Dict[str, Any]:
    """Create a container from ``image`` and summarize its exported rootfs."""
    # The image has an ENTRYPOINT but no CMD, so `create` needs an explicit argv;
    # it only records it. A non-zero exit here is a real Docker failure.
    created = subprocess.run(
        ["docker", "create", "--entrypoint", "/nonexistent", image, "/nonexistent"],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        raise ContainerError(
            f"docker create failed for {image}: {created.stderr.strip()}"
        )
    container_id = created.stdout.strip()

    try:
        export = subprocess.run(
            ["docker", "export", container_id], capture_output=True
        )
        if export.returncode != 0:
            raise ContainerError(
                f"docker export failed for {image}: {export.stderr.decode().strip()}"
            )
        listing = subprocess.run(
            ["tar", "-tv"], input=export.stdout, capture_output=True
        )
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)

    if listing.returncode != 0:
        raise ContainerError(f"could not list the exported rootfs of {image}")
    return parse_rootfs_listing(listing.stdout.decode("utf-8", "replace"))


def verify_image_layout(image: str) -> Dict[str, Any]:
    """Check the Lambda filesystem contract inside a built image.

    The check runs against the exported rootfs. It asserts the paths Lambda
    depends on — a world-writable ``/tmp``, the ``/var/task`` workdir and ``/opt``
    for the payload — refuses an image that still carries a package manager (STIG
    forbids one, and it would let a consumer mutate the image at run time), and
    requires the RPM database the vulnerability scan reads.
    """
    rootfs = export_rootfs(image)
    entries: Dict[str, str] = rootfs["entries"]

    problems = []
    if entries.get("tmp") != "drwxrwxrwt":
        problems.append(f"/tmp mode is {entries.get('tmp', 'missing')}, want drwxrwxrwt")
    for required in ("var/task", "opt"):
        if required not in entries:
            problems.append(f"/{required} is missing from the image")
    for manager in ("usr/bin/microdnf", "usr/bin/dnf", "usr/bin/yum"):
        if manager in entries:
            problems.append(f"/{manager} is still present, so the image ships a package manager")
    # rpm and its database stay on purpose: that is what a vulnerability scanner
    # reads to match a RHEL backport.
    if "var/lib/rpm" not in entries:
        problems.append("/var/lib/rpm is missing, so Grype cannot match RHEL backports")

    if problems:
        raise ContainerError(f"{image}: " + "; ".join(problems))
    return {
        "image": image,
        "tmp_mode": entries["tmp"],
        "unpacked_bytes": rootfs["unpacked_bytes"],
        "paths": sorted(entries),
    }


def record_size(
    image: str,
    runtime_id: str,
    arch: str,
    *,
    unpacked_bytes: Optional[int] = None,
) -> Optional[int]:
    """Write the built image size into build-stats.json and return the bytes.

    ``unpacked_bytes``, measured from the exported rootfs, is preferred: it means
    the same thing on every daemon. The fallback reads the daemon's own number,
    which is the *compressed* size when the daemon uses a containerd image store
    — the same image then reads roughly three times smaller than on the classic
    overlay2 store, so the fallback is labelled rather than passed off as the
    unpacked size.

    There is no package to measure any more: the image is the artifact.
    """
    source = "rootfs-export"
    size = unpacked_bytes
    if size is None:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Size}}", image],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        size = int(result.stdout.strip())
        source = "docker-image-inspect"

    env = runtime_manifest.runtime_env(runtime_id, arch=arch)
    artifact_dir = Path(env["ARTIFACT_DIR"])
    stats_path = artifact_dir / "build-stats.json"
    stats: Dict[str, Any] = {}
    if stats_path.exists():
        stats = json.loads(stats_path.read_text(encoding="utf-8"))

    stats.setdefault("runtime_id", runtime_id)
    stats.setdefault("arch", arch)
    stats["container_image_bytes"] = size
    stats["container_image_bytes_measured"] = source
    stats["container_image"] = image
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats_path.write_text(
        json.dumps(stats, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return size


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare_parser = sub.add_parser("prepare", help="Stage the Docker build context")
    prepare_parser.add_argument("--runtime", required=True)
    prepare_parser.add_argument("--arch", required=True)
    prepare_parser.add_argument("--force", action="store_true")

    verify_parser = sub.add_parser("verify", help="Assert the Lambda filesystem contract")
    verify_parser.add_argument("--image", required=True)

    size_parser = sub.add_parser("record-size", help="Record the image size in build-stats")
    size_parser.add_argument("--runtime", required=True)
    size_parser.add_argument("--arch", required=True)
    size_parser.add_argument("--image", required=True)
    size_parser.add_argument(
        "--unpacked-bytes",
        type=int,
        default=None,
        help=(
            "Unpacked size measured from the exported rootfs. Preferred over "
            "`docker image inspect .Size`, which is the compressed size on a "
            "containerd image store."
        ),
    )

    args = parser.parse_args()

    if args.command == "prepare":
        print(
            json.dumps(
                prepare(args.runtime, args.arch, force=args.force),
                indent=2,
            )
        )
        return

    if args.command == "verify":
        print(json.dumps(verify_image_layout(args.image), indent=2))
        return

    size = record_size(
        args.image,
        args.runtime,
        args.arch,
        unpacked_bytes=args.unpacked_bytes,
    )
    if size is None:
        raise SystemExit(f"could not read the size of {args.image}")
    print(f"{args.image}: {size} bytes")


if __name__ == "__main__":
    _main()
