"""pytest fixtures and sys.path setup for runtime_lib tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_RUNTIME_LIB_DIR = _THIS_DIR.parent

if str(_RUNTIME_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_LIB_DIR))


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