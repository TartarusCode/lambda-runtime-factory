"""Render and stage an OCI image for a runtime.

The image is derived from the same ``${LAYER_ROOT}`` payload the Lambda zip layer
is built from, so there is exactly one copy of every runtime. What differs is the
base, and the manifest picks it per family through ``container.profile``:

* **``distroless``** — a runtime base. ``gcr.io/distroless/cc-debian12`` has no
  shell, no package manager and no OS tooling, which removes the shell dependency
  from the entrypoint and shrinks the OS CVE surface to glibc, libstdc++, libssl
  and ca-certificates. Two things the base does not carry are handled here:
  a static scan of the payload's ``DT_NEEDED`` entries finds the libraries it
  links against (``libz`` for GraalPy, zlib/bzip2/ncurses for portable PyPy) and
  the generated Dockerfile copies those from a ``debian:12-slim`` stage, matching
  the base's own Debian release instead of borrowing the build host's newer glibc;
  and the Lambda filesystem contract (``/tmp`` world-writable, code at
  ``/var/task``) is verified rather than assumed.
* **``build``** — a build environment. A toolchain image is only useful as a
  Dockerfile build stage (``FROM …/go126:1.26 AS build`` then ``RUN go build``),
  and a ``RUN`` step needs a shell, which distroless does not have. The base is
  therefore a shell-bearing image with a package manager, the manifest declares
  the packages to install, and the payload is made runnable with the symlinks the
  manifest declares (musl's loader, rustc's sysroot).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runtime_manifest  # noqa: E402  (sibling module, resolved via sys.path above)

ELF_MAGIC = b"\x7fELF"

# Libraries that ``gcr.io/distroless/cc-debian12`` already provides. Verified
# against the image itself (see CONTRIBUTING.md) — a name missing from this set is
# staged from Debian 12 in the builder stage.
DISTROLESS_CC_PROVIDED = {
    "ld-linux-aarch64.so.1",
    "ld-linux-x86-64.so.2",
    "libBrokenLocale.so.1",
    "libanl.so.1",
    "libc.so.6",
    "libc_malloc_debug.so.0",
    "libcrypto.so.3",
    "libdl.so.2",
    "libgcc_s.so.1",
    "libgomp.so.1",
    "libm.so.6",
    "libmemusage.so",
    "libmvec.so.1",
    "libnsl.so.1",
    "libnss_compat.so.2",
    "libnss_dns.so.2",
    "libnss_files.so.2",
    "libnss_hesiod.so.2",
    "libpcprofile.so",
    "libpthread.so.0",
    "libresolv.so.2",
    "librt.so.1",
    "libssl.so.3",
    "libstdc++.so.6",
    "libthread_db.so.1",
    "libutil.so.1",
}

# Debian 12 package that owns a soname the base does not ship. Unknown names fail
# the build with the soname in the message rather than producing an image that
# starts and then cannot dlopen.
PACKAGE_FOR_SONAME: Dict[str, str] = {
    "libbz2.so.1.0": "libbz2-1.0",
    "libbz2.so.1": "libbz2-1.0",
    "libcrypt.so.1": "libcrypt1",
    "libexpat.so.1": "libexpat1",
    "libffi.so.8": "libffi8",
    "libformw.so.6": "libformw6",
    "libgdbm.so.4": "libgdbm6",
    "libgdbm_compat.so.4": "libgdbm-compat4",
    "liblzma.so.5": "liblzma5",
    "libmenuw.so.6": "libmenuw6",
    "libmpdec.so.3": "libmpdec3",
    "libncurses.so.6": "libncurses6",
    "libncursesw.so.6": "libncursesw6",
    "libpanelw.so.6": "libpanelw6",
    "libreadline.so.8": "libreadline8",
    "libsqlite3.so.0": "libsqlite3-0",
    "libtinfo.so.6": "libtinfo6",
    "libuuid.so.1": "libuuid1",
    "libyaml-0.so.2": "libyaml-0-2",
    "libz.so.1": "zlib1g",
}

TRIPLET_FOR_PLATFORM: Dict[str, str] = {
    "amd64": "x86_64-linux-gnu",
    "arm64": "aarch64-linux-gnu",
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
    own ``$ORIGIN`` rpath, so it is already inside ``/opt`` and must not be staged
    from Debian (no package owns it).
    """
    names: set[str] = set()
    for _dirpath, _dirnames, filenames in os.walk(layer_root):
        for name in filenames:
            if ".so" in name:
                names.add(name)
    return names


