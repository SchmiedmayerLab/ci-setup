<!--
This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Set up runners

Run preparation in the local Terminal; macOS launchd can finish VM provisioning and run subsequent maintenance.

## Get the checkout on a fresh Mac

Apple's Git requires Command Line Tools before you can clone this repository.
If they are missing, run this in a normal local Terminal session:

```sh
xcode-select --install
```

Complete Apple's installation prompts.
If the command says installation was requested but no installer dialog appears, open System Settings → General → Software Update and look for **Command Line Tools for Xcode**.
Install that specific update if offered.
The request message alone does not mean installation has completed.
Verify the active developer directory and Git before continuing:

```sh
xcode-select -p
git --version
```

With Command Line Tools installed, the developer directory is normally `/Library/Developer/CommandLineTools`; a full Xcode installation can supply a different valid path.
Clone into a durable location outside Desktop, Documents and Downloads:

```sh
git clone https://github.com/SchmiedmayerLab/ci-setup.git ~/runner-setup
cd ~/runner-setup
```

For pull-request validation, check out the requested PR head and record and verify its exact revision before running setup.
Do not provision from a source archive or an uncommitted checkout; guest transfer requires committed Git history.
If an automation shell cannot display Apple's installer, use the local Terminal and System Settings rather than treating a zero exit status as successful installation.

## Before starting

Complete macOS Setup Assistant, device enrollment and system updates.
Log in as the account that will own the installation, with administrator access available.
Keep FileVault, SIP, Gatekeeper and your organization's security controls enabled.
Put the checkout in a durable location outside Desktop, Documents and Downloads so background jobs do not depend on protected-folder access.
Review `config.toml`: GitHub destination, runner group, toolchain and machine settings apply to runners.

You supply:

- The GitHub destination in `config.toml` and a PAT authorized to manage its runners.
  Use organization **Self-hosted runners: Read and write**, or repository **Administration: Read and write** for a repository runner.
  Complete any required organization approval first.
  **Actions: Read and write** is only needed for a separate workflow dispatch.
  Set an expiration date and renew before it expires.
- An Apple developer account and its two-factor authentication device for Xcode downloads.
  Complete any pending agreements.
  The Mac need not be signed into iCloud.
- For VMs, an Apple silicon host running macOS 27 or later, enough storage for the planned guests, and an internet connection.
  Setup checks the host OS; it does not upgrade or reboot an existing host.

Setup obtains and checks:

| Component | What setup does |
| --- | --- |
| Command Line Tools | Requests installation if missing; finish Apple's installer and rerun. |
| Homebrew and Python | Installs missing prerequisites. Homebrew may request administrator authorization. |
| UTM | Installs it if missing, verifies its signature and checks that the installed version supports provisioning. The supported minimum is 5.0.8. An older existing installation needs a separate update with its VMs stopped. |
| macOS restore image | Queries Apple's compatible-image API, downloads the IPSW, validates it on this host and pins its URL and checksum. No manual download is needed. |
| Guest macOS | Installs the selected image into each new VM. Setup checks that it supports automatic account creation; macOS 27+ is an installer constraint, not a guest you must prepare. |
| Guest tools and services | Installs Xcode, simulator platforms, Metal tools and runner software, then registers runners and configures startup. |

Stay logged in and unlocked for initial permissions and credential entry.
Keep the host awake and connected to power.
Setup checks resources before creating VMs; restore images, cached Xcode archives, simulator runtimes and jobs also consume host or guest disk space.

## Plan the interactive session

Run the initial guided command in a normal local Terminal that can display password prompts.
An automation shell can request an installer without displaying it, and its terminal may not be available for private input.
A person needs to remain available through prerequisite installation, permissions, credentials and plan confirmation.

| Interaction | What to expect |
| --- | --- |
| Command Line Tools | Complete the Apple installer or the specific Software Update entry, then verify `xcode-select -p` and `git --version`. |
| Homebrew and host power | Enter administrator authorization privately when requested; review the dedicated-host power plan and its restore command before accepting. Homebrew and power changes may prompt separately. |
| GitHub access | Enter the scoped PAT with hidden input; validation must establish access to the configured destination and permission to obtain a registration token. A stored token is not necessarily a valid token. |
| Apple access | Let xcodes handle sign-in and two-factor authentication; validate access afterward without treating a service outage as an incorrect password. |
| macOS consent | Finish UTM first launch and authorize Runner Host Local Network, UTM Automation and Keychain access while unlocked; preparation checks the shared background identity before handoff. |
| VM plan | Review the two names, destination/group, resource allocation and guest automatic-login/SSH settings before creation. |

