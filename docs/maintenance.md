<!--
This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Maintaining macOS compatibility

Follow the organization’s [contribution guidelines](https://github.com/SchmiedmayerLab/.github/blob/main/CONTRIBUTING.md).
This guide covers project-specific diagnosis and testing.
[AGENTS.md](../AGENTS.md) describes the constraints for automated work.
For an operational OS update on an existing host and its guests, use the [macOS update runbook](macos-updates.md).
Updating setup source with `./setup update` on a runner is separate from installing macOS.

Apply the [repository standards](https://github.com/SchmiedmayerLab/.github/blob/main/REPOSITORY_STANDARDS.md) when changing documentation, metadata or CI.
Keep the README's required badge order, fixed closing sections and light/dark footers.
Keep citation authors consistent with the contributor list and preserve existing copyright attribution.
Community files and templates are inherited; do not duplicate them locally.

`.github/workflows/repository-standards.yml` calls the shared Licensing, Docs, Actions and Surface checks at the organization's release tag.
Update that pin when the organization updates its standard.
Python CI generates coverage and uploads it with GitHub OIDC, without a stored upload token.
The local link checker is a quick offline check; it does not replace the shared checks or external link validation.
Dependabot groups GitHub Actions updates into one weekly pull request.

Repository settings, required branch checks and Zenodo integration live outside Git.
Audit them separately.
Configure required checks only after the corresponding workflows are published and producing those exact check names.
Do not invent a DOI or mark remote checks successful before they have run.
The [REUSE badge service](https://api.reuse.software/) requires repository registration and email confirmation; passing the local licensing check does not register the repository.
Verify the badge's information page as well as its image.

Start with the failing phase and a minimal reproduction in an owned test VM.
Record macOS version/build, architecture, UTM and xcodes versions, the setup revision and the command used.
Use sanitized phase results rather than copying private authentication or full runner logs.
Distinguish host and guest failures.

Check the current primary documentation or command help for the failing API.
Keep fixes in the responsible module: `host.py` for UTM and ownership, `restore.py` and `host/restore-image.swift` for Apple restore media, `guests.py` for SSH provisioning, `xcode_cache.py` for host downloads, `xcode.py` for toolchains, and `install.py` for guided preparation.
Reuse the existing locks, credential handling and maintenance guard.
Add a focused regression test that reproduces the failure and proves the safe outcome on retry.

Run the applicable unit tests, Python lint, shell syntax and REUSE checks.
The host installer supplies runtime prerequisites, not the flake8 and REUSE validation tools.
After bootstrap provides Python 3.11+, install validation tools in a virtual environment outside the provisioning checkout:

```sh
python3 -m venv ~/runner-validation-venv
. ~/runner-validation-venv/bin/activate
python -m pip install flake8 'reuse[charset-normalizer]'
```

The REUSE encoding-detection extra avoids requiring a separate system libmagic installation on a clean Mac.
A plain `pip install reuse` can install successfully but leave `reuse lint` unable to start with `NoEncodingModuleError`.
Keep validation dependencies outside the checkout so it remains clean for guest provisioning.

The tests mock machine operations, but `tests/test_update.py` creates Git fixture commits with signing disabled.
Where policy requires every commit to be signed, exclude that module and report the exclusion; do not override the policy.

```sh
python3 - <<'PY'
import unittest

def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        elif not item.id().startswith('test_update.'):
            yield item

suite = unittest.TestSuite(cases(unittest.defaultTestLoader.discover('tests')))
raise SystemExit(not unittest.TextTestRunner().run(suite).wasSuccessful())
PY
flake8 --select=F,E9 cisetup tests bin/ci-setup bin/check-docs bin/runner-cleanup
python3 bin/check-docs
zsh -n setup
zsh -n host/ssh-askpass
bash -n host/bootstrap-guest.sh
bash -n hooks/job-started.sh
bash -n hooks/job-completed.sh
reuse lint
git diff --check
```

Some terminal-input tests create a real pseudo-terminal even though machine operations are mocked.
If these fail only inside an automation sandbox, retain that result and rerun the affected module in a normal permitted local terminal:

```sh
python3 -m unittest discover -s tests -p test_util.py
```

Report both environments and results; a passing rerun does not erase the initial failure or prove that every sandbox failure has the same cause.
Do not weaken system security controls or skip unrelated failing tests to obtain a passing result.

For changes to machine behavior, use a disposable owned guest and verify the affected path: first install, safe rerun, interrupted operation, busy-job deferral, or restart.
Toolchain changes should check selected Xcode, required SDKs, simulator boot, Swift tests and Metal compilation/compute.
Restore-image changes need a real Apple discovery and compatibility check on an Apple silicon host; Linux tests mock that API.
Host permission changes need a background check and, where authorized, a locked-session test.
Use `./setup host check --config ~/runner-host.toml` and `./setup host power` for read-only host validation.
Test power writes with mocked `pmset` and sudo calls; do not apply the dedicated-host option on a workstation.
Cover missing or malformed power data, unsupported settings, declined confirmation, failed authorization, readback mismatch and stale or disabled startup agents.
Never run physical runner installation on a developer workstation to validate it.

Cleanup changes must exercise temporary-directory fixtures before any runner test: root/home/installation paths, hidden files, interrupted-job leftovers, symlink replacement, filesystem boundaries, runner control files, timeouts and failure exit codes.
The hooks share the setup lock.
GitHub prepares actions and command files before invoking the start hook; preserve `_actions`, `_temp`, `_tool` and `_PipelineMapping` in both hooks.
Do not run either hook on a workstation to test it.

Use the organization’s [pull request template](https://github.com/SchmiedmayerLab/.github/blob/main/.github/pull_request_template.md) for the local review draft.
Its sections cover the problem, release notes, documentation and testing, followed by the conduct and contribution agreement.
Link an existing issue or discussion for a non-trivial change; if none exists, record that requirement in the draft before requesting publication.
Leave the personal agreement unchecked for the submitting author.

Describe the observed failure, resulting behavior, versions tested and remaining limitations.
Include a reproducible test without secrets.
Inspect the diff and metadata, sign commits according to the user's configured policy, and verify signatures.
For an existing PR, add a follow-up commit on its branch and preserve the published history.
If a normal push would not fast-forward, inspect the new remote work and prefer reconciliation that preserves published commits.
Avoid force-pushing unless explicitly instructed after reviewing the exact history change, its impact and recovery options.
Refresh the PR description and deployment instructions to name the new verified signed revision and its actual test results.
Publishing a branch, issue or pull request is a separate, explicitly approved action.
If a macOS change needs fresh user consent, document that boundary instead of bypassing it.

## Fresh-host release evidence

Keep a concise current acceptance table separate from the chronological investigation journal.
Record the exact signed source revision, host and guest versions, chosen resource policy, manual prompts, successful stages, failed stages and untested stages.
Distinguish the original source tested on the Mac from local fixes tested only in a review copy.
Record agent exit results after execution; installed, loaded and currently running are not equivalent to successful maintenance.
Separate foreground credential access, background credential access and background guest SSH.
An external outage is not proof of invalid credentials, and a “No route to host” diagnostic alone does not prove a privacy-permission cause.

A complete host validation needs real concurrent GitHub jobs, cleanup through real jobs, active-job maintenance deferral and coordinated restart recovery in addition to local smoke checks.
Record local listener health, successful background maintenance and GitHub online status separately, including any reconnection delay after maintenance.
If the owner defers guest restarts, prohibits a host reboot or has not approved publishing/dispatching the validation workflow, leave those checks explicitly not tested.
Online runners and successful locked-session refresh do not substitute for these missing acceptance checks.
Keep physical-host runner validation explicitly separate when it is outside the test scope.
Follow [the completion checklist](setup.md#completion-checklist) and retain a sanitized reproduction before changing code or machine settings.
The [prepare/automate flow](prepare.md) preserves these distinctions; test its real launchd handoff and failure recovery as well as mocked provisioning phases.

## OS-update recovery regressions

The [OS update runbook](macos-updates.md) separates two behaviors that must remain covered before adding automatic OS-update support.
A successful `softwareupdate` client exit can precede the end of Apple's background operation, so a maintenance pause must not end merely because the client returned.
An older unprivileged-first implementation can block on GUI authorization or raise a timeout before reaching its fallback.
Current unattended preparation uses the existing authorized helper first.
Tests must cover denied authorization, bounded failure/timeout, selection restoration, readiness rechecks, blocked cleanup, active-job deferral and service recovery.
Do not encode termination of a particular PID or broad process matching as automatic recovery.

Any future OS-update/drain command needs explicit ownership checks, a durable scheduling gate across reboot, private administrator/volume-owner input, separate host installation/restart approval, and resumed monitoring after login.
Tests should distinguish successful client exit, active background preparation, a required second authentication prompt, reboot interruption, incomplete Xcode first launch, maintenance contention and delayed GitHub reconnection.
Until such a command is reviewed and shipped, the runbook's operator workflow is not a claim that these lifecycle guarantees are implemented by the setup CLI.

## Multi-version toolchain validation

Retain a per-runner, per-Xcode acceptance matrix for changes to [Xcode policy](xcode-versions.md).
Record source revision, macOS version/build, exact Xcode build/path, configured default, SDK/runtime versions, first launch, Swift tests, Metal compilation/compute and simulator boots.
Use the newest release/RC and its compatible current runtimes for the default verification, plus the pinned toolchain's own SDK-compatible runtimes for pin verification.
A newest-runtime-only check does not detect deletion of older pinned runtimes.
Repeat retention checks after successful host refresh, guest maintenance and approved guest/host restarts.
Check both the runner environment and global selection, and preserve the original registrations.

Mocked tests must cover alternative/mixed policy keys, exact pins, duplicates, missing/ambiguous versions, explicit defaults, optional betas, and host-to-guest catalog resolution.
Verify that incomplete pinned preparation prevents selection and cleanup, and that repeated maintenance and a newer rolling release preserve all required runtime families.
Removing a pin must leave the remaining policy intact.
For external Xcode installations, report application survival and runtime survival separately.
Also verify [bundle location and workflow discovery](xcode-versions.md#application-location-and-workflow-discovery): a CLI-usable external bundle or symlink can remain invisible to `setup-xcode`.
Test the effective build toolchain with the runner's normal `DEVELOPER_DIR`; action discovery and global selection alone do not establish which Xcode a job uses.
Global-selection probes must ignore the inherited `DEVELOPER_DIR` without clearing the job's own override.
Regressions cover correcting a masked global mismatch, restoring the actual prior selection after first launch fails or times out, and reporting global and runner selections independently.

The [validation workflow](hosts.md#validate-the-runners) requires a clean signed deployed revision matching the dispatched revision and an available workflow on the default branch.
Its job-specific toolchain selection does not change global selection.
Report mocked results, direct guest smoke tests, and real dispatched jobs independently.
Do not describe a proposed or locally modified deployment as the final signed source.
