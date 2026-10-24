<!--
This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# A Mac host with two runner VMs

Use one Apple silicon Mac as a host and put one runner in each of two macOS VMs.
Each runner gets its own disk, machine identity, account, credentials, and build tools.
Rebuild a guest from the same restore image and configuration when it needs a clean start.
A dedicated physical Mac can still run the regular `./setup install runner`; the VM layer is optional.

Host preparation does not run runner maintenance on the host or change its FileVault, SIP, firewall, remote access or login settings.
System power settings are preserved unless you explicitly choose and confirm the dedicated-host option below.
`host configure` installs tools and registers runners inside the owned guests.
Do not run plain `./setup` on a host that also serves as a workstation.

## Guided setup

For a new installation, follow the [setup guide](setup.md) and run `./setup install host`.
It performs the preparation, creation, guest configuration and agent installation described below in one flow.
Use the individual commands in this reference for inspection or recovery.

## Prepare the host

1. Complete the normal macOS setup and your organization's device enrollment.
   Install system updates through your normal management process.
   Keep host disk encryption and endpoint security enabled.
2. Put this checkout on the host.
   Install Command Line Tools if macOS requests them, then run:

   ```sh
   ./setup host bootstrap
   ```

   This installs missing Homebrew/Python prerequisites and UTM.
   It verifies UTM's signature and Gatekeeper assessment.
   Existing UTM installations are retained; update UTM separately while all VMs are shut down.
   The VM scripts require UTM 5.0.8 or later.
   Bootstrap does not create or start guests.
3. Open UTM once to finish its first-launch screens.
   In UTM Settings → Application, enable **Prevent system from sleeping when any VM is running** on a dedicated host.
   This is a runtime sleep assertion, not a system-wide power policy.
   Alternatively, use the dedicated-host power option below to disable AC system idle sleep.
   Keep the host connected to power; closing a laptop lid can still suspend it.
4. Create the host configuration below.
   Setup downloads the restore image automatically.

## Automatic macOS image download

`./setup install host` and `host init` download the latest image supported by this Mac when `--image` is omitted.
UTM's wizard has an automatic downloader, but its scripting interface requires a local IPSW.
Setup uses Apple's [restore-image API](https://developer.apple.com/documentation/virtualization/vzmacosrestoreimage) to discover and inspect the image without driving the wizard.

The small Swift helper is compiled from this checkout and locally signed with Apple's required virtualization entitlement.
It only reads image metadata; it does not create VMs or change system permissions.
Command Line Tools supply the compiler.
The helper checks hardware compatibility, guest OS version and minimum CPU/memory requirements before installation.

Images and receipts are cached under `~/Library/Application Support/runner-host/restore-images/`.
The generated host configuration records the local path, Apple HTTPS URL and SHA-256 checksum.
Existing layouts keep their pin even when Apple publishes a newer image.
A missing pinned file can be downloaded again from its saved URL; a checksum mismatch stops setup.
An unrecorded or damaged cache entry is never silently accepted or repinned.

For a specific build or a previously downloaded image, use `--image /path/to/restore.ipsw`.
Obtain overrides from a trusted Apple source.
A locally computed checksum detects later changes; it does not establish the origin of arbitrary bytes.
Apple validates the installation media during restore.

The pin reproduces the base installation, not identical VM identities or fixed versions of subsequently downloaded tools.
Preserve the image and host configuration for rebuilds; old Apple download URLs may eventually become unavailable.

## Host readiness and power settings

Inspect an existing host without starting UTM, accessing credentials, installing agents or changing settings:

```sh
./setup host check --config ~/runner-host.toml
```

The check reads AC power settings, the saved host configuration and both startup-agent definitions and loaded commands.
It accepts either disabled AC system idle sleep or UTM's configured sleep-prevention preference.
The UTM preference check is not a test of an active sleep assertion.
It reports missing, disabled, stale or failed agents and checks that the Xcode refresh interval is six hours.
Exit code `1` means something needs attention; `0` means these checks passed, possibly with an informational hardware limitation.
A successful check does not prove guest health, credential access or recovery from a cold boot.
Use `./setup install host --check --config ~/runner-host.toml` for the separate background access and layout checks as well; that command accesses credentials and creates a temporary LaunchAgent.

For a dedicated host, preview the supported power changes:

```sh
./setup host power
```

To apply them, use an interactive terminal:

