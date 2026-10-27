<!--
This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Updating macOS on a VM host and its runners

Use Apple's Software Update in System Settings or `softwareupdate` over SSH to update an existing installation.
This is separate from updating the setup checkout, Homebrew packages, Xcode, or UTM.
The setup CLI does not currently provide an OS-update command or a persistent operator drain command.
Do not run `./setup update`, `converge`, or `install runner` on the VM host to update macOS.

## Plan the maintenance window

Record `sw_vers`, the setup revision, both saved VM UUIDs, and the original GitHub runner IDs before starting.
Verify the normal secure backup and console-recovery arrangements; setup does not create a VM snapshot or backup for an OS update.
Review host/guest compatibility and organization management policy, and approve guest restarts separately from any host restart.
An Apple silicon update can require a volume-owner credential as well as sudo authorization; SSH access and a stored GitHub PAT do not provide that authorization.
See Apple's [software update process](https://support.apple.com/guide/deployment/software-update-process-dep02c211f3e/web) and the installed `man softwareupdate` for this macOS release.

Check the updates actually offered on each machine:

```sh
sw_vers
softwareupdate --list --os-only
df -h /
```

Choose an explicit label from that machine's output rather than using `--all`, a remembered version, or a beta catalog.
Keep the source checkout and saved VM layout unchanged during the OS update.
Updating an installed guest does not change the pinned restore image: a future rebuild still uses that saved image.
Changing the base image is a separate reviewed layout/rebuild operation; do not edit owned state to make it resemble the installed OS.

## Drain jobs before changing the OS

Use the destination's established scheduling controls to prevent new assignments to these runners, and let active jobs finish.
Verify that the chosen gate actually excludes these runners from normal workflow assignments, including during login recovery.
The setup CLI does not supply that persistent gate; if none is available, stop at download-only preparation and plan the maintenance procedure before installing.
Apply that gate before starting updates and retain it through reboots, health checks, and any approved validation jobs.
An idle GitHub status is a point-in-time observation, not a drain mechanism.
Do not cancel a worker or stop its VM to obtain an idle state.

For local automation, reuse the existing per-user setup lock and maintenance guard when changing runner services.
The guard checks for active workers and defers ambiguous or busy state; an overlapping maintenance operation is a reason to wait, not to delete a lock file.
A persistent external scheduling gate is still needed across reboot, when per-user processes and their locks disappear and login agents can restore runner intake.
Do not turn the internal `maintenance.paused` context into an unreviewed public shutdown or update command.
In particular, a context that ends when `softwareupdate` returns can restore intake while Apple's background update operation is still running.

## Update the guests over SSH

Update only the configured names and saved owned UUIDs.
Use the established SSH connection and UUID-based host-key verification; do not disable verification or print a Keychain password to obtain access.
Update guests sequentially when one must remain available.
When simultaneous downtime is approved, their downloads and installations can run concurrently if shared CPU, memory, disk and network capacity permit.
Use an independent lock, busy check and result for each guest, and preserve partial failures rather than reporting success for the group.
Parallel operation reduces serialization but does not guarantee twice the throughput; progress can differ substantially between guests.

The following commands belong in an operator's interactive SSH session on each drained guest, after reviewing the offered label:

```sh
update_label='EXACT_LABEL_FROM_THIS_GUEST'
sudo softwareupdate --install "$update_label" --restart --user "$(id -un)"
```

`--restart` authorizes that guest's restart; never reuse this invocation on the host before host-restart approval.
Keep the session open until the command returns or SSH disconnects for reboot.
For automation that already manages guest credentials, Apple supports `--stdinpass` on Apple silicon.
Supply credentials only through private input to the intended process; keep them out of argv, shell history, files, captured output and chat.
No OS-update orchestration helper is shipped here, so a one-off SSH script requires review of draining, ownership, credential transport, interruption handling and post-reboot recovery.

## Monitor preparation and reboot separately

