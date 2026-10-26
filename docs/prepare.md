<!--
This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Prepare once, then provision in the background

Use this flow for an Apple silicon host with runner VMs.
Physical runners retain `./setup install runner` and their separate privilege model.

## Start in the local Terminal

On a fresh Mac, [install and verify Command Line Tools, then clone the checkout](setup.md#get-the-checkout-on-a-fresh-mac).
Use a stable location such as `~/runner-setup`, outside Desktop, Documents and Downloads.
Review `config.toml`, including the destination organization and runner group.
Development changes must be reviewed and committed before preparation because only clean committed source is transferred into guests.

On a dedicated VM host, run:

```sh
cd ~/runner-setup
./setup prepare host --dedicated-host
```

Omit `--dedicated-host` to preserve existing power settings; preparation then requires an already configured supported sleep-prevention policy.
Do not run the command with sudo.
Keep your administrator password, scoped GitHub PAT and Apple two-factor device available.

## Finish the prompts before handoff

Preparation performs these steps while you are present:

1. Install missing host dependencies and complete administrator authorization.
2. Review dedicated-host sleep/power changes, apply only accepted changes and verify them.
3. Enter or reuse the GitHub token, immediately checking the configured destination and registration permission.
4. Establish or reuse the Apple session and check it without interactive input.
5. Build the small, locally signed **Runner Host** application and request Local Network consent before any VM download.
   The permission probe connects a UDP socket but sends no network traffic.
   Allow **Runner Host** to access the local network and control **UTM** when macOS asks; complete any Keychain consent.
6. Check the same background app identity used for installation and later maintenance.
7. Review VM names, resources, destination/group and guest automatic-login/SSH settings.
   An unchanged approved plan is reused on retries.
8. Save the preparation record and start the background installer.

Homebrew, system authorization and Apple two-factor authentication may still show separate prompts within this one session.
The host administrator password is not saved or retained for the worker.
The PAT and Apple account remain in their native host credential stores; guests receive their individual generated credentials and short-lived registration tokens.

The launcher stays alive while Python runs, so macOS attributes background UTM and network requests to one app rather than different Python entry points.
The same launcher is used for the credential probe, provisioning worker, VM-start agent and Xcode-refresh agent.
It uses normal macOS consent and no root daemon, privacy exemption or broader sudo rule.
Its executable, configuration and interpreter binding are checked before use.
When reviewed source or Python changes require rebuilding it, the previous generated bundle is retained and macOS may request consent again.

After **Background provisioning started**, the preparation Terminal can be closed.
Keep the host awake and logged in.
A screen lock is different from logout or sleep; cold boot still requires manual FileVault unlock and host login.

For preparation without starting the worker, use `--no-start`; an existing login agent will also leave this prepared state idle until an explicit resume:

```sh
./setup prepare host --dedicated-host --no-start
./setup host resume --config ~/runner-host.toml
```

### When validation requires a cancellation test

Run `./setup prepare host --dedicated-host --no-start` and complete prerequisite prompts, then answer **n** to the first **Apply these power settings to this dedicated VM host?** question.
Retain the preview and restore command before answering.
Verify the cancellation message, unchanged values with `pmset -g custom`, and absence of new VMs or a provisioning worker before rerunning the same preparation command.
On the second attempt, review and accept the power plan, then finish credentials, permissions and VM-plan approval in that same operator session.
Installed prerequisites remain available after cancellation.
If the power settings already match, that confirmation is not presented; record the real cancellation case as not tested and use the mocked tests instead of changing settings merely to recreate it.
If the first plan was accepted, preserve the approved state and report cancellation as not tested rather than repeating preparation blindly.

## Follow progress and recover

```sh
./setup host progress --config ~/runner-host.toml
./setup host progress --config ~/runner-host.toml --json
./setup host resume --config ~/runner-host.toml
```

Progress reads only local state and does not access credentials, start UTM or change settings.
It shows the current phase, last completed phase, attempt count, heartbeat, recovery instruction and log location.
The heartbeat means the worker is alive; it is not a download percentage or proof that an external tool is making progress.
The log and preparation/progress records are private files under `~/Library/Application Support/runner-host/`, named using the saved configuration's identifier.

| State | Meaning |
| --- | --- |
| `prepared` | Interactive checks and plan review finished; no worker was requested yet. |
| `queued` | The approved worker has been requested from launchd. |
| `running` | The worker is active; inspect its phase and heartbeat. |
| `interrupted_or_unresponsive` | No recent worker heartbeat; inspect the agent and owned VM state before resuming. |
| `deferred` | Host/guest maintenance is busy or guest readiness timed out; inspect service logs, let active work finish, then resume. |
| `needs_attention` | A phase failed; follow the recorded instruction and explicitly resume after correcting it. |
| `cancelled` | An operator interruption/cancellation requires explicit continuation. |
| `configured` | Guest configuration, host agent execution and local runner health checks passed; real CI and recovery validation remain separate. |

The worker has no interactive stdin or controlling terminal for host commands.
It cannot accept a hidden late password prompt.
A newly required permission, expired session, failure or ambiguous ownership state stops with **needs attention** instead of silently declaring completion.
On login, the provisioning agent can resume an interrupted run; it does not automatically loop on a failure, cancellation or completed installation.
`host resume` never kills an already running worker.
An unchanged plan does not require repeating plan approval, but changed source, interpreter, settings or configuration requires `prepare host` again.

The installer verifies first-boot guest SSH from its actual background context before configuration.
This final connection check is necessary even after the upfront permission probe: the guest does not exist during initial preparation, and permissions or networking can change later.
Resolve a missing **Runner Host** grant in System Settings → Privacy & Security while unlocked, then resume.
Do not disable security controls or recreate a VM to work around an access error.
Foreground diagnostics from Terminal, an editor or another automation app can have different Local Network attribution from Runner Host.
A successful background guest check does not grant those other processes access, and a foreground failure does not establish that the background installer failed.
If supplementary diagnostics need another app, exercise that app's actual guest connection while the operator is still available after first boot.
Observe the failure and normal consent prompt before attributing it to a permission; “No route to host” alone is not a diagnosis.
Native UI inspection may require manual unlock, even while background maintenance continues successfully with the screen locked.
Keep local progress and agent-result checks available independently of UI automation, and record any monitoring gap rather than claiming continuous observation.

For an automation environment that deliberately owns the foreground process, the same approved worker is also available as:

```sh
./setup install host --prepared --config ~/runner-host.toml
```

That command cannot change the reviewed plan and will never collect credentials interactively.
Prefer `host resume` for a durable launchd handoff.
The older `install host` entry point remains available for a foreground guided installation; keep its terminal open until it returns.

## Hand off to an operator or automation agent

After preparation reports that background provisioning started, give the agent the permanent checkout and configuration paths.
The launchd worker performs the installation independently of the agent's chat or terminal session.
An agent can read `host progress --json`, inspect the named phase if it stops, and request `host resume` once the recorded issue is resolved.
Do not run plain `./setup`, `converge`, `update` or `install runner` on the VM host.
Do not delete state, recreate VMs, alter the approved plan or restart a running worker as a retry strategy.
If source, settings or permissions need to change, return to the preparation session with the operator.

Use this handoff instruction, substituting the actual paths:

```text
Monitor the prepared VM host in ~/runner-setup using ~/runner-host.toml.
Read AGENTS.md and docs/prepare.md first.
Use ./setup host progress --config ~/runner-host.toml --json.
Preserve the approved plan, saved ownership, active jobs and registrations.
After resolving a reported failure, use ./setup host resume with the same config.
Report configured separately from completed recovery and real GitHub job validation.
Return credential or macOS consent requests to the operator; never request secrets in chat.
```

## What retries preserve

The plan fixes resource choices and image selection before long downloads.
The worker verifies and pins the actual restore image before VM creation; a pending plan is not a completed image receipt.
It reuses the existing ownership state, verified downloads, VM UUIDs, registrations and guest maintenance locks.
It never adopts or replaces another VM or clears state to bypass an error.

The worker can migrate an exact idle legacy host-agent definition to the shared launcher after guest configuration, retaining its previous plist.
Preparation with `--no-start` leaves the existing startup and refresh services loaded.
A running, modified or unrelated agent is left intact and reported for inspection.
After configuration, both installed host agents must finish successfully; being loaded or currently running is not sufficient.
At login, refresh waits for guest SSH outside the host lock so it cannot block VM startup; readiness timeout or lock contention is reported as deferred rather than successful maintenance.

Complete [the acceptance checklist](setup.md#completion-checklist), including real GitHub jobs and coordinated restart tests, before treating the installation as fully validated.
