import { evaluate } from './connection.js';

const DEFAULT_TIMEOUT = 10000;  // cold-start safety net only -- a warm switch settles in ~1.3s
// 50ms, measured 2026-08-04: one bare CDP evaluate round-trip costs 14ms, and a
// real symbol/timeframe load finishes (spinner clears) at ~1200-1400ms. At the
// old 200ms the poll interval, not the load, decided when we noticed -- up to
// 190ms of pure lag to spot the clear, then another 200ms for the second
// confirming poll. 50ms keeps a comfortable margin over the 14ms round-trip
// while cutting that detection lag ~4x.
const POLL_INTERVAL = 50;
const CHART_API = 'window.TradingViewApi._activeChartWidgetWV.value()';

/** "BATS:NVDA" and "NVDA" are the same symbol for readiness purposes. */
function normalizeSymbol(sym) {
  return String(sym).toUpperCase().split(':').pop().trim();
}

/** chart.resolution() answers "1D" where callers pass "D"; "120"/"30" are identical both ways. */
function normalizeTimeframe(tf) {
  const s = String(tf).toUpperCase().trim();
  const m = s.match(/^1?([DWM])$/);
  return m ? m[1] : s;
}

/**
 * True once the chart has actually settled on the expected symbol/timeframe.
 *
 * Reads the chart API's own symbol()/resolution() rather than scraping the page
 * (2026-08-04). The previous version compared expectedSymbol against
 * `[data-name="legend-source-title"]`, which in the live app resolves to a
 * stale, unrelated element -- measured returning "SInvesco QQQ Trust, Series 1"
 * for every ticker, so the symbol comparison could never match and this
 * function returned false after burning the full 10s timeout on EVERY symbol
 * switch. tv_data.log is millions of lines of exactly that one warning, with
 * zero successes. Cost was ~10s per switch, 5-7 switches per ticker, ~20
 * tickers per /monitorall run.
 *
 * It also polled `[class*="bar"]` for a stable "bar count", which matches
 * toolbar/sidebar elements, not price bars -- measured pinned at 111 regardless
 * of chart state. That stability check was the only reason setTimeframe (which
 * passed expectedSymbol=null and so skipped the broken symbol comparison)
 * returned true at all, and it was stable for the wrong reason. Both DOM
 * heuristics are gone; symbol and resolution now come from the chart API, which
 * the probe showed correct within 0.7s.
 *
 * `expectedTf` was accepted and then never read by the old implementation --
 * setTimeframe's readiness was never actually verified against the requested
 * resolution. It is honoured now.
 *
 * The loading-spinner check stays: measured genuinely tracking the load
 * (visible ~1.8s after a switch, then hidden), so it is what makes this wait
 * mean "data has arrived" rather than just "the API accepted the request".
 */
export async function waitForChartReady(expectedSymbol = null, expectedTf = null, timeout = DEFAULT_TIMEOUT) {
  const start = Date.now();
  const wantSymbol = expectedSymbol ? normalizeSymbol(expectedSymbol) : null;
  const wantTf = expectedTf ? normalizeTimeframe(expectedTf) : null;
  let stableCount = 0;

  while (Date.now() - start < timeout) {
    const state = await evaluate(`
      (function() {
        var spinner = document.querySelector('[class*="loader"]')
          || document.querySelector('[class*="loading"]')
          || document.querySelector('[data-name="loading"]');
        var isLoading = spinner && spinner.offsetParent !== null;

        var symbol = null, resolution = null;
        try {
          var chart = ${CHART_API};
          symbol = chart.symbol();
          resolution = chart.resolution();
        } catch {}

        return { isLoading: !!isLoading, symbol: symbol, resolution: resolution };
      })()
    `);

    if (!state || !state.symbol) {
      // Chart API not reachable yet (page still booting) -- not a mismatch, just
      // nothing to compare against yet.
      stableCount = 0;
      await new Promise(r => setTimeout(r, POLL_INTERVAL));
      continue;
    }

    const symbolOk = !wantSymbol || normalizeSymbol(state.symbol) === wantSymbol;
    const tfOk = !wantTf || (state.resolution && normalizeTimeframe(state.resolution) === wantTf);

    if (state.isLoading || !symbolOk || !tfOk) {
      stableCount = 0;
      await new Promise(r => setTimeout(r, POLL_INTERVAL));
      continue;
    }

    // Two consecutive clean polls, not one -- a switch can read momentarily
    // settled between the API accepting it and the spinner appearing.
    if (++stableCount >= 2) return true;
    await new Promise(r => setTimeout(r, POLL_INTERVAL));
  }

  // Timeout -- callers must verify independently (tv_data.py's _set_symbol
  // cross-checks chart_get_state and the OHLCV bar signature either way).
  return false;
}
