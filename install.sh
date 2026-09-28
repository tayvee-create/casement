#!/bin/sh
# Install Casement for the current user (no root needed).
set -e
SRC="$(dirname "$(readlink -f "$0")")"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA/casement"
BIN="$HOME/.local/bin"
APP_ID="io.github.tayvee_create.Casement"

# Remove the copy installed under the app's old name ("File Explorer" / w11explorer).
rm -rf "$DATA/w11explorer"
rm -f "$BIN/w11explorer" "$DATA/applications/io.github.w11explorer.desktop" \
      "$DATA/icons/hicolor/scalable/apps/io.github.w11explorer.svg"

mkdir -p "$DEST" "$BIN" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps"
rm -rf "$DEST/casement"
cp -r "$SRC/casement" "$DEST/"
find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} +

cat > "$BIN/casement" <<LAUNCH
#!/bin/sh
cd "$DEST" && exec python3 -m casement "\$@"
LAUNCH
chmod +x "$BIN/casement"

cp "$SRC/$APP_ID.desktop" "$DATA/applications/"
sed -i "s|^Exec=.*|Exec=$BIN/casement %U|" "$DATA/applications/$APP_ID.desktop"
cp "$SRC/casement/icons/hicolor/scalable/apps/$APP_ID.svg" "$DATA/icons/hicolor/scalable/apps/"
update-desktop-database "$DATA/applications" 2>/dev/null || true
gtk4-update-icon-cache -q -t -f "$DATA/icons/hicolor" 2>/dev/null || true

echo "Installed. Launch 'Casement' from the app grid, or run: $BIN/casement"
echo "To make it the default for opening folders:"
echo "  xdg-mime default $APP_ID.desktop inode/directory"
