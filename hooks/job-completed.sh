#!/bin/sh
# Runs after every job (wired via ACTIONS_RUNNER_HOOK_JOB_COMPLETED in the
# runner's .env — see cisetup/runner.py). Leaves no job state behind:
# checkouts, build products, caches, and simulator contents are all wiped.

# The runner's work directory (all job workspaces, plus the runner's _temp/
# _actions/_tool caches — they are recreated on demand). CI_SETUP_WORK_DIR is
# written into .env by the setup; RUNNER_WORKSPACE (<work dir>/<repo name>)
# is the fallback provided by the runner itself.
WORK_DIR="${CI_SETUP_WORK_DIR:-}"
if [ -z "$WORK_DIR" ] && [ -n "${RUNNER_WORKSPACE:-}" ]; then
  WORK_DIR="$(dirname "$RUNNER_WORKSPACE")"
fi
if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
  rm -rf "$WORK_DIR"/*
fi

# Periphery's cache grows unbounded and is stale after the checkout is gone.
rm -rf ~/Library/Caches/com.github.peripheryapp/*

# Leave the simulators clean for the next job.
xcrun simctl shutdown all
xcrun simctl erase all
