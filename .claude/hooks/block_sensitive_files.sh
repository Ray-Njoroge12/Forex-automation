#!/usr/bin/env bash
# PreToolUse hook: block writes to sensitive files
# Reads JSON from stdin: {"tool_name": "...", "tool_input": {"file_path": "..."}}
# Exit 1 to block the tool call; exit 0 to allow.

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

# Block .env files (any .env or .env.* variant)
case "$BASENAME" in
    .env | .env.* | *.env)
        echo "BLOCKED: Claude cannot write to env files ($FILE_PATH)" >&2
        exit 1
        ;;
esac

# Block SQLite database
case "$BASENAME" in
    trading_state.db)
        echo "BLOCKED: Claude cannot write to the trading database ($FILE_PATH)" >&2
        exit 1
        ;;
esac

# Block bridge runtime files (pending signals and active locks)
case "$FILE_PATH" in
    */bridge/pending_signals/*.json)
        echo "BLOCKED: Claude cannot write to pending_signals/ ($FILE_PATH)" >&2
        exit 1
        ;;
    */bridge/active_locks/*.lock)
        echo "BLOCKED: Claude cannot write to active_locks/ ($FILE_PATH)" >&2
        exit 1
        ;;
esac

exit 0
