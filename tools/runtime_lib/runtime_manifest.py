"""Load and validate runtime manifests for the monorepo."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List

SUPPORTED_ARCHS: List[str] = ["x86_64", "arm64"]
DEFAULT_ARCH = "x86_64"

REPO_DEFAULTS: Dict[str, Any] = {
    "runtime_family": "lambda-layer",
    "lambda": {
        "compatible_runtimes": ["provided.al2023"],
    },
    "release": {
        "bucket_base_name": "tartarus-runtimes",
        "regions": ["us-east-1"],
    },
    "local_testing": {
        "sam_template": "examples/sam/template.local.example.yaml",
        "event": "examples/sam/events/hello.json",
        "function_logical_id": "HelloFunction",
        "handler": "hello.handler",
    },
    # Every runtime ships one OCI image, on the DoD-hardened Iron Bank UBI9 base.
    # There is deliberately no second base: a distroless image would mean a second
    # container matrix, a second set of tags, and the `debian:<N>-slim` staging
    # hack that cherry-picks resolved `.so` files out of Debian and into an image
    # whose RPM database never learns they exist.
    #
    # Iron Bank UBI9-minimal is the single base. It runs a real shell, so the
    # entrypoint can be the payload's bootstrap; it carries `microdnf`, so runtime
    # libraries are installed by RPM (leaving `/var/lib/rpm` and `/etc/ld.so.cache`
    # consistent, which is what Grype's RHEL backport matching reads); and it is
    # the base a DoD ATO accepts.
    "container": {
        "enabled": True,
        # `registry1.dso.mil` needs credentials, so a build without them (an
        # external PR, a fork, a local run) falls back to the upstream Red Hat
        # registry, which serves the same UBI9-minimal userland. The fallback is a
        # build-time convenience, never a published artifact.
        "base_image": "registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal:latest",
        "fallback_base_image": "registry.access.redhat.com/ubi9/ubi-minimal:latest",
        # Pinning the digest stops Iron Bank's daily automated rebuild from
        # silently changing the base under a reproducible build. Empty means
        # "follow the tag".
        "base_image_digest": "",
        # DISA STIG: the container runs as a non-root user with no writable home,
        # and /opt and /var/task are read-only at run time. 1001 is the
        # conventional first application UID.
        "nonroot_uid": 1001,
        "nonroot_gid": 1001,
        # UBI9 packages installed with the base's own package manager for a
        # family's native dependencies. The payload's DT_NEEDED scan adds whatever
        # else the base does not ship; nothing is ever copied as a bare .so.
        # The floor every runtime image installs, before the payload's DT_NEEDED
        # scan adds anything else. These are the libraries the interpreter and
        # toolchain families actually need on a UBI9-minimal base, and installing
        # them as RPMs is what keeps /var/lib/rpm able to attribute them.
        "packages": [
            "ca-certificates",
            "zlib",
            "libffi",
            "expat",
            "libxcrypt-compat",
            "bzip2-libs",
            "xz-libs",
        ],
        # Sonames the payload needs that this base genuinely cannot provide. Empty
        # by default, because an unsatisfied soname is a defect and the build must
        # say so with the soname in the message. A family may declare one here —
        # with the reason in its own comment — when the binaries that need it are
        # ones the image's contract does not run.
        "unsatisfied_sonames": [],
        # Registries the release publishes to. The first is the primary: it names
        # the image in the docs, the examples and the release record. Amazon ECR
        # Public is here because a Lambda *function* cannot pull from GHCR at
        # execution time, and ECR Public gives anonymous, high-bandwidth pulls
        # inside AWS; GHCR stays the public browsable copy.
        #
        # The alias in an ECR Public path is the registry namespace, and a registry
        # has a default alias (assigned with its first public repository) plus any
        # custom aliases requested in the ECR console — any *active* alias works for
        # pulling. `tartaruscode` is the custom alias that matches this project's
        # name, so the image path is the same on both hosts; the release checks it
        # is active and refuses to push into an alias the account does not own.
        "registries": [
            "ghcr.io/tartaruscode/lambda-runtime-factory",
            "public.ecr.aws/tartaruscode/lambda-runtime-factory",
        ],
        "registry": "ghcr.io/tartaruscode/lambda-runtime-factory",
        "workdir": "/var/task",
        "layer_root": "/opt",
        # One entrypoint for every family: the payload's bootstrap, which applies
        # the Lambda filesystem contract and execs the runtime. An interpreter
        # bootstrap is a shebang script naming the payload's own interpreter;
        # UBI9-minimal has a shell, so a `#!/bin/sh` wrapper works too.
        "entrypoint": ["/opt/bootstrap"],
        # A toolchain image has no handler to invoke, so its smoke test runs the
        # toolchain (or the launcher's contract) instead: see `smoke_test` in the
        # family defaults. An empty mapping means "this image runs a handler".
        "smoke_test": {},
        # AWS Lambda's filesystem contract, plus the STIG read-only-rootfs rule:
        # / is mounted read-only, so every path a runtime might write has to point
        # under /tmp, the one writable mount.
        "env": {
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "LAMBDA_TASK_ROOT": "/var/task",
            "PYTHONUNBUFFERED": "1",
            "GOCACHE": "/tmp/.cache/go-build",
            "GOPATH": "/tmp/go",
            "CARGO_HOME": "/tmp/.cargo",
            "RUSTUP_HOME": "/tmp/.rustup",
            "DENO_DIR": "/tmp/.deno",
            "PYTHONPYCACHEPREFIX": "/tmp/.pycache",
        },
        "user": "1001:1001",
    },
}

CONTAINER_ARCH_SLUGS: Dict[str, str] = {
    "x86_64": "amd64",
    "arm64": "arm64",
}

_ECR_PUBLIC_PREFIX = "public.ecr.aws/"
_ECR_ALIAS = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# The alias this account can actually push to and pull from. The manifest names
# the alias the project intends to end up on; until that alias is live, an unset
# ECR_PUBLIC_ALIAS resolves to the working one instead of an alias that answers 404.
DEFAULT_ECR_PUBLIC_ALIAS = "q7s5j4e1"


def with_ecr_alias(registries: List[str]) -> List[str]:
    """Rewrite the alias of every `public.ecr.aws/<alias>/...` entry.

    A custom ECR Public alias can sit pending in the console while the account's
    default alias (an auto-generated name) already works, and a release cannot push
    into an alias the registry does not consider active. The manifest names the
    alias the project *wants* — the one the docs and the image URIs use — and
    `ECR_PUBLIC_ALIAS` names the one that is live today, defaulting to the account
    alias that answers.
    """
    alias = os.environ.get("ECR_PUBLIC_ALIAS", "").strip() or DEFAULT_ECR_PUBLIC_ALIAS
    if not _ECR_ALIAS.match(alias):
        raise ValueError(
            f"ECR_PUBLIC_ALIAS '{alias}' is not a valid ECR Public alias "
            "(lowercase letters, digits and hyphens)"
        )

    rewritten: List[str] = []
    for registry in registries:
        if not registry.startswith(_ECR_PUBLIC_PREFIX):
            rewritten.append(registry)
            continue
        remainder = registry[len(_ECR_PUBLIC_PREFIX) :]
        path = remainder.split("/", 1)[1] if "/" in remainder else ""
        rewritten.append(
            f"{_ECR_PUBLIC_PREFIX}{alias}/{path}" if path else f"{_ECR_PUBLIC_PREFIX}{alias}"
        )
    return rewritten

RUNTIME_FAMILY_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "portable-pypy": {
        "arch_map": {
            "x86_64": "linux64",
            "arm64": "aarch64",
        },
        # PyPy changed its archive extension across releases (.tar.bz2 -> .tar.gz
        # at 8.0.0). The extension lives in the manifest so a bump can persist the
        # exact upstream filename instead of relying on a hardcoded template.
        "archive_ext": ".tar.bz2",
        "artifact": {
            "archive_name": "{distribution_version}-{arch_slug}{archive_ext}",
            "archive_url": "https://downloads.python.org/pypy/{distribution_version}-{arch_slug}{archive_ext}",
            "archive_root_dir": "{distribution_version}-{arch_slug}",
            "runtime_dir_name": "pypy",
            "package_name": "{distribution_version}-{arch}.zip",
            "checksum_file": "checksums/pypy.sha256",
            "checksum_name": "{distribution_version}-{arch_slug}{archive_ext}",
        },
        "layout": {
            "bootstrap": "bootstrap/bootstrap.py3",
            "bootstrap_output": "bootstrap",
            "helper_source": "helpers/lambda_runtime_pypy",
            "helper_install_dir": "pypy/site-packages",
        },
        "container": {
            # The bootstrap's shebang names the interpreter, and UBI9-minimal has a
            # shell, so the shared `/opt/bootstrap` entrypoint runs unchanged.
            "env": {"PYTHONPATH": "/opt/pypy/site-packages"},
        },
    },
    "bun": {
        "arch_map": {
            "x86_64": "linux-x64",
            "arm64": "linux-aarch64",
        },
        "artifact": {
            "archive_name": "bun-{arch_slug}.zip",
            "archive_url": "https://github.com/oven-sh/bun/releases/download/bun-v{distribution_version}/bun-{arch_slug}.zip",
            "archive_root_dir": "bun-{arch_slug}",
            "runtime_dir_name": "bun",
            "package_name": "bun-v{distribution_version}-{arch}.zip",
            "checksum_file": "checksums/bun.sha256",
            "checksum_name": "bun-{arch_slug}.zip",
        },
        "layout": {
            "bootstrap": "bootstrap/bootstrap",
            "bootstrap_output": "bootstrap",
            "helper_source": "helpers/lambda_runtime_bun",
            "helper_install_dir": "bun/lib",
        },
    },
    "deno": {
        "arch_map": {
            "x86_64": "x86_64-unknown-linux-gnu",
            "arm64": "aarch64-unknown-linux-gnu",
        },
        "artifact": {
            "archive_name": "deno-{arch_slug}.zip",
            "archive_url": "https://github.com/denoland/deno/releases/download/v{distribution_version}/deno-{arch_slug}.zip",
            "archive_root_dir": "deno",
            "binary_name": "deno",
            "runtime_dir_name": "deno",
            "package_name": "deno-v{distribution_version}-{arch}.zip",
            "checksum_file": "checksums/deno.sha256",
            "checksum_name": "deno-{arch_slug}.zip",
        },
        "layout": {
            "bootstrap": "bootstrap/bootstrap",
            "bootstrap_output": "bootstrap",
            "helper_source": "helpers/lambda_runtime_deno",
            "helper_install_dir": "deno/lib",
        },
        "container": {
            "env": {
                "DENO_NO_UPDATE_CHECK": "1",
                "NO_COLOR": "1",
            },
        },
    },
    "graalpy": {
        "arch_map": {
            "x86_64": "linux-amd64",
            "arm64": "linux-aarch64",
        },
        # The GitHub release tag is not always the distribution version: release
        # `graal-25.3.4` ships assets named `graalpy3.13-25.3.4.1-*`. `release_tag`
        # holds the tag; empty means "same as distribution_version".
        "release_tag": "",
        "artifact": {
            "archive_name": "graalpy{python_version}-{distribution_version}-{arch_slug}.tar.gz",
            "archive_url": "https://github.com/oracle/graalpython/releases/download/graal-{release_tag}/graalpy{python_version}-{distribution_version}-{arch_slug}.tar.gz",
            "archive_root_dir": "graalpy{python_version}-{distribution_version}-{arch_slug}",
            "runtime_dir_name": "graalpy",
            "package_name": "graalpy{python_version}-{distribution_version}-{arch}.zip",
            "checksum_file": "checksums/graalpy.sha256",
            "checksum_name": "graalpy{python_version}-{distribution_version}-{arch_slug}.tar.gz",
        },
        "layout": {
            "bootstrap": "bootstrap/bootstrap.py3",
            "bootstrap_output": "bootstrap",
            "helper_source": "helpers/lambda_runtime_graalpy",
            "helper_install_dir": "graalpy/lib/python{python_version}/site-packages",
        },
        "container": {
            "env": {"PYTHONPATH": "/opt/graalpy/lib/python{python_version}/site-packages"},
        },
    },
    "go-toolchain": {
        "arch_map": {
            "x86_64": "linux-amd64",
            "arm64": "linux-arm64",
        },
        "artifact": {
            "archive_name": "go{distribution_version}.{arch_slug}.tar.gz",
            "archive_url": "https://go.dev/dl/go{distribution_version}.{arch_slug}.tar.gz",
            "archive_root_dir": "go",
            "runtime_dir_name": "go",
            "package_name": "go{distribution_version}-{arch}.zip",
            "checksum_file": "checksums/go.sha256",
            "checksum_name": "go{distribution_version}.{arch_slug}.tar.gz",
        },
        "layout": {
            "bootstrap": "bootstrap/bootstrap",
            "bootstrap_output": "bootstrap",
            "helper_source": "helpers/lambda_runtime_go",
            "helper_install_dir": "go/lambda",
        },
        "container": {
            # No handler in the payload, so the smoke test runs the toolchain:
            # `go version` must exit 0 and name the distribution version, which
            # proves the UBI9 image executes it as UID 1001 on a read-only rootfs.
            "smoke_test": {"argv": ["go", "version"], "expect_version": True},
            "env": {
                "PATH": "/opt/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "GOTOOLCHAIN": "local",
            },
        },
    },
    "rust-musl": {
        "arch_map": {
            "x86_64": "x86_64-unknown-linux-musl",
            "arm64": "aarch64-unknown-linux-musl",
        },
        "artifact": {
            "archive_name": "rust-{distribution_version}-{arch_slug}.tar.gz",
            "archive_url": "https://static.rust-lang.org/dist/rust-{distribution_version}-{arch_slug}.tar.gz",
            "archive_root_dir": "rust-{distribution_version}-{arch_slug}",
            "runtime_dir_name": "rust",
            "package_name": "rust-{distribution_version}-{arch}.zip",
            "checksum_file": "checksums/rust.sha256",
            "checksum_name": "rust-{distribution_version}-{arch_slug}.tar.gz",
        },
        "layout": {
            "bootstrap": "bootstrap/bootstrap",
            "bootstrap_output": "bootstrap",
            "helper_source": "helpers/lambda_runtime_rust",
            "helper_install_dir": "rust/lambda",
        },
        "container": {
            # A consumer's handler is a static musl binary at /opt/rust/bin/<handler>,
            # which the payload's compiled launcher execs. rustc itself is
            # musl-linked (`/lib/ld-musl-x86_64.so.1`) and UBI9 ships no musl
            # loader, so the compiler cannot run on this base — verified. The smoke
            # test therefore proves the entrypoint contract instead: the launcher
            # runs as UID 1001 on a read-only rootfs and reports the Lambda handler
            # contract when no handler is supplied.
            "smoke_test": {
                "argv": ["/opt/bootstrap"],
                "expect_exit": 127,
                "expect_output": "_HANDLER is not set",
            },
            # `libc.so` is musl's soname — resolved by the musl loader, which this
            # base does not ship and UBI9 has no package for. The toolchain binaries
            # that need it (`rustc`) are the ones the runtime contract does not run;
            # a consumer's static musl handler needs neither musl nor glibc.
            "unsatisfied_sonames": ["libc.so"],
            "env": {
                "PATH": "/opt/rust/cargo/bin:/opt/rust/rustc/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            },
        },
    },
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtimes_root() -> Path:
    return repo_root() / "runtimes"


def runtime_manifest_path(runtime_id: str) -> Path:
    return runtimes_root() / runtime_id / "runtime.json"


def list_runtime_ids() -> List[str]:
    if not runtimes_root().exists():
        return []

    return sorted(
        runtime_dir.name
        for runtime_dir in runtimes_root().iterdir()
        if runtime_dir.is_dir() and (runtime_dir / "runtime.json").exists()
    )


def is_deprecated(runtime_id: str) -> bool:
    """Return whether a runtime is marked deprecated (rolled off the build matrix)."""
    manifest_path = runtime_manifest_path(runtime_id)
    if not manifest_path.exists():
        return False
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return bool(data.get("deprecated"))


def list_active_runtime_ids() -> List[str]:
    """Runtime ids that still participate in the build matrix."""
    return [runtime_id for runtime_id in list_runtime_ids() if not is_deprecated(runtime_id)]




def _require_keys(data: Dict[str, Any], keys: Iterable[str], context: str) -> None:
    missing = [key for key in keys if key not in data]
    if missing:
        raise ValueError(f"Missing required keys in {context}: {', '.join(missing)}")


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _format_values(value: Any, context: Dict[str, str]) -> Any:
    if isinstance(value, dict):
        # Keys are templates too: container.links is {link_path: target}, and the
        # path is the half that names the architecture.
        return {
            key.format(**context): _format_values(inner_value, context)
            for key, inner_value in value.items()
        }
    if isinstance(value, list):
        return [_format_values(inner_value, context) for inner_value in value]
    if isinstance(value, str):
        return value.format(**context)
    return value


def _resolve_arch_slug(family_defaults: Dict[str, Any], arch: str) -> str:
    arch_map = family_defaults.get("arch_map", {})
    if arch not in arch_map:
        raise ValueError(
            f"Architecture '{arch}' not supported. "
            f"Available: {', '.join(arch_map.keys())}"
        )
    return arch_map[arch]


def _apply_defaults(runtime_id: str, data: Dict[str, Any], arch: str = DEFAULT_ARCH) -> Dict[str, Any]:
    runtime_family = data.get("runtime_family", REPO_DEFAULTS["runtime_family"])
    family_defaults = RUNTIME_FAMILY_DEFAULTS.get(runtime_family, {})
    merged = _deep_merge(REPO_DEFAULTS, family_defaults)
    merged = _deep_merge(merged, data)

    arch_slug = _resolve_arch_slug(family_defaults, arch) if family_defaults.get("arch_map") else arch

    context = {
        "runtime_id": runtime_id,
        "distribution_version": merged.get("distribution_version", ""),
        "display_name": merged.get("display_name", runtime_id),
        "python_version": merged.get("python_version", ""),
        "archive_ext": merged.get("archive_ext", ""),
        # Upstream release tag, defaulting to the distribution version.
        "release_tag": merged.get("release_tag") or merged.get("distribution_version", ""),
        "arch": arch,
        "arch_slug": arch_slug,
        # musl names its loader after the CPU, not the Debian arch: an arm64
        # payload asks for /lib/ld-musl-aarch64.so.1.
        "musl_arch": {"x86_64": "x86_64", "arm64": "aarch64"}.get(arch, arch),
    }
    merged = _format_values(merged, context)

    merged.setdefault("lambda", {})
    merged["lambda"].setdefault("layer_name", runtime_id)
    merged["lambda"].setdefault(
        "description",
        f"{merged.get('display_name', runtime_id)} Lambda Runtime",
    )

    merged.setdefault("release", {})
    merged["release"].setdefault("s3_key_prefix", runtime_id)

    return merged


def load_runtime(runtime_id: str, arch: str = DEFAULT_ARCH) -> Dict[str, Any]:
    manifest_path = runtime_manifest_path(runtime_id)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Runtime manifest not found: {manifest_path}")

    data = _apply_defaults(
        runtime_id,
        json.loads(manifest_path.read_text(encoding="utf-8")),
        arch=arch,
    )
    validate_runtime_data(runtime_id, data)
    return data


def validate_runtime_data(runtime_id: str, data: Dict[str, Any]) -> None:
    _require_keys(
        data,
        [
            "runtime_id",
            "runtime_family",
            "artifact",
            "lambda",
            "layout",
            "release",
            "local_testing",
        ],
        f"runtime {runtime_id}",
    )

    if data["runtime_id"] != runtime_id:
        raise ValueError(
            f"Runtime manifest id mismatch: expected '{runtime_id}', got '{data['runtime_id']}'"
        )

    _require_keys(
        data["artifact"],
        [
            "archive_name",
            "archive_url",
            "checksum_file",
            "checksum_name",
            "archive_root_dir",
            "runtime_dir_name",
            "package_name",
        ],
        f"runtime {runtime_id} artifact",
    )
    _require_keys(
        data["lambda"],
        ["layer_name", "description", "compatible_runtimes"],
        f"runtime {runtime_id} lambda",
    )
    _require_keys(
        data["layout"],
        ["bootstrap", "bootstrap_output", "helper_source", "helper_install_dir"],
        f"runtime {runtime_id} layout",
    )
    _require_keys(
        data["release"],
        ["bucket_base_name", "regions", "s3_key_prefix"],
        f"runtime {runtime_id} release",
    )
    _require_keys(
        data["local_testing"],
        ["sam_template", "event", "function_logical_id"],
        f"runtime {runtime_id} local_testing",
    )
    _require_keys(
        data["container"],
        [
            "enabled",
            "base_image",
            "fallback_base_image",
            "base_image_digest",
            "nonroot_uid",
            "nonroot_gid",
            "packages",
            "unsatisfied_sonames",
            "registry",
            "registries",
            "workdir",
            "layer_root",
            "entrypoint",
            "env",
            "user",
            "smoke_test",
        ],
        f"runtime {runtime_id} container",
    )

    # A missing base, a non-numeric UID or a malformed smoke test would only
    # surface minutes later, inside a Docker build or a runner.
    for key in ("nonroot_uid", "nonroot_gid"):
        value = data["container"][key]
        if not isinstance(value, int) or value <= 0:
            raise ValueError(
                f"Runtime {runtime_id} container.{key} must be a positive integer, "
                f"not {value!r}"
            )

    # One entrypoint for every family: the payload's bootstrap, as an argv Docker
    # execs directly, never a shell string.
    if not isinstance(data["container"]["entrypoint"], list) or not data["container"]["entrypoint"]:
        raise ValueError(
            f"Runtime {runtime_id} container.entrypoint must be a non-empty argv list"
        )

    smoke_test = data["container"]["smoke_test"]
    if not isinstance(smoke_test, dict):
        raise ValueError(
            f"Runtime {runtime_id} container.smoke_test must be a mapping "
            "(see RUNTIME_FAMILY_DEFAULTS in runtime_manifest.py)"
        )
    if smoke_test:
        # An image with no handler has to be proven some other way: the toolchain
        # command, or the launcher's own contract. Without it the image ships
        # unverified.
        if not isinstance(smoke_test.get("argv"), list) or not smoke_test["argv"]:
            raise ValueError(
                f"Runtime {runtime_id} container.smoke_test.argv must be a non-empty "
                "argv list"
            )
        if "expect_exit" in smoke_test and not isinstance(smoke_test["expect_exit"], int):
            raise ValueError(
                f"Runtime {runtime_id} container.smoke_test.expect_exit must be an "
                "integer exit code"
            )

    unsatisfied = data["container"]["unsatisfied_sonames"]
    if not isinstance(unsatisfied, list) or any(
        not isinstance(name, str) for name in unsatisfied
    ):
        raise ValueError(
            f"Runtime {runtime_id} container.unsatisfied_sonames must be a list of "
            "soname strings"
        )

    # The release pushes to every registry in the list, and the first one is what
    # the docs and the release record name: an empty list publishes nowhere, and a
    # primary that is not in the list would be advertised but never updated.
    registries = [item.rstrip("/") for item in data["container"]["registries"]]
    if not registries:
        raise ValueError(
            f"Runtime {runtime_id} container.registries must name at least one registry"
        )
    if len(set(registries)) != len(registries):
        raise ValueError(f"Runtime {runtime_id} container.registries contains a duplicate")
    if data["container"]["registry"].rstrip("/") not in registries:
        raise ValueError(
            f"Runtime {runtime_id} container.registry must be one of "
            f"container.registries ({', '.join(registries)})"
        )

    if not data["lambda"]["compatible_runtimes"]:
        raise ValueError(f"Runtime {runtime_id} must declare compatible runtimes")
    if not data["release"]["regions"]:
        raise ValueError(f"Runtime {runtime_id} must declare at least one release region")


def _absolute(runtime_id: str, relative_path: str) -> Path:
    return runtimes_root() / runtime_id / Path(relative_path)


def runtime_env(runtime_id: str, arch: str = DEFAULT_ARCH) -> Dict[str, str]:
    if arch not in SUPPORTED_ARCHS:
        raise ValueError(f"Unsupported architecture '{arch}'. Supported: {', '.join(SUPPORTED_ARCHS)}")

    data = load_runtime(runtime_id, arch=arch)
    runtime_dir = runtimes_root() / runtime_id
    temp_root = Path(
        os.environ.get("BUILD_ROOT")
        or os.environ.get("RUNNER_TEMP")
        or os.environ.get("TMPDIR")
        or "/tmp"
    ) / "lambda-runtime-monorepo"
    build_dir = temp_root / runtime_id / arch
    download_cache_root = Path(
        os.environ.get("DOWNLOAD_CACHE_DIR")
        or temp_root / "download-cache"
    )
    dist_dir = repo_root() / "dist" / runtime_id / arch
    layer_root = build_dir / "layer"
    downloads_dir = download_cache_root / runtime_id
    work_dir = build_dir / "work"
    container = data["container"]
    container_arch = CONTAINER_ARCH_SLUGS.get(arch, arch)
    container_registries = with_ecr_alias([item.rstrip("/") for item in container["registries"]])
    container_registry = with_ecr_alias([container["registry"].rstrip("/")])[0]
    container_image = f"{container_registry}/{runtime_id}"
    container_tag = data["distribution_version"]

    env = {
        "REPO_ROOT": str(repo_root()),
        "RUNTIME_ID": runtime_id,
        "RUNTIME_DIR": str(runtime_dir),
        "TEMP_ROOT": str(temp_root),
        "RUNTIME_FAMILY": data["runtime_family"],
        "DISTRIBUTION_VERSION": data["distribution_version"],
        "VERSION_LINE": str(
            data.get("version_line")
            or data.get("python_version")
            or data["distribution_version"]
        ),
        "ARCH": arch,
        "LAMBDA_ARCH": arch,
        "BUILD_DIR": str(build_dir),
        "DIST_DIR": str(dist_dir),
        "LAYER_ROOT": str(layer_root),
        "DOWNLOADS_DIR": str(downloads_dir),
        "WORK_DIR": str(work_dir),
        "ARCHIVE_NAME": data["artifact"]["archive_name"],
        "ARCHIVE_URL": data["artifact"]["archive_url"],
        "ARCHIVE_PATH": str(downloads_dir / data["artifact"]["archive_name"]),
        "CHECKSUM_FILE": str(_absolute(runtime_id, data["artifact"]["checksum_file"])),
        "CHECKSUM_NAME": data["artifact"]["checksum_name"],
        "ARCHIVE_ROOT_DIR": data["artifact"]["archive_root_dir"],
        "BINARY_NAME": data["artifact"].get("binary_name", ""),
        "RUNTIME_DIR_NAME": data["artifact"]["runtime_dir_name"],
        "PACKAGE_NAME": data["artifact"]["package_name"],
        "ARTIFACT_DIR": str(build_dir / "artifacts"),
        "PACKAGE_PATH": str(build_dir / "artifacts" / data["artifact"]["package_name"]),
        # Digest-verified Syft cache, keyed per runtime and architecture. The
        # metadata file written beside the two documents records the image digest,
        # the base image digest and the Syft version that produced them, so a
        # restored entry is reused only when it describes these exact bytes.
        "SBOM_CACHE_DIR": os.environ.get("SBOM_CACHE_DIR") or str(temp_root / "sbom-cache"),
        "BOOTSTRAP_SOURCE": str(_absolute(runtime_id, data["layout"]["bootstrap"])),
        "BOOTSTRAP_OUTPUT": data["layout"]["bootstrap_output"],
        "HELPER_SOURCE": str(_absolute(runtime_id, data["layout"]["helper_source"])),
        "HELPER_INSTALL_DIR": data["layout"]["helper_install_dir"],
        "LAYER_NAME": data["lambda"]["layer_name"],
        "LAYER_DESCRIPTION": data["lambda"]["description"],
        "COMPATIBLE_RUNTIMES": " ".join(data["lambda"]["compatible_runtimes"]),
        "BUCKET_BASE_NAME": data["release"]["bucket_base_name"],
        "RELEASE_REGIONS": " ".join(data["release"]["regions"]),
        "S3_KEY_PREFIX": data["release"]["s3_key_prefix"],
        "LOCAL_TEMPLATE": str(_absolute(runtime_id, data["local_testing"]["sam_template"])),
        "LOCAL_EVENT": str(_absolute(runtime_id, data["local_testing"]["event"])),
        "LOCAL_FUNCTION_NAME": data["local_testing"]["function_logical_id"],
        "LOCAL_HANDLER": data["local_testing"]["handler"],
        "LOCAL_ROOT": str(build_dir / "local"),
        "LOCAL_LAYER_DIR": str(build_dir / "local" / "layer"),
        "LOCAL_SOURCE_DIR": str(build_dir / "local" / "source"),
        "LOCAL_TEMPLATE_RENDERED": str(build_dir / "local" / "template.rendered.yaml"),
        "LOCAL_BUILD_DIR": str(build_dir / "local" / ".aws-sam" / "build"),
        "LOCAL_BUILD_TEMPLATE": str(build_dir / "local" / ".aws-sam" / "build" / "template.yaml"),
        "LOCAL_INVOKE_DIR": str(build_dir / "local" / "invoke"),
        "LOCAL_INVOKE_TEMPLATE": str(build_dir / "local" / "template.invoke.yaml"),
        "LOCAL_CODE_URI": str(
            _absolute(
                runtime_id,
                data["local_testing"].get("code_uri", "examples/sam/hello"),
            )
        ),
        # Container coordinates. The entrypoint and environment travel as JSON so
        # the Dockerfile generator cannot drift from what the manifest declares.
        "CONTAINER_ENABLED": "true" if container["enabled"] else "false",
        "CONTAINER_BASE_IMAGE": container["base_image"],
        "CONTAINER_FALLBACK_BASE_IMAGE": container["fallback_base_image"],
        "CONTAINER_BASE_IMAGE_DIGEST": container["base_image_digest"],
        "CONTAINER_NONROOT_UID": str(container["nonroot_uid"]),
        "CONTAINER_NONROOT_GID": str(container["nonroot_gid"]),
        "CONTAINER_PACKAGES": json.dumps(container["packages"]),
        "CONTAINER_UNSATISFIED_SONAMES": json.dumps(container["unsatisfied_sonames"]),
        "CONTAINER_SMOKE_TEST": json.dumps(container["smoke_test"]),
        "CONTAINER_REGISTRY": container_registry,
        "CONTAINER_REGISTRIES": json.dumps(container_registries),
        "CONTAINER_IMAGE": container_image,
        "CONTAINER_TAG": container_tag,
        "CONTAINER_ARCH": container_arch,
        "CONTAINER_PLATFORM": f"linux/{container_arch}",
        "CONTAINER_WORKDIR": container["workdir"],
        "CONTAINER_LAYER_ROOT": container["layer_root"],
        "CONTAINER_USER": container["user"],
        "CONTAINER_ENTRYPOINT": json.dumps(container["entrypoint"]),
        "CONTAINER_ENV": json.dumps(container["env"], sort_keys=True),
        "CONTAINER_CONTEXT_DIR": str(build_dir / "container"),
        "CONTAINER_LOCAL_IMAGE": f"lambda-runtime-factory/{runtime_id}:{container_tag}-{arch}",
        # Docker Buildx registry-cache coordinate. Every runner rebuilding the same
        # base image and the same installed packages from scratch is what the cache
        # exists to avoid; the ref is derived here so the workflow, the release and
        # a local run all point at the same tag.
        "CONTAINER_CACHE_REF": f"{container_image}:buildcache-{container_arch}",
        "EXPORT_ARTIFACT_DIR": os.environ.get("EXPORT_ARTIFACT_DIR", ""),
    }
    return env


def validate_runtime(runtime_id: str, arch: str = DEFAULT_ARCH) -> None:
    data = load_runtime(runtime_id, arch=arch)
    files_to_check = [
        _absolute(runtime_id, data["artifact"]["checksum_file"]),
        _absolute(runtime_id, data["layout"]["bootstrap"]),
        _absolute(runtime_id, data["layout"]["helper_source"]),
        _absolute(runtime_id, data["local_testing"]["sam_template"]),
        _absolute(runtime_id, data["local_testing"]["event"]),
    ]

    for file_path in files_to_check:
        if not file_path.exists():
            raise FileNotFoundError(f"Referenced path does not exist: {file_path}")


def _head_status(url: str, timeout: int = 30) -> int | None:
    """Return the HTTP status for a HEAD request, or None when unreachable."""
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "lambda-runtime-validate/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None


def validate_archive_urls(runtime_ids: Iterable[str] | None = None) -> List[str]:
    """HEAD each runtime's archive_url and return definitive "missing" problems.

    Only 404/410 count as failures — they prove the URL the build will request
    does not exist (e.g. a derived field such as `release_tag` left stale). Any
    other error is ignored so a transient network blip cannot flake the check.
    """
    ids = list(runtime_ids) if runtime_ids is not None else list_active_runtime_ids()
    problems: List[str] = []
    for runtime_id in ids:
        for arch in SUPPORTED_ARCHS:
            try:
                data = load_runtime(runtime_id, arch=arch)
            except Exception as exc:  # noqa: BLE001 - report, keep checking others
                problems.append(f"{runtime_id}/{arch}: manifest invalid: {exc}")
                continue
            url = data["artifact"]["archive_url"]
            status = _head_status(url)
            if status in (404, 410):
                problems.append(f"{runtime_id}/{arch}: {url} -> HTTP {status}")
    return problems


def compile_runtime_python(runtime_id: str) -> None:
    runtime_dir = runtimes_root() / runtime_id
    python_files = sorted(path for path in runtime_dir.rglob("*.py") if path.is_file())
    if not python_files:
        return

    for python_file in python_files:
        subprocess.run(
            [sys.executable, "-m", "py_compile", str(python_file)],
            check=True,
        )


ARCH_RUNNERS: Dict[str, str] = {
    "x86_64": "ubuntu-26.04",
    "arm64": "ubuntu-26.04-arm",
}

# Pin runner images explicitly: the `ubuntu-latest` label migrates between OS
# releases on GitHub's schedule (24.04 -> 26.04 rolls out from 2026-10-19), which
# would silently change the toolchain under a pipeline that builds native
# runtimes. Bump these deliberately and re-validate the build.
DEFAULT_RUNNER = "ubuntu-26.04"


def manifest_matrix(
    runtime_ids: Iterable[str] | None = None,
    archs: Iterable[str] | None = None,
    include_deprecated: bool = False,
) -> Dict[str, Any]:
    """Return the CI build matrix, optionally filtered to specific runtimes/arches.

    Deprecated runtimes are excluded (so the matrix cannot grow without bound)
    unless explicitly requested with include_deprecated. Every remaining runtime
    appears in both workflows: there is one artifact per runtime now, so there is
    nothing left to narrow.
    """
    selected = list(runtime_ids) if runtime_ids is not None else list_runtime_ids()
    selected_archs = list(archs) if archs is not None else SUPPORTED_ARCHS
    entries = []
    for runtime_id in selected:
        # Merged data, not the raw manifest: a family default can then declare a
        # flag once instead of every line repeating it. Reading the raw file meant
        # a runtime line generated by bump_version.py silently lost the flag — and
        # a new go/rust line walked straight back into the publish matrix.
        data = load_runtime(runtime_id)
        if data.get("deprecated") and not include_deprecated:
            continue
        skip_local = data.get("local_testing", {}).get("skip_local_invoke", False)
        for arch in selected_archs:
            entries.append({
                "runtime": runtime_id,
                "arch": arch,
                "runner": ARCH_RUNNERS.get(arch, DEFAULT_RUNNER),
                "skip_local_invoke": skip_local,
            })
    return {"include": entries}


def print_shell_env(runtime_id: str, arch: str = DEFAULT_ARCH) -> None:
    for key, value in runtime_env(runtime_id, arch=arch).items():
        print(f"{key}={shlex.quote(value)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument(
        "--active", action="store_true", help="Only runtimes in the build matrix (not deprecated)"
    )

    matrix_parser = subparsers.add_parser("matrix")
    matrix_parser.add_argument("--json", action="store_true", default=True)
    matrix_parser.add_argument("--runtime", help="Runtime id, or 'all'")
    matrix_parser.add_argument("--arch", help="Architecture, or 'all'")
    matrix_parser.add_argument(
        "--include-deprecated", action="store_true", help="Also emit deprecated runtimes"
    )

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--runtime")
    validate_parser.add_argument("--arch", default=DEFAULT_ARCH)
    validate_parser.add_argument(
        "--online",
        action="store_true",
        help="Also HEAD each runtime's archive_url and fail on 404/410",
    )

    env_parser = subparsers.add_parser("env")
    env_parser.add_argument("--runtime", required=True)
    env_parser.add_argument("--arch", default=DEFAULT_ARCH)

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--runtime", required=True)
    check_parser.add_argument("--arch", default=DEFAULT_ARCH)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "list":
        runtimes = list_active_runtime_ids() if args.active else list_runtime_ids()
        if args.json:
            print(json.dumps(runtimes))
        else:
            print("\n".join(runtimes))
        return

    if args.command == "matrix":
        runtime_ids = None
        if args.runtime and args.runtime != "all":
            # A caller may narrow to several runtimes at once — the fast PR tier
            # passes the ids whose files changed — so the value is a comma-separated
            # list as well as a single id.
            requested = [item.strip() for item in args.runtime.split(",") if item.strip()]
            known = list_runtime_ids()
            for runtime_id in requested:
                if runtime_id not in known:
                    raise SystemExit(
                        f"Unknown runtime '{runtime_id}'. Available: {', '.join(known)}"
                    )
            runtime_ids = requested
        archs = None
        if args.arch and args.arch != "all":
            if args.arch not in SUPPORTED_ARCHS:
                raise SystemExit(
                    f"Unknown arch '{args.arch}'. Available: {', '.join(SUPPORTED_ARCHS)}"
                )
            archs = [args.arch]
        print(json.dumps(manifest_matrix(
            runtime_ids,
            archs,
            include_deprecated=args.include_deprecated,
        )))
        return

    if args.command == "validate":
        runtime_ids = [args.runtime] if args.runtime else list_runtime_ids()
        for runtime_id in runtime_ids:
            for arch in SUPPORTED_ARCHS:
                validate_runtime(runtime_id, arch=arch)
        if args.online:
            problems = validate_archive_urls(runtime_ids)
            for problem in problems:
                print(f"archive URL check failed: {problem}", file=sys.stderr)
            if problems:
                raise SystemExit(1)
        return

    if args.command == "env":
        print_shell_env(args.runtime, arch=args.arch)
        return

    if args.command == "check":
        validate_runtime(args.runtime, arch=args.arch)
        compile_runtime_python(args.runtime)
        return

    parser.error("Unhandled command")


if __name__ == "__main__":
    main()
