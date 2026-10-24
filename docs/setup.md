<!--
This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Set up runners

Run setup in Terminal; macOS launchd runs subsequent maintenance.

## Before starting

Complete macOS Setup Assistant, device enrollment and system updates. Log in as
the account that will own the installation, with administrator access available.
Keep FileVault, SIP, Gatekeeper and your organization's security controls enabled.
Put the checkout in a durable location outside Desktop, Documents and Downloads
so background jobs do not depend on protected-folder access. Review `config.toml`:
GitHub destination, runner group, toolchain and machine settings apply to runners.

You supply:

- The GitHub destination in `config.toml` and a PAT authorized to manage its
  runners. Use organization **Self-hosted runners: Read and write**, or repository
  **Administration: Read and write** for a repository runner. Complete any required
  organization approval first. **Actions: Read and write** is only needed for a
  separate workflow dispatch. Set an expiration date and renew before it expires.
- An Apple developer account and its two-factor authentication device for Xcode
  downloads. Complete any pending agreements. The Mac need not be signed into iCloud.
- For VMs, an Apple silicon host running macOS 27 or later, enough storage for the
  planned guests, and an internet connection. Setup checks the host OS; it does not
  upgrade or reboot an existing host.

Setup obtains and checks:

| Component | What setup does |
| --- | --- |
| Command Line Tools | Requests installation if missing; finish Apple's installer and rerun. |
| Homebrew and Python | Installs missing prerequisites. Homebrew may request administrator authorization. |
| UTM | Installs it if missing, verifies its signature and checks that the installed version supports provisioning. The supported minimum is 5.0.8. An older existing installation needs a separate update with its VMs stopped. |
| macOS restore image | Queries Apple's compatible-image API, downloads the IPSW, validates it on this host and pins its URL and checksum. No manual download is needed. |
| Guest macOS | Installs the selected image into each new VM. Setup checks that it supports automatic account creation; macOS 27+ is an installer constraint, not a guest you must prepare. |
| Guest tools and services | Installs Xcode, simulator platforms, Metal tools and runner software, then registers runners and configures startup. |

Stay logged in and unlocked for initial permissions and credential entry. Keep the
host awake and connected to power. Setup checks resources before creating VMs;
restore images, cached Xcode archives, simulator runtimes and jobs also consume
host or guest disk space.

## Choose one layout

**Dedicated physical runner:**

```sh
./setup install runner --name 'GitHub Runner 1'
```

Omit `--name` to use the configured or saved name, falling back to the hostname.
This is intended for a dedicated machine: setup installs build tools, configures
the runner, enables job cleanup and applies `config.toml` machine settings.
It asks for the local administrator password once before provisioning. The
password is held in memory for this invocation. The provisioning child removes it
from its environment before runner registration and service startup; it is not
saved for future maintenance.

**Host with two runner VMs:**

```sh
./setup install host
```

This creates `~/runner-host.toml`, downloads a compatible macOS image from Apple
and chooses the two lowest unused `GitHub Runner N` names from GitHub and UTM.
To choose names, supply `--name` once or twice. Names are not reserved remotely; do not provision multiple
hosts against the same destination concurrently without assigning distinct names.

Defaults reserve 8 GiB RAM and two CPU cores for the host, split the remainder
between guests, and size sparse disks according to the host volume. For a host
also used for other work, consider `--reserve-memory-gib 16`. CPU oversubscription
is explicit, for example `--cpu-ratio 1.5`; see [resource sizing](hosts.md#names-and-resources).

To use an existing image for a specific build, pass `--image /path/to/restore.ipsw`.
Setup checks it before creating guests. This is an override, not a prerequisite.
Review the printed plan before proceeding. Generated guest passwords are unique
and stored in the host login Keychain. Guests have automatic login and SSH enabled:
someone with access to a guest console can use its logged-in session. Host login,
encryption and SSH settings are not changed.

Finish UTM's first-launch dialogs when shown. On a dedicated host, enable UTM's
**Prevent system from sleeping when any VM is running** setting. The scripts do
not alter the host's power policy. Closing a laptop lid can still suspend it.

## What happens next

1. Check the host platform and committed source, install missing prerequisites,
   verify the PAT and establish or reuse the Apple session.
2. Run a temporary LaunchAgent to check background Keychain access and, for VM
   hosts, both UTM interfaces. Approve any system dialogs while unlocked. Failure
   stops before VM creation or runner installation.
3. Discover the compatible macOS build and show the VM names and resources.
   Confirm before downloading the image or creating VMs. A physical runner instead
   requests its administrator password before tool installation.
4. Download and verify the image, save its pin, install the guests and create their
   accounts. Install the newest released Xcode or Release Candidate, configured
   simulator platforms and Metal tools, then register runners and load services.
   The host downloads Xcode once and transfers verified archives to its guests.

After the initial inputs, commands run without terminal prompts. A later external
failure stops the affected work and reports the cause; see the limitations below.

Rerun the same command to resume. For an existing VM configuration, omit `--image`
and `--name` and keep the same `--config` path:

```sh
./setup install host --config ~/runner-host.toml
```

Configuration and owned VM identities are preserved. An ambiguous interrupted
creation or first boot stops for inspection; setup never erases a guest to recover.
The host checkout must be clean and committed before transfer to guests. Updating
an existing guest to a different revision requires a reviewed fast-forward; setup
refuses to reset or overwrite it. Custom Homebrew casks can have arbitrary installers; guided setup
rejects managed extra casks up front. Install those separately and remove them from
the managed cask list before using the guided path.

Check an existing installation's prerequisites and background access without
provisioning (this still checks external credentials and creates a temporary agent):

```sh
./setup install host --check --config ~/runner-host.toml
./setup install runner --check
```

These are access checks, not full runner health tests. Use `./setup status` on a
runner, `./setup host status` on a host, and the GitHub runner page for readiness.
The `host` subcommands remain available for individual steps and recovery.

## Background services

Each service is a per-user macOS LaunchAgent.

| Where | LaunchAgent | When and purpose |
| --- | --- | --- |
| Host | `com.selfhosted-runner.virtual-machines` | At login, start owned VMs |
| Host | `com.selfhosted-runner.xcode-downloads` | At login and every six hours, check for newer Xcode, cache it, transfer it and run guest maintenance |
| Each runner | `com.selfhosted-runner.setup` by default | At login and every six hours, reconcile runner software and tools |

The guided host flow installs and loads the two host agents. The refresh agent
keeps Apple credentials on the host; guests receive Xcode archives, not the Apple
account or PAT. xcodes retains the Apple account/session using its native storage
and Keychain integration, so successful sessions can be reused. Guest maintenance
uses the staged toolchain and defers while jobs are active. Physical runners use
their own Apple session. Logs are under `~/Library/Application Support/runner-host/`
on hosts and `~/Library/Logs/ci-runner-setup/` on runners.

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

To renew Apple authentication, run `./setup host xcode-login` in Terminal on the
machine downloading Xcode. A physical runner may need another guided run when
new Xcode terms require privileged acceptance. For failures, inspect the named
phase and logs; see the [maintenance guide](maintenance.md) for compatibility fixes.

The preflight is a current access check, not a guarantee that future macOS policy,
Apple sessions or network conditions will remain unchanged. See Apple's
[Automation controls](https://support.apple.com/guide/mac-help/mchl108e1718/mac).
