/**
 * Earnings + economic (CPI/PPI/NFP/FOMC/etc.) calendar data.
 *
 * Unlike every other file in core/, these two calls do NOT go through the CDP
 * `evaluate()` bridge -- they hit TradingView's own public JSON endpoints
 * directly with plain Node `fetch()` (Referer/Origin headers only, no auth,
 * no cookies). Verified live 2026-07-26: both endpoints return real CORS
 * headers scoped to https://www.tradingview.com, i.e. these are the same
 * requests tradingview.com's own widgets make from the browser -- not an
 * internal/private API being reverse-engineered blind.
 *
 * Consequence: unlike chart_get_state/data_get_ohlcv/etc., these tools work
 * even when no TradingView chart tab is open in Chrome at all (no CDP target
 * needed). They fail only on real network/HTTP errors from TradingView.
 */

const SCANNER_URL = 'https://scanner.tradingview.com/america/scan';
const ECONOMIC_CALENDAR_URL = 'https://economic-calendar.tradingview.com/events';
const TV_HEADERS = { Referer: 'https://www.tradingview.com/', Origin: 'https://www.tradingview.com' };

// Symbols are commonly passed around this codebase as bare tickers ("AAPL"),
// not "EXCHANGE:TICKER" -- the scanner endpoint requires the full form and
// silently drops anything it can't resolve (no error), so try each ticker
// against the exchanges that cover the vast majority of US listings and keep
// whichever comes back.
const US_EXCHANGES = ['NASDAQ', 'NYSE', 'AMEX'];

// TradingView's earnings_release_*_time code: -1 = not yet confirmed by the
// company, 0 = before market open, 1 = after market close, 2 = during market
// hours. Reverse-engineered by live probing (2026-07-26) -- not documented
// anywhere by TradingView.
const SESSION_LABELS = { '-1': 'unconfirmed', 0: 'before_market', 1: 'after_market', 2: 'during_market' };

// Default high-relevance macro releases (STRATEGY_v3.md section a: "CPI, NFP,
// FOMC" + PPI). Matched case-insensitively as a substring of either the
// event's `title` or `indicator` field. Deliberately excludes low-importance
// sub-components (e.g. "PPI Ex Food, Energy and Trade YoY") by requiring the
// SHORT canonical form to appear, not the other way around.
export const DEFAULT_MACRO_KEYWORDS = [
  'CPI', 'PPI', 'Non Farm Payrolls', 'Nonfarm Payrolls', 'FOMC',
  'Fed Interest Rate', 'Interest Rate Decision', 'Unemployment Rate', 'GDP',
];

function isoDate(unixSeconds) {
  if (unixSeconds == null) return null;
  return new Date(unixSeconds * 1000).toISOString();
}

export async function getEarningsCalendar({ symbols } = {}) {
  const clean = (symbols || []).map(s => String(s).trim().toUpperCase().replace(/^[A-Z]+:/, '')).filter(Boolean);
  if (clean.length === 0) throw new Error('symbols required (non-empty array of tickers)');

  const tickers = clean.flatMap(t => US_EXCHANGES.map(ex => `${ex}:${t}`));
  const resp = await fetch(SCANNER_URL, {
    method: 'POST',
    headers: { ...TV_HEADERS, 'Content-Type': 'text/plain;charset=UTF-8' },
    body: JSON.stringify({
      symbols: { tickers, query: { types: [] } },
      columns: ['earnings_release_next_date', 'earnings_release_next_time', 'earnings_release_date', 'earnings_release_time', 'description'],
    }),
  });
  if (!resp.ok) throw new Error(`TradingView scanner HTTP ${resp.status}`);
  const body = await resp.json();

  const bySymbol = {};
  for (const row of body.data || []) {
    const ticker = row.s.split(':')[1];
    const [nextDate, nextTime, lastDate, lastTime, description] = row.d;
    // Prefer the first exchange that resolves (NASDAQ before NYSE before AMEX);
    // don't overwrite an already-found match with a later, spurious one.
    if (bySymbol[ticker]) continue;
    bySymbol[ticker] = {
      symbol: ticker,
      resolved_symbol: row.s,
      description: description ?? null,
      next_earnings_date: nextDate != null ? isoDate(nextDate).slice(0, 10) : null,
      next_earnings_session: nextTime != null ? (SESSION_LABELS[nextTime] ?? null) : null,
      next_earnings_confirmed: nextTime != null && nextTime !== -1,
      last_earnings_date: lastDate != null ? isoDate(lastDate).slice(0, 10) : null,
      last_earnings_session: lastTime != null ? (SESSION_LABELS[lastTime] ?? null) : null,
    };
  }

  const results = clean.map(t => bySymbol[t] || {
    symbol: t, resolved_symbol: null, description: null,
    next_earnings_date: null, next_earnings_session: null, next_earnings_confirmed: false,
    last_earnings_date: null, last_earnings_session: null,
    error: 'symbol not found on NASDAQ/NYSE/AMEX',
  });

  return { success: true, source: 'tradingview_scanner', fetched_at: new Date().toISOString(), results };
}

export async function getEconomicCalendar({ days_ahead, days_back, countries, keywords } = {}) {
  const ahead = days_ahead ?? 7;
  const back = days_back ?? 0;
  const countryList = countries || 'US';
  const kw = (keywords && keywords.length ? keywords : DEFAULT_MACRO_KEYWORDS).map(k => k.toLowerCase());

  const now = new Date();
  const from = new Date(now.getTime() - back * 86400000);
  const to = new Date(now.getTime() + ahead * 86400000);

  const url = `${ECONOMIC_CALENDAR_URL}?from=${from.toISOString()}&to=${to.toISOString()}&countries=${encodeURIComponent(countryList)}`;
  const resp = await fetch(url, { headers: TV_HEADERS });
  if (!resp.ok) throw new Error(`TradingView economic calendar HTTP ${resp.status}`);
  const body = await resp.json();
  if (body.status !== 'ok') throw new Error(`TradingView economic calendar returned status: ${body.status}`);

  const allEvents = (body.result || []).map(e => ({
    date: e.date, title: e.title, indicator: e.indicator, country: e.country,
    importance: e.importance, actual: e.actual, forecast: e.forecast, previous: e.previous, unit: e.unit,
  }));

  const matched = allEvents.filter(e => {
    const hay = `${e.title} ${e.indicator}`.toLowerCase();
    return kw.some(k => hay.includes(k));
  }).sort((a, b) => a.date.localeCompare(b.date));

  return {
    success: true, source: 'tradingview_economic_calendar', fetched_at: new Date().toISOString(),
    window: { from: from.toISOString(), to: to.toISOString(), countries: countryList },
    keywords_matched: kw, event_count: matched.length, events: matched,
  };
}
