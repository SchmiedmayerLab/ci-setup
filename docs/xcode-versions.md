<!--
This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Retaining multiple Xcode versions

Configure which Xcodes maintenance retains independently from the default used by jobs.
The supplied configuration pins Xcode 26.6 alongside the newest release or Release Candidate and keeps that moving release/RC as the default.
Older configurations that omit the new keys preserve their previous latest-release/RC behavior, with a newer beta only when explicitly enabled.
The same policy applies to physical runners and the host-downloaded archives supplied to VM runners.

## Pin a version alongside the latest

The supplied policy retains 26.6 while continuing to update the default release/RC:

```toml
[xcode]
manage = true
track_latest = true
pinned_versions = ["26.6"]
default_version = "latest"
install_beta = false
platforms = ["all"]
```

This example expresses a retention policy, not a promise of OS compatibility.
Check Apple's [Xcode system requirements](https://developer.apple.com/xcode/system-requirements/) before enabling a pin on a machine.
Apple lists Xcode 26.6 for macOS 26.2–26.x.
CLI checks have succeeded on macOS 27.2 test guests, but this is empirical evidence outside that documented range; GUI compatibility was not validated.
Retention support does not bypass Apple's platform restrictions or make an unsupported pairing supported.
Reassess compatibility before OS updates or choosing a new guest image; a floating Xcode release may also increase its minimum OS requirement.

Every retained Xcode receives the configured platform SDKs, simulator runtimes, first-launch preparation and Metal tools through the existing maintenance path.
`platforms = ["all"]` requests the platforms available to each retained Xcode; it does not install every historical simulator release from Apple's catalog.
Xcode and runtime versions need not match: Xcode 26.6 supplies 26.5 platform SDKs and uses their compatible runtime families.
Simulator runtimes are shared machine resources and are not duplicated just because two Xcodes can use them.

## Other policies

| Goal | Settings under `[xcode]` |
| --- | --- |
| Existing latest-release/RC behavior | `track_latest = true`, `pinned_versions = []`, `default_version = "latest"` |
| Keep 26.6 plus latest, with latest as default | `track_latest = true`, `pinned_versions = ["26.6"]`, `default_version = "latest"` |
| Keep 26.6 plus latest, with 26.6 as default | `track_latest = true`, `pinned_versions = ["26.6"]`, `default_version = "26.6"` |
| Stop tracking and request only explicit versions | `track_latest = false`, `pinned_versions = ["26.6", "27.1"]`, `default_version = "27.1"` |

With `track_latest = false`, at least one pin is required.
`default_version = "latest"` selects the newest retained release/RC; it does not add a release omitted by the retention policy.
An exact default must already be retained by a pin or the tracking rules.
Changing the default does not remove the other retained toolchains.

`install_beta = true` requires `track_latest = true` and adds the newest beta only when it is newer than the tracked release/RC.
That beta does not become the implicit default.
An explicitly pinned beta is retained regardless of `install_beta`; a beta-only policy requires an explicit default rather than `"latest"`.

## Migrating another policy format

The supported keys are `manage`, `track_latest`, `pinned_versions`, `default_version`, `install_beta` and `platforms`.
Unknown keys, including `track` and `versions`, fail configuration loading before maintenance rather than silently dropping a pin.
Translate an existing site policy deliberately and deploy matching source and configuration to host and guests.

| Previous setting | Explicit replacement |
| --- | --- |
| `track = "release-or-rc"` | `track_latest = true`, retaining the existing pins and default intent. |
| `track = "none"` | `track_latest = false`, with at least one exact pin and a retained default. |
| `versions = ["latest", "26.6"]` | `track_latest = true`, `pinned_versions = ["26.6"]`, `default_version = "latest"`. |
| `versions = ["26.6", "27.1"]` | `track_latest = false`, both exact pins, and an explicitly reviewed default. |

A final-only rolling `track = "stable"` or `latest-stable` selector has no direct equivalent here.
Choose explicit final-version pins if RCs are not eligible for that site; do not silently translate it into latest tracking.
Do not mix old and new keys.

## Exact version resolution

Pins use exact `xcodes list` identifiers such as `"26.6"`, `"27.2 Release Candidate"` or `"27.3 Beta 2"`.
Wildcards, version ranges and per-major tracking rules are not implemented.
Equivalent trailing-zero forms such as `26.6` and `26.6.0` resolve to one installation.
Duplicate pins and a pin that overlaps the tracked latest version also install only once.

