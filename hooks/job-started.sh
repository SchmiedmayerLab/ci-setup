#!/bin/sh
# Runs before every job (wired via ACTIONS_RUNNER_HOOK_JOB_STARTED in the
# runner's .env — see cisetup/runner.py). Failures of the individual cleanup
# commands must not fail the job; only the last command's status counts.

# Start every job from a pristine simulator state.
xcrun simctl shutdown all
xcrun simctl erase all

# Lets workflows detect they run on a self-hosted runner
# (e.g. `if: env.selfhosted == 'true'`).
echo "selfhosted=true" >> "$GITHUB_ENV"
