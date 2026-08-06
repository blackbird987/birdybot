#!/usr/bin/env bash
# Generate a double-clickable launcher for start.sh.
#
#   ./scripts/install_desktop_launcher.sh
#
# Writes "Start Claude Bot.desktop" into the repo root, ~/Desktop, and the
# application menu. The repo's absolute path is baked in at generation time, so
# re-run this after moving the repo or when a live-USB session mounts it
# somewhere new.
set -uo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

NAME="Start Claude Bot"
FILE="$NAME.desktop"

chmod +x start.sh

write_launcher() {
    cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$NAME
Comment=Start the Claude Code Discord bot
Exec=env LAUNCHED_FROM_GUI=1 "$REPO/start.sh"
Path=$REPO
Icon=utilities-terminal
Terminal=true
Categories=Development;
EOF
    chmod +x "$1"
    # KDE/GNOME refuse to launch .desktop files they don't trust; this marks it.
    gio set "$1" metadata::trusted true 2>/dev/null || true
}

write_launcher "$REPO/$FILE"
echo "Created $REPO/$FILE"

if [[ -d "$HOME/Desktop" ]]; then
    write_launcher "$HOME/Desktop/$FILE"
    echo "Created $HOME/Desktop/$FILE"
fi

mkdir -p "$HOME/.local/share/applications"
write_launcher "$HOME/.local/share/applications/claude-bot-start.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Added to application menu (search for \"$NAME\")"

echo
echo "Double-click the desktop icon to start the bot."
echo "The first launch may ask you to confirm/trust the launcher — accept it."