Treat these as distinct states: offered, downloading, preparing, ready for restart, rebooting, and verified on the target build.
Apple's download percentage can remain near completion while update workers prepare the installation.
Worker CPU activity is supporting evidence that work continues; neither an active process nor an unchanged percentage proves success or failure.
CLI output redirected to a file can also lag because of buffering.

In the observed 27.0.1 to 27.2 update, guest CLI commands returned zero while Apple background work continued and `sw_vers` still reported the old build.
A second query could report that another process was performing an operation.
Wait for the existing operation and inspect its phase instead of launching competing installers or treating the exit code as a completed OS update.
SSH loss during an approved guest restart is expected; inspect the console and wait for SSH recovery rather than power-cycling the VM.
An Apple-logo progress screen is an installation phase, not evidence that the guest has failed.

Use the operator Terminal, Software Update status, and narrowly filtered `/var/log/install.log` entries for diagnosis.
Preserve timestamps and fixed error categories; do not publish complete logs or authentication context.
Avoid high-frequency repeated scans, blind retries, forced VM stops, or termination of Apple update workers.

## Prepare and update the host

Host download-only staging can overlap guest updates, subject to shared capacity:

```sh
update_label='EXACT_LABEL_FROM_THIS_HOST'
sudo softwareupdate --download "$update_label"
```

If the operator has not approved host installation/restart, stop at download-only staging.
Omitting `--restart` from an install command does not establish a persistent hold against independent management or automatic-update policy.
Agree on those policies and the exact installation/restart boundary before advancing.

Once guest updates are verified, keep or reapply the scheduling gate and wait for jobs and maintenance to finish before shutting the guests down.
For an approved host installation, acquire the existing host orchestration lock, resolve unrelated workloads with their owner, and verify both owned VMs are stopped before starting the installer.
An SSH disconnect or accepted shutdown request does not prove the VM stopped; inspect UTM state for each saved UUID.
If any guest is installing, recovering, busy or deferred, withhold host installation.
Keep the host lock while a coordinator runs; at a GUI handoff, stop competing operations and recheck the stopped state before installation.
Prefer guest `sudo shutdown -h now` through the guarded administration path, or UTM's graceful shutdown request, then verify the actual stopped state.
With `utmctl`, verify the installed help and use `stop --request OWNED_UUID` for a graceful request; the default `stop` action forces power off.
Never use `--force` or `--kill` as the normal update procedure.
Downloading an update alone does not require shutting down otherwise healthy guests for the entire approval wait.

For an operator-approved installation with a separately controlled restart, use the operator's normal local Terminal:

```sh
sudo softwareupdate --install "$update_label" --user "$(id -un)"
```

Enter the host password privately when requested; approval to install is separate from administrator authentication.
Do not assume a prior sudo authorization or authentication on a guest applies to the host.
Inspect the command result and Software Update status before advancing.
In the observed host update, Apple's log reported that the update was already prepared and System Settings offered **Restart Now**.
That establishes readiness for the restart step, not that the target OS is already running.

After explicit host-restart approval, use **Restart Now** in Software Update to finish the prepared update.
Another password prompt can appear even after successful CLI authorization; let the operator complete it privately.
Do not replace the update-specific restart with an unverified generic reboot or weaken authentication to bypass a prompt.
Plan for the administration session to disconnect and for manual FileVault unlock and host login afterward.

## When Terminal blocks the host restart

A foreground updater can leave Terminal waiting for confirmation to terminate its processes, preventing the requested restart.
The command's `Restarting...` message is not evidence of a reboot.
If macOS reports an app-blocked restart, first verify that both owned guests are stopped and the updater has reached its restart handoff.
Inspect the specific Terminal window and processes, and save unrelated work.
If that window belongs only to the reviewed update handoff, the operator can confirm **Terminate**, recheck the stopped guests, then retry through Software Update's **Install Now** or **Restart Now** interface.
Do not use this recovery during active download/installation, terminate unrelated processes, or force a power-off.
Preserve any new error and verify the OS build after the actual reboot.

Closing the preparation Terminal after launchd takes over provisioning is a different lifecycle from closing a foreground OS updater.
Future automation needs an explicit durable restart handoff; the current setup CLI does not provide one.
A missing status receipt does not establish that a password was never entered: inspect the recorded phase and the operator's visible prompt without capturing credentials.

