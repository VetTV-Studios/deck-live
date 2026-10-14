#!/bin/sh
# Deck Live uninstaller.
#   curl -L https://github.com/VetTV-Studios/deck-live/raw/main/uninstall.sh | sh
# Removes the plugin folder. Your settings/stream keys in ~/homebrew/settings/Deck Live are kept
# unless you run with PURGE=1 (e.g. "... | PURGE=1 sh").
set -eu

PLUGIN_DIR_NAME="Deck Live"
HB="${DECKY_HOMEBREW_DIR:-$HOME/homebrew}"
PLUGINS_DIR="${DECKY_PLUGINS_DIR:-$HB/plugins}"
DEST="$PLUGINS_DIR/$PLUGIN_DIR_NAME"

ok()  { printf '\033[1;32m✔\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✘ Error:\033[0m %s\n' "$*" >&2; exit 1; }

[ -d "$PLUGINS_DIR" ] || die "Decky Loader not found ($PLUGINS_DIR missing)."
if [ "$(id -u)" -eq 0 ] || [ -w "$PLUGINS_DIR" ]; then SUDO=""; else SUDO="sudo"; $SUDO -v || die "sudo authentication failed"; fi

if [ -e "$DEST" ]; then
    $SUDO rm -rf "$DEST"
    ok "Removed $DEST"
else
    ok "Deck Live is not installed (nothing to remove)."
fi
$SUDO rm -rf "$PLUGINS_DIR/.deck-live.new" "$PLUGINS_DIR/.deck-live.old"
if [ "${PURGE:-0}" = "1" ]; then
    $SUDO rm -rf "$HB/settings/$PLUGIN_DIR_NAME" "$HB/logs/$PLUGIN_DIR_NAME"
    ok "Removed settings (including saved stream keys) and logs."
fi
if command -v systemctl >/dev/null 2>&1 && $SUDO systemctl restart plugin_loader; then
    ok "Restarted Decky Loader."
else
    printf 'Could not restart plugin_loader; reboot or run: sudo systemctl restart plugin_loader\n' >&2
fi
