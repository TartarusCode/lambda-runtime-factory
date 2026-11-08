"""Idempotency and caching contracts for the publish pipeline.

Every test runs the real script against stubbed registry clients, because the
property being tested is a *decision the script makes* — push or skip, convert or
reuse, attach or refresh — and reading the source for a string does not prove the
decision is reached. The stubs answer exactly what a registry answers: a comparable
config digest, a manifest with layers, a referrer list with digests.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOLS = _REPO_ROOT / "tools" / "bin"

SPDX_DOCUMENT = {
    "spdxVersion": "SPDX-2.3",
    "name": "pypy311",
    "packages": [
        {
            "name": "glibc",
            "externalRefs": [
                {"referenceType": "purl", "referenceLocator": "pkg:rpm/rhel/glibc@2.34-100"}
            ],
        },
        {
            "name": "zlib",
            "externalRefs": [
                {"referenceType": "purl", "referenceLocator": "pkg:rpm/rhel/zlib@1.2.11"}
            ],
        },
        {
            "name": "openssl-libs",
            "externalRefs": [
                {"referenceType": "purl", "referenceLocator": "pkg:rpm/rhel/openssl-libs@3.0.7"}
            ],
        },
        {
            "name": "ca-certificates",
            "externalRefs": [
                {
                    "referenceType": "purl",
                    "referenceLocator": "pkg:rpm/rhel/ca-certificates@2023.2.60",
                }
            ],
        },
        {
            "name": "python",
            "externalRefs": [
                {"referenceType": "purl", "referenceLocator": "pkg:generic/python@3.11.9"}
            ],
        },
    ],
}

CDX_DOCUMENT = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.5",
    "components": [{"name": "glibc"}, {"name": "python"}],
}


def _stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run(script: str, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_TOOLS / script), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=_REPO_ROOT,
    )


def _base_environment(tmp_path: Path, bin_dir: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        # The developer's own build root/cache must not decide the test.
        if key not in {"BUILD_ROOT", "SBOM_CACHE_DIR", "CONTAINER_REGISTRIES_OVERRIDE"}
    }
    environment["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    environment["RUNNER_TEMP"] = str(tmp_path / "runner")
    return environment


# --- Task 1: the digest-verified SBOM cache -------------------------------------


def _sbom_run(
    tmp_path: Path,
    *,
    image_digest: str = "sha256:" + "a" * 64,
    base_image_digest: str = "sha256:" + "b" * 64,
    syft_version: str = "1.57.0",
    cdx_components: bool = True,
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")

    build = tmp_path / "runner" / "lambda-runtime-monorepo" / "pypy311" / "x86_64"
    artifacts = build / "artifacts"
    (artifacts / "compliance").mkdir(parents=True, exist_ok=True)
    (build / "layer").mkdir(parents=True, exist_ok=True)
    (build / "layer" / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "runtime_id": "pypy311",
                "arch": "x86_64",
                "distribution_version": "pypy3.11-v8.0.1",
                "upstream_archive_url": "https://example.invalid/pypy.tar.bz2",
                "upstream_sha256": "c" * 64,
            }
        ),
        encoding="utf-8",
    )
    (artifacts / "container.json").write_text(
        json.dumps(
            {
                "runtime_id": "pypy311",
                "arch": "x86_64",
                "image_digest": image_digest,
                "base_image_ref": "registry1.dso.mil/ironbank/redhat/ubi/ubi9-minimal:latest",
                "base_image_digest": base_image_digest,
                "unpacked_size_bytes": 4096,
                "user": "1001:1001",
                "package_managers_removed": True,
                "rpm_db_present": True,
                "read_only_rootfs_verified": True,
            }
        ),
        encoding="utf-8",
    )

    cdx = dict(CDX_DOCUMENT)
    if not cdx_components:
        cdx = {**CDX_DOCUMENT, "components": []}

    _stub(
        bin_dir / "syft",
        f"""#!{os.path.realpath(__import__("sys").executable)}
import json
import pathlib
import sys

args = sys.argv[1:]
with open({str(calls)!r}, "a", encoding="utf-8") as handle:
    handle.write("SYFT " + " ".join(args) + "\\n")

if args and args[0] == "version":
    print(json.dumps({{"application": "syft", "version": {syft_version!r}}}))
    sys.exit(0)

targets = {{}}
for argument in args:
    for flag in ("spdx-json", "cyclonedx-json"):
        prefix = flag + "="
        if argument.startswith(prefix):
            targets[flag] = argument[len(prefix):]

# Both formats in one invocation: two passes would unpack the image twice, which
# is the thing the single-pass call exists to avoid.
if set(targets) != {{"spdx-json", "cyclonedx-json"}}:
    sys.exit("syft stub: the generator must request both formats in one pass")

