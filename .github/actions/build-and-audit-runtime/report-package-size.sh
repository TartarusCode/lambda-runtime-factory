#!/bin/bash
# Append this cell's artifact budget to the job summary.
#
# Runs whether or not the build ran: on a package-cache hit there is no
# build-stats.json, and package_size.py recovers the numbers from the package
# itself, so the summary always states what this cell costs against the 250 MB
# quota. When build-container ran, the same sidecar carries the image size and it
# appears in the row too.
set -euo pipefail

runtime_id=$1
arch=$2

exec python3 tools/runtime_lib/package_size.py summary \
    --runtime "${runtime_id}" \
    --arch "${arch}" \
    --summary-file "${GITHUB_STEP_SUMMARY:-/dev/null}"
