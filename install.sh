#!/bin/sh
# Deck Live installer / updater for Decky Loader (no developer mode needed).
#   curl -L https://github.com/VetTV-Studios/deck-live/raw/main/install.sh | sh
# Run from Desktop Mode (Konsole) as your normal user. Safe to re-run to update.
set -eu

REPO="VetTV-Studios/deck-live"
PLUGIN_DIR_NAME="Deck Live"   # folder name inside the release zip
FALLBACK_URL="https://github.com/$REPO/releases/latest/download/deck-live.zip"
PLUGINS_DIR="${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✔\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✘ Error:\033[0m %s\n' "$*" >&2; exit 1; }

# Reject path escapes and require the plugin layout. Reads the central directory only;
# nothing is extracted. python3 is required so a bad member cannot be unpacked by unzip.
check_zip_layout() {
    _zip=$1
    _root=$2
    command -v python3 >/dev/null 2>&1 || die "python3 is required to verify the zip layout before unpacking"
    python3 - "$_zip" "$_root" <<'PY'
import sys, zipfile
zpath, root = sys.argv[1], sys.argv[2]
try:
    z = zipfile.ZipFile(zpath)
except zipfile.BadZipFile:
    sys.stderr.write("zip is corrupt\n")
    sys.exit(1)
names = z.namelist()
z.close()
for n in names:
    norm = n.replace("\\", "/")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if norm.startswith("/") or ".." in parts:
        sys.stderr.write("zip member escapes the archive: %s\n" % n)
        sys.exit(1)
need = ["%s/plugin.json" % root, "%s/main.py" % root, "%s/dist/index.js" % root]
missing = [p for p in need if p not in set(names)]
if missing:
    sys.stderr.write("unexpected zip layout (missing %s)\n" % ", ".join(missing))
    sys.exit(1)
PY
}

if [ "${1:-}" = "--check-zip" ]; then
    [ -n "${2:-}" ] || die "usage: install.sh --check-zip <file.zip>"
    check_zip_layout "$2" "$PLUGIN_DIR_NAME"
    exit 0
fi

# --- preflight -------------------------------------------------------------
[ -d "$PLUGINS_DIR" ] || die "Decky Loader not found ($PLUGINS_DIR missing). Install Decky first: https://decky.xyz"
command -v curl >/dev/null 2>&1 || die "curl is required"

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
elif [ -w "$PLUGINS_DIR" ]; then
    SUDO=""
else
    command -v sudo >/dev/null 2>&1 || die "sudo is required ($PLUGINS_DIR is not writable)"
    SUDO="sudo"
    info "Administrator rights are needed to write to $PLUGINS_DIR."
    info "If you never set a password, run 'passwd' in Konsole first."
    $SUDO -v || die "sudo authentication failed"
fi

TMP="$(mktemp -d "${TMPDIR:-/tmp}/deck-live.XXXXXX")"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT INT TERM

# --- find & download -------------------------------------------------------
if [ -n "${DECK_LIVE_ZIP:-}" ]; then          # local zip override (testing/offline)
    info "Using local zip: $DECK_LIVE_ZIP"
    cp "$DECK_LIVE_ZIP" "$TMP/plugin.zip" || die "cannot read $DECK_LIVE_ZIP"
else
    info "Looking up the latest release..."
    URL="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
        | grep -o '"browser_download_url": *"[^"]*\.zip"' | head -n 1 | sed 's/.*"\(https[^"]*\)"/\1/' || true)"
    if [ -z "$URL" ]; then
        info "GitHub API unavailable (rate limit?); using fallback URL."
        URL="$FALLBACK_URL"
    fi
    info "Downloading $URL"
    curl -fL --retry 3 -o "$TMP/plugin.zip" "$URL" || die "download failed"
fi

# --- extract & validate ----------------------------------------------------
# Layout is checked before any member is written to disk.
check_zip_layout "$TMP/plugin.zip" "$PLUGIN_DIR_NAME"
mkdir "$TMP/x"
if command -v unzip >/dev/null 2>&1; then
    unzip -q "$TMP/plugin.zip" -d "$TMP/x" || die "zip is corrupt"
elif command -v python3 >/dev/null 2>&1; then
    python3 -m zipfile -e "$TMP/plugin.zip" "$TMP/x" || die "zip is corrupt"
else
    die "need unzip or python3 to extract the plugin"
fi
SRC="$TMP/x/$PLUGIN_DIR_NAME"
if [ ! -f "$SRC/plugin.json" ] || [ ! -f "$SRC/main.py" ] || [ ! -f "$SRC/dist/index.js" ]; then
    die "unexpected zip layout (missing '$PLUGIN_DIR_NAME/plugin.json', main.py or dist/index.js)"
fi
VERSION="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$SRC/package.json" | head -n 1)"

# --- install atomically ----------------------------------------------------
# Stage inside the plugins dir (same filesystem) so the final swap is a rename.
DEST="$PLUGINS_DIR/$PLUGIN_DIR_NAME"
STAGE="$PLUGINS_DIR/.deck-live.new"
OLD="$PLUGINS_DIR/.deck-live.old"
$SUDO rm -rf "$STAGE" "$OLD"
$SUDO cp -r "$SRC" "$STAGE" || die "could not write to $PLUGINS_DIR"
if [ -e "$DEST" ]; then
    $SUDO mv "$DEST" "$OLD"
fi
if ! $SUDO mv "$STAGE" "$DEST"; then
    [ -e "$OLD" ] && $SUDO mv "$OLD" "$DEST"
    die "could not install into $DEST (previous version restored)"
fi
$SUDO rm -rf "$OLD"
ok "Installed Deck Live ${VERSION:-?} to $DEST"

# --- restart Decky ---------------------------------------------------------
if command -v systemctl >/dev/null 2>&1 && $SUDO systemctl restart plugin_loader; then
    ok "Restarted Decky Loader (plugin_loader)."
else
    printf 'Could not restart plugin_loader; reboot or run: sudo systemctl restart plugin_loader\n' >&2
fi
ok "Done. Open Quick Access > Decky > Deck Live (switch to Game Mode)."
