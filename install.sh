#!/usr/bin/env bash
# claude-cockpit installer. Safe to re-run: every step is idempotent and any
# file it changes is backed up first (*.cockpit-backup-<timestamp>).
#
#   ./install.sh                  plugin + ccx
#   ./install.sh --with-ghostty   also Ghostty (macOS/Homebrew) + the cockpit profile
#   ./install.sh --local          use this checkout as the plugin source (default: its git remote)
#   ./install.sh --no-plugin | --no-ccx   skip a component
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKETPLACE="claude-cockpit"
PLUGIN="orchestrator@${MARKETPLACE}"
BIN_DIR="${COCKPIT_BIN_DIR:-$HOME/.local/bin}"
GHOSTTY_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/ghostty"
INCLUDE_LINE="config-file = ?cockpit.conf"
INCLUDE_RE='^[[:space:]]*config-file[[:space:]]*=[[:space:]]*\??cockpit\.conf[[:space:]]*$'
STAMP="$(date +%Y%m%d-%H%M%S)"

with_ghostty=0; with_plugin=1; with_ccx=1; force_local=0
for arg in "$@"; do
  case "$arg" in
    --with-ghostty) with_ghostty=1 ;;
    --no-plugin) with_plugin=0 ;;
    --no-ccx) with_ccx=0 ;;
    --local) force_local=1 ;;
    -h | --help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
# Capture first, then match: `cmd | grep -q` can SIGPIPE `cmd` and fail under pipefail.
output_has() { local out; out="$("${@:2}" 2>/dev/null || true)"; grep -qF -- "$1" <<<"$out"; }

preflight() {
  say "Preflight"
  command -v claude >/dev/null || die "claude CLI not found: https://code.claude.com/docs/en/setup"
  ok "claude $(claude --version 2>/dev/null | head -1)"
  command -v python3 >/dev/null || die "python3 not found (needed by ccx and the agent status line)"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "python3 >= 3.9 required, found $(python3 --version)"
  ok "$(python3 --version)"
}

marketplace_source() {
  local remote=""
  if [ "$force_local" -eq 0 ]; then
    remote="$(git -C "$REPO" remote get-url origin 2>/dev/null || true)"
  fi
  if [ -n "$remote" ]; then echo "$remote"; else echo "$REPO"; fi
}

install_plugin() {
  say "Orchestrator plugin"
  local source; source="$(marketplace_source)"
  if output_has "\"$MARKETPLACE\"" claude plugin marketplace list --json \
     || output_has "$MARKETPLACE" claude plugin marketplace list; then
    if [ "$force_local" -eq 1 ]; then
      claude plugin marketplace remove "$MARKETPLACE" >/dev/null
      claude plugin marketplace add "$source" >/dev/null
      ok "marketplace $MARKETPLACE re-pointed at $source"
    else
      claude plugin marketplace update "$MARKETPLACE" >/dev/null
      ok "marketplace $MARKETPLACE refreshed (use --local to install this checkout instead)"
    fi
  else
    claude plugin marketplace add "$source" >/dev/null
    ok "marketplace $MARKETPLACE added from $source"
  fi
  if output_has "\"$PLUGIN\"" claude plugin list --json; then
    claude plugin update "$PLUGIN" >/dev/null 2>&1 || warn "plugin update reported a problem; run: claude plugin update $PLUGIN"
    claude plugin enable "$PLUGIN" >/dev/null 2>&1 || true
    ok "$PLUGIN updated and enabled"
  else
    claude plugin install "$PLUGIN" >/dev/null
    ok "$PLUGIN installed"
  fi
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

ghostty_bin() {
  if command -v ghostty >/dev/null; then command -v ghostty
  elif [ -x /Applications/Ghostty.app/Contents/MacOS/ghostty ]; then
    echo /Applications/Ghostty.app/Contents/MacOS/ghostty
  fi
}

include_profile() {
  local main="$1" tmp
  if grep -Eqs "$INCLUDE_RE" "$main"; then ok "already included from $main"; return 0; fi
  backup "$main"
  tmp="$(mktemp "${TMPDIR:-/tmp}/ghostty-config.XXXXXX")"
  {
    echo "# claude-cockpit profile (remove these two lines to opt out)"
    echo "$INCLUDE_LINE"
    echo
    if [ -e "$main" ]; then cat "$main"; fi
  } > "$tmp"
  # Write through the path (not mv) so a symlinked dotfile keeps its link and mode.
  cat "$tmp" > "$main"
  rm -f "$tmp"
  ok "included from $main (your own settings below it still win)"
}

install_ghostty() {
  say "Ghostty terminal"
  if [ -z "$(ghostty_bin)" ]; then
    if [ "$(uname)" = "Darwin" ] && command -v brew >/dev/null; then
      brew install --cask ghostty
    else
      warn "install Ghostty from https://ghostty.org/download, then re-run with --with-ghostty"
      return 0
    fi
  fi
  ok "$("$(ghostty_bin)" --version | head -1)"
  mkdir -p "$GHOSTTY_DIR"
  local profile="$GHOSTTY_DIR/cockpit.conf"
  if [ -e "$profile" ] && ! cmp -s "$profile" "$REPO/terminal/ghostty/cockpit.conf"; then
    backup "$profile"
  fi
  cp "$REPO/terminal/ghostty/cockpit.conf" "$profile"
  ok "profile → $profile"
  include_profile "$GHOSTTY_DIR/config"
  if "$(ghostty_bin)" +validate-config >/dev/null 2>&1; then ok "Ghostty config valid"
  else warn "Ghostty reports config problems: run \`ghostty +validate-config\`"; fi
}

preflight
if [ "$with_plugin" -eq 1 ]; then install_plugin; fi
if [ "$with_ccx" -eq 1 ]; then install_ccx; fi
if [ "$with_ghostty" -eq 1 ]; then install_ghostty; fi

say "Done. Open a new Claude session (or /clear) to load the orchestrator, then run: ccx"
"$REPO/bin/ccx" doctor || true
