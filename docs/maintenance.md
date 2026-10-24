<!--
This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Maintaining macOS compatibility

Follow the organization’s [contribution guidelines](https://github.com/SchmiedmayerLab/.github/blob/main/CONTRIBUTING.md).
This guide covers project-specific diagnosis and testing. [AGENTS.md](../AGENTS.md)
describes the constraints for automated work.

Apply the [repository standards](https://github.com/SchmiedmayerLab/.github/blob/main/REPOSITORY_STANDARDS.md)
when changing documentation, metadata or CI. Keep the README's required badge order,
fixed closing sections and light/dark footers. Keep citation authors consistent
with the contributor list and preserve existing copyright attribution. Community
files and templates are inherited; do not duplicate them locally.

`.github/workflows/repository-standards.yml` calls the shared Licensing, Docs,
Actions and Surface checks at the organization's release tag. Update that pin when
the organization updates its standard. Python CI generates coverage and uploads
it with GitHub OIDC, without a stored upload token. The local link checker is a
quick offline check; it does not replace the shared checks or external link validation.
Dependabot groups GitHub Actions updates into one weekly pull request.

Repository settings, required branch checks and Zenodo integration live outside
Git. Audit them separately. Configure required checks only after the corresponding
workflows are published and producing those exact check names. Do not invent a DOI
or mark remote checks successful before they have run.
The [REUSE badge service](https://api.reuse.software/) requires repository
registration and email confirmation; passing the local licensing check does not
register the repository. Verify the badge's information page as well as its image.

Start with the failing phase and a minimal reproduction in an owned test VM.
Record macOS version/build, architecture, UTM and xcodes versions, the setup
revision and the command used. Use sanitized phase results rather than copying
private authentication or full runner logs. Distinguish host and guest failures.

Check the current primary documentation or command help for the failing API.
Keep fixes in the responsible module: `host.py` for UTM and ownership,
`restore.py` and `host/restore-image.swift` for Apple restore media,
`guests.py` for SSH provisioning, `xcode_cache.py` for host downloads, `xcode.py`
for toolchains, and `install.py` for guided preparation. Reuse the existing
locks, credential handling and maintenance guard. Add a focused regression test
that reproduces the failure and proves the safe outcome on retry.

Run the applicable unit tests, Python lint, shell syntax and REUSE checks. The
tests mock machine operations, but `tests/test_update.py` creates Git fixture
commits with signing disabled. Where policy requires every commit to be signed,
exclude that module and report the exclusion; do not override the policy.

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
zsh -n setup host/ssh-askpass
bash -n host/bootstrap-guest.sh
bash -n hooks/job-started.sh hooks/job-completed.sh
reuse lint
git diff --check
```

For changes to machine behavior, use a disposable owned guest and verify the
affected path: first install, safe rerun, interrupted operation, busy-job deferral,
or restart. Toolchain changes should check selected Xcode, required SDKs, simulator
boot, Swift tests and Metal compilation/compute. Restore-image changes need a real Apple discovery and compatibility check on an
Apple silicon host; Linux tests mock that API. Host permission changes need a
background check and, where authorized, a locked-session test. Never run physical
runner installation on a developer workstation to validate it.

Cleanup changes must exercise temporary-directory fixtures before any runner test:
root/home/installation paths, hidden files, interrupted-job leftovers, symlink
replacement, filesystem boundaries, runner control files, timeouts and failure
exit codes. The hooks share the setup lock. GitHub prepares actions and command
files before invoking the start hook; preserve `_actions`, `_temp`, `_tool` and
`_PipelineMapping` in both hooks. Do not run either hook on a workstation to test it.

Use the organization’s [pull request template](https://github.com/SchmiedmayerLab/.github/blob/main/.github/pull_request_template.md)
for the local review draft. Its sections cover the problem, release notes,
documentation and testing, followed by the conduct and contribution agreement.
Link an existing issue or discussion for a non-trivial change; if none exists,
record that requirement in the draft before requesting publication. Leave the
personal agreement unchecked for the submitting author.

Describe the observed failure, resulting behavior, versions tested and remaining
limitations. Include a reproducible test
without secrets. Inspect the diff and metadata, sign commits according to the
user's configured policy, and verify signatures. Publishing a branch, issue or
pull request is a separate, explicitly approved action. If a macOS change needs
fresh user consent, document that boundary instead of bypassing it.