pathlib.Path(targets["spdx-json"]).write_text(json.dumps({SPDX_DOCUMENT!r}), encoding="utf-8")
pathlib.Path(targets["cyclonedx-json"]).write_text(json.dumps({cdx!r}), encoding="utf-8")
""",
    )
    _stub(
        bin_dir / "docker",
        f"""#!{os.path.realpath(__import__("sys").executable)}
import sys

args = sys.argv[1:]
with open({str(calls)!r}, "a", encoding="utf-8") as handle:
    handle.write("DOCKER " + " ".join(args) + "\\n")

if args[:2] == ["image", "inspect"] and "{{.Id}}" in " ".join(args):
    print({image_digest!r})
sys.exit(0)
""",
    )
    _stub(
        bin_dir / "grype",
        f"""#!{os.path.realpath(__import__("sys").executable)}
import pathlib
import sys

with open({str(calls)!r}, "a", encoding="utf-8") as handle:
    handle.write("GRYPE " + " ".join(sys.argv[1:]) + "\\n")

for argument in sys.argv[1:]:
    if argument.startswith("json="):
        pathlib.Path(argument[len("json="):]).write_text('{{"matches": []}}', encoding="utf-8")
    elif argument.startswith("sarif="):
        pathlib.Path(argument[len("sarif="):]).write_text('{{"runs": []}}', encoding="utf-8")
