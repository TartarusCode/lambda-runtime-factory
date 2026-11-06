#!/bin/bash

# The tag a SOCI-enabled image is published under. Per architecture it is
# `<tag>-<arch>-soci`; the merge turns the two into `<tag>-soci` and
# `<line>-soci`. Lambda's function image URI points at the merged tag.
CONTAINER_SOCI_SUFFIX="-soci"

common::repo_root() {
    cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd
}

common::manifest_cli() {
    printf '%s/tools/runtime_lib/runtime_manifest.py' "$(common::repo_root)"
}

common::require_arch() {
    local arch=${1:-${ARCH:-x86_64}}
    printf '%s' "${arch}"
}

common::load_runtime_env() {
    local runtime_id=$1
    local arch
    arch=$(common::require_arch "${2:-}")
    local variant=${3:-standard}
    eval "$(
        python3 "$(common::manifest_cli)" env --runtime "${runtime_id}" --arch "${arch}" --variant "${variant}"
    )"
}

common::ensure_runtime_built() {
    local runtime_id=$1
    local arch
    arch=$(common::require_arch "${2:-}")
    local variant=${3:-standard}
    if [[ "${RUNTIME_BUILD_IN_PROGRESS:-0}" == "1" ]]; then
        return 0
    fi

    if [[ ! -d "${LAYER_ROOT}" || ! -f "${PACKAGE_PATH}" ]]; then
        bash "$(common::repo_root)/tools/bin/build-runtime" "${runtime_id}" "${arch}"
        common::load_runtime_env "${runtime_id}" "${arch}" "${variant}"
    fi
}

common::require_runtime_id() {
    local runtime_id=${1:-${RUNTIME:-}}
    if [[ -z "${runtime_id}" ]]; then
        echo "Runtime id must be provided as the first argument or RUNTIME=..." >&2
        exit 1
    fi

    printf '%s' "${runtime_id}"
}

common::sha256_file() {
    local target=$1
    if type -P sha256sum >/dev/null; then
        sha256sum "${target}" | awk '{ print $1 }'
    else
        shasum -a 256 "${target}" | awk '{ print $1 }'
    fi
}

common::temp_root() {
    python3 - <<'PY'
import os
from pathlib import Path

temp_root = Path(
    os.environ.get("BUILD_ROOT")
    or os.environ.get("RUNNER_TEMP")
    or os.environ.get("TMPDIR")
    or "/tmp"
) / "lambda-runtime-monorepo"
print(temp_root)
PY
}
