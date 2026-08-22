#!/bin/zsh
#
# setup.zsh — bootstrap + entry point for the self-hosted CI runner setup.
#
# This script only guarantees the minimal foundation (Xcode Command Line
# Tools, Homebrew, a modern python3) and then hands off to the Python
# implementation in cisetup/. Everything is safe to re-run; re-runs only
# change what needs changing.
#
# Usage:
#   ./setup.zsh                        converge the machine (default)
#   ./setup.zsh status                 show current state
#   ./setup.zsh store-pat [token]      save a GitHub PAT to the Keychain
#                                      (without a token: prints the PAT
#                                      requirements and prompts, input hidden)
#   ./setup.zsh uninstall              deregister the runner from GitHub
#   ./setup.zsh converge --non-interactive
#                                      what the boot LaunchAgent runs: never
#                                      prompts, never uses sudo

set -e
set -u
set -o pipefail

SCRIPT_DIR="${0:A:h}"

NONINTERACTIVE=0
for arg in "$@"; do
  if [[ $arg == --non-interactive ]]; then
    NONINTERACTIVE=1
  fi
done

log()  { print -r -- "==> $*" }
fail() { print -r -- "setup.zsh: error: $*" >&2; exit 1 }

if [[ $(uname -s) != Darwin ]]; then
  fail "this only runs on macOS"
fi
if (( EUID == 0 )); then
  fail "do not run as root — the runner and its services are per-user"
fi

# --- Xcode Command Line Tools (needed for git and by Homebrew) --------------

if ! /usr/bin/xcode-select -p &>/dev/null; then
  if (( NONINTERACTIVE )); then
    fail "Xcode Command Line Tools are missing; run ./setup.zsh interactively once"
  fi
  log "Xcode Command Line Tools are missing — requesting installation (GUI dialog)."
  /usr/bin/xcode-select --install || true
  fail "re-run ./setup.zsh once the Command Line Tools installation has finished"
fi

# --- Self-update (unattended runs only) --------------------------------------
# The boot LaunchAgent should always run the latest committed version of this
# setup. Manual runs are left alone — the operator controls the checkout.
# A failed pull (offline, no upstream, dirty tree) must never block converging.

if (( NONINTERACTIVE )) && [[ -z ${CI_SETUP_PULLED:-} ]]; then
  if /usr/bin/git -C "$SCRIPT_DIR" rev-parse --git-dir &>/dev/null; then
    typeset before="" after=""
    before="$(/usr/bin/git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || true)"
    if /usr/bin/git -C "$SCRIPT_DIR" pull --ff-only --quiet; then
      after="$(/usr/bin/git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || true)"
      if [[ -n $before && -n $after && $before != $after ]]; then
        log "setup updated ($before -> $after) — re-executing"
        # Guard against re-exec loops; git replaces files by rename, so the
        # currently running copy stayed intact until this point.
        CI_SETUP_PULLED=1 exec "$SCRIPT_DIR/setup.zsh" "$@"
      fi
    else
      log "git pull failed (offline? no upstream?) — continuing with the current version"
    fi
  fi
fi

# --- Homebrew ----------------------------------------------------------------

find_brew() {
  local candidate
  for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [[ -x $candidate ]]; then
      print -r -- "$candidate"
      return 0
    fi
  done
  return 1
}

BREW_BIN="$(find_brew || true)"
if [[ -z $BREW_BIN ]]; then
  if (( NONINTERACTIVE )); then
    fail "Homebrew is missing; run ./setup.zsh interactively once"
  fi
  log "Installing Homebrew (its installer will ask for your password once)."
  typeset installer
  installer="$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
    || fail "could not download the Homebrew installer (no network?)"
  /bin/bash -c "$installer"
  BREW_BIN="$(find_brew || true)"
  if [[ -z $BREW_BIN ]]; then
    fail "Homebrew installation did not complete"
  fi
fi

eval "$("$BREW_BIN" shellenv)"

# Make brew available in login shells too (handy when SSHing into the box).
if ! grep -qsF "brew shellenv" "$HOME/.zprofile"; then
  print -r -- "eval \"\$($BREW_BIN shellenv)\"" >> "$HOME/.zprofile"
fi

# --- Python >= 3.11 from Homebrew -------------------------------------------

PYTHON="$("$BREW_BIN" --prefix)/bin/python3"
if [[ ! -x $PYTHON ]]; then
  log "Installing python3 via Homebrew."
  # --yes: Homebrew 6 asks for confirmation before installing by default.
  "$BREW_BIN" install --quiet --yes python3
fi
if [[ ! -x $PYTHON ]]; then
  fail "expected Homebrew python3 at $PYTHON"
fi

# --- Hand off to the Python implementation ----------------------------------

exec "$PYTHON" "$SCRIPT_DIR/bin/ci-setup" "$@"