## Verify recovery before restoring scheduling

After host login, inspect the automatic recovery before manually starting anything:

```sh
sw_vers
./setup host check --config ~/runner-host.toml
./setup host status --config ~/runner-host.toml
```

`host check` verifies host settings and agents, while `host status` checks VM ownership/state; neither establishes remote runner connectivity.
Confirm both owned VMs started, SSH works, and both host agents completed successfully, including a refresh that reached the guests.
Inside each guest, inspect the service, latest phase results, and toolchain:

```sh
sw_vers
./setup status --json
xcodebuild -checkFirstLaunchStatus
xcrun swift --version
xcodebuild -showsdks
xcrun --find metal
xcrun simctl list runtimes
```

Record the actual OS build and changed boot identity, original VM UUID and runner ID, clean setup revision, runner version, selected Xcode, available runtimes and any inventory errors.
Repeat first-launch, SDK, Swift, Metal and simulator checks with the explicit `DEVELOPER_DIR` of every [retained Xcode](xcode-versions.md), including pins that are not the default.
After startup maintenance and cleanup complete, confirm both toolchains and their required runtime families remain, the global/runner defaults agree, and no recovery marker remains pending.
A loaded agent, a running local listener, successful maintenance and an online GitHub registration are separate observations.
An OS update can invalidate Xcode first-launch readiness even when Xcode itself has not changed; follow the [Xcode recovery procedure](#xcode-first-launch-after-an-os-update).
Login maintenance can temporarily stop a listener, or defer with exit code 2 while another maintenance run or a job owns the machine.
Check the newest phase results and let the responsible operation finish instead of repeatedly restarting the listener or agent.

Check GitHub online status separately after local maintenance completes.
Allow a bounded reconnection interval, such as five minutes with checks every 30 seconds.
If a runner remains offline, inspect sanitized listener/connectivity diagnostics and the current maintenance state without replacing its registration.
Before restoring scheduling, run an explicitly approved validation job on each runner if available.
Report service/toolchain checks separately from Swift builds, simulator boots, Metal compute and cleanup exercised through real CI.
An unavailable or undispatched workflow remains untested; online status is not a substitute.

## Xcode first-launch after an OS update

First inspect the running maintenance phase and the affected Xcode's `-checkFirstLaunchStatus` with its explicit `DEVELOPER_DIR`.
Incomplete readiness after an OS change does not imply a broken registration or require reinstalling Xcode.
Current maintenance invokes its authorized first-launch path directly and rechecks readiness before component downloads or cleanup.
Scheduled maintenance uses the existing passwordless helper with temporary selection and restoration; host-driven bootstrap uses its private guest credential, and interactive runs use normal administrator authorization.
The command remains bounded, and denied authorization, an incomplete recheck or a restoration failure is reported for inspection.
No broader sudo permission or general process-termination recovery is installed.

An older deployed revision can still be waiting in an unprivileged first-launch process on a guest authorization dialog.
Establish the exact child, owning maintenance process and visible authorization request before intervening.
The operator can complete that request privately, or arrange targeted cancellation under supervision.
Do not kill a runner worker, the maintenance parent, an active package installer or arbitrary processes by name.
Do not launch a competing first-launch command while another maintenance pass owns the lock.
After recovery, require completed maintenance, successful readiness for every retained Xcode, restored service and original registration connectivity.

## Record results separately

Keep sanitized per-target before/after evidence outside the repository.
Record the exact source revision and any local differences separately from the installed macOS, Xcode and runtime builds.
Report download completion, verified OS installation, guest restart recovery, host restart recovery, toolchain readiness and real CI acceptance as separate results.
Preserve shutdown-time service-reload errors and independently verify post-login recovery rather than clearing markers to hide the error.
A successful historical update of another machine or source variant does not validate this deployment.
No unattended OS-update coordinator, automatic FileVault unlock, physical-runner update acceptance or universal macOS-release compatibility is implied by this runbook.
