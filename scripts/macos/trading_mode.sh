#!/usr/bin/env bash
# Switch this Mac between trading mode and development/research mode.
# See fx_ai_engine/docs/2026-11-04-mac-host-operating-model.md.
#
#   scripts/macos/trading_mode.sh on       # prepare the host for an owner-started trading run
#   scripts/macos/trading_mode.sh off      # back to development/research mode
#   scripts/macos/trading_mode.sh status   # run the host check (trading mode)
#
# "on" never starts the engine or places orders. It disables App Nap for MT5,
# installs the keep-awake agent, and prints the manual steps that need the MT5
# window or administrator rights. Start the engine afterwards with
# scripts/macos/install_launchd.sh --engine once the host check shows no BLOCK.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENGINE_DIR="$REPO_ROOT/fx_ai_engine"
PYTHON="$REPO_ROOT/.venv/bin/python"
MT5_ID="net.metaquotes.wine.MetaTrader5"

host_check() { (cd "$ENGINE_DIR" && "$PYTHON" -m validation.host_check --mode "$1") || true; }

case "${1:-status}" in
  on)
    defaults write "$MT5_ID" NSAppSleepDisabled -bool YES
    echo "App Nap disabled for MetaTrader 5 (takes effect after MT5 restarts)."
    "$REPO_ROOT/scripts/macos/install_launchd.sh" >/dev/null
    echo "Keep-awake agent installed (com.fxai.keepawake: caffeinate -i -m -s)."
    cat <<'EOF'

Manual steps (once):
  1. Plug in the charger. macOS only blocks system sleep on AC power. Keep the lid open
     (a closed lid sleeps the Mac unless an external display, keyboard and power are attached).
  2. In MT5: Tools > Options > Charts > Max bars in chart = 100000 (not Unlimited).
     Tools > Options > Server: untick "Enable news". Keep ONE chart (EURUSD H1 with FX_Execution),
     no indicators. Market Watch: right-click > Hide All, then show only the 6 traded pairs.
     Restart MT5 so App Nap and Max bars take effect, then re-attach FX_Execution.
  3. Optional, administrator: on AC power only, stop system sleep and Power Nap churn:
       sudo pmset -c sleep 0 powernap 0 standby 0 disksleep 0
     (undo: sudo pmset -c sleep 1 powernap 1 standby 1 disksleep 10)
  4. Before trading hours: quit extra Kiro windows and pause workflows, close browser tabs,
     and stop any replay job. After a day of heavy use, reboot first to clear swap.
EOF
    host_check trading
    ;;
  off)
    "$REPO_ROOT/scripts/macos/install_launchd.sh" --uninstall >/dev/null || true
    echo "Engine, watchdog, calendar and keep-awake agents removed; the Mac can sleep again."
    echo "App Nap for MT5 stays disabled (harmless). Re-enable with: defaults delete $MT5_ID NSAppSleepDisabled"
    host_check research
    ;;
  status)
    host_check trading
    ;;
  *)
    echo "usage: $0 on|off|status" >&2; exit 2;;
esac