```sh
./setup host power --dedicated-host
```

The command shows the current and proposed values and an exact command to restore them before asking for confirmation.
It disables system idle sleep on AC power and enables restart after power failure if `pmset` reports support.
It applies only changed values with `sudo pmset -c`, then reads them back and fails if they do not match.
Battery and UPS settings, display sleep, screen locking, automatic login and encryption are unchanged.
Unsupported power-recovery settings are reported and left alone.
If authorization, application or verification fails, inspect `pmset -g custom`; a partial change is possible, and the printed restore command lets you recover the previous values.

For a new dedicated host, include the same option in the guided entry point:

```sh
./setup install host --dedicated-host
```

Power review and administrator authorization occur before GitHub and Apple authentication, restore-image downloads and guest provisioning.
If you decline, installation stops without applying power changes or creating guests.
Without this option, guided setup checks sleep prevention and stops early if neither AC policy nor the UTM preference confirms it.
The final readiness check also reports power-recovery and agent problems after installation.
These settings are not enforced by a recurring privileged service; rerun the read-only check after host changes.

## Host checklist

| Item | Setup coverage and operator responsibility |
| --- | --- |
| Enrollment and security | Complete device enrollment and macOS updates first; retain FileVault, SIP, firewall and endpoint controls. Setup does not alter them. |
| Power and sleep | Use a reliable power connection and either UTM's sleep-prevention preference or the reviewed dedicated-host option. Display sleep and screen locking can stay enabled. Laptop lid closure, explicit sleep and loss of power can still interrupt VMs. |
| Power-failure recovery | The dedicated-host option enables supported restart behavior. This starts the Mac; it does not unlock FileVault or log in the host user. |
| Startup | Guided setup installs and loads the VM-start and Xcode-refresh agents for the host user. Confirm them with `host check`; this replaces separate Dock login items or launcher applications. |
| Permissions and credentials | Complete UTM first launch, Automation and Keychain consent while unlocked. Guided setup checks background access. Renew expired PATs and Apple authentication when required; keep secrets out of logs and configuration. |
| Login and restart recovery | Keep the host user logged in; locking the screen is supported. After a cold boot, unlock FileVault and log in, then check host readiness, guest startup and runner availability. Test this during a planned maintenance window. |
| Host and UTM updates | Pause new jobs through the runner group's normal administration process, wait for active jobs and maintenance to finish, then shut down owned guests before updating or rebooting. After login, check both guests and run a validation job before restoring normal scheduling. Setup does not orchestrate host updates or restart crashed VMs. |
| Configuration and backups | Keep the checkout and its interpreter at stable paths outside protected user folders. Back up the saved layout, ownership state and credentials through the normal secure backup process. A layout alone cannot recover passwords or irreplaceable guest files; setup does not create backups. |

Host SSH and Screen Sharing are optional administration choices and are not enabled by setup.
Never run physical-runner setup on the host to obtain these host settings.

## Names and resources

Set the destination in `config.toml` first.
With `--discover-names`, setup reads the token from the host login Keychain.
If no token is stored, an interactive terminal prompts for hidden input before creating the configuration or any VMs.
For unattended runs, store it beforehand with `./setup store-pat`.
A locked or inaccessible Keychain stops setup without replacing its credential.

Use a fine-grained token with the destination as its resource owner:

| Destination | Required permission | Repository selection |
| --- | --- | --- |
| Organization runners | Organization **Self-hosted runners: Read and write** | No repository permissions needed |
| Repository runners | Repository **Administration: Read and write** | Only the destination repository |

Your account must have administrative access to that destination.
If the organization requires token approval, approval must be complete before setup.
Choose an expiration date and renew with `./setup store-pat` before it expires.
Setup checks list access and obtains a short-lived registration token to verify write access; that check does not register or remove a runner.
See GitHub's [runner API permissions](https://docs.github.com/en/rest/actions/self-hosted-runners) and [token approval requirements](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

Dispatching an existing validation workflow separately requires repository **Actions: Read and write**, limited to its repository.
It is optional for installation and runner operation; builds use their own workflow credentials.
See [workflow dispatch permissions](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event).

Generate a configuration outside the checkout:

```sh
./setup host init --config ~/runner-host.toml --discover-names \
  --automatic-login --remote-login
```