sys.exit(0)
""",
    )

    result = _run("generate-compliance-artifacts", ["pypy311", "x86_64"], _base_environment(tmp_path, bin_dir))
    return result, artifacts / "compliance", calls


def test_a_fresh_scan_populates_the_cache_and_the_next_run_restores_it(tmp_path: Path) -> None:
    """The package list is the expensive part of the bundle and it only changes when
    the image changes. The first run scans once; the second run of the same digest
    restores the same documents and does not scan at all."""
    first, compliance, calls = _sbom_run(tmp_path)

    assert first.returncode == 0, first.stderr
    assert "cache miss" in first.stdout
    assert calls.read_text(encoding="utf-8").count("SYFT docker:") == 1

    cache = tmp_path / "runner" / "lambda-runtime-monorepo" / "sbom-cache" / "pypy311" / "x86_64"
    meta = json.loads((cache / "sbom-cache-meta.json").read_text(encoding="utf-8"))
    assert meta["image_digest"] == "sha256:" + "a" * 64
    assert meta["base_image_digest"] == "sha256:" + "b" * 64
    assert meta["syft_version"] == "1.57.0"
    assert meta["packages"] == {"total": 5, "rpm": 4, "runtime": 1}

    spdx_before = (compliance / "pypy311-x86_64.spdx.json").read_bytes()
    cdx_before = (compliance / "pypy311-x86_64.cdx.json").read_bytes()
    provenance_before = json.loads(
        (compliance / "pypy311-x86_64.provenance.json").read_text(encoding="utf-8")
    )
    assert provenance_before["sbom_cache_hit"] is False

    calls.write_text("", encoding="utf-8")
    second, compliance, calls = _sbom_run(tmp_path)

    assert second.returncode == 0, second.stderr
    assert "cache hit" in second.stdout
    assert "SYFT docker:" not in calls.read_text(encoding="utf-8"), "a hit must not rescan"
    # The scan is dynamic even on a hit: the CVE report is recomputed every time.
    assert "GRYPE " in calls.read_text(encoding="utf-8")

    # Byte-identical documents and a provenance that says where they came from.
    assert (compliance / "pypy311-x86_64.spdx.json").read_bytes() == spdx_before
    assert (compliance / "pypy311-x86_64.cdx.json").read_bytes() == cdx_before
    provenance = json.loads(
        (compliance / "pypy311-x86_64.provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["sbom_cache_hit"] is True
    assert provenance["sbom_sha256"]["spdx"] == provenance_before["sbom_sha256"]["spdx"]


def test_a_rebuilt_image_never_inherits_another_images_package_list(tmp_path: Path) -> None:
    """The cache key is not the answer: a rebuilt image can carry the same tag, the
    same runtime and the same Syft version. The recorded image digest is."""
    _sbom_run(tmp_path)
    result, compliance, calls = _sbom_run(tmp_path, image_digest="sha256:" + "d" * 64)

    assert result.returncode == 0, result.stderr
    assert "cache miss: image digest" in result.stdout
    assert calls.read_text(encoding="utf-8").count("SYFT docker:") == 1
    provenance = json.loads(
        (compliance / "pypy311-x86_64.provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["sbom_cache_hit"] is False


def test_a_new_base_image_digest_invalidates_the_cache(tmp_path: Path) -> None:
    _sbom_run(tmp_path)
    result, _compliance, calls = _sbom_run(tmp_path, base_image_digest="sha256:" + "e" * 64)

    assert result.returncode == 0, result.stderr
    assert "cache miss: base digest" in result.stdout
    assert calls.read_text(encoding="utf-8").count("SYFT docker:") == 1


def test_a_syft_upgrade_invalidates_the_cache(tmp_path: Path) -> None:
    """A newer Syft recognises packages the old one missed, so its output cannot be
    served from an entry an older Syft wrote."""
    _sbom_run(tmp_path)
    result, _compliance, calls = _sbom_run(tmp_path, syft_version="1.58.0")

    assert result.returncode == 0, result.stderr
    assert "cache miss: syft 1.57.0 -> 1.58.0" in result.stdout
    assert calls.read_text(encoding="utf-8").count("SYFT docker:") == 1


def test_a_document_that_lost_its_components_is_refused(tmp_path: Path) -> None:
    """The CycloneDX document is attached to the digest as evidence, so an empty one
    fails the same way an empty SPDX does — and nothing is cached."""
    result, _compliance, _calls = _sbom_run(tmp_path, cdx_components=False)

    assert result.returncode != 0
    assert "no components" in result.stderr
    cache = tmp_path / "runner" / "lambda-runtime-monorepo" / "sbom-cache"
    assert not (cache / "pypy311" / "x86_64" / "sbom-cache-meta.json").exists()


def test_the_cache_directory_is_offered_by_the_manifest_and_honours_the_override() -> None:
    script = _TOOLS.parent / "runtime_lib" / "runtime_manifest.py"
    text = script.read_text(encoding="utf-8")
    assert 'os.environ.get("SBOM_CACHE_DIR") or str(temp_root / "sbom-cache")' in text

    environment = dict(os.environ)
    environment["SBOM_CACHE_DIR"] = "/tmp/explicit-sbom-cache"
    result = subprocess.run(
        [
            "python3",
            str(script),
            "env",
            "--runtime",
            "pypy311",
            "--arch",
            "x86_64",
        ],
        capture_output=True,
        text=True,
        env=environment,
        cwd=_REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "SBOM_CACHE_DIR=/tmp/explicit-sbom-cache" in result.stdout


def test_the_build_environment_caches_the_sbom_directory_per_runtime_and_arch() -> None:
    """The `sbom-cache-meta.json` check is what makes a shared cache safe, so the
    workflow key only has to be good enough to offer the right cell its own entry."""
    action = yaml.safe_load(
        (_REPO_ROOT / ".github/actions/runtime-build-env/action.yml").read_text(encoding="utf-8")
    )
    steps = action["runs"]["steps"]
    prepare = next(step for step in steps if step.get("name") == "Prepare cache directories")
    assert "SBOM_CACHE_DIR=" in prepare["run"]

    cache_step = next(step for step in steps if step.get("name") == "Cache SBOMs")
    with_ = cache_step["with"]
    assert with_["path"] == "${{ env.SBOM_CACHE_DIR }}"
    assert with_["key"].startswith("sbom-${{ inputs.runtime }}-${{ inputs.arch }}-${{ env.SYFT_VERSION }}-")
    assert "tools/bin/generate-compliance-artifacts" in with_["key"]
    assert with_["restore-keys"].strip() == (
        "sbom-${{ inputs.runtime }}-${{ inputs.arch }}-${{ env.SYFT_VERSION }}-"
    )
    assert cache_step["if"] == "inputs.runtime != '' && inputs.arch != ''"


# --- Task 2a: push-container -----------------------------------------------------

IMAGE_DIGEST = "sha256:" + "a" * 64
PUBLISHED_DIGEST = "sha256:" + "9" * 64


def _push_run(
    tmp_path: Path, *, remote_config: str | None, local_digest: str = IMAGE_DIGEST
) -> tuple[subprocess.CompletedProcess, str, dict]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")

    artifacts = tmp_path / "runner" / "lambda-runtime-monorepo" / "pypy311" / "x86_64" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    record = artifacts / "container.json"
    record.write_text(json.dumps({"image_digest": local_digest}), encoding="utf-8")

    _stub(
        bin_dir / "docker",
        f"""#!{os.path.realpath(__import__("sys").executable)}
import sys

args = sys.argv[1:]
with open({str(calls)!r}, "a", encoding="utf-8") as handle:
    handle.write("DOCKER " + " ".join(args) + "\\n")

if args[:2] == ["image", "inspect"]:
    template = args[args.index("--format") + 1] if "--format" in args else ""
    if "{{.Id}}" in template:
        print({local_digest!r})
    elif "RepoDigests" in template:
        print("registry.example/project/pypy311@" + {PUBLISHED_DIGEST!r})
sys.exit(0)
""",
    )
    _stub(
        bin_dir / "oras",
        f"""#!{os.path.realpath(__import__("sys").executable)}
import json
import sys

args = sys.argv[1:]
with open({str(calls)!r}, "a", encoding="utf-8") as handle:
    handle.write("ORAS " + " ".join(args) + "\\n")

