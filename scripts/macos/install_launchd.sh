#!/usr/bin/env bash
# Install macOS launchd agents for unattended demo running (plan build I7).
#
#   scripts/macos/install_launchd.sh            # keep-awake agent only
#   scripts/macos/install_launchd.sh --engine   # keep-awake + watchdog (starts main.py --mode demo)
#   scripts/macos/install_launchd.sh --uninstall
#
# The keep-awake agent runs `caffeinate -i -m -s`, which prevents idle and disk
# sleep, and system sleep while on AC power. Closing the laptop lid still sleeps
# the Mac unless an external display is attached.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENGINE_DIR="$REPO_ROOT/fx_ai_engine"
PYTHON="$REPO_ROOT/.venv/bin/python"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$ENGINE_DIR/logs"
CAFF_LABEL="com.fxai.keepawake"
WD_LABEL="com.fxai.watchdog"
CAL_LABEL="com.fxai.calendar"

unload() { launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true; rm -f "$AGENTS/$1.plist"; }

if [[ "${1:-}" == "--uninstall" ]]; then
  unload "$CAL_LABEL"; unload "$WD_LABEL"; unload "$CAFF_LABEL"; echo "Removed $CAL_LABEL, $WD_LABEL and $CAFF_LABEL"; exit 0
fi

mkdir -p "$AGENTS" "$LOGS"

cat > "$AGENTS/$CAFF_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$CAFF_LABEL</string>
  <key>ProgramArguments</key><array><string>/usr/bin/caffeinate</string><string>-i</string><string>-m</string><string>-s</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
EOF
unload_only() { launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true; }
unload_only "$CAFF_LABEL"
launchctl bootstrap "gui/$(id -u)" "$AGENTS/$CAFF_LABEL.plist"
echo "Installed $CAFF_LABEL"

if [[ "${1:-}" == "--engine" ]]; then
  [[ -x "$PYTHON" ]] || { echo "Python venv not found at $PYTHON" >&2; exit 1; }
  cat > "$AGENTS/$WD_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$WD_LABEL</string>
  <key>ProgramArguments</key><array><string>$PYTHON</string><string>$ENGINE_DIR/watchdog.py</string></array>
  <key>WorkingDirectory</key><string>$ENGINE_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$LOGS/watchdog.launchd.out.log</string>
  <key>StandardErrorPath</key><string>$LOGS/watchdog.launchd.err.log</string>
</dict></plist>
EOF
  unload_only "$WD_LABEL"
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$WD_LABEL.plist"

  # Daily calendar refresh at 05:30 local time (before the London session).
  cat > "$AGENTS/$CAL_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$CAL_LABEL</string>
  <key>ProgramArguments</key><array><string>$PYTHON</string><string>-m</string><string>validation.refresh_mt5_calendar</string></array>
  <key>WorkingDirectory</key><string>$ENGINE_DIR</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>5</integer><key>Minute</key><integer>30</integer></dict>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOGS/calendar_refresh.log</string>
  <key>StandardErrorPath</key><string>$LOGS/calendar_refresh.log</string>
</dict></plist>
EOF
  unload_only "$CAL_LABEL"
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$CAL_LABEL.plist"
  echo "Installed $CAL_LABEL (daily 05:30 calendar refresh)"
  echo "Installed $WD_LABEL (watchdog starts: main.py --mode demo; engine reads fx_ai_engine/.env)"
fi
launchctl list | grep -E "com\.fxai\." || true
