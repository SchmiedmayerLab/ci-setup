#!/bin/sh
#
# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT
#
# GitHub still needs its control files while completing the job.
set -eu
: "${CI_SETUP_PYTHON:?Run setup to configure the cleanup interpreter}"
HOOK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$CI_SETUP_PYTHON" -I "$HOOK_DIR/../bin/runner-cleanup" completed
