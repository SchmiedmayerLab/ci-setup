#!/bin/bash
# This source file is part of the macOS Self-Hosted GitHub Actions Runner open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

# Run over SSH in a new macOS guest. Its password arrives on stdin, never argv.
set -euo pipefail
[[ $# == 2 || $# == 3 ]] || { echo 'Expected display name and hostname, or --converge and checkout' >&2; exit 1; }
[[ -n "${SSH_CONNECTION:-}" && "$(/usr/sbin/sysctl -n hw.model)" == VirtualMac* ]] \
  || { echo 'This bootstrap is restricted to a macOS virtual machine over SSH' >&2; exit 1; }
[[ $EUID != 0 ]] || { echo 'Run as the guest administrator, not root' >&2; exit 1; }

IFS= read -r RUNNER_BOOTSTRAP_PASSWORD
[[ -n $RUNNER_BOOTSTRAP_PASSWORD ]] || exit 1
export RUNNER_BOOTSTRAP_PASSWORD
staging="$(mktemp -d -t runner-bootstrap)"
cleanup() {
  unset RUNNER_BOOTSTRAP_PASSWORD SUDO_ASKPASS
  /usr/bin/sudo -k
  /bin/rm -f "$staging/askpass" "$staging/install-homebrew.sh"
  /bin/rmdir "$staging"
}
trap cleanup EXIT
cat > "$staging/askpass" <<'ASKPASS'
#!/bin/sh
printf '%s\n' "$RUNNER_BOOTSTRAP_PASSWORD"
ASKPASS
chmod 700 "$staging/askpass"
export SUDO_ASKPASS="$staging/askpass"

if [[ $1 == --converge ]]; then
  [[ $# == 2 ]] || exit 1
  cd "$2"
  ./setup converge --non-interactive </dev/null
  exit
fi

/usr/bin/sudo -A /usr/sbin/scutil --set ComputerName "$1"
/usr/bin/sudo -A /usr/sbin/scutil --set LocalHostName "$2"
/usr/bin/sudo -A /usr/sbin/scutil --set HostName "$2"

if [[ ! -x /opt/homebrew/bin/brew ]] || ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
  /usr/bin/curl --fail --silent --show-error --location --proto '=https' \
    https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh \
    --output "$staging/install-homebrew.sh"
  NONINTERACTIVE=1 /bin/bash "$staging/install-homebrew.sh" </dev/null
fi
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1
if [[ ! -x /opt/homebrew/bin/python3 ]]; then
  /opt/homebrew/bin/brew install --quiet --yes python3 </dev/null
fi
/opt/homebrew/bin/python3 --version
/usr/bin/xcode-select -p
if [[ $# == 3 ]]; then
  cd "$3"
  /opt/homebrew/bin/python3 - <<'PY'
from pathlib import Path
from cisetup import config, power, util, xcode

# This explicit initial setup has a transient administrator credential.
# Scheduled convergence remains noninteractive and cannot request sudo.
util.INTERACTIVE = True
xcode.ensure_sudoless_select()
xcode.ensure_wwdr_certificate()
power.ensure(config.load(Path.cwd()))
PY
fi
printf '%s\n' 'Guest prerequisites installed.'