def required_packages(
    sonames: Iterable[str], *, provided_by_payload: Iterable[str] = ()
) -> List[str]:
    """Map sonames to the Debian packages that provide what the base does not."""
    payload = set(provided_by_payload)
    missing = [
        name
        for name in sonames
        if name not in DISTROLESS_CC_PROVIDED and name not in payload
    ]
    unknown = [name for name in missing if name not in PACKAGE_FOR_SONAME]
    if unknown:
        raise ContainerError(
            "The distroless base does not provide these shared libraries and no "
            f"Debian 12 package is mapped for them: {', '.join(unknown)}. Add them "
            "to PACKAGE_FOR_SONAME in tools/runtime_lib/container_image.py."
        )
    return sorted({PACKAGE_FOR_SONAME[name] for name in missing})


def _docker_env(env: Dict[str, str]) -> str:
    """Render ENV lines with Docker-escaped values."""
    lines = []
    for key in sorted(env):
        value = str(env[key]).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'ENV {key}="{value}"')
    return "\n".join(lines)


def package_manager_for(base_image: str) -> Optional[str]:
    """Return the package manager a base image ships, or None for distroless.

    The profile decides *whether* packages are installed; the base decides *how*.
    An unrecognised base is an error rather than a guess: emitting a Dockerfile
    with the wrong package manager fails minutes later inside the build.
    """
    if base_image.startswith("gcr.io/distroless/"):
        return None
    if base_image.startswith("debian"):
        return "apt"
    if base_image.startswith("alpine"):
        return "apk"
    raise ContainerError(
        f"no package manager is known for base image '{base_image}'. Add it to "
        "package_manager_for() in tools/runtime_lib/container_image.py."
    )


def _install_line(manager: str, packages: Sequence[str]) -> str:
    """Return the single RUN line that installs packages in a build image."""
    joined = " ".join(packages)
    if manager == "apt":
        return (
            "RUN apt-get update \\\n"
            f" && apt-get install -y --no-install-recommends {joined} \\\n"
            " && rm -rf /var/lib/apt/lists/*"
        )
    if manager == "apk":
        return f"RUN apk add --no-cache {joined}"
    raise ContainerError(f"unsupported package manager '{manager}'")


