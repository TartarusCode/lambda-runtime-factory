"""The Dockerfile's instruction order is a cache contract.

Every runtime shares one OS layer — the same `ubi9-minimal` pull and the same
`microdnf install` — so the twelve cells on an architecture do not each pay for it.
That only holds while nothing runtime-specific appears above it: a `LABEL` carrying
the version, an `ARG RUNTIME_ID`, or a `COPY` of the payload changes the cache key
and silently rebuilds the expensive layer twelve times.

These tests pin the order, the floor and the cache scopes, because the failure is
invisible in a green build — it only shows up as build minutes.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from runtime_manifest import REPO_DEFAULTS
from container_image import RUNTIME_LIBRARY_FLOOR

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCKERFILE = _REPO_ROOT / "tools" / "docker" / "Dockerfile"
_BUILD_CONTAINER = _REPO_ROOT / "tools" / "bin" / "build-container"


def _dockerfile_lines() -> list[str]:
    return _DOCKERFILE.read_text(encoding="utf-8").splitlines()


def _index_of(needle: str) -> int:
    for index, line in enumerate(_dockerfile_lines()):
        if needle in line:
            return index
    raise AssertionError(f"tools/docker/Dockerfile has no line containing {needle!r}")


def test_dockerfile_keeps_the_shared_os_layer_above_every_runtime_specific_instruction() -> None:
    floor_index = _index_of("RUN microdnf install")

    for runtime_specific in (
        "@@LABELS@@",
        "@@ENV@@",
        "layer/ @@LAYER_ROOT@@/",
    ):
        assert _index_of(runtime_specific) > floor_index, (
            f"{runtime_specific} must come after the shared microdnf layer, or BuildKit "
            "rebuilds that layer for every runtime"
        )

    for forbidden in ("ARG RUNTIME_ID", "ARG DISTRIBUTION_VERSION", "ARG RUNTIME_FAMILY"):
        assert forbidden not in _dockerfile_lines() or _index_of(forbidden) > floor_index


def test_nothing_above_the_shared_layer_is_runtime_specific() -> None:
    """The region above `RUN microdnf install` must be identical for all twelve
    runtimes, so it may contain no substitution placeholder at all — the renderer
    substitutes those from the manifest — and no label."""
    floor_index = _index_of("RUN microdnf install")
    above = "\n".join(_dockerfile_lines()[:floor_index])

    placeholders = sorted(set(re.findall(r"@@[A-Z_]+@@", above)))
    assert not placeholders, (
        f"these placeholders render per runtime and sit above the shared layer: {placeholders}"
    )
    assert not re.search(r"^LABEL ", above, flags=re.M), (
        "a LABEL above the shared layer changes the cache key for every runtime"
    )


def test_the_library_floor_matches_the_dockerfile_and_the_manifest() -> None:
    floor_index = _index_of("RUN microdnf install")
    install_block = []
    for line in _dockerfile_lines()[floor_index:]:
        install_block.append(line)
        if "rm -rf /var/cache/dnf" in line:
            break
    install_text = "\n".join(install_block)

    for package in RUNTIME_LIBRARY_FLOOR:
        assert package in install_text, (
            f"{package} is in RUNTIME_LIBRARY_FLOOR but the Dockerfile does not install it"
        )

    declared = set(REPO_DEFAULTS["container"]["packages"])
    assert declared == set(RUNTIME_LIBRARY_FLOOR), (
        "the manifest floor and container_image.RUNTIME_LIBRARY_FLOOR must agree, or "
        "`prepare` rejects a payload the image would have built fine"
    )


def test_build_container_publishes_the_shared_base_cache_scope() -> None:
    script = _BUILD_CONTAINER.read_text(encoding="utf-8")

    assert "type=gha,scope=ubi9-base-${CONTAINER_ARCH}" in script, (
        "the base scope is what lets the twelve cells reuse one OS layer"
    )
    assert "type=gha,scope=runtime-${runtime_id}-${CONTAINER_ARCH}" in script
    assert "mode=max" in script, "mode=min would drop the intermediate base layers"
    # The daemon copy is what `docker image inspect`, syft and the smoke test read.
    assert "--load" in script
    assert "CONTAINER_BUILD_CACHE" in script
    assert 'GITHUB_ACTIONS:-}' in script, "the gha backend must be the CI default"


def test_the_actions_do_not_hand_build_container_a_registry_cache_ref() -> None:
    """Two cache backends at once means the registry ref is pulled for nothing."""
    action = yaml.safe_load(
        (_REPO_ROOT / ".github" / "actions" / "build-and-audit-runtime" / "action.yml").read_text(
            encoding="utf-8"
        )
    )
    build = next(s for s in action["runs"]["steps"] if s.get("name") == "Build the container image")

    assert build["env"]["CONTAINER_BUILD_CACHE"] == "gha"
    assert "cache-from" not in build["run"], "the script owns the cache args now"
    assert "cache-to" not in build["run"]
