import { z } from 'zod';
import { jsonResult } from './_format.js';
import * as core from '../core/watchlist.js';

export function registerWatchlistTools(server) {
  server.tool('watchlist_get', 'Get all symbols from the current TradingView watchlist with last price, change, and change%', {}, async () => {
    try { return jsonResult(await core.get()); }
    catch (err) { return jsonResult({ success: false, error: err.message }, true); }
  });

  server.tool('watchlist_add', 'Add a symbol to a TradingView watchlist', {
    symbol: z.string().describe('Symbol to add (e.g., AAPL, BTCUSD, ES1!, NYMEX:CL1!)'),
    list: z.string().optional().describe('Named watchlist to switch to first (must already appear in the "recently used" quick-switch list). Omit to add to whichever list is currently active.'),
  }, async ({ symbol, list }) => {
    try { return jsonResult(await core.add({ symbol, list })); }
    catch (err) {
      // Try to close any open search/input on error
      try {
        const { getClient } = await import('../connection.js');
        const c = await getClient();
        await c.Input.dispatchKeyEvent({ type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
        await c.Input.dispatchKeyEvent({ type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
      } catch (_) {}
      return jsonResult({ success: false, error: err.message }, true);
    }
  });

  server.tool('watchlist_remove', 'Remove a symbol from whichever TradingView watchlist is currently active (use watchlist_select_list first to target a specific named list)', {
    symbol: z.string().describe('Symbol to remove (e.g., AAPL) -- matches by short or full (EXCHANGE:SYMBOL) form'),
  }, async ({ symbol }) => {
    try { return jsonResult(await core.remove({ symbol })); }
    catch (err) { return jsonResult({ success: false, error: err.message }, true); }
  });

  server.tool('watchlist_select_list', 'Switch the active TradingView watchlist to a named list (must already appear in the "recently used" quick-switch list)', {
    name: z.string().describe('Exact watchlist name, e.g. "Bot Watchlist"'),
  }, async ({ name }) => {
    try { return jsonResult(await core.selectList({ name })); }
    catch (err) { return jsonResult({ success: false, error: err.message }, true); }
  });
}
