#!/usr/bin/env bash
# Compile FX_Execution.mq5 with the MetaEditor bundled in MetaTrader 5.app (Wine).
#
# Usage:
#   scripts/mt5/compile_ea.sh            # compile in a staging folder only
#   scripts/mt5/compile_ea.sh --deploy   # compile, then copy the .ex5 into the terminal's MQL5/Experts
#
# Deploying replaces the EA binary the terminal loads; MT5 reloads it on any chart
# where FX_Execution is attached. Only deploy to the demo terminal.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SOURCE="$REPO_ROOT/fx_ai_engine/mt5_ea/FX_Execution.mq5"
ARTIFACTS="$REPO_ROOT/fx_ai_engine/artifacts/mt5-build"
WINE="/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"
export WINEPREFIX="$HOME/Library/Application Support/net.metaquotes.wine.metatrader5"
TERMINAL_DIR="$WINEPREFIX/drive_c/Program Files/MetaTrader 5"
STAGE="/private/tmp/fx-mt5-build"
DEPLOY=0
[[ "${1:-}" == "--deploy" ]] && DEPLOY=1

[[ -x "$WINE" ]] || { echo "Wine not found at $WINE" >&2; exit 1; }
[[ -f "$TERMINAL_DIR/metaeditor64.exe" ]] || { echo "metaeditor64.exe not found" >&2; exit 1; }

rm -rf "$STAGE"
mkdir -p "$STAGE" "$ARTIFACTS"
cp "$SOURCE" "$STAGE/FX_Execution.mq5"
cp -R "$TERMINAL_DIR/MQL5/Include" "$STAGE/Include"

# Z: maps to the macOS root inside Wine.
"$WINE" "$TERMINAL_DIR/metaeditor64.exe" \
  /compile:"Z:$STAGE/FX_Execution.mq5" \
  /include:"Z:$STAGE" \
  /log:"Z:$STAGE/compile.log" >/dev/null 2>&1 || true

[[ -f "$STAGE/compile.log" ]] || { echo "No compile log produced" >&2; exit 1; }
iconv -f UTF-16LE -t UTF-8 "$STAGE/compile.log" > "$ARTIFACTS/compile.txt" 2>/dev/null || cp "$STAGE/compile.log" "$ARTIFACTS/compile.txt"
cp "$STAGE/compile.log" "$ARTIFACTS/compile.log"
RESULT_LINE="$(grep -a "Result:" "$ARTIFACTS/compile.txt" | tail -1 || true)"
echo "${RESULT_LINE:-no result line}"
grep -a -E ": error |: warning " "$ARTIFACTS/compile.txt" || true

if [[ ! -f "$STAGE/FX_Execution.ex5" ]] || ! grep -q " 0 errors" <<<"$RESULT_LINE"; then
  echo "Compile failed" >&2
  exit 1
fi
cp "$STAGE/FX_Execution.ex5" "$ARTIFACTS/FX_Execution.ex5"
shasum -a 256 "$SOURCE" "$ARTIFACTS/FX_Execution.ex5" | tee "$ARTIFACTS/build.sha256"

if [[ $DEPLOY -eq 1 ]]; then
  cp "$ARTIFACTS/FX_Execution.ex5" "$TERMINAL_DIR/MQL5/Experts/FX_Execution.ex5"
  echo "Deployed to $TERMINAL_DIR/MQL5/Experts/FX_Execution.ex5"
fi