def render_dockerfile(
    *,
    base_image: str,
    platform: str,
    layer_root: str,
    workdir: str,
    user: str,
    entrypoint: Sequence[str],
    env: Dict[str, str],
    packages: Sequence[str],
    sonames: Sequence[str],
    profile: str = "distroless",
    links: Optional[Dict[str, str]] = None,
    labels: Optional[Dict[str, str]] = None,
) -> str:
    """Return the Dockerfile for one runtime and architecture.

    A ``distroless`` image is multi-stage and runs no shell in the final stage: the
    libraries the payload needs are copied in from a ``debian:12-slim`` builder. A
    ``build`` image is single-stage and does run a shell — that is the point of it.
    """
    arch = platform.split("/", 1)[1]
    triplet = TRIPLET_FOR_PLATFORM.get(arch, f"{arch}-linux-gnu")
    manager = package_manager_for(base_image)

    lines: List[str] = [
        "# syntax=docker/dockerfile:1",
        "# Generated by tools/runtime_lib/container_image.py. Do not edit by hand:",
        "# the entrypoint and environment come from the runtime manifest.",
        f'ARG BASE_IMAGE="{base_image}"',
    ]

    if profile == "distroless" and packages:
        lines += [
            "",
            "FROM debian:12-slim AS libs",
            "# Same Debian release as the distroless base, so the staged libraries",
            "# match its glibc instead of borrowing the build host's newer one.",
            "RUN apt-get update \\",
            " && apt-get install -y --no-install-recommends "
            + " ".join(packages)
            + " \\",
            " && rm -rf /var/lib/apt/lists/*",
            "RUN mkdir -p /out \\",
            " && for soname in " + " ".join(sonames) + "; do \\",
            f'      for dir in /lib/{triplet} /usr/lib/{triplet}; do \\',
            '        if [ -e "$dir/$soname" ]; then cp -a "$dir/$soname"* /out/; fi; \\',
            "      done; \\",
            "    done",
        ]

    lines += ["", "FROM ${BASE_IMAGE}"]

    for key, value in sorted((labels or {}).items()):
        lines.append(f'LABEL {key}="{value}"')

    if profile == "distroless" and packages:
        lines += [
            "# Non-glibc runtime libraries the payload links against.",
            f"COPY --from=libs /out/ /usr/lib/{triplet}/",
        ]

    if profile == "build" and packages:
        if manager is None:
            raise ContainerError(
                f"profile 'build' needs a package manager, but '{base_image}' has none"
            )
        lines += [
            "",
            "# The toolchain's own runtime dependencies: a build image is expected to",
            "# run the compiler, not just carry it.",
            _install_line(manager, packages),
        ]

    lines += [
        "# The layer payload lives at /opt, exactly as Lambda mounts a layer.",
        f"COPY layer/ {layer_root.rstrip('/')}/",
    ]

    if links:
        # After the COPY: a link may point into the payload (rustc's sysroot).
        lines += ["", "# Symlinks the payload needs to be runnable in this base."]
        for link_path, target in sorted(links.items()):
            # `ln -sfn` into an existing directory creates the link *inside* it,
            # which looks like it worked and leaves the real path untouched. The
            # manifest says this path is a link, so whatever is there goes.
            lines.append(f"RUN rm -rf {link_path} && ln -s {target} {link_path}")

    lines += [
        "",
        _docker_env(env),
        "",
        f"WORKDIR {workdir}",
        f"USER {user}",
        "",
        "# Exec form only: a distroless image has no shell to interpret a string.",
        "ENTRYPOINT [" + ", ".join(json.dumps(arg) for arg in entrypoint) + "]",
        "",
    ]
    return "\n".join(lines)


def _spec_for(runtime_id: str, arch: str) -> Dict[str, Any]:
    env = runtime_manifest.runtime_env(runtime_id, arch=arch)
    return {
        "runtime_id": runtime_id,
        "arch": arch,
        "profile": env["CONTAINER_PROFILE"],
        "base_image": env["CONTAINER_BASE_IMAGE"],
        "packages": json.loads(env["CONTAINER_PACKAGES"]),
        "links": json.loads(env["CONTAINER_LINKS"]),
        "platform": env["CONTAINER_PLATFORM"],
        "layer_root": env["CONTAINER_LAYER_ROOT"],
        "workdir": env["CONTAINER_WORKDIR"],
        "user": env["CONTAINER_USER"],
        "entrypoint": json.loads(env["CONTAINER_ENTRYPOINT"]),
        "env": json.loads(env["CONTAINER_ENV"]),
        "context_dir": Path(env["CONTAINER_CONTEXT_DIR"]),
        "source_layer": Path(env["LAYER_ROOT"]),
        "tag": env["CONTAINER_TAG"],
        "local_image": env["CONTAINER_LOCAL_IMAGE"],
        "image": env["CONTAINER_IMAGE"],
    }


