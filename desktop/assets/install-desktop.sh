#!/bin/sh
# Register this portable package in the current user's application menu.
set -eu
bundle=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
data=${XDG_DATA_HOME:-"$HOME/.local/share"}
mkdir -p "$data/applications" "$data/icons/hicolor"
cp -R "$bundle/share/icons/hicolor/." "$data/icons/hicolor/"
# Desktop Entry Exec uses its own quoting, then string-value escaping.
executable=$(printf '%s' "$bundle/lisem-desktop" | sed 's/[\\"`$]/\\&/g; s/\\/\\\\/g; s/%/%%/g')
{
    while IFS= read -r line; do
        case "$line" in
            Exec=*) printf 'Exec="%s"\n' "$executable" ;;
            *) printf '%s\n' "$line" ;;
        esac
    done < "$bundle/share/applications/com.listenai.emulator.desktop"
} > "$data/applications/com.listenai.emulator.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$data/applications"
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache --ignore-theme-index "$data/icons/hicolor" >/dev/null 2>&1 || true
fi
printf 'Lisem added to the application menu.\n'
