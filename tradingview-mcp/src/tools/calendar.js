import { z } from 'zod';
import { jsonResult } from './_format.js';
import * as core from '../core/calendar.js';

export function registerCalendarTools(server) {
  server.tool('calendar_get_earnings', 'Get next/last earnings report date for one or more tickers, from TradingView (no chart needed). Use before claiming an "earnings_verified" date -- never infer from memory.', {
    symbols: z.array(z.string()).min(1).describe('Ticker symbols, bare or "EXCHANGE:TICKER" (e.g. ["AAPL", "NVDA"])'),
  }, async ({ symbols }) => {
    try { return jsonResult(await core.getEarningsCalendar({ symbols })); }
    catch (err) { return jsonResult({ success: false, error: err.message }, true); }
  });

  server.tool('calendar_get_economic', 'Get upcoming/recent US macro calendar events (CPI, PPI, NFP, FOMC, Fed rate decisions, unemployment, GDP by default) from TradingView (no chart needed). Use for the "same-day Fed/CPI/NFP release" event check.', {
    days_ahead: z.coerce.number().optional().describe('Days forward from now to include (default 7)'),
    days_back: z.coerce.number().optional().describe('Days backward from now to include (default 0)'),
    countries: z.string().optional().describe('Comma-separated ISO country codes (default "US")'),
    keywords: z.array(z.string()).optional().describe('Override the default macro-event keyword filter (CPI/PPI/NFP/FOMC/etc.) with your own substrings'),
  }, async ({ days_ahead, days_back, countries, keywords }) => {
    try { return jsonResult(await core.getEconomicCalendar({ days_ahead, days_back, countries, keywords })); }
    catch (err) { return jsonResult({ success: false, error: err.message }, true); }
  });
}