A version pin follows the build published for that exact release identifier in the catalog; it is not an immutable build/checksum pin.
Host archive receipts continue to verify downloaded bytes against their recorded checksums.
If a requested version is missing or has ambiguous builds, or the default is not retained, the Xcode phase fails before installing, selecting or removing toolchains.
The host resolves the whole policy before downloading any archive; a missing pin does not silently fall back to the latest.

## Downloads, selection and cleanup

The host downloads and verifies every selected archive once, then supplies the full selected set to each guest.
Guests resolve the shared configuration against that supplied catalog without receiving the host's Apple credentials.
Review and deploy the same committed configuration and source revision to host and guests; an old guest checkout does not understand these settings.
Do not run runner convergence on the VM host to apply this policy.
Plan source rollout separately from [guest configuration](hosts.md#configure-each-runner).
`host refresh-xcode` does not update guest source/configuration, and `host configure` refuses an existing guest checkout at another revision.
Preserve authorized uncommitted deployment changes, inspect the guest upstream/bundle and host preparation bindings, and advance only through the reviewed signed-source procedure.
Do not reset dirty checkouts, recreate guests or clear ownership/preparation records to force a migration.

Maintenance prepares all retained Xcodes before final selection and cleanup.
Incomplete first launch uses the existing authorized path directly: private bootstrap credentials during host-driven setup, interactive authorization when requested, or the existing passwordless helper for unattended maintenance.
The unattended helper restores the previous global selection even when first launch fails or times out, and preparation requires a successful readiness recheck.
If authorization or a new license requires a person, the phase fails for operator recovery; it does not wait on an unprivileged GUI prompt or broaden sudo rules.
The default controls the global developer directory and the runner's job `DEVELOPER_DIR` through the existing environment update path.
For a particular workflow, inspect `xcodes installed` for the actual app path and override `DEVELOPER_DIR` for that job or step.
Prefer a job-specific environment variable over changing the machine-wide selection during a job.

Cleanup keeps every configured pin and tracked version, plus the previous toolchain still referenced by the runner environment until a later safe pass.
Runtime cleanup matches the SDK/runtime requirements of every retained Xcode before deleting anything.
Removing a pin makes that old toolchain eligible for the existing cleanup rules after successful preparation; do not remove pins while jobs still require them.
Unrecognized or newer installed Xcodes retain the existing conservative protection.
Failed installation/preparation or incomplete runtime discovery preserves the existing safety checks and does not authorize destructive cleanup.

App archives are cached on the host; platform and Metal downloads still run independently on each guest and can take longer than app extraction.
An HTTP 429/5xx response alone does not establish invalid credentials.
Inspect the sanitized phase error and, when authentication needs attention, use the normal interactive host login flow privately, then retry the failed operation.
Host archive pruning and shared simulator-download caching are not implemented by this policy.

Use a maintenance window with sufficient disk space for all selected apps, host/guest archives, runtimes and temporary extraction files.
Retaining another full Xcode increases download, disk and maintenance costs; the existing minimum-free-space checks do not constitute a complete capacity estimate.

## Verify the result

On each runner, inspect the installed apps and the inventory:

```sh
xcodes installed
./setup status --json
```

For each retained app, set `DEVELOPER_DIR` to its actual `Contents/Developer` directory and verify first-launch status, Swift, SDKs, simulator availability and Metal as applicable.
Check that the runner's recorded default matches the configured default and that maintenance succeeds on a second pass without removing pins or their required runtimes.
After approved guest or host restarts, wait for startup maintenance to finish before repeating these checks.
Record original VM/runner identities, each toolchain's exact build/path, both runtime families, the global and runner defaults, and the absence of pending maintenance recovery.
Use the [validation workflow](hosts.md#validate-the-runners) to exercise both Xcodes with their own compatible runtimes through real jobs.
Unit tests mock downloads, privileged preparation and cleanup; real installation and concurrent CI validation are separate acceptance checks.
An app installed outside the managed inventory can survive cleanup while its runtimes are removed.
Inventory such installations and plan their migration explicitly; app presence or successful compilation alone does not prove runtime retention.