if args[:2] == ["manifest", "fetch"]:
    if "--descriptor" in args:
        print(json.dumps({{"digest": {PUBLISHED_DIGEST!r}}}))
        sys.exit(0)
    if {remote_config!r}:
        print(json.dumps({{"config": {{"digest": {remote_config!r}}}, "layers": []}}))
        sys.exit(0)
    sys.exit(1)
sys.exit(1)
""",
    )

    environment = _base_environment(tmp_path, bin_dir)
    environment["CONTAINER_REGISTRIES_OVERRIDE"] = '["registry.example/project"]'
    result = _run("push-container", ["pypy311", "x86_64"], environment)
    return result, calls.read_text(encoding="utf-8"), json.loads(record.read_text(encoding="utf-8"))


def test_a_push_whose_bytes_the_registry_already_serves_is_skipped(tmp_path: Path) -> None:
    """Re-releasing unchanged bytes used to re-push them, which repoints the tag,
    which forces every downstream signature and attachment to be redone."""
    result, calls, record = _push_run(tmp_path, remote_config=IMAGE_DIGEST)

    assert result.returncode == 0, result.stderr
    assert "Skipping push" in result.stdout
    assert "DOCKER push" not in calls
    recorded = record["registry_digests"]["registry.example/project"]
    assert recorded["digest"] == PUBLISHED_DIGEST
    assert recorded["skipped"] is True


def test_a_registry_serving_different_bytes_gets_the_push(tmp_path: Path) -> None:
    result, calls, record = _push_run(tmp_path, remote_config="sha256:" + "f" * 64)

    assert result.returncode == 0, result.stderr
    assert "DOCKER push" in calls
    assert "Skipping push" not in result.stdout
    recorded = record["registry_digests"]["registry.example/project"]
    assert recorded["digest"] == PUBLISHED_DIGEST
    assert recorded["skipped"] is False


def test_a_missing_tag_is_pushed_rather_than_reported_as_unchanged(tmp_path: Path) -> None:
    result, calls, record = _push_run(tmp_path, remote_config=None)

    assert result.returncode == 0, result.stderr
    assert "DOCKER push" in calls
    assert record["registry_digests"]["registry.example/project"]["digest"] == PUBLISHED_DIGEST


# --- Task 2b: publish-soci-index -------------------------------------------------

SOCI_DIGEST = "sha256:" + "7" * 64
SOCI_SUBJECT = "sha256:" + "8" * 64
IMAGE_MANIFEST_DIGEST = "sha256:" + "3" * 64
LAYERS = ["sha256:" + "1" * 64, "sha256:" + "2" * 64]


def _soci_index_document() -> dict:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": SOCI_SUBJECT,
                "size": 1779,
                "annotations": {"com.amazon.soci.index-digest": SOCI_DIGEST},
                "platform": {"architecture": "amd64", "os": "linux"},
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": SOCI_DIGEST,
                "size": 1491,
                "annotations": {"com.amazon.soci.image-manifest-digest": SOCI_SUBJECT},
                "platform": {"architecture": "amd64", "os": "linux"},
                "artifactType": "application/vnd.amazon.soci.index.v2+json",
            },
        ],
    }


def _soci_run(tmp_path: Path, *, same_layers: bool, soci_exists: bool = True):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")

    artifacts = tmp_path / "runner" / "lambda-runtime-monorepo" / "pypy311" / "x86_64" / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    record = artifacts / "container.json"
    record.write_text(json.dumps({"image_digest": IMAGE_DIGEST}), encoding="utf-8")

    soci_layers = LAYERS if same_layers else ["sha256:" + "4" * 64, "sha256:" + "5" * 64]

    _stub(
        bin_dir / "oras",
        f"""#!{os.path.realpath(__import__("sys").executable)}
import json
import pathlib
import sys

args = sys.argv[1:]
with open({str(calls)!r}, "a", encoding="utf-8") as handle:
    handle.write("ORAS " + " ".join(args) + "\\n")

soci_exists = {soci_exists!r}

if args[:3] == ["manifest", "fetch", "--descriptor"]:
    reference = args[3]
    if reference.endswith("-soci"):
        if not soci_exists:
            sys.exit(1)
        print(json.dumps({{"digest": {SOCI_DIGEST!r}}}))
    else:
        print(json.dumps({{"digest": {IMAGE_MANIFEST_DIGEST!r}}}))
    sys.exit(0)

if args[:2] == ["manifest", "fetch"]:
    reference = args[-1]
    if reference.endswith("-soci"):
        print(json.dumps({_soci_index_document()!r}))
    elif {SOCI_SUBJECT!r} in reference:
        print(json.dumps({{"layers": [{{"digest": d}} for d in {soci_layers!r}]}}))
    else:
        print(json.dumps({{"layers": [{{"digest": d}} for d in {LAYERS!r}]}}))
    sys.exit(0)

