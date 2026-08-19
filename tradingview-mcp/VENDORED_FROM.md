# Vendoring provenance

This directory is an **independent, vendored copy** of the TradingView↔CDP MCP connector. It was copied from a sibling project so that `Trading New` has no runtime dependency on that project's continued existence or location.

- **Source path:** `D:\Stocks Playbook\tradingview-mcp`
- **Date copied:** 2026-07-02
- **What was copied:** `src/`, `scripts/`, `skills/`, `agents/`, `tests/`, `package.json`, `package-lock.json`, docs (`README.md`, `SETUP_GUIDE.md`, `RESEARCH.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CLAUDE.md`, `LICENSE`, `.gitignore`)
- **What was excluded:** `node_modules/` (reinstalled fresh via `npm install` in this directory), `screenshots/` (runtime output, not source), `.git/`

This was a frozen, unmodified snapshot as of the date above, with one deliberate divergence made since:

## Local modification: `data_get_ohlcv` pagination (`before` parameter)
**Files:** `src/core/data.js` (`getOhlcv`), `src/tools/data.js` (`data_get_ohlcv` tool schema).
**Why:** the upstream tool always anchors its `count` window to `bars.lastIndex()` (the live edge) — there is no way to ask for bars further back than the most recent `count` (max 500), which silently caps daily history at ~2 years. `Trading New` requires 5-year daily lookback (`STRATEGY_v3.md`/`SCREENER_v3.md`). Added an optional `before` (unix seconds) parameter: when set, the tool binary-searches the in-memory bar buffer for the last bar strictly before that timestamp and anchors the window there instead, plus returns `reached_start_of_buffer` so a caller knows when it's hit the edge of what TradingView has loaded (e.g. IPO date, or needs `chart_scroll_to_date` first to trigger more lazy-loading). This lets a caller page backward by re-issuing calls with `before` = the oldest `time` seen in the previous page.
**Not upstream** — if a bug is ever suspected in `data_get_ohlcv` unrelated to pagination, check whether the upstream copy at the source path has already fixed it before re-deriving a fix independently; this specific pagination capability won't be there to compare against.

## Local modification: `chart_scroll_to_date` missing dependency resolution (bug fix)
**File:** `src/core/chart.js` (`scrollToDate`).
**Why:** found via live testing — every other exported function in this file resolves its
`evaluate`/`evaluateAsync` helpers via `const { evaluate } = _resolve(_deps);` at the top of
the function (supporting both the real connection module and test doubles), but
`scrollToDate` referenced the bare `evaluate` identifier directly without that resolution
step or even accepting a `_deps` parameter — a pre-existing upstream bug, not something this
project introduced. It threw `ReferenceError: evaluate is not defined` on every call, which
broke `get_daily_history`'s 5-year pagination (that function depends on `chart_scroll_to_date`
to trigger TradingView's lazy-loading of older history before paging backward). Fixed by
adding `_deps` to the function signature and the same `_resolve(_deps)` line every other
function in this file already has. Pure bug fix, no behavior change beyond "the function
actually works" — worth checking if upstream has already fixed this independently.

## Known limitation (not fixed): `chart_scroll_to_date` / `chart_set_visible_range` don't trigger real history loading
**Files:** `src/core/chart.js` (`scrollToDate`, `setVisibleRange`).
**Finding, from live testing:** both functions reposition the chart viewport via
`timeScale().zoomToBarsRange(fromIdx, toIdx)`, computed from indices within the bars
**already loaded** in `mainSeries().bars()`. Neither actually asks TradingView to fetch
more historical data. Confirmed empirically: after calling `chart_scroll_to_date` to jump
5 years back and waiting up to 15 seconds, `bars.size()` (`total_available` in
`data_get_ohlcv`'s response) never changed from its initial value (509). TradingView's
real lazy-loading of older history is triggered by genuine user scroll/pan input on the
chart canvas, which neither of these tools simulates.

**Practical consequence:** `get_daily_history` in `bot/tv_data.py` is capped at whatever's
preloaded for a freshly-set symbol — observed ~500-700 daily bars (~2 years), not the 5
years `STRATEGY_v3.md`/`SCREENER_v3.md` ask for. `get_daily_history`'s `before`-pagination
(previous section) still helps with whatever small surplus is already buffered beyond 500,
but can't force-load genuinely new history. Decided (2026-07-02) to accept this ~2-year
ceiling for now rather than build real CDP-level mouse-wheel input simulation (`Input`
domain `dispatchMouseEvent` with `type: mouseWheel`, mimicking actual user scroll — a
JS-dispatched `WheelEvent` was not attempted/expected to work reliably since canvas
renderers commonly ignore synthetic input events) — revisit if the shorter window turns
out to matter in practice.

## Local modification: TradingView Desktop -> TradingView-in-Chrome (2026-07-12)

**Files:** `src/core/health.js` (`launch`), `src/tools/health.js` (`tv_launch`,
`tv_health_check` hint text), `src/core/tab.js` (comments), `src/server.js` (instructions
text), `scripts/launch_tv_debug.bat`, `scripts/launch_tv_debug_mac.sh`,
`scripts/launch_tv_debug_linux.sh`, `scripts/launch_tv_debug.vbs` (removed), plus
`CLAUDE.md`/`README.md`/`SETUP_GUIDE.md`/`SECURITY.md`/`CONTRIBUTING.md`/`RESEARCH.md`.

**Why:** the TradingView Desktop (Electron) app stopped being usable in this setup, so
this project switched to running TradingView as a regular web page in Chrome instead. CDP
itself needed zero changes to support this — `connection.js`'s `findChartTarget()` was
already matching on `tradingview.com/chart` URLs generically, which works identically
whether that tab is inside the Electron desktop app or a plain Chrome window, and every
`window.TradingViewApi...` JS path used throughout `src/core/*.js` is a property of the
TradingView *web app* itself, present on `tradingview.com` regardless of host process. The
only thing that was actually Desktop-specific was the **launcher**: `tv_launch` searched
for and spawned `TradingView.exe`.

**What changed:** `launch()` in `src/core/health.js` now finds and spawns Chrome instead,
pointed at `https://www.tradingview.com/chart/`, using a **dedicated `--user-data-dir`**
(`tradingview-mcp-chrome-profile`, platform-appropriate location) so it never touches the
user's everyday Chrome profile/windows/tabs. The old TradingView-Desktop version defaulted
`kill_existing` to `true` and ran `taskkill /IM TradingView.exe` / `pkill -f TradingView`
unconditionally — safe for a single-purpose Electron binary, but `chrome.exe` is also the
user's daily browser, so blindly killing it by image name would have been destructive.
`kill_existing` now **defaults to `false`**, and when explicitly requested it only kills
Chrome processes whose command line contains the dedicated profile dir (matched via `wmic`
on Windows, `pgrep -f` elsewhere) — the user's regular Chrome windows are never targeted.
The Windows-Store-specific `launch_tv_debug.vbs` (launched the MSIX-packaged Desktop app
via `ELECTRON_EXTRA_LAUNCH_ARGS`) was deleted as dead weight; there is no equivalent
concept for a Chrome tab.

**User-facing consequence:** on first `tv_launch` (or the platform launch script), the
user must log into TradingView once in the Chrome window that opens — the session then
persists in that dedicated profile across future launches, same as the old Desktop app's
persistent login did.
