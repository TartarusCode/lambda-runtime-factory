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
    "release": {
        "bucket_base_name": "tartaruscode-custom-runtimes",
        "regions": ["eu-central-1", "us-west-1"],
    },
    "local_testing": {
        "sam_template": "examples/sam/template.local.example.yaml",
        "event": "examples/sam/events/hello.json",
        "function_logical_id": "HelloFunction",
        "handler": "hello.handler",
    },
}

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
        return {key: _format_values(inner_value, context) for key, inner_value in value.items()}
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

    env = {
        "REPO_ROOT": str(repo_root()),
        "RUNTIME_ID": runtime_id,
        "RUNTIME_DIR": str(runtime_dir),
        "TEMP_ROOT": str(temp_root),
        "RUNTIME_FAMILY": data["runtime_family"],
        "DISTRIBUTION_VERSION": data["distribution_version"],
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
    "x86_64": "ubuntu-latest",
    "arm64": "ubuntu-24.04-arm",
}


def manifest_matrix() -> Dict[str, Any]:
    entries = []
    for runtime_id in list_runtime_ids():
        data = json.loads(runtime_manifest_path(runtime_id).read_text(encoding="utf-8"))
        # Deprecated runtimes are rolled off the matrix so it cannot grow without
        # bound; they remain buildable on demand via `make build RUNTIME=...`.
        if data.get("deprecated"):
            continue
        skip_local = data.get("local_testing", {}).get("skip_local_invoke", False)
        for arch in SUPPORTED_ARCHS:
            entries.append({
                "runtime": runtime_id,
                "arch": arch,
                "runner": ARCH_RUNNERS.get(arch, "ubuntu-latest"),
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
        print(json.dumps(manifest_matrix()))
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
