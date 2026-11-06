"""Load and validate runtime manifests for the monorepo."""

from __future__ import annotations

import argparse
import json
import os
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
    # A deployment package is capped at 250 MB unzipped, and that ceiling covers
    # the function plus every layer attached to it. These are repo-wide so the
    # build and the publish matrix cannot disagree about the limit.
    "layer_publishable": True,
    "max_unzipped_bytes": 262144000,
    "warn_unzipped_bytes": 209715200,
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
    # Every runtime also ships as a distroless OCI image. A container carries a
    # 10 GB uncompressed ceiling instead of the 250 MB layer budget, so it is the
    # delivery path for the toolchains that can never be a layer.
    #
    # Distroless means no shell, no package manager and no OS userland: the
    # entrypoint must name a real binary and its arguments. Families that need a
    # different invocation (an interpreter, or the runtime with flags) override
    # `entrypoint` in RUNTIME_FAMILY_DEFAULTS below.
    "container": {
        "enabled": True,
        # `distroless` is a runtime base: no shell, no package manager, and the
        # entrypoint must be a real binary in the payload. `build` is a build
        # environment: the base has a shell and a package manager, so a consumer
        # can `RUN go build` / `RUN cargo build` in a Dockerfile FROM this image.
        # A toolchain image is only useful as a build stage, and a distroless base
        # cannot run a `RUN` step at all.
        "profile": "distroless",
        "base_image": "gcr.io/distroless/cc-debian12:nonroot",
        # Every runtime also ships a `<tag>-debug` variant on a base that carries a
        # busybox shell and nothing else, so a failing image can be inspected
        # (`docker run --rm -it --entrypoint /busybox/sh <image>`) without putting a
        # shell into the production image. Empty means "no debug variant": the
        # `build` families already have a shell, so a second copy would be the same
        # image under another tag.
        "debug_base_image": "gcr.io/distroless/cc-debian12:debug-nonroot",
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
        "entrypoint": ["/opt/bootstrap"],
        # Build profiles install these with the base's own package manager; the
        # distroless profile derives its packages from the payload's DT_NEEDED
        # scan instead, so this stays empty there.
        "packages": [],
        # Symlinks the image needs to make the payload runnable, as
        # {link_path: target}. Values are formatted with the arch context.
        "links": {},
        # A build image has no handler to invoke, so its smoke test runs the
        # toolchain instead. The output must name the distribution version.
        "smoke_test": [],
        # AWS Lambda's filesystem contract: the task code is at /var/task and the
        # only writable path is /tmp. HOME and TMPDIR point there because the
        # distroless user (65532) has no writable home of its own.
        "env": {
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "LAMBDA_TASK_ROOT": "/var/task",
            "PYTHONUNBUFFERED": "1",
        },
        "user": "65532:65532",
    },
}

CONTAINER_ARCH_SLUGS: Dict[str, str] = {
    "x86_64": "amd64",
    "arm64": "arm64",
}

# `distroless` images are runtime bases; `build` images are build environments
# with a shell and a package manager. See the container block in REPO_DEFAULTS.
CONTAINER_PROFILES = ("distroless", "build")

