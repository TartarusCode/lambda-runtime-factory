#!/usr/bin/env python3
"""Render the weekly runtime-update pull request from the check's JSON report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as error:
        raise SystemExit(f"{path}: {error}")
    except ValueError as error:
        raise SystemExit(f"{path} is not valid JSON: {error}")


def title(data: Dict[str, Any]) -> str:
    base = "chore: bump runtime distribution versions and add new lines"
    retired = data.get("retired_waivers") or []
    if not retired:
        return base
    advisories = ", ".join(sorted({str(item["advisory"]) for item in retired}))
    return f"[Security: Retire {advisories}] {base}"


def body(data: Dict[str, Any], failures: List[Dict[str, Any]]) -> str:
    lines = [
        "## Runtime Version Bumps",
        "",
        "The following runtimes were updated to their latest upstream versions:",
        "",
        "| Runtime | Current | Latest |",
        "| --- | --- | --- |",
    ]
    for runtime_id, info in sorted((data.get("outdated") or {}).items()):
        lines.append(f"| `{runtime_id}` | `{info['current']}` | `{info['latest']}` |")

    new_lines = data.get("new_lines") or {}
    if new_lines:
        lines += [
            "",
            "## New Runtime Lines",
            "",
            "New runtime packages were created for newly available version lines:",
            "",
        ]
        for family, info in sorted(new_lines.items()):
            lines.append(
                f"- **{info['id']}**: new {family} line {info['line']} at version {info['version']}"
            )

    retired = data.get("retired_waivers") or []
    if retired:
        lines += [
            "",
            "## Retired Security Waivers",
            "",
            "The fixed RPM these acknowledgements were waiting for is now published by UBI9,",
            "so the entries were removed rather than renewed:",
            "",
        ]
        for item in retired:
            lines.append(
                f"- **{item['advisory']}** — `{item['package']}` {item['fixed_evr']}; "
                f"UBI9 now publishes {', '.join(item['published_evrs'])}"
            )
        advisories = ", ".join(sorted({str(item["advisory"]) for item in retired}))
        lines += [
            "",
            "## Release Required After Merge",
            "",
            "Merging this deletes the waiver, so the next build installs the fixed RPM and the",
            "scan reports the package clean. Publish the patched images rather than waiting for",
            "the weekly audit to notice:",
            "",
            "```bash",
            "git fetch origin main && git checkout main && git pull",
            f'git tag -s "release-$(date -u +%Y.%m.%d)" -m "Release $(date -u +%Y.%m.%d): UBI9 security update ({advisories})"',
            'git push origin "release-$(date -u +%Y.%m.%d)"',
            "```",
            "",
            "Or dispatch `.github/workflows/release-runtime.yml` from `main` for the affected runtimes.",
        ]

    lines += ["", "Checksums were fetched from the official upstream release channels."]
    if failures:
        lines += [
            "",
            "## Skipped Runtimes",
            "",
            "These runtimes could not be updated and were left untouched:",
            "",
        ]
        for item in failures:
            lines.append(f"- **{item['runtime']}**: {item['message']}")
        lines += ["", "_Resolve these upstream and re-run the workflow to pick them up._"]

    lines += [
        "",
        "## Test Plan",
        "- [ ] CI validates manifests and checksums",
        "- [ ] CI builds all updated runtime packages",
        "- [ ] CI audits updated packages with Grype",
    ]
    return "\n".join(lines) + "\n"


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    title_parser = sub.add_parser("title")
    title_parser.add_argument("--updates", required=True)

    body_parser = sub.add_parser("body")
    body_parser.add_argument("--updates", required=True)
    body_parser.add_argument("--failures", default="")
    body_parser.add_argument("--out", default="")

    args = parser.parse_args(argv)
    data = _load(args.updates)
    if args.command == "title":
        print(f"title={title(data)}")
        return 0

    failures: List[Dict[str, Any]] = []
    if args.failures and Path(args.failures).exists():
        failures = _load(args.failures).get("failures", [])
    rendered = body(data, failures)
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
