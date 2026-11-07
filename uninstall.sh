#!/usr/bin/env bash
# Remove claude-cockpit. Keeps the Ghostty app and your own configs; only
# undoes what install.sh added. Changed files are backed up first.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKETPLACE="claude-cockpit"
PLUGIN="orchestrator@${MARKETPLACE}"
BIN_DIR="${COCKPIT_BIN_DIR:-$HOME/.local/bin}"
GHOSTTY_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/ghostty"
INCLUDE_RE='^[[:space:]]*config-file[[:space:]]*=[[:space:]]*\??cockpit\.conf[[:space:]]*$'
MARKER_RE='^# claude-cockpit profile'
STAMP="$(date +%Y%m%d-%H%M%S)"

ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }

if command -v claude >/dev/null; then
  if claude plugin uninstall "$PLUGIN" >/dev/null 2>&1; then ok "uninstalled $PLUGIN"; fi
  if claude plugin marketplace remove "$MARKETPLACE" >/dev/null 2>&1; then ok "removed marketplace $MARKETPLACE"; fi
fi

if [ -L "$BIN_DIR/ccx" ] && [ "$(readlink "$BIN_DIR/ccx")" = "$REPO/bin/ccx" ]; then
  rm -f "$BIN_DIR/ccx" && ok "removed $BIN_DIR/ccx"
fi

main="$GHOSTTY_DIR/config"
if [ -f "$main" ] && grep -Eqs "$INCLUDE_RE" "$main"; then
  cp -pL "$main" "$main.cockpit-backup-$STAMP"
  tmp="$(mktemp "${TMPDIR:-/tmp}/ghostty-config.XXXXXX")"
  grep -Ev -e "$INCLUDE_RE" -e "$MARKER_RE" "$main" > "$tmp" || true   # empty result is fine
  cat "$tmp" > "$main"   # write through symlinks
  rm -f "$tmp"
  ok "removed include from $main"
fi
if [ -f "$GHOSTTY_DIR/cockpit.conf" ]; then
  mv "$GHOSTTY_DIR/cockpit.conf" "$GHOSTTY_DIR/cockpit.conf.cockpit-backup-$STAMP"
  ok "moved cockpit.conf aside (backup kept)"
fi
echo "Done. Restart Claude sessions to drop the routing policy."