if args and args[0] == "cp":
    if "--to-oci-layout" in args:
        layout = args[args.index("--to-oci-layout") + 1].split(":")[0]
        pathlib.Path(layout).mkdir(parents=True, exist_ok=True)
    sys.exit(0)

sys.exit(0)
""",
    )
    _stub(
        bin_dir / "soci",
        f"""#!{os.path.realpath(__import__("sys").executable)}
import json
import pathlib
import sys

with open({str(calls)!r}, "a", encoding="utf-8") as handle:
    handle.write("SOCI " + " ".join(sys.argv[1:]) + "\\n")

out = pathlib.Path(sys.argv[-1])
out.mkdir(parents=True, exist_ok=True)
(out / "index.json").write_text(
    json.dumps({{"manifests": [{{"digest": {SOCI_SUBJECT!r}}}]}}), encoding="utf-8"
)
sys.exit(0)
""",
    )

    environment = _base_environment(tmp_path, bin_dir)
    environment.update(
        {
            "SOCI_BIN": str(bin_dir / "soci"),
            "ORAS_BIN": str(bin_dir / "oras"),
            "CONTAINER_REGISTRIES_OVERRIDE": '["public.ecr.aws/alias/project"]',
        }
    )
    result = _run("publish-soci-index", ["pypy311", "x86_64", "--push"], environment)
    return result, calls.read_text(encoding="utf-8"), json.loads(record.read_text(encoding="utf-8"))


def test_an_unchanged_image_reuses_the_soci_index_instead_of_minting_a_new_digest(
    tmp_path: Path,
) -> None:
    """A new SOCI index is a new digest, a new signature and a new set of referrers
    for an image Lambda would lazy-load identically. The layers are the thing that
    decides it: `soci convert` rewrites the image config, so the manifest digest can
    never match, but identical layers mean identical lazy-loadable bytes."""
    result, calls, record = _soci_run(tmp_path, same_layers=True)

    assert result.returncode == 0, result.stderr
    assert "Reusing" in result.stdout
    assert "SOCI " not in calls, "a reusable copy must not be converted"
    assert "ORAS cp " not in calls, "a reusable copy must not be pushed"
    recorded = record["soci_digests"]["public.ecr.aws/alias/project"]
    assert recorded["digest"] == SOCI_DIGEST
    assert recorded["skipped"] is True


def test_different_layers_rebuild_the_soci_index(tmp_path: Path) -> None:
    """The reuse check must not be a rubber stamp: an image whose layers changed
    gets a new index, because the old one indexes bytes that are no longer served."""
    result, calls, record = _soci_run(tmp_path, same_layers=False)

    assert result.returncode == 0, result.stderr
    assert "Reusing" not in result.stdout
    assert "SOCI " in calls
    assert "ORAS cp " in calls
    recorded = record["soci_digests"]["public.ecr.aws/alias/project"]
    assert recorded["digest"] == SOCI_DIGEST
    assert recorded["skipped"] is False


def test_a_missing_soci_tag_is_built(tmp_path: Path) -> None:
    result, calls, _record = _soci_run(tmp_path, same_layers=True, soci_exists=False)

    assert result.returncode == 0, result.stderr
    assert "Reusing" not in result.stdout
    assert "SOCI " in calls


# --- Task 2c: attest-container --refresh-dynamic ---------------------------------

SIGNATURE = "application/vnd.dev.sigstore.bundle.v0.3+json"
GRYPE = "application/vnd.grype.report.v1+json"
PROVENANCE = "application/vnd.lambda-runtime.provenance.v1+json"


def _attest_run(
    tmp_path: Path,
    referrers: list[dict],
    *extra: str,
    identical_scan: bool = False,
) -> tuple[str, list[str]]:
    artifacts = tmp_path / "runner" / "lambda-runtime-monorepo" / "pypy311" / "x86_64" / "artifacts"
    compliance = artifacts / "compliance"
    compliance.mkdir(parents=True, exist_ok=True)
    for name in ("spdx", "cdx", "grype", "provenance"):
        (compliance / f"pypy311-x86_64.{name}.json").write_text("{}", encoding="utf-8")

    document = tmp_path / "referrers.json"
    document.write_text(json.dumps({"referrers": referrers}), encoding="utf-8")
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    _stub(
        bin_dir / "oras",
        f"""#!{os.path.realpath(__import__("sys").executable)}
import json
import pathlib
import sys

args = sys.argv[1:]
with open({str(calls)!r}, "a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")

path = pathlib.Path({str(document)!r})

if args and args[0] == "discover":
    print(path.read_text(encoding="utf-8"))
    sys.exit(0)

if args[:2] == ["manifest", "fetch"]:
    print('{{"digest": "sha256:deadbeef"}}')
    sys.exit(0)

