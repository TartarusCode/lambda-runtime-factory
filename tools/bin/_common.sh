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
    local arch env_text
    arch=$(common::require_arch "${2:-}")
    # A dropped runtime has no manifest, and `eval "$(failed command)"` evaluates
    # the empty string: the script then died on an unbound CONTAINER_TAG far from
    # the cause. Fail here instead, with the generator's own error above it.
    if ! env_text=$(python3 "$(common::manifest_cli)" env --runtime "${runtime_id}" --arch "${arch}"); then
        echo "No environment for ${runtime_id} (${arch}): is the runtime still in the manifest?" >&2
        exit 1
    fi
    eval "${env_text}"
}

common::ensure_runtime_built() {
    local runtime_id=$1
    local arch
    arch=$(common::require_arch "${2:-}")
    if [[ "${RUNTIME_BUILD_IN_PROGRESS:-0}" == "1" ]]; then
        return 0
    fi

    if [[ ! -d "${LAYER_ROOT}" || ! -f "${PACKAGE_PATH}" ]]; then
        bash "$(common::repo_root)/tools/bin/build-runtime" "${runtime_id}" "${arch}"
        common::load_runtime_env "${runtime_id}" "${arch}"
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

# common::soci_registries
#
# One registry per line: every configured registry that can carry a SOCI copy.
# Lambda reads SOCI indexes from Amazon ECR, so a copy anywhere else is bytes
# nothing consumes and is only published when SOCI_REGISTRIES names it.
common::soci_registries() {
    python3 - "${SOCI_REGISTRIES:-}" "${CONTAINER_REGISTRIES_OVERRIDE:-${CONTAINER_REGISTRIES:-}}" <<'PYEOF'
import json
import sys

explicit, configured = sys.argv[1], sys.argv[2]
candidates = json.loads(explicit) if explicit else json.loads(configured)
for registry in candidates:
    if explicit or registry.startswith("public.ecr.aws/") or ".dkr.ecr." in registry:
        print(registry)
PYEOF
}

# common::record_container_digest <field> <registry> <reference> <digest> <skipped>
#
# Record what a registry actually published in ${ARTIFACT_DIR}/container.json,
# keyed by registry under <field> (`registry_digests` for the image itself,
# `soci_digests` for its SOCI copy). The release reads one file for "what did we
# publish" instead of re-resolving mutable tags, and a consumer can check the digest
# the registry served against the record.
common::record_container_digest() {
    local field=$1 registry=$2 reference=$3 digest=$4 skipped=$5
    local record="${ARTIFACT_DIR}/container.json"
    if [[ ! -f "${record}" ]]; then
        echo "::notice::${record} is missing; the digest for ${reference} was not recorded" >&2
        return 0
    fi
    python3 - "${record}" "${field}" "${registry}" "${reference}" "${digest}" "${skipped}" <<'PYEOF'
import json
import sys

path, field, registry, reference, digest, skipped = sys.argv[1:7]

with open(path, encoding="utf-8") as handle:
    document = json.load(handle)
document.setdefault(field, {})[registry] = {
    "reference": reference,
    "digest": digest,
    "skipped": skipped == "true",
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(document, handle, indent=2, sort_keys=True)
    handle.write("\n")
PYEOF
}

# common::delete_referrer <repository> <digest>
#
# Remove one referrer manifest. `oras manifest delete` is the portable path, but
# Amazon ECR Public rejects a direct manifest DELETE on its OCI endpoint, so a
# `public.ecr.aws` repository falls back to the ECR Public API, which addresses the
# same manifest by digest. The repository name there is the part after the alias —
# the alias is registry-level, not part of the repository.
common::delete_referrer() {
    local repository=$1 digest=$2 name
    if oras manifest delete --force "${repository}@${digest}" >/dev/null 2>&1; then
        return 0
    fi
    case "${repository}" in
        public.ecr.aws/*)
            name=${repository#public.ecr.aws/}
            name=${name#*/}
            if type -P aws >/dev/null; then
                aws ecr-public batch-delete-image \
                    --region "${ECR_PUBLIC_REGION:-us-east-1}" \
                    --repository-name "${name}" \
                    --image-ids "imageDigest=${digest}" >/dev/null && return 0
            fi
            ;;
    esac
    return 1
}

# common::remote_manifest_digest <reference>
#
# The digest the registry currently serves for a reference, or an empty string when
# it serves nothing (404). `oras` is the reader push and attest already depend on; a
# fallback to `docker buildx imagetools inspect` keeps a local run working without it.
common::remote_manifest_digest() {
    local reference=$1
    if type -P oras >/dev/null; then
        oras manifest fetch --descriptor "${reference}" 2>/dev/null \
            | python3 -c 'import json, sys
try:
    print(json.load(sys.stdin).get("digest", ""))
except Exception:
    pass'
        return 0
    fi
    docker buildx imagetools inspect "${reference}" 2>/dev/null \
        | awk '/^Digest:/ { print $2; exit }'
}

# common::remote_config_digest <reference>; 2 means "not a manifest"
#
# The image *config* digest a manifest points at. `docker image inspect {{.Id}}`
# reports the same value, so this is what a push compares: manifest digests hash the
# manifest and would never match an image id.
common::remote_config_digest() {
    local reference=$1 document
    document=$(oras manifest fetch "${reference}" 2>/dev/null) || return 1
    printf '%s' "${document}" | python3 -c 'import json, sys
try:
    document = json.load(sys.stdin)
except Exception:
    sys.exit(1)
if document.get("manifests"):
    sys.exit(2)
print((document.get("config") or {}).get("digest", ""))'
}

# common::superseded_digest <image> <reference>
#
# The manifest digest a reference serves when it serves a *different* image than the
# one about to be pushed, else empty. That digest is what a push turns into an
# untagged manifest, and it is the value the provenance records and the pruner reads.
# A registry that cannot be read (an audit run does not log in) answers empty, so the
# lineage is recorded as unknown rather than guessed.
common::superseded_digest() {
    local image=$1 reference=$2 local_config remote_manifest remote_config
    local_config=$(docker image inspect --format '{{.Id}}' "${image}" 2>/dev/null) || return 0
    remote_manifest=$(common::remote_manifest_digest "${reference}")
    if [[ -z "${remote_manifest}" ]]; then
        return 0
    fi
    remote_config=$(common::remote_config_digest "${reference}" 2>/dev/null) || return 0
    if [[ -z "${remote_config}" || "${remote_config}" == "${local_config}" ]]; then
        return 0
    fi
    printf '%s' "${remote_manifest}"
}

# common::record_supersedes <registry> <reference> <digest>
#
# container.json carries the digest the reference served before this build replaced
# it: `supersedes_digest` is the single value the provenance quotes (null when nothing
# was replaced), and `supersedes_digests` keeps the per-registry detail so two
# registries that drifted are both visible.
common::record_supersedes() {
    local registry=$1 reference=$2 digest=$3
    local record="${ARTIFACT_DIR}/container.json"
    if [[ ! -f "${record}" ]]; then
        echo "::notice::${record} is missing; the superseded digest for ${reference} was not recorded" >&2
        return 0
    fi
    if [[ -z "${digest}" ]]; then
        echo "::notice::no superseded digest for ${reference}: the tag is new, or the registry serves these bytes" >&2
        return 0
    fi
    python3 - "${record}" "${registry}" "${reference}" "${digest}" <<'PYEOF'
import json
import sys

path, registry, reference, digest = sys.argv[1:5]

with open(path, encoding="utf-8") as handle:
    document = json.load(handle)
document.setdefault("supersedes_digests", {})[registry] = {
    "reference": reference,
    "digest": digest,
}
# The first registry to report one wins the scalar; the map keeps the rest.
document.setdefault("supersedes_digest", digest)
with open(path, "w", encoding="utf-8") as handle:
    json.dump(document, handle, indent=2, sort_keys=True)
    handle.write("\n")
PYEOF
}

# common::retry <attempts> <command...>
#
# Run a command until it succeeds, up to <attempts> times, with a growing delay
# between tries (RETRY_DELAY seconds times the attempt number, default 5).
# A registry can answer `not found` for a blob that a push moments earlier wrote:
# the tag resolves before every layer is readable, and the window is seconds.
common::retry() {
    local tries=$1
    shift
    local delay=${RETRY_DELAY:-5}
    local try=1
    while :; do
        if "$@"; then
            return 0
        fi
        if (( try >= tries )); then
            return 1
        fi
        echo "Attempt ${try}/${tries} failed; retrying in $(( delay * try ))s" >&2
        sleep $(( delay * try ))
        try=$(( try + 1 ))
    done
}
