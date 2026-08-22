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
  `JAVA_HOME`, and the latest stable Xcode via `DEVELOPER_DIR` — configured
  through the runner's `.env`/`.path` files, no shell profiles, no
  `xcode-select`, no sudo.
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
   git clone https://github.com/<you>/ContinuousIntegration.git ~/ci-setup
   cd ~/ci-setup
   ```

4. Create and edit the config:

   ```sh
   cp config.example.toml config.toml
   open -e config.toml   # set github.owner / github.repo etc.
   ```

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
| Homebrew packages | installs missing, upgrades outdated, otherwise no-op |
| Xcode releases | installs newly released stable/previous/beta versions, refreshes simulators/SDKs/Metal toolchain, warns about superseded betas (never deletes) |
| Runner software | updates when a newer release exists (checksum-verified); the runner also self-updates between runs |
| Registration | re-registers only when `config.toml` changed (name/labels/URL/group/work dir); otherwise untouched |
| Job env (`.env`/`.path`) | rewritten only on change; service restarted only then |
| Service / boot agent | (re)installed only when missing or changed |

A lock file guarantees a manual run and the boot-time run never overlap.

## The boot agent (automatic re-runs)

`converge` installs `~/Library/LaunchAgents/com.selfhosted-runner.setup.plist`,
which runs `setup.zsh converge --non-interactive` at every login. In this
mode the setup **never prompts and never uses sudo**; anything that would
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
./setup.zsh store-pat      # save the GitHub PAT to the login Keychain
./setup.zsh uninstall      # deregister runner, remove services (asks first)
./setup.zsh converge --skip-xcode   # useful while iterating
```

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
- The PAT lives in the login Keychain (`github-runner-pat`), not on disk —
  but be aware that **CI jobs run as the same user** and can read that
  Keychain item while the session is unlocked (always, on an auto-login CI
  box). Use a fine-grained PAT limited to runner administration on exactly
  this repo/org so a compromised job can't do more than re-register runners.
- Runner tarballs are SHA-256-verified against the official release notes;
  installation fails closed if no checksum is published.
- Registration/removal tokens are short-lived (1 h), never stored, and
  redacted from error messages/logs.

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
