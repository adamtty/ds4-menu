#!/bin/bash
# Install (or reinstall) the ds4 menu bar app, which runs the oMLX-style lazy proxy for
# ds4-server on 127.0.0.1:8010, as a per-user LaunchAgent that starts at login.
# Usage: ./install.sh            install / restart (re-run after editing any source file)
#        ./install.sh uninstall  stop and remove it (settings and log in ~/.local/share/ds4-menu are kept)
#        DS4_DIR=/path/to/ds4 ./install.sh   use a ds4 checkout other than ~/Documents/code-work/ds4
set -eu
LABEL=com.adam.ds4menu
DS4_DIR=${DS4_DIR:-$HOME/Documents/code-work/ds4}
DIR=$(cd "$(dirname "$0")" && pwd)
AGENTS="$HOME/Library/LaunchAgents"
STATE="$HOME/.local/share/ds4-menu"
APP="$HOME/Applications/ds4.app"
GUI="gui/$(id -u)"

# bootout makes the app quit, which unloads ds4-server first; wait for that
launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
for _ in $(seq 50); do
  pgrep -f "$STATE/app/ds4proxy.py" >/dev/null || launchctl print "$GUI/$LABEL" >/dev/null 2>&1 || break
  sleep 1
done

if [ "${1:-}" = "uninstall" ]; then
  rm -f "$AGENTS/$LABEL.plist"
  rm -rf "$APP" "$STATE/app"
  echo "ds4 menu removed"
  exit 0
fi

mkdir -p "$STATE/app"
cp "$DIR/ds4proxy.py" "$STATE/app/"

# Only rebuilt when its source (or this script) changes: it's ad-hoc signed, so every rebuild
# looks like a new app to macOS and it would ask for ~/Documents access again.
BIN="$APP/Contents/MacOS/ds4"
if [ ! -x "$BIN" ] || [ "$DIR/menubar/DS4Menu.swift" -nt "$BIN" ] || [ "$DIR/menubar/logo@2x.png" -nt "$BIN" ] || [ "$0" -nt "$BIN" ]; then
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$DIR/menubar/logo.png" "$DIR/menubar/logo@2x.png" "$APP/Contents/Resources/"
swiftc -O -target arm64-apple-macos13 -framework AppKit \
  -o "$BIN" "$DIR/menubar/DS4Menu.swift"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleName</key><string>ds4</string>
  <key>CFBundleExecutable</key><string>ds4</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>NSDocumentsFolderUsageDescription</key><string>ds4 runs ds4-server and its models from your ds4 checkout.</string>
</dict>
</plist>
EOF
codesign --force --sign - "$APP" >/dev/null 2>&1
fi
# KeepAlive only on crash, so "Quit ds4" stays quit until next login
cat > "$AGENTS/$LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$BIN</string></array>
  <key>EnvironmentVariables</key><dict><key>DS4_DIR</key><string>$DS4_DIR</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>ProcessType</key><string>Interactive</string>
  <key>ExitTimeOut</key><integer>45</integer>
</dict>
</plist>
EOF
launchctl bootstrap "$GUI" "$AGENTS/$LABEL.plist"

echo "ds4 menu running → endpoint http://127.0.0.1:8010 (model loads on first request)"
