<!--
This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Working on runner setup

Read [the maintenance guide](docs/maintenance.md) and the relevant setup guide before editing.
These instructions apply to automated contributors and people using scripting tools.

Read the organization's [repository standards](https://github.com/SchmiedmayerLab/.github/blob/main/REPOSITORY_STANDARDS.md)
for documentation, metadata and workflow changes. Checking the PR template alone
does not satisfy those standards. Preserve the required README sections and
footers, and use the shared standards workflow instead of replacing its checks.

- Identify whether the current machine is a workstation, VM host or dedicated
  runner. Never run plain `./setup`, `install runner`, `converge` or `update` on a
  workstation or VM host. Use mocked tests and `install host --check` there.
- VM mutations must use the configured names and saved owned UUIDs. Never adopt,
  delete, replace, reset or stop unrelated VMs. Inspect interrupted state before
  retrying; do not remove state files to get past an ownership error.
- Preserve active jobs. Runner mutations belong inside the existing per-user lock
  and maintenance guard. Keep host orchestration out of runner maintenance.
- Keep passwords, PATs, Apple credentials, authentication URLs and private terminal
  output out of source, logs, tests, command arguments and review drafts. Test with
  fake values. Read only the diagnostic fields needed to understand a failure.
- Use supported macOS, UTM and GitHub interfaces. Never bypass consent, disable
  FileVault/SIP/Gatekeeper, expand sudo privileges or weaken SSH verification to
  make automation pass. State a remaining interactive boundary clearly.
- Preserve existing work and registrations. Add focused regression tests for the
  failing behavior; avoid unrelated rewrites or speculative compatibility layers.
- Follow the user's identity and signing policy for every commit, including test
  fixtures. Never disable signing to make a test pass. Verify created signatures.
- Prepare changes and review text locally. Do not push, publish or communicate on
  GitHub without explicit approval of the concrete action and content.
- Report what was tested, what was mocked and what still needs real-machine
  validation. Never treat successful prerequisite checks as a completed install.
