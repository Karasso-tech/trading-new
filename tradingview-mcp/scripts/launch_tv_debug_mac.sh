#!/bin/bash
# Launch a dedicated Chrome window on macOS pointed at TradingView with Chrome
# DevTools Protocol enabled. Uses its own browser profile (tradingview-mcp-chrome-profile),
# completely separate from your everyday Chrome -- this script never touches your
# regular browsing windows/tabs/profile.
# Usage: ./scripts/launch_tv_debug_mac.sh [port]

PORT="${1:-9222}"
PROFILE_DIR="$HOME/Library/Application Support/tradingview-mcp-chrome-profile"

# Auto-detect Chrome install location
APP=""
LOCATIONS=(
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)

for loc in "${LOCATIONS[@]}"; do
  if [ -f "$loc" ]; then
    APP="$loc"
    break
  fi
done

if [ -z "$APP" ]; then
  echo "Error: Google Chrome not found."
  echo "Checked: /Applications/Google Chrome.app, ~/Applications/Google Chrome.app"
  echo ""
  echo "If installed elsewhere, run manually:"
  echo "  /path/to/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=$PORT --user-data-dir=\"$PROFILE_DIR\" https://www.tradingview.com/chart/"
  exit 1
fi

echo "Found Chrome at: $APP"
echo "Using dedicated profile: $PROFILE_DIR"
echo "(First run only: log into TradingView in the window that opens -- the session"
echo " persists in this profile after that, so you won't need to log in again.)"
echo "Launching with --remote-debugging-port=$PORT ..."
"$APP" --remote-debugging-port=$PORT --user-data-dir="$PROFILE_DIR" --no-first-run --no-default-browser-check https://www.tradingview.com/chart/ &
TV_PID=$!
echo "PID: $TV_PID"

# Wait for CDP to be ready
echo "Waiting for CDP..."
for i in $(seq 1 15); do
  if curl -s "http://localhost:$PORT/json/version" > /dev/null 2>&1; then
    echo "CDP ready at http://localhost:$PORT"
    curl -s "http://localhost:$PORT/json/version" | python3 -m json.tool 2>/dev/null || curl -s "http://localhost:$PORT/json/version"
    exit 0
  fi
  sleep 1
done

echo "Warning: CDP not responding after 15s. Chrome may still be loading."
echo "Check manually: curl http://localhost:$PORT/json/version"