# A real attach adds a referrer, unless the bytes are identical — then the
# registry already holds that manifest and the list does not grow.
if args and args[0] == "attach" and not {identical_scan!r}:
    media = args[args.index("--artifact-type") + 1]
    document = json.loads(path.read_text(encoding="utf-8"))
    document["referrers"].append(
        {{
            "artifactType": media,
            "digest": "sha256:fresh-" + media.rsplit("/", 1)[-1].split("+")[0],
            "annotations": {{"org.opencontainers.image.created": "2026-11-09T00:00:00Z"}},
        }}
    )
    path.write_text(json.dumps(document), encoding="utf-8")
sys.exit(0)
""",
    )
    _stub(bin_dir / "cosign", f"#!{os.path.realpath(__import__('sys').executable)}\nimport sys\nprint('SIGN')\nsys.exit(0)\n")

    environment = _base_environment(tmp_path, bin_dir)
    environment.update(
        {
            "SIGSTORE_ID_TOKEN": "stub",
            "CONTAINER_REGISTRIES_OVERRIDE": '["public.ecr.aws/alias/project"]',
        }
    )
    result = _run("attest-container", ["pypy311", "x86_64", *extra], environment)
    assert result.returncode == 0, result.stderr
    return result.stdout, calls.read_text(encoding="utf-8").splitlines()


def test_refresh_dynamic_replaces_the_scan_and_prunes_what_it_supersedes(tmp_path: Path) -> None:
    """An operator re-scanning an unchanged image wants the fresh CVE report on the
    digest — with one of each artifact, not a growing pile of scans."""
    referrers = [
        {"artifactType": SIGNATURE, "digest": "sha256:sig"},
        {"artifactType": "application/spdx+json", "digest": "sha256:spdx"},
        {"artifactType": "application/vnd.cyclonedx+json", "digest": "sha256:cdx"},
        {
            "artifactType": GRYPE,
            "digest": "sha256:oldgrype",
            "annotations": {"org.opencontainers.image.created": "2026-11-01T00:00:00Z"},
        },
        {
            "artifactType": PROVENANCE,
            "digest": "sha256:oldprov",
            "annotations": {"org.opencontainers.image.created": "2026-11-01T00:00:00Z"},
        },
    ]
    stdout, calls = _attest_run(tmp_path, referrers, "--refresh-dynamic")

    attached = [
        call.split()[-1].split(":", 1)[1]
        for call in calls
        if call.startswith("attach")
    ]
    assert sorted(attached) == sorted([GRYPE, PROVENANCE]), "only the two scan artifacts are rewritten"

    deleted = [call for call in calls if call.startswith("manifest delete")]
    assert len(deleted) == 2, "each fresh scan replaces exactly one older referrer"
    assert any("sha256:oldgrype" in call for call in deleted)
    assert any("sha256:oldprov" in call for call in deleted)
    assert "Pruned superseded" in stdout


def test_without_the_flag_a_dynamic_referrer_is_left_alone(tmp_path: Path) -> None:
    """Default idempotency: a re-release over the same digest writes nothing, even
    though the CVE report on disk is freshly generated."""
    referrers = [
        {"artifactType": SIGNATURE, "digest": "sha256:sig"},
        {"artifactType": "application/spdx+json", "digest": "sha256:spdx"},
        {"artifactType": "application/vnd.cyclonedx+json", "digest": "sha256:cdx"},
        {"artifactType": GRYPE, "digest": "sha256:grype"},
        {"artifactType": PROVENANCE, "digest": "sha256:prov"},
    ]
    _stdout, calls = _attest_run(tmp_path, referrers)

    assert not [call for call in calls if call.startswith("attach")]
    assert not [call for call in calls if call.startswith("manifest delete")]


def test_a_refresh_whose_scan_is_unchanged_does_not_delete_the_live_referrer(
    tmp_path: Path,
) -> None:
    """Attaching identical bytes yields the same referrer digest, so "keep the newest"
    must resolve to the one referrer that is there — deleting it would leave the
    digest with no CVE report at all."""
    referrers = [
        {"artifactType": SIGNATURE, "digest": "sha256:sig"},
        {"artifactType": "application/spdx+json", "digest": "sha256:spdx"},
        {"artifactType": "application/vnd.cyclonedx+json", "digest": "sha256:cdx"},
        {
            "artifactType": GRYPE,
            "digest": "sha256:onlygrype",
            "annotations": {"org.opencontainers.image.created": "2026-11-01T00:00:00Z"},
        },
        {
            "artifactType": PROVENANCE,
            "digest": "sha256:onlyprov",
            "annotations": {"org.opencontainers.image.created": "2026-11-01T00:00:00Z"},
        },
    ]
    _stdout, calls = _attest_run(tmp_path, referrers, "--refresh-dynamic", identical_scan=True)

    assert not [call for call in calls if call.startswith("manifest delete")]
    assert [call for call in calls if call.startswith("attach")], "the refresh still attaches"


# --- Task 5a: ECR Public deletion fallback --------------------------------------


def test_ecr_public_deletion_falls_back_to_the_api(tmp_path: Path) -> None:
    """ECR Public refuses a manifest DELETE on its OCI endpoint, so the referrer has
    to be removed through the API — addressed by digest, with the repository name
    taken from after the alias."""
    referrers = tmp_path / "referrers.json"
    referrers.write_text(
        json.dumps(
            {
                "referrers": [
                    {
                        "artifactType": "application/spdx+json",
                        "digest": "sha256:keep",
                        "annotations": {"org.opencontainers.image.created": "2026-11-01T00:00:00Z"},
                    },
                    {
                        "artifactType": "application/spdx+json",
                        "digest": "sha256:dupe",
                        "annotations": {"org.opencontainers.image.created": "2026-11-02T00:00:00Z"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")

    _stub(
        bin_dir / "oras",
        f"""#!{os.path.realpath(__import__("sys").executable)}