Name discovery uses the destination in `config.toml`, reads every page of its runner list, and considers local UTM names too.
It selects the two lowest unused positive numbers in `GitHub Runner N`: existing 1 and 3 produce 2 and 4.
The VM and runner display name retain spaces; the macOS hostname is `github-runner-n`.
Discovery never guesses after an authorization or network failure.
For explicit names, replace `--discover-names` with two `--name` arguments.
Names are a point-in-time selection, not a reservation across hosts; provision one host at a time and check for collisions before registration.

`init` obtains the image, pins its checksum and writes fixed resource values for review.
It never overwrites a configuration or creates VMs.
For two guests it:

- Divides RAM after reserving 8 GiB for the host.
  Use `--reserve-memory-gib 16` on a workstation that also runs other applications.
- Divides all but two host CPU cores.
  `--cpu-ratio 1.5` allocates 50% more vCPUs and records explicit CPU oversubscription in the configuration.
  On an 18-core host this gives 12 vCPUs per guest instead of 8.
  A busy guest can use more cores when the other is idle; simultaneous builds share the same real CPU capacity and may suffer additional contention.
  Measure both build latency and combined throughput before increasing it further.
- Rounds disk capacity down in 128 GiB steps, capped at 512 GiB per guest.
  It reserves 128 GiB of host disk capacity and at least 64 GiB of current free space.
  A mostly empty 1 TB host gets 384 GiB per guest; 2 TB or larger gets 512 GiB.
  It refuses a layout with less than 256 GiB per guest.

RAM is never oversubscribed.
CPU counts do not pin particular physical cores or guarantee host CPU headroom.
These checks do not account for the working sets of unrelated applications.
Keep UTM storage on the home volume for the disk capacity check to apply.
The manual alternative is `host.example.toml`; choose names and resource values before creating guests.
Creation and startup refuse to allocate more guests while an unrelated UTM VM is active.
Review that workload's capacity separately; the script never stops it.

## Create the guests

Review the plan, then create the VMs:

```sh
./setup host plan --config ~/runner-host.toml
./setup host create --config ~/runner-host.toml --provision --keychain-passwords
./setup host status --config ~/runner-host.toml
```

`plan` reads configuration and host capacity without launching UTM or writing state.
`create` checks the image checksum, creates independent Apple Virtualization VMs with shared/NAT networking and no shared host directories, installs macOS, and boots each guest.
It never installs onto a preexisting VM.
Allow the normal macOS Automation prompt for your terminal to control UTM.
Shared networking is not a security boundary against the host or local network; use trusted workflows and your organization's network policy.

The command above requires macOS 27 or later **on both host and guest**.
It creates the initial account with the automatic-login and SSH settings chosen in `host init`.
To complete Setup Assistant manually instead, omit `--provision` and `--keychain-passwords` on the first creation.
These flags cannot provision an account after that first boot has already happened.

With `--keychain-passwords`, each VM gets a different cryptographically random password.
The host stores it in its login Keychain under `com.selfhosted-runner.vm.<UUID>`, with the guest username as the account.
Only that non-secret item identifier is recorded in state.
Passwords travel to UTM through stdin, never through command arguments, configuration, or logs.
An interrupted provisioning run reuses the same Keychain item rather than changing the password.
Keep these items in your normal credential backup.

The system `security` executable is trusted to retrieve these items for local automation.
This means processes running as your host user can request them; it is not a separate security boundary between applications.
Existing Keychain lock settings remain in effect.
If it is locked or access is denied, the command stops; unlock it and approve access once before unattended provisioning.
Locking the screen and locking the Keychain are distinct operations.
Use Keychain Access to retrieve a guest password privately for console recovery.

Without `--keychain-passwords`, the command asks for a guest password without echoing or saving it.
To supply one from another secret manager, pipe it and add `--password-stdin`.
Never put a password in command arguments or commit it.
`guest.automatic_login` and `guest.remote_login` are explicit opt-ins and affect only new guests.
Set them before creation if wanted.
Without provisioning, complete Setup Assistant in each guest.
Earlier macOS versions need this interactive account setup.
Never copy an already registered runner or a guest containing credentials to make the second runner.

For runners that must return after a guest restart without manual login, set `guest.automatic_login = true` **before creation** and use per-VM Keychain passwords (or supply a password privately).
Host-driven provisioning also requires `guest.remote_login = true` for SSH.
These first-boot options do not change accounts in an existing VM; configure those guests through macOS settings instead.
Automatic login exposes the guest desktop to anyone who can open its console.

