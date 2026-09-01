/**
 * Ensures TradingView is accessible via CDP on port 9222.
 *
 * Priority:
 * 1. CDP on 9222 already has a TV chart tab → done
 * 2. CDP on 9222 exists but no TV tab → open tradingview.com/chart in a new tab
 * 3. CDP not available → launch Chrome with --remote-debugging-port=9222, then open TV
 */
import { execSync, spawn } from 'child_process';
import { existsSync } from 'fs';

const CDP_PORT = 9222;
const TV_URL = 'https://www.tradingview.com/chart/';

async function cdpList() {
  const r = await fetch(`http://localhost:${CDP_PORT}/json/list`, { signal: AbortSignal.timeout(2000) });
  return r.json();
}

async function openTab(url) {
  const r = await fetch(`http://localhost:${CDP_PORT}/json/new`, { method: 'PUT', signal: AbortSignal.timeout(3000) });
  const tab = await r.json();
  // Navigate via CDP WebSocket
  const { default: CDP } = await import('chrome-remote-interface');
  const client = await CDP({ target: tab.id });
  await client.Page.enable();
  await client.Page.navigate({ url });
  await new Promise(r => setTimeout(r, 2000));
  await client.close();
  console.log(`Opened TradingView in new tab: ${tab.id}`);
}

function findChrome() {
  const candidates = [
    `${process.env['PROGRAMFILES(X86)']}\\Google\\Chrome\\Application\\chrome.exe`,
    `${process.env.PROGRAMFILES}\\Google\\Chrome\\Application\\chrome.exe`,
    `${process.env.LOCALAPPDATA}\\Google\\Chrome\\Application\\chrome.exe`,
    `${process.env['PROGRAMFILES(X86)']}\\Microsoft\\Edge\\Application\\msedge.exe`,
    `${process.env.PROGRAMFILES}\\Microsoft\\Edge\\Application\\msedge.exe`,
    `${process.env.LOCALAPPDATA}\\Microsoft\\Edge\\Application\\msedge.exe`,
  ];
  for (const p of candidates) {
    if (p && existsSync(p)) return p;
  }
  // Try where
  try {
    const found = execSync('where chrome.exe 2>nul', { timeout: 2000 }).toString().trim().split('\n')[0].trim();
    if (found && existsSync(found)) return found;
  } catch {}
  return null;
}

async function main() {
  // Step 1: Check if CDP already has a TV chart tab
  try {
    const targets = await cdpList();
    const tvTab = targets.find(t => /tradingview\.com\/chart/i.test(t?.url || ''));
    if (tvTab) {
      console.log(`TradingView chart already open: ${tvTab.title}`);
      process.exit(0);
    }

    // Step 2: CDP is available but no TV tab — open one
    console.log('CDP available, opening TradingView tab...');
    await openTab(TV_URL);
    console.log('TradingView opened in browser. Please log in if prompted.');
    process.exit(0);
  } catch {
    // CDP not available
  }

  // Step 3: Launch Chrome with CDP
  const chrome = findChrome();
  if (!chrome) {
    console.error('Chrome/Edge not found. Please open Chrome manually and navigate to tradingview.com/chart');
    console.error(`Then start Chrome with: chrome.exe --remote-debugging-port=${CDP_PORT}`);
    process.exit(1);
  }

  // Use a dedicated clean profile — no extensions, no sync, much lighter than default profile
  const profileDir = `${process.env.TEMP}\\tv-bridge-profile`;
  console.log(`Launching browser with CDP: ${chrome}`);
  const proc = spawn(chrome, [
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${profileDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    '--disable-sync',
    '--disable-background-networking',
    TV_URL,
  ], { detached: true, stdio: 'ignore' });
  proc.unref();

  // Wait for CDP to become available
  for (let i = 0; i < 15; i++) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const targets = await cdpList();
      const tvTab = targets.find(t => /tradingview/i.test(t?.url || ''));
      if (tvTab) {
        console.log('Browser with TradingView is ready.');
        process.exit(0);
      }
    } catch {}
  }

  console.warn('Browser launched but TradingView tab not confirmed. Continuing...');
  process.exit(0);
}

main().catch(e => { console.error(e.message); process.exit(1); });