The host administrator password is not a credential to save for later background use.
Host-only privileged preparation finishes interactively; guest provisioning uses each new guest's separate generated credential from the login Keychain.
Do not extend sudo privileges or keep the host administrator password alive across a long unattended installation.

Use `prepare host` to collect the initial prompts and hand off to the background worker; follow [the preparation guide](prepare.md).
The worker checks actual guest SSH after first boot and stops with a named phase if new permission or credential attention is needed.
`install host` remains a foreground alternative; keep its terminal open until it returns.

## Choose one layout

**Dedicated physical runner:**

```sh
./setup install runner --name 'GitHub Runner 1'
```

Omit `--name` to use the configured or saved name, falling back to the hostname.
This is intended for a dedicated machine: setup installs build tools, configures the runner, enables job cleanup and applies `config.toml` machine settings.
It asks for the local administrator password once before provisioning.
The password is held in memory for this invocation.
The provisioning child removes it from its environment before runner registration and service startup; it is not saved for future maintenance.

**Dedicated host with two runner VMs:**

```sh
./setup prepare host --dedicated-host
```

Omit `--dedicated-host` when an existing supported sleep-prevention policy should be preserved.
Preparation requests the initial prompts and starts a durable background worker after plan approval.
Use `./setup host progress` to inspect it and `./setup host resume` to continue after a failure.
The foreground alternative is `./setup install host`.

This creates `~/runner-host.toml`, downloads a compatible macOS image from Apple and chooses the two lowest unused `GitHub Runner N` names from GitHub and UTM.
To choose names, supply `--name` once or twice.
Names are not reserved remotely; do not provision multiple hosts against the same destination concurrently without assigning distinct names.