# A runtime publishes a standard image and, when its base is distroless, a
# `<tag>-debug` variant of the same payload with a busybox shell in it.
CONTAINER_VARIANTS = ("standard", "debug")

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
            # The shebang names the interpreter, but an ENTRYPOINT that names it
            # explicitly does not depend on a shebang at all — and distroless has
            # no /usr/bin/env to fall back on.
            "entrypoint": ["/opt/pypy/bin/pypy", "/opt/bootstrap"],
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
        "container": {
            # No /bin/sh in distroless: invoke Bun on the helper directly. The
            # helper initializes the environment the old shell bootstrap set up.
            "entrypoint": ["/opt/bun/bun", "run", "/opt/bun/lib/lambda_runtime_bun/runtime.ts"],
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
            # The shell bootstrap's permission flags have to travel in argv when
            # there is no shell to expand them.
            "entrypoint": [
                "/opt/deno/deno",
                "run",
                "--allow-net",
                "--allow-env",
                "--allow-read=/var/task,/opt/deno/lib",
                "/opt/deno/lib/lambda_runtime_deno/runtime.ts",
            ],
            "env": {
                "DENO_DIR": "/tmp/.deno",
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
            "entrypoint": ["/opt/graalpy/bin/graalpy", "/opt/bootstrap"],
            "env": {"PYTHONPATH": "/opt/graalpy/lib/python{python_version}/site-packages"},
        },
    },
    "go-toolchain": {
        "arch_map": {
            "x86_64": "linux-amd64",
            "arm64": "linux-arm64",
        },
        # Deliberately not a layer, and not because of the byte budget: after
        # slimming, Go 1.26 measures ~176 MB unzipped, which fits. A layer is the
        # wrong shape for a toolchain — the bootstrap expects a compiled handler
        # under /opt/go/bin, and a consumer's code lives read-only at /var/task.
        # The Go image is the published artifact; see DELIVERY.md.
        "layer_publishable": False,
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
            # A toolchain image is a build environment, not a runtime: the
            # documented use is `FROM .../go126:1.26 AS build` followed by
            # `RUN go build`. Distroless has no /bin/sh, so that RUN step fails
            # before it starts; debian:12-slim is the same glibc the Go toolchain
            # is built against and carries a shell.
            "profile": "build",
            "base_image": "debian:12-slim",
            # A build image already ships a shell, so there is no separate debug
            # variant to publish.
            "debug_base_image": "",
            "user": "0:0",
            "entrypoint": ["/bin/sh"],
            "smoke_test": ["go", "version"],
            "env": {
                "HOME": "/root",
                "PATH": "/opt/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "GOCACHE": "/tmp/go-build",
                "GOPATH": "/tmp/go",
                "GOTOOLCHAIN": "local",
            },
        },
    },
    "rust-musl": {
        "arch_map": {
            "x86_64": "x86_64-unknown-linux-musl",
            "arm64": "aarch64-unknown-linux-musl",
        },
        # Same delivery-shape answer as Go: the Rust distribution is a toolchain,
        # not a handler host, and it is far over the layer budget anyway.
        "layer_publishable": False,
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
            # The musl toolchain's own binaries are dynamically linked against
            # musl (`NEEDED libc.so`, interpreter /lib/ld-musl-x86_64.so.1), so a
            # glibc base cannot run rustc at all. Alpine is the musl base, and it
            # has the shell a `RUN cargo build` needs.
            "profile": "build",
            "base_image": "alpine:3.22",
            # Same as Go: the musl toolchain base already has a shell.
            "debug_base_image": "",
            "user": "0:0",
            "entrypoint": ["/bin/sh"],
            "smoke_test": ["rustc", "--version"],
            # libgcc supplies libgcc_s.so.1 (NEEDED by rustc); gcc and musl-dev
            # supply the `cc` linker the musl target invokes.
            "packages": ["libgcc", "gcc", "musl-dev"],
            "links": {
                # musl's soname is libc.so, but Alpine ships the file as
                # libc.musl-<arch>.so.1; the loader resolves the soname by name.
                "/usr/lib/libc.so": "/lib/ld-musl-{musl_arch}.so.1",
                # The distribution is a component tree, not a merged prefix:
                # rustc's sysroot is /opt/rust/rustc, and the std it needs lives
                # in the rust-std component. Without this link every build fails
                # with E0463 "can't find crate for `std`". Only the target's lib/
                # is linked: rustc's own rustlib/<target>/bin holds rust-lld.
                "/opt/rust/rustc/lib/rustlib/{arch_slug}/lib": "/opt/rust/rust-std-{arch_slug}/lib/rustlib/{arch_slug}/lib",
            },
            "env": {
                "HOME": "/root",
                "PATH": "/opt/rust/cargo/bin:/opt/rust/rustc/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "CARGO_HOME": "/tmp/cargo",
                "RUSTUP_HOME": "/tmp/rustup",
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


def is_layer_publishable(runtime_id: str) -> bool:
    """Return whether the runtime's package can be published as a Lambda layer.

    A deployment package is capped at 250 MB unzipped, and that limit covers the
    function plus every layer attached to it — so a runtime shipping a whole
    upstream toolchain cannot be delivered as a layer no matter how it is split.
    Such a runtime is still built and audited like any other; it is only kept out
    of the publish matrix, so a release does not fail on an AWS quota instead of
    publishing.

    Resolved through ``load_runtime``, so the answer comes from the merged view:
    a family default in RUNTIME_FAMILY_DEFAULTS declares it once for every line,
    including lines that do not exist yet.
    """
    return bool(load_runtime(runtime_id).get("layer_publishable", True))


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
            "profile",
            "base_image",
            "debug_base_image",
            "registry",
            "registries",
            "workdir",
            "layer_root",
            "entrypoint",
            "env",
            "user",
            "packages",
            "links",
            "smoke_test",
        ],
        f"runtime {runtime_id} container",
    )

    if data["container"]["profile"] not in CONTAINER_PROFILES:
        raise ValueError(
            f"Runtime {runtime_id} container.profile must be one of "
            f"{', '.join(sorted(CONTAINER_PROFILES))}, not "
            f"'{data['container']['profile']}'"
        )

    # A distroless image has no shell, so a shell string here can never run: the
    # entrypoint must already be the argv Docker will exec. A build profile has a
    # shell, so it may name one.
    if not isinstance(data["container"]["entrypoint"], list) or not data["container"]["entrypoint"]:
        raise ValueError(
            f"Runtime {runtime_id} container.entrypoint must be a non-empty argv list, "
            "not a shell string (the distroless base has no /bin/sh)"
        )

    if data["container"]["profile"] == "distroless" and data["container"]["packages"]:
        raise ValueError(
            f"Runtime {runtime_id} container.packages is only meaningful for the "
            "'build' profile; a distroless image stages the libraries its payload "
            "links against instead"
        )

    # A build image has no handler to invoke, so the toolchain command is the only
    # thing that proves the image works. Without it the image ships unverified.
    if data["container"]["profile"] == "build" and not data["container"]["smoke_test"]:
        raise ValueError(
            f"Runtime {runtime_id} container.smoke_test must name the toolchain "
            "command that proves a build image works (e.g. ['go', 'version'])"
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


def runtime_env(runtime_id: str, arch: str = DEFAULT_ARCH, variant: str = "standard") -> Dict[str, str]:
    if arch not in SUPPORTED_ARCHS:
        raise ValueError(f"Unsupported architecture '{arch}'. Supported: {', '.join(SUPPORTED_ARCHS)}")
    if variant not in CONTAINER_VARIANTS:
        raise ValueError(
            f"Unsupported container variant '{variant}'. Supported: {', '.join(CONTAINER_VARIANTS)}"
        )

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
    container_registries = [item.rstrip("/") for item in container["registries"]]
    container_registry = container["registry"].rstrip("/")
    container_image = f"{container_registry}/{runtime_id}"
    container_tag = data["distribution_version"]
    # A debug image is the same payload on a base that carries a busybox shell.
    # Only the base and the tags change: the entrypoint, user and environment stay
    # identical, so what a user inspects is what failed in production.
    debug_base_image = container.get("debug_base_image", "")
    debug = variant == "debug"
    if debug and not debug_base_image:
        raise ValueError(
            f"{runtime_id} has no debug container variant: container.debug_base_image is "
            f"empty (a '{container['profile']}' base already carries a shell)"
        )
    container_base_image = debug_base_image if debug else container["base_image"]
    variant_suffix = "-debug" if debug else ""

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
        "BOOTSTRAP_SOURCE": str(_absolute(runtime_id, data["layout"]["bootstrap"])),
        "BOOTSTRAP_OUTPUT": data["layout"]["bootstrap_output"],
        "HELPER_SOURCE": str(_absolute(runtime_id, data["layout"]["helper_source"])),
        "HELPER_INSTALL_DIR": data["layout"]["helper_install_dir"],
        "LAYER_NAME": data["lambda"]["layer_name"],
        "LAYER_DESCRIPTION": data["lambda"]["description"],
        "COMPATIBLE_RUNTIMES": " ".join(data["lambda"]["compatible_runtimes"]),
        # The publish decision and its byte budget travel to the build script, so
        # the size gate and the publish matrix can never disagree about either.
        "LAYER_PUBLISHABLE": "true" if data.get("layer_publishable", True) else "false",
        "MAX_UNZIPPED_BYTES": str(data.get("max_unzipped_bytes", 262144000)),
        "WARN_UNZIPPED_BYTES": str(data.get("warn_unzipped_bytes", 209715200)),
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
        "CONTAINER_VARIANT": variant,
        "CONTAINER_TAG_SUFFIX": variant_suffix,
        "CONTAINER_DEBUG_ENABLED": "true" if debug_base_image else "false",
        "CONTAINER_DEBUG_BASE_IMAGE": debug_base_image,
        "CONTAINER_PROFILE": container["profile"],
        "CONTAINER_BASE_IMAGE": container_base_image,
        "CONTAINER_PACKAGES": json.dumps(container["packages"]),
        "CONTAINER_LINKS": json.dumps(container["links"], sort_keys=True),
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
        "CONTAINER_CONTEXT_DIR": str(build_dir / ("container-debug" if debug else "container")),
        "CONTAINER_LOCAL_IMAGE": (
            f"lambda-runtime-factory/{runtime_id}:{container_tag}-{arch}{variant_suffix}"
        ),
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
    layer_publishable_only: bool = False,
) -> Dict[str, Any]:
    """Return the CI build matrix, optionally filtered to specific runtimes/arches.

    Deprecated runtimes are excluded (so the matrix cannot grow without bound)
    unless explicitly requested with include_deprecated. The build matrix keeps
    every publishable-by-nothing runtime, because CI's job is to build and audit
    them; only the publish matrix narrows further with layer_publishable_only.
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
        if layer_publishable_only and not data.get("layer_publishable", True):
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


def print_shell_env(runtime_id: str, arch: str = DEFAULT_ARCH, variant: str = "standard") -> None:
    for key, value in runtime_env(runtime_id, arch=arch, variant=variant).items():
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
    matrix_parser.add_argument(
        "--layer-publishable-only",
        action="store_true",
        help="Only runtimes that can be published as a layer (the publish matrix)",
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
    env_parser.add_argument(
        "--variant",
        default="standard",
        choices=CONTAINER_VARIANTS,
        help="container variant whose coordinates to emit (standard or debug)",
    )

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
            known = list_runtime_ids()
            if args.runtime not in known:
                raise SystemExit(
                    f"Unknown runtime '{args.runtime}'. Available: {', '.join(known)}"
                )
            runtime_ids = [args.runtime]
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
            layer_publishable_only=args.layer_publishable_only,
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
        try:
            print_shell_env(args.runtime, arch=args.arch, variant=args.variant)
        except ValueError as error:
            # A variant the runtime does not have is a user error, not a crash:
            # the build scripts eval this output and need a one-line message.
            raise SystemExit(str(error))
        return

    if args.command == "check":
        validate_runtime(args.runtime, arch=args.arch)
        compile_runtime_python(args.runtime)
        return

    parser.error("Unhandled command")


if __name__ == "__main__":
    main()
