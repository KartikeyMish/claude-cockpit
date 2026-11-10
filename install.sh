#!/usr/bin/env bash
# claude-cockpit installer for a git clone. Safe to re-run: every step is
# idempotent and any file it changes is backed up first (*.cockpit-backup-<timestamp>).
#
#   ./install.sh                  plugin (release channel KartikeyMish/claude-cockpit#stable) + ccx
#   ./install.sh --with-ghostty   also Ghostty (macOS/Homebrew) + the cockpit profile
#   ./install.sh --local          use this checkout as the plugin source (development)
#   ./install.sh --no-plugin | --no-ccx   skip a component
#
# The plugin and profile steps are `ccx setup` (lib/cockpit/installer.py), the same
# code Homebrew users run. This script adds what only a clone needs: the
# ~/.local/bin/ccx link and, with --with-ghostty, the Ghostty app.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${COCKPIT_BIN_DIR:-$HOME/.local/bin}"
STAMP="$(date +%Y%m%d-%H%M%S)"

with_ghostty=0; with_plugin=1; with_ccx=1; force_local=0
for arg in "$@"; do
  case "$arg" in
    --with-ghostty) with_ghostty=1 ;;
    --no-plugin) with_plugin=0 ;;
    --no-ccx) with_ccx=0 ;;
    --local) force_local=1 ;;
    -h | --help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
backup() {
  if [ -e "$1" ]; then cp -pL "$1" "$1.cockpit-backup-$STAMP" && warn "backed up $1"; fi
}

preflight() {
  say "Preflight"
  if [ "$with_plugin" -eq 1 ]; then
    command -v claude >/dev/null || die "claude CLI not found: https://code.claude.com/docs/en/setup"
    ok "claude $(claude --version 2>/dev/null | head -1)"
  fi
  command -v python3 >/dev/null || die "python3 not found (needed by ccx and the agent status line)"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "python3 >= 3.9 required, found $(python3 --version)"
  ok "$(python3 --version)"
}

install_ccx() {
  say "ccx cockpit"
  mkdir -p "$BIN_DIR"
  local target="$BIN_DIR/ccx"
  if [ -e "$target" ] && [ ! -L "$target" ]; then backup "$target"; rm -f "$target"; fi
  ln -sfn "$REPO/bin/ccx" "$target"
  ok "linked $target → $REPO/bin/ccx"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on PATH; add to your shell rc: export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
}

install_ghostty_app() {
  say "Ghostty terminal"
  if command -v ghostty >/dev/null || [ -x /Applications/Ghostty.app/Contents/MacOS/ghostty ]; then
    ok "Ghostty is installed"
  elif [ "$(uname)" = "Darwin" ] && command -v brew >/dev/null; then
    brew install --cask ghostty
  else
    warn "install Ghostty from https://ghostty.org/download; the profile is installed either way"
  fi
}

# Plugin and profile: one implementation, shared with Homebrew installs.
run_setup() {
  local args=()
  if [ "$with_plugin" -eq 0 ]; then args+=(--no-plugin); fi
  if [ "$with_ghostty" -eq 0 ]; then args+=(--no-ghostty); fi
  if [ "$force_local" -eq 1 ]; then args+=(--source "$REPO"); fi
  # ${args[@]+...}: an empty array under `set -u` is an error in bash 3.2 (macOS).
  python3 "$REPO/bin/ccx" setup ${args[@]+"${args[@]}"}
}

preflight
if [ "$with_ccx" -eq 1 ]; then install_ccx; fi
if [ "$with_ghostty" -eq 1 ]; then install_ghostty_app; fi
if [ "$with_plugin" -eq 1 ] || [ "$with_ghostty" -eq 1 ]; then run_setup; fi

say "Checking the setup"
python3 "$REPO/bin/ccx" doctor || true