Defaults reserve one quarter of host RAM, rounded down to whole GiB and bounded between 8 and 16 GiB, then split the remaining memory between guests.
A 32 GiB Mac reserves 8 GiB and assigns 12 GiB per guest; a 64 GiB Mac reserves 16 GiB and assigns 24 GiB per guest.
Use `--reserve-memory-gib` to override this starting point for the host workload, and validate memory pressure and swap during concurrent builds.
Sparse disks are sized according to the host volume.
CPU allocation defaults to `--cpu-ratio 1.5`: subtract two cores for the baseline, multiply it by 1.5, then split the vCPUs between guests.
For an 18-core host this selects 12 vCPUs per guest; it does not reserve physical CPU cores for the host.
Use `--cpu-ratio 1` to disable oversubscription for a new layout; see [resource sizing](hosts.md#names-and-resources).
Existing layouts retain their saved resource allocation when rerun.

To use an existing image for a specific build, pass `--image /path/to/restore.ipsw`.
Setup checks it before creating guests.
This is an override, not a prerequisite.
Review the printed plan before proceeding.
Generated guest passwords are unique and stored in the host login Keychain.
Guests have automatic login and SSH enabled: someone with access to a guest console can use its logged-in session.
Host login, encryption and SSH settings are not changed.
On a dedicated VM host, `./setup install host --dedicated-host` also previews and requests confirmation for AC idle-sleep and supported power-failure restart settings before provisioning.
The [host checklist](hosts.md#host-checklist) covers power, restart recovery, permissions, updates and backups.

Finish UTM's first-launch dialogs when shown.
On a dedicated host, enable UTM's **Prevent system from sleeping when any VM is running** setting.
Without `--dedicated-host`, the scripts preserve the host's power policy and require either disabled AC system idle sleep or UTM's sleep-prevention preference before provisioning.
Closing a laptop lid can still suspend it.

## What happens next

1. Check the host platform and committed source, install missing prerequisites, verify the PAT and establish or reuse the Apple session.
   Before credential checks, host setup verifies sleep prevention; `--dedicated-host` first shows and confirms the supported AC power changes.
2. Run a temporary LaunchAgent to check background Keychain access and, for VM hosts, both UTM interfaces.
   Approve any system dialogs while unlocked.
   Failure stops before VM creation or runner installation.
3. Discover the compatible macOS build and show the VM names and resources.
   Confirm before downloading the image or creating VMs.
   A physical runner instead requests its administrator password before tool installation.
4. Download and verify the image, save its pin, install the guests and create their accounts.
   Install the newest released Xcode or Release Candidate, configured simulator platforms and Metal tools, then register runners and load services.
   The host downloads Xcode once and transfers verified archives to its guests.

After the initial inputs, commands run without terminal prompts.
A later external failure stops the affected work and reports the cause; see the limitations below.

Guided setup verifies each PAT entry by requesting and discarding a short-lived runner-registration token before Apple authentication or provisioning.
An HTTP 401 rejection prompts for a replacement during interactive setup, with at most three validation attempts per invocation.
Permission, organization-approval, network and locked-Keychain failures stop for inspection instead of repeatedly replacing the token.
`store-pat` alone stores a token; the guided installer performs the destination-permission check.

Apple access is checked before provisioning, immediately after an interactive sign-in, and again in a temporary background agent.
If access is unavailable, interactive setup offers Apple sign-in and permits up to three sign-in command attempts; decline or press Ctrl-C to stop.
xcodes handles private account, password and two-factor input, including its own invalid-password prompts.
If sign-in completes but the separate non-interactive check fails, setup stops and identifies that boundary instead of requesting the password again.
Background failures can involve the saved session, Keychain access or the network; they do not prove the credentials are invalid.
Background checks never prompt for credentials and must pass before guest provisioning.
Explicit HTTP 429/5xx responses, timeouts and tool-launch failures stop with an availability error rather than requesting another Apple sign-in.
An opaque xcodes error cannot reliably identify invalid credentials; setup keeps its raw authentication output out of captured diagnostics.

For prepared background installations, resume with `./setup host resume --config ~/runner-host.toml`; rerun `prepare host` only when access or reviewed source/settings need renewal.
For the foreground installer, rerun the same command to resume.
For an existing VM configuration, omit `--image` and `--name` and keep the same `--config` path:

```sh
./setup install host --config ~/runner-host.toml
```

Configuration and owned VM identities are preserved.
An ambiguous interrupted creation or first boot stops for inspection; setup never erases a guest to recover.
The host checkout must be clean and committed before transfer to guests.
Updating an existing guest to a different revision requires a reviewed fast-forward; setup refuses to reset or overwrite it.
Custom Homebrew casks can have arbitrary installers; guided setup rejects managed extra casks up front.
Install those separately and remove them from the managed cask list before using the guided path.

Check an existing installation's prerequisites and background access without provisioning (this still checks external credentials and creates a temporary agent):

```sh
./setup install host --check --config ~/runner-host.toml
./setup install runner --check
```

Host access checks also include the read-only host readiness report.
For a check that does not access credentials or start a temporary agent, use `./setup host check --config ~/runner-host.toml`.
These are access and configuration checks, not full runner health tests.
Use `./setup status` on a runner, `./setup host status` on a host, and the GitHub runner page for readiness.
The `host` subcommands remain available for individual steps and recovery.

## Completion checklist

Treat these as separate checkpoints rather than one success message:

1. **Prerequisites and access checked:** tools are installed, the saved layout is valid, and the credential/UTM background probe passes.
   This probe does not establish guest SSH access.
2. **Guests configured:** both owned VMs have the expected resources and names; each guest reports healthy runner status, selected Xcode, required platforms and Metal tools.
   `host status` reports VM ownership and installation state; `phase = ready` alone is not runner readiness.
3. **Background operation verified:** both installed host agents have completed successfully, including an actual Xcode refresh that reaches the guests from its LaunchAgent context.
   A loaded or still-running agent has not yet proved successful completion.
4. **Recovery verified:** coordinated guest restarts restore automatic login, SSH and the same runner identities; a coordinated host restart restores both guests after manual FileVault unlock and host login.
   Verify locked-session behavior separately from restart, logout and sleep.
5. **CI verified:** with approval for the exact workflow actions, run the validation workflow on each new runner, run both concurrently, and exercise cleanup and busy-job deferral through real jobs.
   SSH-only Swift, simulator and Metal checks remain useful but do not prove GitHub job integration.

Record the tested source revision, successful checkpoints and unresolved failures.
Do not mark installation fully validated while a background agent fails or required real-job checks remain untested.

## Troubleshooting by phase

| Symptom | Check or recovery step |
| --- | --- |
| `xcode-select --install` says installation was requested, but Git still fails | Finish the specific Command Line Tools installer or Software Update entry, then verify the developer directory and Git. The request message is not completion. |
| An administrator password prompt is invisible | Rerun the same documented command in the operator's normal local Terminal; never paste a password into chat, a log or command arguments. |
| PAT is rejected | Correct an explicitly invalid token privately. For a permission/approval failure, fix the destination permission or organization approval; for an availability failure, retry later without replacing the token blindly. |
| Apple sign-in succeeds but a background check fails | Separate foreground sign-in from background Keychain/session/network access. An HTTP 429/5xx response or timeout is an availability failure, not evidence of a wrong password. |
| Both VMs remain stopped after host login; startup reports another host operation and refresh reports a stopped guest | Inspect both agent logs before manual recovery. Refresh must wait for guest readiness outside the host lock; use the updated startup ordering described in the host guide, then repeat the coordinated restart. Do not delete lock or ownership files. |
| Background SSH reports “No route to host” while foreground SSH works | Confirm the owned guest is running and inspect Local Network access for Runner Host (or Python for legacy agents). Bare Python access failed in the fresh-host test; the shared Runner Host app passed the background guest check after consent; see the [host reference](hosts.md#apple-authentication-and-xcode-refresh). Do not disable protections to make the test pass. |
| SSH is unavailable immediately after guest first boot | Allow the guided SSH wait to finish and inspect first boot in UTM. A transient first-boot connection failure does not justify recreating the VM. |
| Provisioning is interrupted | Inspect `./setup host status --config ~/runner-host.toml`, then resume with the same configuration and names after checking the saved owned UUIDs. Do not delete state to bypass an error. |
| Refresh fails before guest maintenance starts | Inspect `~/Library/Application Support/runner-host/xcode-refresh.log` for the failing host phase; an older guest `converge.log` cannot explain a connection or archive-staging failure. |
| Tools are installed but a real workflow cannot be dispatched | Check that the workflow is published and available for dispatch, and that separately approved workflow credentials have the required repository access. Runner-registration access alone is not dispatch permission. |

Guest provisioning logs are under `~/Library/Application Support/runner-bootstrap/` inside each guest.
Use the failing phase's `bootstrap.log`, `privileged-setup.log`, `registration.log` or `converge.log` locally; do not publish raw authentication or registration output.
The VM-start agent logs to `~/Library/Application Support/runner-host/login.log` on the host.
Preserve the original failure and report only sanitized diagnostics when requesting help.

## Background services

Each service is a per-user macOS LaunchAgent.

| Where | LaunchAgent | When and purpose |
| --- | --- | --- |
| Host | `com.selfhosted-runner.provision.<configuration-id>` | After preparation; resumes an interrupted run at login, stops after completion or a failure needing attention |
| Host | `com.selfhosted-runner.virtual-machines` | At login, start owned VMs |
| Host | `com.selfhosted-runner.xcode-downloads` | At login and every six hours, check for newer Xcode, cache it, transfer it and run guest maintenance |
| Each runner | `com.selfhosted-runner.setup` by default | At login and every six hours, reconcile runner software and tools |

The guided host flow installs and loads the two host agents.
The refresh agent keeps Apple credentials on the host; guests receive Xcode archives, not the Apple account or PAT.
xcodes retains the Apple account/session using its native storage and Keychain integration, so successful sessions can be reused.
Guest maintenance uses the staged toolchain and defers while jobs are active.
Physical runners use their own Apple session.
Logs are under `~/Library/Application Support/runner-host/` on hosts and `~/Library/Logs/ci-runner-setup/` on runners.

## Limitations

| Boundary | What still needs attention |
| --- | --- |
| Host OS and existing UTM | Setup does not upgrade/reboot the host or replace an outdated UTM installation that may own other VMs. Update those through the normal administration process. |
| Initial consent | Device enrollment, installer authorization, UTM first launch, Automation and Keychain dialogs require a person. OS updates or changed app identities may require approval again. |
| Apple and GitHub access | Apple can request fresh 2FA or new agreements; PATs expire or lose approval. Setup cannot manufacture consent or credentials. Renew access and rerun. |
| Cold boot and logout | Per-user agents require a logged-in session. FileVault requires host disk unlock after a cold boot. Guest automatic login is configured, but host automatic login and encryption are unchanged. |
| Screen lock and sleep | Screen locking is supported with prior authorization and an accessible Keychain. Locking the Keychain, logging out, sleeping or closing a laptop lid can interrupt work. |
| Restore service and disk space | Apple must offer a compatible image. Downloads require network access and free storage. Failed partial downloads remain for inspection; they are never treated as usable images. `--image` can reuse a trusted local image. |
| Reproducibility | The restore URL and checksum are fixed in the saved layout. Later Xcode, Homebrew and runner versions follow the update policy, so separate runs need not produce identical toolchains. Compare inventories. |
| Guest replacement and source changes | Setup does not erase, adopt or overwrite existing VMs or dirty checkouts. Ambiguous interrupted state and source-version changes require review. |
| Names and capacity | Names are discovered, not reserved across hosts. Avoid concurrent name allocation against the same destination. CPU oversubscription shares real capacity; memory is never oversubscribed. |
| Custom installers | Guided setup rejects extra Homebrew casks because their installers may require later dialogs. Install them separately or use the individual maintenance commands. |

To renew Apple authentication, run `./setup host xcode-login` in Terminal on the machine downloading Xcode.
A physical runner may need another guided run when new Xcode terms require privileged acceptance.
For failures, inspect the named phase and logs; see the [maintenance guide](maintenance.md) for compatibility fixes.

The preflight is a current access check, not a guarantee that future macOS policy, Apple sessions or network conditions will remain unchanged.
See Apple's [Automation controls](https://support.apple.com/guide/mac-help/mchl108e1718/mac).
