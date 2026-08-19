#!/bin/bash
# Launch a dedicated Chrome (or Chromium) window on Linux pointed at TradingView
# with Chrome DevTools Protocol enabled. Uses its own browser profile
# (~/.tradingview-mcp-chrome-profile), completely separate from your everyday
# browser -- this script never touches your regular browsing windows/tabs/profile.
# Usage: ./scripts/launch_tv_debug_linux.sh [port]

PORT="${1:-9222}"
PROFILE_DIR="$HOME/.tradingview-mcp-chrome-profile"

# Auto-detect Chrome/Chromium install location
APP=""
LOCATIONS=(
  "/usr/bin/google-chrome"
  "/usr/bin/google-chrome-stable"
  "/usr/bin/chromium-browser"
  "/usr/bin/chromium"
  "/snap/bin/chromium"
)

for loc in "${LOCATIONS[@]}"; do
  if [ -f "$loc" ] && [ -x "$loc" ]; then
    APP="$loc"
    break
  fi
done

# Fallback: which
if [ -z "$APP" ]; then
  APP=$(which google-chrome 2>/dev/null || which google-chrome-stable 2>/dev/null || which chromium-browser 2>/dev/null || which chromium 2>/dev/null)
fi

if [ -z "$APP" ] || [ ! -f "$APP" ]; then
  echo "Error: Chrome/Chromium not found."
  echo "Checked: /usr/bin/google-chrome, /usr/bin/chromium-browser, /usr/bin/chromium, snap, PATH"
  echo ""
  echo "If installed elsewhere, run manually:"
  echo "  /path/to/chrome --remote-debugging-port=$PORT --user-data-dir=\"$PROFILE_DIR\" https://www.tradingview.com/chart/"
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
