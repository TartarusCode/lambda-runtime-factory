"""Load and validate runtime manifests for the monorepo."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List


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
        "artifact": {
            "archive_name": "{distribution_version}-linux64.tar.bz2",
            "archive_url": "https://downloads.python.org/pypy/{distribution_version}-linux64.tar.bz2",
            "archive_root_dir": "{distribution_version}-linux64",
            "runtime_dir_name": "pypy",
            "package_name": "{distribution_version}.zip",
            "checksum_file": "checksums/pypy.sha256",
            "checksum_name": "{distribution_version}-linux64.tar.bz2",
        },
        "layout": {
            "bootstrap": "bootstrap/bootstrap.py3",
            "bootstrap_output": "bootstrap",
            "helper_source": "helpers/lambda_runtime_pypy",
            "helper_install_dir": "pypy/site-packages",
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


def _apply_defaults(runtime_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    runtime_family = data.get("runtime_family", REPO_DEFAULTS["runtime_family"])
    merged = _deep_merge(REPO_DEFAULTS, RUNTIME_FAMILY_DEFAULTS.get(runtime_family, {}))
    merged = _deep_merge(merged, data)

    context = {
        "runtime_id": runtime_id,
        "distribution_version": merged.get("distribution_version", ""),
        "display_name": merged.get("display_name", runtime_id),
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


def load_runtime(runtime_id: str) -> Dict[str, Any]:
    manifest_path = runtime_manifest_path(runtime_id)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Runtime manifest not found: {manifest_path}")

    data = _apply_defaults(
        runtime_id,
        json.loads(manifest_path.read_text(encoding="utf-8")),
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


def runtime_env(runtime_id: str) -> Dict[str, str]:
    data = load_runtime(runtime_id)
    runtime_dir = runtimes_root() / runtime_id
    temp_root = Path(
        os.environ.get("BUILD_ROOT")
        or os.environ.get("RUNNER_TEMP")
        or os.environ.get("TMPDIR")
        or "/tmp"
    ) / "lambda-runtime-monorepo"
    build_dir = temp_root / runtime_id
    dist_dir = repo_root() / "dist" / runtime_id
    layer_root = build_dir / "layer"
    downloads_dir = build_dir / "downloads"
    work_dir = build_dir / "work"

    env = {
        "REPO_ROOT": str(repo_root()),
        "RUNTIME_ID": runtime_id,
        "RUNTIME_DIR": str(runtime_dir),
        "TEMP_ROOT": str(temp_root),
        "RUNTIME_FAMILY": data["runtime_family"],
        "DISTRIBUTION_VERSION": data["distribution_version"],
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


def validate_runtime(runtime_id: str) -> None:
    data = load_runtime(runtime_id)
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


def compile_runtime_python(runtime_id: str) -> None:
    runtime_dir = runtimes_root() / runtime_id
    python_files = sorted(path for path in runtime_dir.rglob("*.py") if path.is_file())
    if not python_files:
        raise RuntimeError(f"No Python files found under {runtime_dir}")

    for python_file in python_files:
        subprocess.run(
            [sys.executable, "-m", "py_compile", str(python_file)],
            check=True,
        )


def manifest_matrix() -> Dict[str, Any]:
    return {"include": [{"runtime": runtime_id} for runtime_id in list_runtime_ids()]}


def print_shell_env(runtime_id: str) -> None:
    for key, value in runtime_env(runtime_id).items():
        print(f"{key}={shlex.quote(value)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--json", action="store_true")

    matrix_parser = subparsers.add_parser("matrix")
    matrix_parser.add_argument("--json", action="store_true", default=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--runtime")

    env_parser = subparsers.add_parser("env")
    env_parser.add_argument("--runtime", required=True)

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--runtime", required=True)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "list":
        runtimes = list_runtime_ids()
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
            validate_runtime(runtime_id)
        return

    if args.command == "env":
        print_shell_env(args.runtime)
        return

    if args.command == "check":
        validate_runtime(args.runtime)
        compile_runtime_python(args.runtime)
        return

    parser.error("Unhandled command")


if __name__ == "__main__":
    main()