import pathlib
import sys

args = sys.argv[1:]
with open({str(calls)!r}, "a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")

if args and args[0] == "discover":
    print(pathlib.Path({str(referrers)!r}).read_text(encoding="utf-8"))
    sys.exit(0)
if args[:2] == ["manifest", "fetch"]:
    print('{{"digest": "sha256:subject"}}')
    sys.exit(0)
if args[:3] == ["manifest", "delete", "--force"]:
    sys.exit(1)
sys.exit(0)
""",
    )
    _stub(
        bin_dir / "aws",
        f"""#!{os.path.realpath(__import__("sys").executable)}
import sys

with open({str(calls)!r}, "a", encoding="utf-8") as handle:
    handle.write("AWS " + " ".join(sys.argv[1:]) + "\\n")
sys.exit(0)
""",
    )

    environment = _base_environment(tmp_path, bin_dir)
    environment["CONTAINER_REGISTRIES_OVERRIDE"] = '["public.ecr.aws/alias/project"]'
    result = _run("prune-duplicate-referrers", ["pypy311", "x86_64", "--delete"], environment)

    assert result.returncode == 0, result.stderr
    assert "removed sha256:dupe" in result.stdout
    aws_call = next(line for line in calls.read_text(encoding="utf-8").splitlines() if line.startswith("AWS"))
    assert "batch-delete-image" in aws_call
    assert "--repository-name project" in aws_call, "the alias is not part of the repository name"
    assert "imageDigest=sha256:dupe" in aws_call


# --- Task 3: Iron Bank credentials in every container-building workflow ----------


def test_every_container_workflow_logs_into_iron_bank_with_repository_secrets() -> None:
    """A missing credential silently downgrades the base image to the Red Hat
    mirror, so each workflow that builds a container has to wire both halves."""
    workflows = {
        name: yaml.safe_load(
            (_REPO_ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        )
        for name in ("ci.yml", "audit-runtimes.yml", "release-runtime.yml")
    }

    jobs = {
        # name: (job, the step that builds the image, which the login must precede)
        "ci.yml": ("runtime-checks", "Build and audit runtime package"),
        "audit-runtimes.yml": ("audit", "Build and audit runtime package"),
        "release-runtime.yml": ("publish-containers", "Build the container image"),
    }
    for name, (job_name, build_step_name) in jobs.items():
        job = workflows[name]["jobs"][job_name]
        assert job["env"]["IRONBANK_TOKEN"] == "${{ secrets.IRONBANK_TOKEN }}", name

        login = next(step for step in job["steps"] if step.get("name") == "Log in to Iron Bank Registry")
        assert login["env"]["IRONBANK_USERNAME"] == "${{ secrets.IRONBANK_USERNAME }}", name
        assert "docker login registry1.dso.mil" in login["run"]
        assert login["if"] == "${{ env.IRONBANK_TOKEN != '' }}", (
            f"{name} must skip the login when a fork has no secrets"
        )

        build_at = next(
            index
            for index, step in enumerate(job["steps"])
            if step.get("name") == build_step_name
        )
        assert job["steps"].index(login) < build_at, f"{name} logs in before it builds"


# --- Task 4: pinned retries and the tag-release SOCI default ---------------------


# --- the verification sweep ------------------------------------------------------


def _verify_run(tmp_path: Path, layout: dict[str, list[str]]) -> subprocess.CompletedProcess:
    """Run verify-referrers against a stubbed registry.

    `layout` maps a reference to the artifact types published on it, so a test says
    what the registry holds and the tool decides whether that is complete.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fixture = tmp_path / "layout.json"
    fixture.write_text(json.dumps(layout), encoding="utf-8")

    _stub(
        bin_dir / "oras",
        f"""#!{os.path.realpath(__import__("sys").executable)}
import json
import sys

args = sys.argv[1:]
if args and args[0] == "discover":
    layout = json.load(open({str(fixture)!r}, encoding="utf-8"))
    types = layout.get(args[-1])
    if types is None:
        sys.exit(1)
    print(json.dumps({{"referrers": [{{"artifactType": kind}} for kind in types]}}))
    sys.exit(0)
sys.exit(1)
""",
    )
    environment = _base_environment(tmp_path, bin_dir)
    return _run(
        "verify-referrers",
        ["--registry", "registry.example/project", "--runtime", "pypy311", "--arch", "x86_64"],
        environment,
    )


