"""pytest fixtures and sys.path setup for runtime_lib tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_RUNTIME_LIB_DIR = _THIS_DIR.parent

if str(_RUNTIME_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_LIB_DIR))


# A runtime table shaped like the README's, with a row for each family the
# new-line tests clone from.
_README_FIXTURE = """\
# Runtime factory

## Runtimes

| Runtime id | Runtime | Line | Notes |
| --- | --- | --- | --- |
| `bun13` | Bun 1.3 | 1.3 | Bun bootstraps the Runtime API directly |
| `pypy311` | PyPy 3.11 | 3.11 | Portable PyPy build from python.org |

Ids follow the framework and version line.
"""


@pytest.fixture(autouse=True)
def isolated_readme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the README writer at a scratch file.

    `add_runtime_line` records the new line in the README's runtime table, so a test
    that creates one would otherwise edit the checkout's own README.
    """
    import bump_version

    path = tmp_path / "README.md"
    path.write_text(_README_FIXTURE, encoding="utf-8")
    monkeypatch.setattr(bump_version, "readme_path", lambda: path)
    return path


@pytest.fixture()
def bun_releases() -> list[dict[str, object]]:
    """Fake GitHub releases API response for oven-sh/bun (releases list)."""
    return [
        {"tag_name": "bun-v1.4.0"},
        {"tag_name": "bun-v1.3.14"},
        {"tag_name": "bun-v1.3.13"},
        {"tag_name": "bun-v1.2.5"},
    ]


@pytest.fixture()
def deno_releases() -> list[dict[str, object]]:
    """Fake GitHub releases API response for denoland/deno (releases list)."""
    return [
        {"tag_name": "v2.9.6"},
        {"tag_name": "v2.9.0"},
        {"tag_name": "v2.8.2"},
        {"tag_name": "v1.48.0"},
    ]


@pytest.fixture()
def rust_stable_channel() -> str:
    """Fake channel-rust-stable.toml with a newer minor line present."""
    return "\n".join(
        [
            "[pkg.rust]",
            'version = "1.98.0 (2026-08-20)"',
            "",
            "[pkg.rust.x86_64-unknown-linux-musl]",
            "version = 1.98.0",
            "",
            "[pkg.rust.aarch64-unknown-linux-musl]",
            "version = 1.98.0",
        ]
    )


@pytest.fixture()
def graalpy_releases() -> list[dict[str, object]]:
    """Fake GitHub releases API response for oracle/graalpython."""
    return [
        {
            "tag_name": "graal-25.3.4.1",
            "assets": [
                {
                    "name": "graalpy3.13-25.3.4.1-linux-aarch64.tar.gz",
                    "url": "graalpy3.13-25.3.4.1-linux-aarch64.tar.gz",
                },
                {
                    "name": "graalpy3.13-25.3.4.1-linux-amd64.tar.gz",
                    "url": "graalpy3.13-25.3.4.1-linux-amd64.tar.gz",
                },
                {
                    "name": "graalpy3.13-25.3.4.1-macos-aarch64.tar.gz",
                    "url": "graalpy3.13-25.3.4.1-macos-aarch64.tar.gz",
                },
            ],
        },
        {
            "tag_name": "graal-25.2.4",
            "assets": [
                {
                    "name": "graalpy3.12-25.2.4-linux-aarch64.tar.gz",
                    "url": "graalpy3.12-25.2.4-linux-aarch64.tar.gz",
                },
                {
                    "name": "graalpy3.12-25.2.4-linux-amd64.tar.gz",
                    "url": "graalpy3.12-25.2.4-linux-amd64.tar.gz",
                },
            ],
        },
        {
            "tag_name": "graal-25.0.3",
            "assets": [
                {
                    "name": "graalpy-25.0.3-linux-aarch64.tar.gz",
                    "url": "graalpy-25.0.3-linux-aarch64.tar.gz",
                },
                {
                    "name": "graalpy-25.0.3-linux-amd64.tar.gz",
                    "url": "graalpy-25.0.3-linux-amd64.tar.gz",
                },
            ],
        },
    ]