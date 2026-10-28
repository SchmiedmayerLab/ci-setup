<!--

This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# macOS Self-Hosted GitHub Actions Runner

[![Build and Test](https://github.com/SchmiedmayerLab/ci-setup/actions/workflows/ci.yml/badge.svg)](https://github.com/SchmiedmayerLab/ci-setup/actions/workflows/ci.yml)
[![Codecov](https://codecov.io/gh/SchmiedmayerLab/ci-setup/branch/main/graph/badge.svg)](https://codecov.io/gh/SchmiedmayerLab/ci-setup)
[![REUSE status](https://api.reuse.software/badge/github.com/SchmiedmayerLab/ci-setup)](https://api.reuse.software/info/github.com/SchmiedmayerLab/ci-setup)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE.md)

Set up a dedicated physical runner or an Apple silicon host with runner VMs.
Start in a normal local Terminal while logged in and unlocked.
On a fresh Mac, first follow [getting the checkout](docs/setup.md#get-the-checkout-on-a-fresh-mac): install and verify Command Line Tools, then clone to a durable location such as `~/runner-setup`.
Keep the administrator password, scoped GitHub PAT and Apple two-factor device available for the [interactive preparation](docs/setup.md#plan-the-interactive-session).
Review the destination and settings in `config.toml`, then choose one command:

```sh
# Dedicated physical runner
./setup install runner

# Host with two runner VMs
./setup prepare host --dedicated-host
```

**Use `install runner` only on a dedicated runner machine.**
It installs tools, registers a runner, applies machine settings and enables job cleanup.
The host layout installs UTM if missing, downloads compatible macOS from Apple, and provisions the runners inside owned VMs.
Host security settings are preserved.
For a dedicated VM host, `prepare host --dedicated-host` includes reviewed AC power settings; see the [host checklist](docs/hosts.md#host-checklist).

`prepare host` collects credentials, requests macOS permissions and reviews the plan before handing provisioning to a persistent background agent.
After it reports **Background provisioning started**, close the Terminal if desired and use `./setup host progress` to follow the work or `./setup host resume` after resolving a failure.
See [interactive preparation and automatic provisioning](docs/prepare.md) for the prompt sequence, progress states and recovery commands.
The foreground `install host` command remains available for compatibility; keep its terminal open until it finishes.
See the [setup guide](docs/setup.md) for prerequisites, token scopes, unattended operation and the steps that still require a person.
The [host reference](docs/hosts.md) documents individual VM commands and resource choices.
For existing installations, the [macOS update runbook](docs/macos-updates.md) covers parallel guest updates over SSH, safe host preparation, explicit restart approval and recovery checks.

## What gets installed

- **GitHub Actions runner**: latest release, registered to your repo/org, running as a per-user launchd service, kept up to date.
- **Toolchain** (Homebrew): openjdk (java), firebase-cli, fastlane, node, xcbeautify, git-lfs, aria2, xcodes, jq, python3, swiftlint, periphery — plus xcpretty (Ruby user gem; it has no Homebrew formula).
- **Xcode**: Xcode 26.6 alongside the newest release or Release Candidate in the supplied configuration, with all configured platform SDKs, simulator runtimes and (Xcode 26+) the Metal toolchain.
  Newer betas are opt-in.
  See [multiple Xcode versions](docs/xcode-versions.md) for pin-only policies and a separately configured default.
- **Job environment**: CI jobs automatically get Homebrew on `PATH`, `JAVA_HOME`/`LANG`/`LC_ALL`, and the configured default Xcode via `DEVELOPER_DIR` — configured through the runner's `.env`/`.path` files (the runner's own mechanism for injecting env/PATH into every job).
  The global `xcode-select` uses the same configured default, and Apple's WWDR intermediate certificate is installed for code signing.
- **Maintenance**: per-user LaunchAgents run at login and every six hours.
  Runner maintenance defers active jobs.
  VM hosts download Xcode and distribute it to their guests; physical runners download it locally.
  See [background services](docs/setup.md#background-services) for startup and login requirements.

## Verify the result

For a physical runner, use `./setup status` and confirm that the runner appears as **Idle** in the destination's **Settings → Actions → Runners**.
For a VM host, use `./setup host check --config ~/runner-host.toml` for power and startup agents, then `./setup host status --config ~/runner-host.toml` and check both registered runners.
A successful preflight or a VM state of `ready` does not prove that a runner can execute jobs.
Use the [completion checklist](docs/setup.md#completion-checklist) to verify both guests, actual background maintenance and a real GitHub validation workflow.
See [troubleshooting](docs/setup.md#troubleshooting-by-phase) for the observed fresh-machine failure modes and the correct logs.

## Existing runners and maintenance

### Migrating an existing runner without GitHub tokens

For an existing runner installation, clone this repository into a separate `~/runner-setup` directory on the runner.
Keep its runner directory and registration intact.
From the new checkout, run as the same logged-in runner user:

```sh
cd ~/runner-setup
./setup adopt ~/runner --skip-xcode
./setup info
```

`adopt` validates that the existing runner belongs to the configured GitHub organization/repository, waits for no jobs by deferring busy maintenance, pauses intake, and runs the regular maintenance phases.
`--skip-xcode` retains the current Xcode during the migration; a later `./setup update` can install newer releases and request Apple authentication if necessary.
Adoption needs an existing Python 3.11+ and does not bootstrap tools before validating the registration and pausing intake.

The runner's ID, name, group, existing GitHub labels, credentials, directory, and working folder are preserved.
Neither a GitHub PAT nor registration or removal tokens are needed.
The new cleanup hooks use the existing working folder.
Duplicate machine hostnames do not change the preserved runner names.
The known legacy Homebrew autoupdate LaunchAgent is retired before tool changes, so updates are owned by this setup's maintenance schedule.
If the legacy updater is executing, adoption stops before changing packages; let it finish and retry.
Unexpected legacy agent definitions require manual review.

Adoption metadata is stored in `~/Library/Application Support/ci-runner-setup/adopted-runner.json`, outside the Git checkout.
Keep `config.toml` unmodified so future self-updates and switching the checkout to `main` work normally.
Existing labels are preserved, not reconciled with `runner.labels`.
Subsequent registration-policy changes or unexpected local identity changes fail without implicit re-registration; review such a change as a separate migration.

The retired Homebrew plist is kept under the setup state directory's `legacy` folder.
Restoring that updater requires enabling its launchd label as well as restoring/loading the archived plist; first stop the new maintenance agent to avoid competing schedules.
Adoption does not delete the old installer, old cleanup scripts, or any credential files.

### Regular maintenance

On a dedicated runner, `./setup` (the default `converge` command) checks each phase and applies needed changes.
On a VM host, use the [host commands](docs/hosts.md#configure-each-runner) instead.

| Phase | Re-run behaviour |
|---|---|
| Homebrew packages | refreshes metadata, installs/upgrades each managed package independently; reports outdated pinned formulae without changing the pins |
| Passwordless xcode-select | sudoers rule (`/etc/sudoers.d/xcode`) installed on the first interactive run: `sudo xcode-select -s` and `sudo xcodebuild -runFirstLaunch` work without a password — for CI jobs and for unattended converges finishing a new Xcode's first-launch setup |
| Xcode releases | installs every pinned/tracked release and requested components; selects the configured default only after successful preparation; retains the Xcode still referenced by the runner environment until a later pass |
| Runner software | updates when a newer release exists (checksum-verified); the runner also self-updates between runs |
| Registration | re-registers only when `config.toml` changed (name/labels/URL/group/work dir); otherwise untouched |
| Job env (`.env`/`.path`) | rewritten only on change; a persisted restart marker survives an interrupted update |
| Service / boot agent | (re)installed only when missing or changed |

A lock prevents overlapping Python maintenance passes and remains held through source/Python restarts.
Before changing tools, maintenance checks for active jobs, briefly freezes the idle listener to close the job-start race, checks again, then stops its service.
Busy or ambiguous state defers the run.
The service stays stopped across setup restarts.
Existing service intake is restored on completion or handled failure; an incomplete fresh installation does not begin accepting jobs.
A recovery marker lets the next run restore service after an abrupt interruption.

Independent phases continue after a handled command failure.
Failures remain visible in the final result; they are not converted into success because a later phase worked.
Exit codes are `0` for completion (possibly with warnings), `1` for failure/incomplete setup, and `2` for deferred maintenance.
An expired Apple session therefore does not prevent independent runner/configuration repairs, and failed Xcode preparation preserves existing Xcodes and runtimes.

Simulator cleanup runs after successful downloads and preparation.
It preserves runtimes associated with every retained Xcode, including runtimes for the same platform and OS major/minor version as its SDKs when their patch versions or build numbers differ.
Unknown runtime metadata is not grounds for deletion.

Python maintenance commands have wall-clock limits, including commands that keep printing output without advancing.
Ordinary probes default to two minutes.
Longer operations have explicit limits: Homebrew metadata refreshes get ten minutes, each package install/upgrade gets one hour, each Xcode install or platform/Metal download gets four hours, and first-launch setup and Xcode/runtime removal get thirty minutes.
Runner registration gets five minutes, archive extraction ten minutes, and service commands one minute.
The runner archive download checks a thirty-minute transfer deadline between reads, with a separate sixty-second socket timeout.
During a long transfer it logs the bytes received every five minutes.
The initial shell bootstrap (installing Homebrew and Python on a fresh Mac) is outside these Python limits.

Commands emit a single redacted **still running** notice only after half their timeout has elapsed (two hours for an Xcode install or platform download).
This confirms that setup is monitoring the child, not that it is making progress.
Captured commands, which can return credentials, and commands without a deadline do not emit these notices.
A timeout stops the command's process group and becomes a normal recorded failure; independent work continues and the maintenance pause attempts service recovery.
Output silence alone is not treated as a hang: Xcode extraction and builds can legitimately be quiet.

## The boot agent (automatic re-runs)

`converge` installs `~/Library/LaunchAgents/com.selfhosted-runner.setup.plist`, which runs `setup converge --non-interactive` at every login **and every 6 hours**, so long-lived login sessions still pick up new Xcode releases, runner updates, and config changes.
Maintenance defers while a job is running and pauses intake while changing the machine.
Guests provisioned by the host use its staged Xcode catalog; the host refresh agent supplies newer builds.
Unattended runs first fast-forward the checkout's configured Git upstream, then re-execute if the source changed.
A dirty checkout, missing upstream, divergence, or pull failure is reported; automatic runs still attempt local maintenance and finish with a failure result.
Git never prompts or discards local changes.
Homebrew Python upgrades also trigger a restart before later phases use the interpreter.

Unattended setup gives child commands no stdin or controlling terminal.
Passwordless Xcode sudo rules may be used; work requiring interaction fails, is reported as deferred, or reaches its command timeout if a tool retries a prompt instead of exiting.
Output and phase results are retained as described below.
Setting `boot.install_agent = false` in `config.toml` removes the agent on the next manual converge.
If the active agent changes its own definition, the new definition takes effect at the next login; renaming or disabling the active agent requires a subsequent manual converge to unload its old schedule.

## Commands

Initial setup and host inspection:

```sh
./setup install runner
./setup prepare host --dedicated-host
./setup host progress
./setup host resume
./setup install host # foreground alternative
./setup install host --config ~/runner-host.toml # resume an existing layout
./setup install host --check --config ~/runner-host.toml # background access check
./setup host status --config ~/runner-host.toml
./setup host check --config ~/runner-host.toml # read-only power and startup-agent checks
./setup host power # preview dedicated-host AC power settings
./setup host --help
```

Run these on a dedicated runner, including inside a configured VM:

```sh
./setup                # converge (default)
./setup update         # fast-forward source, restart if changed, converge/reload boot agent
./setup adopt ~/runner # preserve an existing registration and migrate maintenance
./setup status         # show runner/service/Xcode state and current job
./setup info           # current setup revision, services, tool versions/pins and Xcode selections
./setup info --json    # same comparable inventory as status --json
./setup logs --lines 200 # recent timestamped maintenance output
./setup status --json  # read-only comparable inventory
./setup compare /tmp/other-runner.json # nonzero if different or incomplete
./setup store-pat      # print PAT requirements, prompt for it (hidden)
./setup uninstall      # deregister runner, remove services (asks first)
./setup converge --skip-xcode   # useful while iterating
```

`update` stops before changing packages if its pull fails.
It updates an existing runner's registration only when its configuration changed; unchanged registrations retain their identity.
`status`, `info`, `logs`, `compare`, and help use an existing Python 3.11+ and never bootstrap missing dependencies.

`info` prints a fresh local summary: checkout revision and macOS version, runner registration/service and boot-agent state, last maintenance result, pending recovery/restart markers, log location/retention, and installed/global/job-selected Xcode builds.
A final tools section lists versions and pins only for Homebrew formulae/casks explicitly requested by setup or configuration.
`info --json` retains the full dependency inventory for comparisons.
It does not update packages, authenticate, restart services, or write maintenance state.
Unavailable probes are listed as incomplete and return exit code 1 while the remaining information is still displayed.

Both `status` and `info` show local activity (busy, idle, paused, or offline).
While busy, they show the current job's name, repository, workflow, ref, start time, elapsed time and GitHub run link when available.
This uses the live worker's open diagnostic log, works with existing jobs, and needs no GitHub credentials or new hooks.
Idle means a local listener is running without a worker; it does not verify GitHub connectivity.
Job start/elapsed time measures local job receipt, including preparation and cleanup, rather than queue time.
If the log has rotated, is unreadable, exceeds 8 MiB or lacks usable metadata, activity remains busy with job details unavailable.
Historical jobs are never used as a fallback.
JSON includes this under `runner.activity`, outside the toolchain comparison, and exports only selected job metadata, never the full job payload or environment.

## Logs and keeping runners in sync

Each Python maintenance run records its start, finish, exit code, phase outcomes and child stdout/stderr in `~/Library/Logs/ci-runner-setup/setup-YYYY-MM-DD.log`.
Run IDs connect output across phases.
`./setup status` shows the latest result, also saved in `~/Library/Application Support/ci-runner-setup/last-run.json`.
A run still marked `running` after its process has gone indicates an abrupt interruption.

`[logging] retention_days = 30` retains up to 30 calendar days.
`max_bytes = 52428800` additionally caps the total dated logs at 50 MiB, so heavy output can shorten that window.
Oldest output is discarded first.
Known tokens are redacted; captured credential responses and stdin are not logged.
Files are created owner-only.
Bootstrap failures before Python starts appear in `bootstrap.log`, overwritten at the start of the next agent attempt.
An old `~/Library/Logs/ci-runner-setup.log` is historical and can be removed manually after inspecting it.

Shared configuration specifies the same update policy, but two machines running at different times can still acquire different versions or encounter different failures.
Export inventories to check the actual state.
For example, these commands execute setup **on the runners via SSH**:

```sh
ssh runner@github-runner-1.local 'cd ~/runner-setup && ./setup status --json' > /tmp/runner-1.json
scp /tmp/runner-1.json runner@github-runner-2.local:/tmp/runner-1.json
ssh runner@github-runner-2.local 'cd ~/runner-setup && ./setup compare /tmp/runner-1.json'
```

Comparison includes setup commit/config, macOS build/architecture, runner version, active managed Homebrew packages and their dependencies/pins, and installed/selected Xcode builds.
Host names, capture times, and old inactive Homebrew kegs do not create differences.
Incomplete or dirty snapshots cannot report a match.
Each maintenance pass also saves `inventory.json` alongside the run summary; `status --json` takes a fresh snapshot.

## Apple authentication for Xcode downloads

For the VM layout, authenticate **on the host**.
For a physical runner, authenticate on that runner.
Guided setup does this before provisioning.
To renew an expired session, open Terminal on the downloading machine and run:

```sh
./setup host xcode-login
```

This command only handles Apple login; it does not install UTM or create VMs.
xcodes retains the account, Keychain password and session.
Apple may still request a new two-factor code or agreement.
After signing in, rerun `./setup host refresh-xcode --config ~/runner-host.toml` on a VM host, or `./setup install runner` on a physical runner.

Only Xcode archives and checksums are transferred to provisioned guests.
They receive neither the host's Apple credentials nor its GitHub PAT.
The host cache supports later rebuilds; Xcode still comes directly from Apple.
Failed downloads leave the installed toolchain available.
See [Apple authentication and caching](docs/hosts.md#apple-authentication-and-xcode-refresh) for storage locations and background operation.

## Per-job cleanup

The runner runs two hook scripts around every job (wired via `ACTIONS_RUNNER_HOOK_JOB_STARTED`/`_COMPLETED` in the runner's `.env`; disable with `runner.cleanup_hooks = false` in `config.toml`):

- [`hooks/job-started.sh`](hooks/job-started.sh) removes leftover job contents, including hidden files, clears Periphery's cache and resets simulators.
  Cleaning before the job also recovers from a previous crash or interrupted post-job hook.
  On success it exports `selfhosted=true` into `$GITHUB_ENV`.
- [`hooks/job-completed.sh`](hooks/job-completed.sh) performs the same cleanup after workflow steps and their post-job actions have finished.

Both hooks verify the work directory against setup configuration, the runner's registration and the job environment.
Cleanup rejects root/home/installation targets, paths containing the setup checkout, parent traversal, symlinked target directories and filesystem boundaries.
Symlinks inside disposable job contents are unlinked without following their targets.
A former work directory is retained for inspection when registration changes its location.

GitHub prepares actions and command files before the start hook and still needs them during the end hook.
Both hooks retain `_actions`, `_temp`, `_tool` and `_PipelineMapping`, plus the current workspace directories.
They remove the job contents within those workspace directories.
Files outside the registered work directory are untouched except for the runner user's fixed Periphery cache.

The hooks use the setup maintenance lock.
Filesystem cleanup has a five-minute deadline; simulator shutdown and erase each have a two-minute deadline.
Any failure returns a nonzero exit code and is reported in the job's hook log.
Failed start cleanup prevents the job from proceeding.
A later run retries cleanup; setup does not force permissions or elevate privileges to delete files.

Normal runner maintenance installs the Python interpreter path and hook settings in `.env`, then restarts the idle service when needed.
Update through that path before using the new hooks; do not copy hook files into an active installation.
These hooks provide cleanup for trusted jobs, not isolation from malicious code running under the same account or a replacement for rebuilding a VM.

For Xcode builds, put DerivedData inside the job workspace as shown below so it is covered by cleanup.
The hooks do not erase the user's global DerivedData, dependency caches, installed tools, simulator runtimes or host download cache.

## Using the runner in workflows

```yaml
jobs:
  build:
    # ARM64 on Apple Silicon, X64 on an Intel Mac; plus labels from config.toml
    runs-on: [self-hosted, macOS, ARM64]
    steps:
      - uses: actions/checkout@v4
      - run: xcodebuild build -scheme MyApp -derivedDataPath "$GITHUB_WORKSPACE/.derived-data"
```

Jobs needing a specific Xcode can set `DEVELOPER_DIR` to the path of an installed Xcode.
Keep that version in `xcode.pinned_versions` so maintenance retains its application and required simulator runtimes.
The supplied policy pins 26.6 and tracks the newest release or RC as the default.
Xcode 26.6 on macOS 27 is an accepted configuration for `xcodebuild` and other CLI use; the Xcode GUI is outside this use case.
Follow the [per-toolchain validation and compatibility guidance](docs/xcode-versions.md), including the recorded scope of this accepted pairing.

## Credentials and job access

Run only trusted workflows.
Jobs execute as the runner user and can access that user's files, processes and unlocked Keychain.
Restrict runner-group access to the intended repositories and prevent untrusted pull requests from using them.

In the VM layout, the long-lived PAT, Apple credentials and generated guest passwords remain on the host.
Each guest receives a short-lived registration token and keeps its runner registration credentials.
Shared/NAT networking is not a boundary against the host or local network.

On a physical runner, the PAT and Apple credentials belong to the same user as jobs.
Limit the PAT to runner administration and use a dedicated Apple account.
Store tokens with the hidden `./setup store-pat` prompt; never place them in shell arguments, configuration, logs or review drafts.

Runner downloads are checked against published SHA-256 values.
Guest transfers also verify checksums, and xcodes verifies Apple's application signature.

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| Background access check fails | Resolve the named Keychain, Apple or Automation permission in the logged-in session, then rerun the check. |
| Runner offline after reboot | Unlock FileVault and log in on the host or physical runner. VM runners also need their guest login session; provisioned guests use automatic login. |
| Apple asks for authentication | Run `./setup host xcode-login` on the machine downloading Xcode, then retry maintenance. |
| Maintenance fails | Run `./setup status` and `./setup logs` on the runner. If Python never started, inspect `~/Library/Logs/ci-runner-setup/bootstrap.log`. |
| Host refresh fails | Inspect `~/Library/Application Support/runner-host/xcode-refresh.log` and guest phase results. |
| Guest source differs from the host | Inspect both clean revisions before a reviewed fast-forward. Provisioning never resets an existing checkout. |
| Runner directory must change | Drain jobs and review migration first. Deregister using the old configuration before creating a replacement registration. |

Service status is available through `~/actions-runner/svc.sh status`; runner logs are under `~/actions-runner/_diag/`.
These paths follow `runner.dir` if changed.

## Maintenance and repository checks

The [maintenance guide](docs/maintenance.md) covers compatibility fixes and local validation.
[AGENTS.md](AGENTS.md) defines constraints for automated work.
The organization’s [repository standards](https://github.com/SchmiedmayerLab/.github/blob/main/REPOSITORY_STANDARDS.md) apply to documentation, licensing, citation metadata and workflows.
CI calls the shared standards workflow and reports Python coverage to Codecov.
Its first coverage report will appear after the workflow runs on GitHub.

## Contributing

Contributions to this project are welcome.
Please make sure to read the [contribution guidelines](https://github.com/SchmiedmayerLab/.github/blob/main/CONTRIBUTING.md) and the [contributor covenant code of conduct](https://github.com/SchmiedmayerLab/.github/blob/main/CODE_OF_CONDUCT.md) first.
You can find a list of contributors in the [CONTRIBUTORS.md](CONTRIBUTORS.md) file.

## License

This project is licensed under the MIT License.
See [LICENSE.md](LICENSE.md) for more information.

## Citation

If you use this software, please cite it using the metadata in [CITATION.cff](CITATION.cff), which GitHub surfaces through the [*Cite this repository*](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-citation-files) button.

## Our Research

For more information, visit the [Schmiedmayer Lab GitHub organization](https://github.com/SchmiedmayerLab).

![Schmiedmayer Lab](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/footer-light.png#gh-light-mode-only)
![Schmiedmayer Lab](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/footer-dark.png#gh-dark-mode-only)