def test_the_verification_sweep_accepts_a_complete_digest(tmp_path: Path) -> None:
    """5 referrers on each per-arch digest, 1 signature on each index — that is the
    whole contract, and the tool has to be able to say so."""
    bundle = [
        "application/spdx+json",
        "application/vnd.cyclonedx+json",
        GRYPE,
        PROVENANCE,
        SIGNATURE,
    ]
    result = _verify_run(
        tmp_path,
        {
            "registry.example/project/pypy311:pypy3.11-v8.0.1-amd64": bundle,
            "registry.example/project/pypy311:pypy3.11-v8.0.1-amd64-soci": bundle,
            "registry.example/project/pypy311:pypy3.11-v8.0.1": [SIGNATURE],
            "registry.example/project/pypy311:pypy3.11-v8.0.1-soci": [SIGNATURE],
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no duplicates" in result.stdout


def test_the_verification_sweep_rejects_a_duplicate_referrer(tmp_path: Path) -> None:
    """Ten referrers where five are expected is the symptom of a non-idempotent
    re-release, and the sweep is what has to catch it."""
    doubled = [
        "application/spdx+json",
        "application/spdx+json",
        "application/vnd.cyclonedx+json",
        GRYPE,
        PROVENANCE,
        SIGNATURE,
    ]
    result = _verify_run(
        tmp_path,
        {
            "registry.example/project/pypy311:pypy3.11-v8.0.1-amd64": doubled,
            "registry.example/project/pypy311:pypy3.11-v8.0.1-amd64-soci": doubled,
            "registry.example/project/pypy311:pypy3.11-v8.0.1": [SIGNATURE],
            "registry.example/project/pypy311:pypy3.11-v8.0.1-soci": [SIGNATURE],
        },
    )

    assert result.returncode == 1
    assert "duplicate" in result.stdout


def test_the_verification_sweep_rejects_an_unattested_soci_copy(tmp_path: Path) -> None:
    bundle = [
        "application/spdx+json",
        "application/vnd.cyclonedx+json",
        GRYPE,
        PROVENANCE,
        SIGNATURE,
    ]
    result = _verify_run(
        tmp_path,
        {
            "registry.example/project/pypy311:pypy3.11-v8.0.1-amd64": bundle,
            "registry.example/project/pypy311:pypy3.11-v8.0.1-amd64-soci": [],
            "registry.example/project/pypy311:pypy3.11-v8.0.1": [SIGNATURE],
            "registry.example/project/pypy311:pypy3.11-v8.0.1-soci": [SIGNATURE],
        },
    )

    assert result.returncode == 1
    assert "soci=0 expected 5" in result.stdout


def test_every_tool_download_retries() -> None:
    """A single blip on the release asset CDN used to fail a whole matrix cell."""
    action = yaml.safe_load(
        (_REPO_ROOT / ".github/actions/runtime-build-env/action.yml").read_text(encoding="utf-8")
    )
    runs = "\n".join(
        step["run"] for step in action["runs"]["steps"] if isinstance(step.get("run"), str)
    )
    joined = runs.replace("\\\n", " ")
    curl_lines = [line.strip() for line in joined.splitlines() if line.strip().startswith("curl")]
    assert curl_lines, "the action installs Grype with curl"
    for line in curl_lines:
        for flag in (
            "--retry 5",
            "--retry-all-errors",
            "--retry-delay 3",
            "--retry-connrefused",
            "--connect-timeout 30",
        ):
            assert flag in line, f"{flag} missing from: {line}"


def test_a_release_tag_publishes_soci_copies_by_default() -> None:
    """A tag names a runtime Lambda Managed Instances may lazy-load; leaving the SOCI
    copies out would publish a runtime a function cannot load."""
    workflow = yaml.safe_load(
        (_REPO_ROOT / ".github/workflows/release-runtime.yml").read_text(encoding="utf-8")
    )
    # PyYAML reads the bare `on:` key as True.
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["workflow_dispatch"]["inputs"]["publish_soci"]["default"] is True
    assert workflow["env"]["PUBLISH_SOCI"] == "${{ github.event_name == 'push' || inputs.publish_soci }}"
