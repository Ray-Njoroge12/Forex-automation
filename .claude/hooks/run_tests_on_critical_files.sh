#!/usr/bin/env bash
# PostToolUse hook: run tests when critical files are edited
# Reads JSON from stdin: {"tool_name": "...", "tool_input": {"file_path": "..."}}
# Exit non-zero on test failure to surface as a hard stop to Claude.

FX_ENGINE_DIR="/mnt/c/Users/rayng/Desktop/Forex-automation/fx_ai_engine"
# Use Windows Python 3.11 which has the project dependencies (pytest, etc.)
PYTHON="/mnt/c/Users/rayng/AppData/Local/Programs/Python/Python311/python.exe"

# Use python3 for reliable JSON parsing (avoids jq dependency)
FILE_PATH=$(cat | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('tool_input', {}).get('file_path', ''))
except Exception:
    print('')
" 2>/dev/null)

if [ -z "$FILE_PATH" ]; then
    exit 0
fi

BASENAME=$(basename "$FILE_PATH")

# Map critical filenames to their test files
case "$BASENAME" in
    hard_risk_engine.py)
        TEST_FILE="tests/test_risk_engine.py"
        ;;
    signal_router.py)
        TEST_FILE="tests/test_signal_router.py"
        ;;
    portfolio_manager.py)
        TEST_FILE="tests/test_agents.py"
        ;;
    execution_feedback.py)
        TEST_FILE="tests/test_execution_feedback.py"
        ;;
    *)
        # Not a critical file — no tests to run
        exit 0
        ;;
esac

echo "--- Auto-test triggered for $BASENAME → $TEST_FILE ---" >&2

cd "$FX_ENGINE_DIR" || { echo "ERROR: Cannot cd to $FX_ENGINE_DIR" >&2; exit 1; }

USE_MT5_MOCK=1 "$PYTHON" -m pytest "$TEST_FILE" -q 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "TESTS FAILED for $BASENAME — review changes before proceeding." >&2
fi

exit $EXIT_CODE
