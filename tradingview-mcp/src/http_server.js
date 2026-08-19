/**
 * HTTP REST bridge for the TradingView MCP — port 9223.
 * Lets the Python app call TradingView tools without speaking MCP JSON-RPC.
 */

import { createServer } from 'http';
import { setSymbol } from './core/chart.js';
import { captureScreenshot } from './core/capture.js';
import { getOhlcv, getStudyValues, getQuote } from './core/data.js';

const PORT = 9223;

function json(res, data, status = 200) {
  const body = JSON.stringify(data);
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) });
  res.end(body);
}

async function readBody(req) {
  return new Promise((resolve) => {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      try { resolve(JSON.parse(body || '{}')); }
      catch { resolve({}); }
    });
  });
}

async function handle(req, res) {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const path = url.pathname;

  try {
    // Health check — fast CDP list scan, no retry loop
    if (path === '/tv/health') {
      let connected = false;
      try {
        const r = await fetch('http://localhost:9222/json/list', { signal: AbortSignal.timeout(1000) });
        const targets = await r.json();
        connected = targets.some(t => /tradingview\.com\/chart/i.test(t?.url || ''));
      } catch { connected = false; }
      return json(res, { connected, port: PORT });
    }

    // Switch symbol — POST /tv/symbol  { symbol: "AAPL" }
    if (path === '/tv/symbol' && req.method === 'POST') {
      const { symbol } = await readBody(req);
      if (!symbol) return json(res, { error: 'symbol required' }, 400);
      const result = await setSymbol({ symbol });
      return json(res, result);
    }

    // Screenshot — GET /tv/screenshot
    if (path === '/tv/screenshot') {
      const result = await captureScreenshot({ region: 'chart' });
      if (!result.file_path) return json(res, { error: 'Screenshot failed', detail: result }, 500);
      return json(res, { success: true, file_path: result.file_path });
    }

    // Real-time quote — GET /tv/quote
    if (path === '/tv/quote') {
      const result = await getQuote({});
      if (result.open && result.close && !result.change_pct) {
        const pct = (result.close - result.open) / result.open * 100;
        result.change_pct = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
      }
      return json(res, result);
    }

    // OHLCV summary + indicators — GET /tv/data
    if (path === '/tv/data') {
      const [ohlcv, indicators] = await Promise.all([
        getOhlcv({ count: 100, summary: true }).catch(e => ({ error: e.message })),
        getStudyValues().catch(e => ({ error: e.message })),
      ]);
      return json(res, { success: true, ohlcv, indicators });
    }

    return json(res, { error: 'Not found' }, 404);
  } catch (err) {
    return json(res, { error: err.message }, 500);
  }
}

export function startHttpServer() {
  const server = createServer(handle);
  server.listen(PORT, '127.0.0.1', () => {
    console.error(`[HTTP Bridge] Listening on http://127.0.0.1:${PORT}`);
  });
  return server;
}