The scripts keep an owner-only state file under `~/Library/Application Support/runner-host/`, keyed by the absolute configuration path.
Keep that file and the configuration together in backups.
Changing the configuration or moving it to a new path does not authorize adopting existing VMs.
A second invocation leaves completed guests alone.
If installation is still running in the same UTM session, it resumes waiting for that installation.

If creation or first boot was interrupted, or UTM lost its installation progress when it restarted, the command stops for inspection.
Check the guest in UTM and the saved state before recovery; do not delete state just to retry an installation.
No command deletes, reinstalls, restores, or force-stops a VM.

## Configure each runner

VM creation alone does not install or register a runner.
The host state's `ready` phase records completion of installation and the first boot request; it does not verify the guest account, automatic login, tools, or runner service.

For provisioned guests with automatic login, SSH, and Keychain passwords, first complete [Apple login on the host](#apple-authentication-and-xcode-refresh).
Then run from a clean, committed checkout on the host:

```sh
./setup host configure --config ~/runner-host.toml
```

This sets each guest's computer name and hostname, installs Command Line Tools, Homebrew and Python, transfers the committed setup revision, and performs the initial privileged runner configuration.
It obtains a short-lived registration token using the host's long-lived PAT, registers each guest with its selected name, and runs the normal tool and service setup.
The PAT never enters a guest.
The guest keeps its runner name outside the checkout and also uses that name as a custom label, so a validation workflow can target each runner explicitly.

SSH uses the individual guest password through an askpass helper.
It does not forward the host SSH agent, read the user's SSH configuration, or share host folders.
Host keys are remembered by VM UUID; a changed key stops the connection.
The administrator password is supplied transiently for host-driven setup and Xcode refresh.
It allows Xcode license acceptance and first-launch initialization without a prompt.
The provisioning process keeps it in memory.
Its maintenance child removes the credential from its environment before configuring or starting the runner, and the parent removes its askpass helper when provisioning finishes.
The guest's own scheduled maintenance cannot retrieve the host credential or prompt for sudo.

The transfer uses a Git bundle containing only committed source and history.
The guest checkout is `~/runner-setup`, with its pinned bundle retained under `~/Library/Application Support/runner-bootstrap/`.
It can run before changes are published.
Reruns accept the same clean revision; a different or modified guest checkout requires inspection.
For ongoing source updates after publication, configure the guest's upstream to your repository and intended branch.
Provisioning logs remain in that same `runner-bootstrap` directory.
A failed phase stops configuration; it never marks an incomplete runner ready.

## Apple authentication and Xcode refresh

Xcode downloads require an initial Apple account login and can request two-factor authentication.
Install the download CLI on the host and sign in from Terminal on the host Mac:

```sh
HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 brew install xcodes
./setup host xcode-login
```

The login command uses the host's interactive terminal.
`xcodes` remembers the Apple account and saves its password in the host login Keychain.
It also retains an Apple session.
Later downloads reuse that session or saved credentials; Apple may require another two-factor code, password, or acceptance of new terms.
Approve any Keychain access request while the host is unlocked.
The setup does not change Keychain lock settings or host login security.
A locked or inaccessible Keychain stops downloads with an error rather than prompting in the background.
Do not put Apple credentials in configuration files or environment files.

The host downloads each selected build once into `~/Library/Application Support/runner-host/xcode-cache`.
Only archives and their SHA-256 checksums reach the guests over SSH; Apple credentials and cookies stay on the host.
The checksum verifies transfer integrity; `xcodes` still verifies Apple's application signature during installation.
Archives stay cached on the host for future guest rebuilds.
Interrupted or inconsistent downloads stop for inspection rather than being silently reused or deleting existing files.

Guest maintenance uses the catalog supplied by the last successful `host configure` or `host refresh-xcode`.
Run `./setup host refresh-xcode --config ~/runner-host.toml` to discover, distribute, and install newer releases without repeating initial provisioning.
The guided installer enables the refresh agent.
When using individual commands, after both guests finish configuration, enable the refresh every six hours:

```sh
./setup host xcode-agent --config ~/runner-host.toml
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.selfhosted-runner.xcode-downloads.plist
```

The agent runs in the existing host login session, including while the screen is locked.
It requires prior Keychain authorization and a usable Apple session; it cannot bypass a locked Keychain or Apple's two-factor challenge.
Failures are recorded in `~/Library/Application Support/runner-host/xcode-refresh.log`.
Guest maintenance acquires its existing lock and defers active runner jobs.
Sleeping, logged-out, and powered-off hosts cannot run this login agent.
For initial provisioning, `host configure --vm "GitHub Runner 1"` limits work to one guest.
Only the newest release or Release Candidate is kept by default, with all platforms, simulator runtimes, and Metal tools.
Older managed Xcodes are removed after the replacement passes readiness checks.
Beta installation is disabled by default.
See the [Xcodes authentication instructions](https://github.com/XcodesOrg/xcodes#usage).
Guests created without automatic provisioning can follow the [dedicated runner instructions](setup.md#choose-one-layout) inside the guest.
That layout stores its GitHub and Apple credentials in the guest.

## Validate the runners

After setup, check `./setup status` inside both guests and their online state in GitHub.
Restart each guest and verify automatic login and runner recovery.
The included `Runner validation` workflow accepts both runner names, tests a Swift package, compiles a Metal shader, and boots one temporary simulator for each of iOS, tvOS, watchOS and visionOS.
It must be published before GitHub can dispatch it; local VM checks do not substitute for a successful workflow run.

For a smoke test without runner registration or Xcode downloads, run `./setup host bootstrap` **inside a fresh guest**, then run it again.
This checks prerequisite installation and rerun behavior.
It does not need nested virtualization: do not create nested guests.
It also does not validate runner registration, Xcode authentication, or a real workflow job.

## Start after login

The guided installer loads the startup agent.
When using individual commands, run this once after configuring the guests:

```sh
./setup host login-agent --config ~/runner-host.toml
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.selfhosted-runner.virtual-machines.plist
```

Do this first load while the host is unlocked.
Allow Python to control UTM when macOS asks: a background job has its own Automation permission, separate from your terminal.
Check the agent's log below and use `launchctl print "gui/$(id -u)/com.selfhosted-runner.virtual-machines"` to confirm that its last exit code is zero.
Do not load an already loaded agent again; use `./setup host start --config ~/runner-host.toml` for later manual starts.

The LaunchAgent starts only the recorded VM UUIDs at the next host user login.
It does not poll, restart crashed guests, or resume suspended guests.
Existing running guests are left alone.
Review startup output at `~/Library/Application Support/runner-host/login.log`.
Keep this checkout and its host configuration and the Python interpreter used to install the agent at their current paths.
The agent invokes that interpreter directly; it does not depend on your terminal's PATH.
To disable startup, remove its Login Item/LaunchAgent through your normal administration process after reviewing `~/Library/LaunchAgents/com.selfhosted-runner.virtual-machines.plist`.

FileVault requires a disk unlock after a cold boot.
Host automatic login is not configured, and this workflow does not disable encryption to bypass that step.
A logged-in host can lock its screen while its VMs run.
Guest runner services also start at guest login: use manual login, or explicitly configure automatic login in a dedicated guest under your security policy.
A fully unattended cold boot is therefore not promised.

Screen locking, sleep, and logout are different states.
Background commands and existing runner sessions can continue while the screen is locked; desktop automation cannot complete interactive prompts there.
Grant UTM's Automation permission and finish initial setup before leaving the host unattended.
Keep the host awake using UTM's setting above, and keep each guest's runner user logged in.
Guest SSH and automatic login are not needed to keep an already running VM working while the host screen is locked.

## Rebuild a runner

Drain the old runner and preserve anything that is not reproducible.
Use a new host configuration path and a new VM name to build the replacement from the pinned image.
Register it independently and validate a job before retiring the old runner.
Keep at most two macOS guests running and shut down the old guest before starting its replacement.
Remove the old registration and VM explicitly only after reviewing their contents.
The scripts deliberately provide no one-command destructive rebuild.

## Upstream interfaces

UTM documents [macOS VM creation](https://docs.getutm.app/scripting/cheat-sheet/), [installation and first-boot provisioning](https://docs.getutm.app/scripting/reference/), and [host sleep behavior](https://docs.getutm.app/preferences/macos/).
The two-guest layout also needs to fit the macOS license applicable to your installation; consult [Apple's software license agreements](https://www.apple.com/legal/sla/).
