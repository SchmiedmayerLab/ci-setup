<!--
This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Working on runner setup

Read [the maintenance guide](docs/maintenance.md) and the relevant setup guide before editing.
For a new VM host, also read [interactive preparation and background provisioning](docs/prepare.md).
For toolchain policy or OS-update work, read [Xcode retention](docs/xcode-versions.md) and the [macOS update runbook](docs/macos-updates.md).
These instructions apply to automated contributors and people using scripting tools.

Keep each Markdown prose sentence on one source line, with line breaks between sentences.
Let the editor or rendered page wrap long lines; preserve code blocks, tables and other structural formatting.

Read the organization's [repository standards](https://github.com/SchmiedmayerLab/.github/blob/main/REPOSITORY_STANDARDS.md) for documentation, metadata and workflow changes.
Checking the PR template alone does not satisfy those standards.
Preserve the required README sections and footers, and use the shared standards workflow instead of replacing its checks.

- Identify whether the current machine is a workstation, VM host or dedicated runner.
  Never run plain `./setup`, `install runner`, `converge` or `update` on a workstation or VM host.
  Use mocked tests, `host check`, the `host power` preview and `install host --check` there.
  Never apply `--dedicated-host` power settings on a workstation during validation.
- Deploy only an approved, clean, signed revision and record it on the host and guests.
  Use `prepare host` for the initial operator session, then `host progress --json` and `host resume` to follow or continue the saved plan.
  Let the background worker perform provisioning; do not duplicate its steps or restart an active worker.
  Treat `configured` as local setup completion, with real jobs, locked-session operation and restart recovery still requiring separate validation.
- VM mutations must use the configured names and saved owned UUIDs.
  Never adopt, delete, replace, reset or stop unrelated VMs.
  Inspect interrupted state before retrying; do not remove state files to get past an ownership error.
- Preserve active jobs.
  Runner mutations belong inside the existing per-user lock and maintenance guard.
  Keep host orchestration out of runner maintenance.
  OS-update scheduling protection must survive reboot; the in-process maintenance guard is not a persistent drain.
- Keep the shared Xcode policy consistent on the host and guests.
  Retain the configured pins as well as the newest eligible release/RC, prepare all retained toolchains before cleanup, and validate each against its own SDK-compatible runtimes.
  Record exact toolchain/runtime builds and check retention again after maintenance and approved restarts.
- Keep passwords, PATs, Apple credentials, authentication URLs and private terminal output out of source, logs, tests, command arguments and review drafts.
  Test with fake values.
  Read only the diagnostic fields needed to understand a failure.
- Use supported macOS, UTM and GitHub interfaces.
  Never bypass consent, disable FileVault/SIP/Gatekeeper, expand sudo privileges or weaken SSH verification to make automation pass.
  State a remaining interactive boundary clearly.
- Preserve existing work and registrations.
  Add focused regression tests for the failing behavior; avoid unrelated rewrites or speculative compatibility layers.
- Follow the user's identity and signing policy for every commit, including test fixtures.
  Never disable signing to make a test pass.
  Verify created signatures.
- Prepare changes and review text locally.
  Prefer signed follow-up commits and normal pushes on the existing PR branch; avoid rewriting published history unless explicitly instructed after reviewing the impact and recovery options.
  Do not push, publish or communicate on GitHub without explicit approval of the concrete action and content.
- Report what was tested, what was mocked and what still needs real-machine validation.
  Never treat successful prerequisite checks as a completed install.
