<!--

This source file is part of the SchmiedmayerLab ci-setup open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# macOS Self-Hosted GitHub Actions Runner

Everything needed to turn a **fresh macOS install** into a self-hosted GitHub
Actions CI runner. Clone this repo onto a dedicated runner Mac and run the
setup there. **Do not run setup on a developer workstation**: it installs and
registers a runner, changes system settings, and enables destructive job cleanup.
A LaunchAgent checks for updates at login and every six hours.

## What a converged machine has

- **GitHub Actions runner**: latest release, registered to your repo/org,
  running as a per-user launchd service, kept up to date.
- **Toolchain** (Homebrew): openjdk (java), firebase-cli, fastlane, node,
  xcbeautify, git-lfs, aria2, xcodes, jq, python3, swiftlint, periphery —
  plus xcpretty (Ruby user gem; it has no Homebrew formula).
- **Xcode**: the newest stable release, the newest release of the previous
  minor train, and (if newer than stable) the newest beta/RC — each with
  simulator runtimes, SDKs and (Xcode 26+) the Metal toolchain.
- **Job environment**: CI jobs automatically get Homebrew on `PATH`,
  `JAVA_HOME`/`LANG`/`LC_ALL`, and the latest stable Xcode via
  `DEVELOPER_DIR` — configured through the runner's `.env`/`.path` files
  (the runner's own mechanism for injecting env/PATH into every job).
  The global `xcode-select` also tracks the latest stable Xcode, and
  Apple's WWDR intermediate certificate is installed for code signing.
- **Boot agent**: a LaunchAgent that re-runs the whole setup at every login,
  unattended and sudo-free.

## Setting up a new runner Mac

Step 1 happens once in GitHub; everything else on the Mac (Terminal.app on the
machine itself or via Screen Sharing — launchd agents cannot be loaded over
plain SSH).

1. Create a PAT that can manage self-hosted runners:
   classic PAT with `repo` scope (repo-level runner) or `admin:org`
   (org-level); fine-grained PAT with repo **Administration: write** or org
   **Self-hosted runners: write**.
2. Have an **Apple ID** ready (a free developer account is enough) — `xcodes`
   needs it to download Xcode. Complete 2FA interactively when requested;
   Apple may require verification again when its session expires. The Mac
   does not need to be signed into iCloud.
3. Clone this repo (the `git clone` triggers the Xcode Command Line Tools
   install dialog on a fresh machine — accept it, then clone again):

   ```sh
   git clone https://github.com/SchmiedmayerLab/ci-setup.git ~/ci-setup
   cd ~/ci-setup
   ```

4. Check `config.toml` (committed with the repo, shared by every runner Mac —
   it targets the SchmiedmayerLab org and holds no secrets; runner names
   default to each machine's hostname). Usually nothing to change here.

5. Store the PAT in the Keychain:

   ```sh
   ./setup store-pat
   ```

   `store-pat` needs nothing but macOS itself (no Homebrew/python bootstrap),
   so this works first thing on a factory-fresh machine. Run it bare to see
   the PAT requirements.

6. Run the setup (expect a long first run — Xcode + simulators are tens of
   GB; keep ~150 GB of disk free):

   ```sh
   ./setup
   ```

   The first run will interactively ask for: your Apple ID (Xcode downloads
   via `xcodes`, including 2FA; credentials/session are retained by `xcodes`), and possibly sudo
   for Xcode's first-launch package installation.

7. Enable **auto-login** for this user (System Settings → Users & Groups →
   Automatically log in as…; requires FileVault to be off). The runner and
   the boot agent are per-user LaunchAgents — they start at login, so the
   machine must log in by itself after a reboot/power failure.

Verify: the runner shows as *Idle* under the repo/org's
**Settings → Actions → Runners**, and `./setup status` shows the service
running and the latest maintenance result.

## How re-running works (idempotency)

### Migrating an existing runner without GitHub tokens

For an existing installation such as `StanfordBDHG/ContinuousIntegration`,
clone this repository into a separate `~/ci-setup` directory on the runner
VM. Keep the existing runner directory and registration intact. Once the
desired branch is checked out, run as the same logged-in runner user:

```sh
cd ~/ci-setup
./setup adopt ~/runner --skip-xcode
./setup info
```

`adopt` validates that the existing runner belongs to the configured GitHub
organization/repository, waits for no jobs by deferring busy maintenance,
pauses intake, and runs the regular maintenance phases. `--skip-xcode`
retains the current Xcode during the migration; a later `./setup update`
can install newer releases and request Apple authentication if necessary.
Adoption needs an existing Python 3.11+ and does not bootstrap tools before
validating the registration and pausing intake.

The runner's ID, name, group, existing GitHub labels, credentials, directory,
and working folder are preserved. Neither a GitHub PAT nor registration or
removal tokens are needed. The new cleanup hooks use the existing working
folder. Duplicate machine hostnames do not change the preserved runner names.
The known legacy Homebrew autoupdate LaunchAgent is retired before tool
changes, so updates are owned by this setup's maintenance schedule. If the
legacy updater is executing, adoption stops before changing packages; let it
finish and retry. Unexpected legacy agent definitions require manual review.

Adoption metadata is stored in
`~/Library/Application Support/ci-runner-setup/adopted-runner.json`, outside
the Git checkout. Keep `config.toml` unmodified so future self-updates and
switching the checkout to `main` work normally. Existing labels are preserved,
not reconciled with `runner.labels`. Subsequent registration-policy changes
or unexpected local identity changes fail without implicit re-registration;
review such a change as a separate migration.

The retired Homebrew plist is kept under the setup state directory's `legacy`
folder. Restoring that updater requires enabling its launchd label as well as
restoring/loading the archived plist; first stop the new maintenance agent
to avoid competing schedules. Adoption does not delete the old installer,
old cleanup scripts, or any credential files.

### Regular maintenance

`./setup` (command `converge`, the default) converges every phase and
skips whatever is already correct:

| Phase | Re-run behaviour |
|---|---|
| Homebrew packages | refreshes metadata, installs/upgrades each managed package independently; reports outdated pinned formulae without changing the pins |
| Passwordless xcode-select | sudoers rule (`/etc/sudoers.d/xcode`) installed on the first interactive run: `sudo xcode-select -s` and `sudo xcodebuild -runFirstLaunch` work without a password — for CI jobs and for unattended converges finishing a new Xcode's first-launch setup |
| Xcode releases | installs stable/previous/beta versions and requested components; only selects a ready stable release and cleans up after successful preparation; retains the Xcode still referenced by the runner environment until a later pass |
| Runner software | updates when a newer release exists (checksum-verified); the runner also self-updates between runs |
| Registration | re-registers only when `config.toml` changed (name/labels/URL/group/work dir); otherwise untouched |
| Job env (`.env`/`.path`) | rewritten only on change; a persisted restart marker survives an interrupted update |
| Service / boot agent | (re)installed only when missing or changed |

A lock prevents overlapping Python maintenance passes and remains held
through source/Python restarts. Before changing tools, maintenance checks for
active jobs, briefly freezes the idle listener to close the job-start race,
checks again, then stops its service. Busy or ambiguous state defers the run.
The service stays stopped across setup restarts. Existing service intake is
restored on completion or handled failure; an incomplete fresh installation
does not begin accepting jobs. A recovery marker lets the next run restore
service after an abrupt interruption.

Independent phases continue after a handled command failure. Failures remain
visible in the final result; they are not converted into success because a
later phase worked. Exit codes are `0` for completion (possibly with warnings),
`1` for failure/incomplete setup, and `2` for deferred maintenance. An expired
Apple session therefore does not prevent independent runner/configuration
repairs, and failed Xcode preparation preserves existing Xcodes and runtimes.

Python maintenance commands have wall-clock limits, including commands that
keep printing output without advancing. Ordinary probes default to two
minutes. Longer operations have explicit limits: Homebrew metadata refreshes
get ten minutes, each package install/upgrade gets one hour, each Xcode
install or platform/Metal download gets four hours, and first-launch setup
and Xcode/runtime removal get thirty minutes. Runner registration gets five
minutes, archive extraction ten minutes, and service commands one minute.
The runner archive download checks a thirty-minute transfer deadline between
reads, with a separate sixty-second socket timeout. During a long transfer it
logs the bytes received every five minutes. The initial shell bootstrap (installing Homebrew
and Python on a fresh Mac) is outside these Python limits.

Long-running commands with visible output emit a redacted **still running**
message every five minutes with elapsed time and their limit. This confirms
that setup is monitoring the child, not that it is making progress. Captured
commands, which can return credentials, do not emit these messages. A timeout
stops the command's process group and becomes a normal recorded failure;
independent work continues and the maintenance pause attempts service recovery.
Output silence alone is not treated as a hang: Xcode extraction and builds
can legitimately be quiet.

## The boot agent (automatic re-runs)

`converge` installs `~/Library/LaunchAgents/com.selfhosted-runner.setup.plist`,
which runs `setup converge --non-interactive` at every login **and every
6 hours**, so long-lived login sessions still pick up new Xcode releases,
runner updates, and config changes. No quiet-hour scheduling is needed (the
team spans too many time zones for one to exist): the entire maintenance pass
defers while a job is running and pauses intake while changing the machine.
Unattended runs first fast-forward the checkout's configured Git upstream,
then re-execute if the source changed. A dirty checkout, missing upstream,
divergence, or pull failure is reported; automatic runs still attempt local
maintenance and finish with a failure result. Git never prompts or discards
local changes. Homebrew Python upgrades also trigger a restart before later
phases use the interpreter.

Unattended setup gives child commands no stdin or controlling terminal.
Passwordless Xcode sudo rules may be used; work requiring interaction fails,
is reported as deferred, or reaches its command timeout if a tool retries a
prompt instead of exiting. Output and phase results are retained as described
below.
Setting `boot.install_agent = false` in `config.toml` removes the agent on
the next manual converge. If the active agent changes its own definition,
the new definition takes effect at the next login; renaming or disabling the
active agent requires a subsequent manual converge to unload its old schedule.

## Commands

```sh
./setup                # converge (default)
./setup update         # fast-forward source, restart if changed, converge/reload boot agent
./setup adopt ~/runner # preserve an existing registration and migrate maintenance
./setup status         # show runner/service/Xcode state
./setup info           # current setup revision, services, tool versions/pins and Xcode selections
./setup info --json    # same comparable inventory as status --json
./setup logs --lines 200 # recent timestamped maintenance output
./setup status --json  # read-only comparable inventory
./setup compare /tmp/other-runner.json # nonzero if different or incomplete
./setup store-pat      # print PAT requirements, prompt for it (hidden)
./setup store-pat TOKEN  # store the given PAT directly
./setup uninstall      # deregister runner, remove services (asks first)
./setup converge --skip-xcode   # useful while iterating
```

`update` stops before changing packages if its pull fails. It updates an
existing runner's registration only when its configuration changed; unchanged
registrations retain their identity. `status`, `info`, `logs`, `compare`, and help
use an existing Python 3.11+ and never bootstrap missing dependencies.

`info` prints a fresh local summary: checkout revision and macOS version,
runner registration/service and boot-agent state, last maintenance result,
pending recovery/restart markers, log location/retention, and
installed/global/job-selected Xcode builds. A final tools section lists
versions and pins only for Homebrew
formulae/casks explicitly requested by setup or configuration. `info --json`
retains the full dependency inventory for comparisons. It does not update
packages, authenticate, restart services,
or write maintenance state. Unavailable probes are listed as incomplete and
return exit code 1 while the remaining information is still displayed.

## Logs and keeping runners in sync

Each Python maintenance run records its start, finish, exit code, phase
outcomes and child stdout/stderr in
`~/Library/Logs/ci-runner-setup/setup-YYYY-MM-DD.log`. Run IDs connect output
across phases. `./setup status` shows the latest result, also saved in
`~/Library/Application Support/ci-runner-setup/last-run.json`. A run still
marked `running` after its process has gone indicates an abrupt interruption.

`[logging] retention_days = 30` retains up to 30 calendar days.
`max_bytes = 52428800` additionally caps the total dated logs at 50 MiB, so
heavy output can shorten that window. Oldest output is discarded first.
Known tokens are redacted; captured credential responses and stdin are not
logged. Files are created owner-only. Bootstrap failures before Python starts
appear in `bootstrap.log`, overwritten at the start of the next agent attempt.
An old `~/Library/Logs/ci-runner-setup.log` is historical and can be removed
manually after inspecting it.

Shared configuration specifies the same update policy, but two machines
running at different times can still acquire different versions or encounter
different failures. Export inventories to check the actual state. For example,
these commands execute setup **on the runners via SSH**:

```sh
ssh ci@LukasMBPM1.local 'cd ~/ci-setup && ./setup status --json' > /tmp/m1-inventory.json
scp /tmp/m1-inventory.json ci@LukasMBPM4.local:/tmp/m1-inventory.json
ssh ci@LukasMBPM4.local 'cd ~/ci-setup && ./setup compare /tmp/m1-inventory.json'
```

Comparison includes setup commit/config, macOS build/architecture, runner
version, active managed Homebrew packages and their dependencies/pins, and
installed/selected Xcode builds. Host names, capture times, and old inactive
Homebrew kegs do not create differences. Incomplete or dirty snapshots cannot
report a match. Each maintenance pass also saves `inventory.json` alongside
the run summary; `status --json` takes a fresh snapshot.

## Apple authentication for Xcode downloads

Use a dedicated CI Apple Account and complete its normal password/2FA flow
interactively on each runner. `xcodes` reuses authentication when Apple accepts
the cached session. When unattended authentication fails, run `./setup`
interactively **on that runner** and complete verification. Session validity
is controlled by Apple; this cannot guarantee unattended downloads forever.

`xcodes` 2.1.0 has no CLI session-validation command or flag to require an
existing valid session without attempting login. Its underlying authentication
library has a validate-only API, but setup does not currently integrate it.
In particular, one phone-selection prompt retries on EOF; supplying no stdin
does not guarantee an immediate authentication failure. The install deadline
bounds that case, but is not an early 2FA detector. See the upstream
[CLI definition](https://github.com/XcodesOrg/xcodes/blob/2.1.0/Sources/xcodes/App.swift),
[session validation](https://github.com/XcodesOrg/XcodesLoginKit/blob/929f9aac3140caf7b64cbb5385f4f645c5f9913d/Sources/XcodesLoginKit/Client.swift#L413-L419),
and [phone-selection implementation](https://github.com/XcodesOrg/xcodes/blob/2.1.0/Sources/XcodesKit/TwoFactorAuthentication.swift#L101-L114).

App-specific passwords and App Store Connect API keys do not authenticate
Xcode downloads ([xcodes maintainer explanation](https://github.com/XcodesOrg/xcodes/issues/293#issuecomment-2074920422)).
Accounts created with 2FA cannot disable it ([Apple documentation](https://support.apple.com/en-us/102660)).
App Store/TestFlight upload authentication is separate: workflows can use an
App Store Connect API key for those operations.

Downloads remain automatic and direct from Apple. No intermediate XIP hosting
or manually maintained release mirror is required. An authentication failure
leaves installed Xcodes available and appears in the run's failure summary.

## Per-job cleanup

The runner runs two hook scripts around every job (wired via
`ACTIONS_RUNNER_HOOK_JOB_STARTED`/`_COMPLETED` in the runner's `.env`;
disable with `runner.cleanup_hooks = false` in `config.toml`):

- [`hooks/job-started.sh`](hooks/job-started.sh) — shuts down and erases all
  simulators so every job starts from a pristine device state, and exports
  `selfhosted=true` into `$GITHUB_ENV` so workflows can detect the
  self-hosted runner (`if: env.selfhosted == 'true'`).
- [`hooks/job-completed.sh`](hooks/job-completed.sh) — wipes the runner's
  work directory (all checkouts and build products), clears Periphery's
  cache, and resets the simulators again.

## Using the runner in workflows

```yaml
jobs:
  build:
    # ARM64 on Apple Silicon, X64 on an Intel Mac; plus labels from config.toml
    runs-on: [self-hosted, macOS, ARM64]
    steps:
      - uses: actions/checkout@v4
      - run: xcodebuild build -scheme MyApp   # uses the wired DEVELOPER_DIR
```

Jobs needing a specific Xcode can override it:
`env: { DEVELOPER_DIR: /Applications/Xcode-26.0.0-Beta.5.app/Contents/Developer }`.

## Security notes

- **Never attach self-hosted runners to a public repository** — fork PRs
  could execute arbitrary code on this machine.
- The PAT lives exclusively in the login Keychain (`github-runner-pat`);
  `config.toml` carries no secrets and is safe to commit. Be aware that
  **CI jobs run as the same user** and can read that Keychain item while the
  session is unlocked (always, on an auto-login CI box) — use a fine-grained
  PAT limited to runner administration on exactly this repo/org so a
  compromised job can't do more than re-register runners.
- Runner tarballs are SHA-256-verified against the official release notes;
  installation fails closed if no checksum is published.
- Registration/removal tokens are short-lived (1 h), never stored, and
  redacted from error messages/logs.
- `xcodes` retains Apple authentication so unattended runs can install new
  Xcode releases while the session remains valid. Same caveat as the PAT: jobs run as this user —
  use a dedicated CI Apple ID with no other roles.

## Troubleshooting

- **Boot-time run did something odd** → `./setup status`, then `./setup logs`;
  inspect `~/Library/Logs/ci-runner-setup/bootstrap.log` if Python never started.
- **Homebrew package failed or remains pinned** → inspect its phase output;
  independent packages still get an attempt. Resolve the cause on both runners,
  re-run maintenance, then compare fresh inventories.
- **Runner offline after reboot** → is auto-login enabled (step 7)? LaunchAgents
  only start once the user session exists.
- **`xcodes` asks for Apple ID again** → sessions expire; run `./setup`
  interactively once.
- **Service state** → `cd ~/actions-runner && ./svc.sh status`; runner logs in
  `~/actions-runner/_diag/`.
- **Changed `runner.dir` in config.toml** → run `./setup uninstall` with
  the old config first, then converge; otherwise the old registration is
  orphaned.