def assert_native_entrypoint(
    layer_root: Path, entrypoint: Sequence[str], *, runtime_id: str = "", arch: str = ""
) -> Path:
    """Fail unless the entrypoint names a real ELF binary inside the payload.

    A distroless base has no shell and no /usr/bin/env, so a shebang script (or a
    missing path) can only fail at the first invocation in production. Resolve it
    here instead, while the build context is still being prepared.
    """
    prefix = "/opt/"
    primary = entrypoint[0]
    if not primary.startswith(prefix):
        return Path(primary)
    target = layer_root / primary[len(prefix):]
    if not target.exists():
        raise ContainerError(
            f"{runtime_id} ({arch}): entrypoint {primary} does not exist in the payload"
        )
    try:
        with target.open("rb") as handle:
            magic = handle.read(4)
    except OSError as exc:
        raise ContainerError(f"{runtime_id} ({arch}): cannot read {primary}: {exc}")
    if magic != ELF_MAGIC:
        raise ContainerError(
            f"{runtime_id} ({arch}): entrypoint {primary} is not an ELF binary "
            "(a script cannot run in a distroless image — there is no shell and "
            "no /usr/bin/env). Compile a native launcher or point the entrypoint "
            "at the runtime binary."
        )
    return target


def prepare(runtime_id: str, arch: str, *, force: bool = False) -> Dict[str, Any]:
    """Stage a Docker build context and return what the builder needs.

    The payload is hardlinked into ``context/layer`` where the filesystem allows
    it, so a ~700 MB Rust toolchain is not copied twice just to satisfy Docker's
    context boundary.
    """
    spec = _spec_for(runtime_id, arch)
    layer_root: Path = spec["source_layer"]
    if not layer_root.is_dir():
        raise ContainerError(
            f"{runtime_id} ({arch}): no payload at {layer_root}. "
            "Build the runtime package first (tools/bin/build-runtime)."
        )

    if spec["profile"] == "distroless":
        assert_native_entrypoint(
            layer_root, spec["entrypoint"], runtime_id=runtime_id, arch=arch
        )

    context_dir: Path = spec["context_dir"]
    layer_link = context_dir / "layer"
    if force or context_dir.exists():
        shutil.rmtree(context_dir, ignore_errors=True)
    context_dir.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            ["cp", "-al", str(layer_root), str(layer_link)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        shutil.copytree(layer_root, layer_link, symlinks=True)

    if spec["profile"] == "distroless":
        sonames = scan_payload_sonames(layer_root)
        provided = payload_library_names(layer_root)
        packages = required_packages(sonames, provided_by_payload=provided)
        # Only the libraries the base lacks are copied in. Re-copying glibc or
        # libstdc++ from the builder could downgrade a patched base library.
        staged_sonames = [
            name
            for name in sonames
            if name not in DISTROLESS_CC_PROVIDED and name not in provided
        ]
    else:
        # A build image has a package manager, so the manifest names the packages
        # instead of the scan deriving them: the toolchain's own binaries are not
        # the only thing that runs in it (a consumer's `RUN cargo build` is), and
        # a soname-to-package map for musl would be a second, weaker source of
        # truth. The image is verified by running the toolchain, not by the scan.
        sonames = []
        staged_sonames = []
        packages = list(spec["packages"])

    version = spec["tag"]
    description = (
        f"Distroless AWS Lambda runtime image for {runtime_id} ({arch})"
        if spec["profile"] == "distroless"
        else f"Build environment for the {runtime_id} Lambda runtime ({arch})"
    )
    labels = {
        "org.opencontainers.image.title": f"lambda-runtime-factory {runtime_id}",
        "org.opencontainers.image.description": description,
        "org.opencontainers.image.source": "https://github.com/TartarusCode/lambda-runtime-factory",
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.licenses": "Apache-2.0",
        "com.tartaruscode.lambda.runtime": runtime_id,
        "com.tartaruscode.lambda.arch": spec["platform"].split("/", 1)[1],
    }

    dockerfile = render_dockerfile(
        base_image=spec["base_image"],
        platform=spec["platform"],
        layer_root=spec["layer_root"],
        workdir=spec["workdir"],
        user=spec["user"],
        entrypoint=spec["entrypoint"],
        env=spec["env"],
        packages=packages,
        sonames=staged_sonames,
        profile=spec["profile"],
        links=spec["links"],
        labels=labels,
    )
    (context_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8", newline="\n")

    staged = {
        "runtime_id": runtime_id,
        "arch": arch,
        "profile": spec["profile"],
        "platform": spec["platform"],
        "base_image": spec["base_image"],
        "context_dir": str(context_dir),
        "dockerfile": str(context_dir / "Dockerfile"),
        "local_image": spec["local_image"],
        "registry_image": spec["image"],
        "tag": version,
        "entrypoint": spec["entrypoint"],
        "env": spec["env"],
        "sonames": sonames,
        "staged_sonames": staged_sonames,
        "packages": packages,
        "provided_by_base": [name for name in sonames if name in DISTROLESS_CC_PROVIDED],
    }
    (context_dir / "container-spec.json").write_text(
        json.dumps(staged, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return staged


def verify_image_layout(image: str) -> Dict[str, Any]:
    """Check the Lambda filesystem contract inside a built image.

    The image has no shell, so the check runs against the exported rootfs. It
    asserts the two paths Lambda depends on — a world-writable ``/tmp`` and the
    ``/var/task`` workdir — plus ``/opt`` for the mounted payload.
    """
    created = subprocess.run(
        ["docker", "create", "--entrypoint", "/nonexistent", image, "/nonexistent"],
        capture_output=True,
        text=True,
    )
    # Some runtimes have no /nonexistent entrypoint, but `create` only records the
    # argv; it does not execute it. A non-zero exit here is a real Docker failure.
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

    entries: Dict[str, str] = {}
    for line in listing.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 6:
            entries[parts[-1].rstrip("/")] = parts[0]

    problems = []
    if entries.get("tmp") != "drwxrwxrwt":
        problems.append(f"/tmp mode is {entries.get('tmp', 'missing')}, want drwxrwxrwt")
    for required in ("var/task", "opt"):
        if required not in entries:
            problems.append(f"/{required} is missing from the image")

    if problems:
        raise ContainerError(f"{image}: " + "; ".join(problems))
    return {"image": image, "tmp_mode": entries["tmp"], "paths": sorted(entries)}


def record_size(image: str, runtime_id: str, arch: str) -> Optional[int]:
    """Write the built image size next to the package stats and return the bytes."""
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Size}}", image],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    size = int(result.stdout.strip())

    env = runtime_manifest.runtime_env(runtime_id, arch=arch)
    artifact_dir = Path(env["ARTIFACT_DIR"])
    stats_path = artifact_dir / "build-stats.json"
    stats: Dict[str, Any] = {}
    if stats_path.exists():
        stats = json.loads(stats_path.read_text(encoding="utf-8"))

    # A cached package restored without its sidecar still has the artifact, and
    # the summary row needs both halves to be meaningful.
    if "unzipped_bytes" not in stats:
        packages = sorted(artifact_dir.glob("*.zip"), key=lambda path: path.stat().st_size)
        if packages:
            import package_size  # noqa: PLC0415 - sibling module, avoids an import cycle

            stats.update(package_size.measure(packages[-1]))

    stats.setdefault("runtime_id", runtime_id)
    stats.setdefault("arch", arch)
    stats.setdefault("max_unzipped_bytes", int(env["MAX_UNZIPPED_BYTES"]))
    stats.setdefault("warn_unzipped_bytes", int(env["WARN_UNZIPPED_BYTES"]))
    stats.setdefault("layer_publishable", env["LAYER_PUBLISHABLE"] == "true")
    stats["container_image_bytes"] = size
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

    args = parser.parse_args()

    if args.command == "prepare":
        print(json.dumps(prepare(args.runtime, args.arch, force=args.force), indent=2))
        return

    if args.command == "verify":
        print(json.dumps(verify_image_layout(args.image), indent=2))
        return

    size = record_size(args.image, args.runtime, args.arch)
    if size is None:
        raise SystemExit(f"could not read the size of {args.image}")
    print(f"{args.image}: {size} bytes")


if __name__ == "__main__":
    _main()
