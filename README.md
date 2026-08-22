# macOS Self-Hosted GitHub Actions Runner

Everything needed to turn a **fresh macOS install** into a self-hosted GitHub
Actions CI runner. Clone this repo onto the Mac once, run one script, done.
Re-running the script is always safe — it only changes what needs changing
(idempotent convergence), and a LaunchAgent re-runs it automatically after
every reboot so the machine heals itself.

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
   needs it once to download Xcode. The Mac does not need to be signed into
   iCloud.
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
   ./setup.zsh store-pat
   ```

   Note: whichever `./setup.zsh` command runs first also bootstraps Homebrew
   and python3 — so this step already triggers the Homebrew installer and its
   one-time password prompt.

6. Run the setup (expect a long first run — Xcode + simulators are tens of
   GB; keep ~150 GB of disk free):

   ```sh
   ./setup.zsh
   ```

   The first run will interactively ask for: your Apple ID (Xcode downloads
   via `xcodes`; the session is cached in the Keychain), and possibly sudo
   for Xcode's first-launch package installation.

7. Enable **auto-login** for this user (System Settings → Users & Groups →
   Automatically log in as…; requires FileVault to be off). The runner and
   the boot agent are per-user LaunchAgents — they start at login, so the
   machine must log in by itself after a reboot/power failure.

Verify: the runner shows as *Idle* under the repo/org's
**Settings → Actions → Runners**, and `./setup.zsh status` reports
everything green.

## How re-running works (idempotency)

`./setup.zsh` (command `converge`, the default) converges every phase and
skips whatever is already correct:

| Phase | Re-run behaviour |
|---|---|
| Homebrew packages | installs missing, upgrades outdated, otherwise no-op (plus a daily `brew autoupdate` LaunchAgent between converges) |
| Passwordless xcode-select | sudoers rule (`/etc/sudoers.d/xcode`) installed on the first interactive run: `sudo xcode-select -s` and `sudo xcodebuild -runFirstLaunch` work without a password — for CI jobs and for unattended converges finishing a new Xcode's first-launch setup |
| Xcode releases | installs newly released stable/previous/beta versions, refreshes simulators/SDKs/Metal toolchain, warns about superseded betas (never deletes) |
| Runner software | updates when a newer release exists (checksum-verified); the runner also self-updates between runs |
| Registration | re-registers only when `config.toml` changed (name/labels/URL/group/work dir); otherwise untouched |
| Job env (`.env`/`.path`) | rewritten only on change; service restarted only then |
| Service / boot agent | (re)installed only when missing or changed |

A lock file guarantees a manual run and the boot-time run never overlap.

## The boot agent (automatic re-runs)

`converge` installs `~/Library/LaunchAgents/com.selfhosted-runner.setup.plist`,
which runs `setup.zsh converge --non-interactive` at every login. Unattended
runs first `git pull --ff-only` this repo (re-executing themselves if the
setup changed), so every reboot runs the latest committed version; a failed
pull just means converging with the current checkout. In this
mode the setup **never prompts and never uses sudo** (beyond the
passwordless sudoers rule); anything that would
need either (a brand-new Xcode's first-launch step, an expired Apple ID
session, a re-registration without a usable PAT) is skipped with a warning
and left for the next manual run — an unattended run never tears down a
working runner. Output lands in `~/Library/Logs/ci-runner-setup.log`.
Setting `boot.install_agent = false` in `config.toml` removes the agent on
the next converge.

## Commands

```sh
./setup.zsh                # converge (default)
./setup.zsh status         # show runner/service/Xcode state
./setup.zsh store-pat      # print PAT requirements, prompt for it (hidden)
./setup.zsh store-pat TOKEN  # store the given PAT directly
./setup.zsh uninstall      # deregister runner, remove services (asks first)
./setup.zsh converge --skip-xcode   # useful while iterating
```

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
- `xcodes` keeps the Apple ID password/session in the login Keychain so
  unattended runs can install new Xcode releases (the old setup ran
  `xcodes signout` instead). Same caveat as the PAT: jobs run as this user —
  use a dedicated CI Apple ID with no other roles.

## Troubleshooting

- **Boot-time run did something odd** → `~/Library/Logs/ci-runner-setup.log`.
- **Runner offline after reboot** → is auto-login enabled (step 7)? LaunchAgents
  only start once the user session exists.
- **`xcodes` asks for Apple ID again** → sessions expire; run `./setup.zsh`
  interactively once.
- **Service state** → `cd ~/actions-runner && ./svc.sh status`; runner logs in
  `~/actions-runner/_diag/`.
- **Changed `runner.dir` in config.toml** → run `./setup.zsh uninstall` with
  the old config first, then converge; otherwise the old registration is
  orphaned.
