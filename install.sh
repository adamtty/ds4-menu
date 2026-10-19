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
  lsof -nP -iTCP:8011 -sTCP:LISTEN >/dev/null 2>&1 || launchctl print "$GUI/$LABEL" >/dev/null 2>&1 || break
  sleep 1
done

if [ "${1:-}" = "uninstall" ]; then
  rm -f "$AGENTS/$LABEL.plist"
  rm -rf "$APP" "$STATE/app"
  echo "ds4 menu removed"
  exit 0
fi

# older versions ran a copy of the proxy from here; the app now embeds it
rm -rf "$STATE/app"

# Only rebuilt when a source (or this script) changes: it's ad-hoc signed, so every rebuild
# looks like a new app to macOS and it would ask for ~/Documents access again. The proxy is
# compiled into the binary, so editing ds4proxy.py also means a rebuild.
BIN="$APP/Contents/MacOS/ds4"
stale=0
# a different DS4_DIR also needs a rebuild, since the path is compiled in
[ "$(cat "$APP/Contents/Resources/ds4-dir" 2>/dev/null)" = "$DS4_DIR" ] || stale=1
for f in "$DIR/ds4proxy.py" "$DIR/menubar/DS4Menu.swift" "$DIR/menubar/build.sh" "$DIR/menubar/logo@2x.png" "$0"; do
  [ "$f" -nt "$BIN" ] && stale=1
done
if [ ! -x "$BIN" ] || [ $stale = 1 ]; then
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$DIR/menubar/logo.png" "$DIR/menubar/logo@2x.png" "$APP/Contents/Resources/"
DS4_DIR="$DS4_DIR" "$DIR/menubar/build.sh" "$BIN"
printf %s "$DS4_DIR" > "$APP/Contents/Resources/ds4-dir"
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
# hardened runtime: macOS ignores DYLD_* variables (e.g. planted in the LaunchAgent), so no
# other code can be loaded into the process that holds the Documents permission
codesign --force --options runtime --sign - "$APP" >/dev/null 2>&1
fi
# KeepAlive only on crash, so "Quit ds4" stays quit until next login
cat > "$AGENTS/$LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$BIN</string></array>
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
