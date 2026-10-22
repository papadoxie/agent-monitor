#!/bin/sh
# Builds build/AgentMonitor.app (needs only the Xcode Command Line Tools).
set -e
cd "$(dirname "$0")"
APP=build/AgentMonitor.app
rm -rf build && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O -o "$APP/Contents/MacOS/AgentMonitor" AgentMonitor.swift -framework Cocoa -framework WebKit
cp ../server.py ../index.html "$APP/Contents/Resources/"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Agent Monitor</string>
  <key>CFBundleExecutable</key><string>AgentMonitor</string>
  <key>CFBundleIdentifier</key><string>dev.agentmonitor.widget</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/><key>NSAllowsArbitraryLoadsInWebContent</key><true/></dict>
</dict></plist>
PLIST
codesign --force --deep -s - "$APP" 2>/dev/null || true
echo "Built $PWD/$APP"
