/**
 * Core health/discovery/launch logic.
 */
import { getClient, getTargetInfo, evaluate } from '../connection.js';
import { existsSync } from 'fs';
import { execSync, spawn } from 'child_process';

export async function healthCheck() {
  await getClient();
  const target = await getTargetInfo();

  const state = await evaluate(`
    (function() {
      var result = { url: window.location.href, title: document.title };
      try {
        var chart = window.TradingViewApi._activeChartWidgetWV.value();
        result.symbol = chart.symbol();
        result.resolution = chart.resolution();
        result.chartType = chart.chartType();
        result.apiAvailable = true;
      } catch(e) {
        result.symbol = 'unknown';
        result.resolution = 'unknown';
        result.chartType = null;
        result.apiAvailable = false;
        result.apiError = e.message;
      }
      return result;
    })()
  `);

  return {
    success: true,
    cdp_connected: true,
    target_id: target.id,
    target_url: target.url,
    target_title: target.title,
    chart_symbol: state?.symbol || 'unknown',
    chart_resolution: state?.resolution || 'unknown',
    chart_type: state?.chartType ?? null,
    api_available: state?.apiAvailable ?? false,
  };
}

export async function discover() {
  const paths = await evaluate(`
    (function() {
      var results = {};
      try {
        var chart = window.TradingViewApi._activeChartWidgetWV.value();
        var methods = [];
        for (var k in chart) { if (typeof chart[k] === 'function') methods.push(k); }
        results.chartApi = { available: true, path: 'window.TradingViewApi._activeChartWidgetWV.value()', methodCount: methods.length, methods: methods.slice(0, 50) };
      } catch(e) { results.chartApi = { available: false, error: e.message }; }
      try {
        var col = window.TradingViewApi._chartWidgetCollection;
        var colMethods = [];
        for (var k in col) { if (typeof col[k] === 'function') colMethods.push(k); }
        results.chartWidgetCollection = { available: !!col, path: 'window.TradingViewApi._chartWidgetCollection', methodCount: colMethods.length, methods: colMethods.slice(0, 30) };
      } catch(e) { results.chartWidgetCollection = { available: false, error: e.message }; }
      try {
        var ws = window.ChartApiInstance;
        var wsMethods = [];
        for (var k in ws) { if (typeof ws[k] === 'function') wsMethods.push(k); }
        results.chartApiInstance = { available: !!ws, path: 'window.ChartApiInstance', methodCount: wsMethods.length, methods: wsMethods.slice(0, 30) };
      } catch(e) { results.chartApiInstance = { available: false, error: e.message }; }
      try {
        var bwb = window.TradingView && window.TradingView.bottomWidgetBar;
        var bwbMethods = [];
        if (bwb) { for (var k in bwb) { if (typeof bwb[k] === 'function') bwbMethods.push(k); } }
        results.bottomWidgetBar = { available: !!bwb, path: 'window.TradingView.bottomWidgetBar', methodCount: bwbMethods.length, methods: bwbMethods.slice(0, 20) };
      } catch(e) { results.bottomWidgetBar = { available: false, error: e.message }; }
      try {
        var replay = window.TradingViewApi._replayApi;
        results.replayApi = { available: !!replay, path: 'window.TradingViewApi._replayApi' };
      } catch(e) { results.replayApi = { available: false, error: e.message }; }
      try {
        var alerts = window.TradingViewApi._alertService;
        results.alertService = { available: !!alerts, path: 'window.TradingViewApi._alertService' };
      } catch(e) { results.alertService = { available: false, error: e.message }; }
      return results;
    })()
  `);

  const available = Object.values(paths).filter(v => v.available).length;
  const total = Object.keys(paths).length;

  return { success: true, apis_available: available, apis_total: total, apis: paths };
}

export async function uiState() {
  const state = await evaluate(`
    (function() {
      var ui = {};
      var bottom = document.querySelector('[class*="layout__area--bottom"]');
      ui.bottom_panel = { open: !!(bottom && bottom.offsetHeight > 50), height: bottom ? bottom.offsetHeight : 0 };
      var right = document.querySelector('[class*="layout__area--right"]');
      ui.right_panel = { open: !!(right && right.offsetWidth > 50), width: right ? right.offsetWidth : 0 };
      var monacoEl = document.querySelector('.monaco-editor.pine-editor-monaco');
      ui.pine_editor = { open: !!monacoEl, width: monacoEl ? monacoEl.offsetWidth : 0, height: monacoEl ? monacoEl.offsetHeight : 0 };
      var stratPanel = document.querySelector('[data-name="backtesting"]') || document.querySelector('[class*="strategyReport"]');
      ui.strategy_tester = { open: !!(stratPanel && stratPanel.offsetParent) };
      var widgetbar = document.querySelector('[data-name="widgetbar-wrap"]');
      ui.widgetbar = { open: !!(widgetbar && widgetbar.offsetWidth > 50) };
      ui.buttons = {};
      var btns = document.querySelectorAll('button');
      var seen = {};
      for (var i = 0; i < btns.length; i++) {
        var b = btns[i];
        if (b.offsetParent === null || b.offsetWidth < 15) continue;
        var text = b.textContent.trim();
        var aria = b.getAttribute('aria-label') || '';
        var dn = b.getAttribute('data-name') || '';
        var label = text || aria || dn;
        if (!label || label.length > 60) continue;
        var key = label.replace(/[^a-zA-Z0-9 ]/g, '').substring(0, 40);
        if (seen[key]) continue;
        seen[key] = true;
        var rect = b.getBoundingClientRect();
        var region = 'other';
        if (rect.y < 50) region = 'top_bar';
        else if (rect.y < 90 && rect.x < 650) region = 'toolbar';
        else if (rect.x < 45) region = 'left_sidebar';
        else if (rect.x > 650 && rect.y < 100) region = 'pine_header';
        else if (rect.y > 750) region = 'bottom_bar';
        if (!ui.buttons[region]) ui.buttons[region] = [];
        ui.buttons[region].push({ label: label.substring(0, 40), disabled: b.disabled, x: Math.round(rect.x), y: Math.round(rect.y) });
      }
      ui.key_buttons = {};
      var keyLabels = {
        'add_to_chart': /add to chart/i, 'save_and_add': /save and add/i,
        'update_on_chart': /update on chart/i, 'save': /^Save(Save)?$/,
        'saved': /^Saved/, 'publish_script': /publish script/i,
        'compile_errors': /error/i, 'unsaved_version': /unsaved version/i,
      };
      for (var i = 0; i < btns.length; i++) {
        var b = btns[i];
        if (b.offsetParent === null) continue;
        var text = b.textContent.trim();
        for (var k in keyLabels) {
          if (keyLabels[k].test(text)) {
            ui.key_buttons[k] = { text: text.substring(0, 40), disabled: b.disabled, visible: b.offsetWidth > 0 };
          }
        }
      }
      try {
        var chart = window.TradingViewApi._activeChartWidgetWV.value();
        ui.chart = { symbol: chart.symbol(), resolution: chart.resolution(), chartType: chart.chartType(), study_count: chart.getAllStudies().length };
      } catch(e) { ui.chart = { error: e.message }; }
      try {
        var replay = window.TradingViewApi._replayApi;
        function unwrap(v) { return (v && typeof v === 'object' && typeof v.value === 'function') ? v.value() : v; }
        ui.replay = { available: unwrap(replay.isReplayAvailable()), started: unwrap(replay.isReplayStarted()) };
      } catch(e) { ui.replay = { error: e.message }; }
      return ui;
    })()
  `);

  return { success: true, ...state };
}

// Dedicated Chrome profile, separate from the user's everyday browsing profile --
// this is what makes it safe to launch/kill this Chrome instance independently of
// whatever other Chrome windows the user has open. TradingView login persists here
// across launches once the user has signed in once.
function debugProfileDir(platform) {
  if (platform === 'win32') return `${process.env.LOCALAPPDATA}\\tradingview-mcp-chrome-profile`;
  if (platform === 'darwin') return `${process.env.HOME}/Library/Application Support/tradingview-mcp-chrome-profile`;
  return `${process.env.HOME}/.tradingview-mcp-chrome-profile`;
}

// Find PIDs of Chrome processes running with OUR dedicated profile dir specifically
// (matched via command line), never chrome.exe in general -- the user's regular
// browsing Chrome also runs as chrome.exe and must never be touched by tv_launch.
function findDebugChromePids(profileDir, platform) {
  try {
    if (platform === 'win32') {
      const escaped = profileDir.replace(/\\/g, '\\\\');
      const out = execSync(
        `wmic process where "name='chrome.exe' and CommandLine like '%${escaped}%'" get ProcessId`,
        { timeout: 5000 },
      ).toString();
      return out.split(/\r?\n/).map(l => l.trim()).filter(l => /^\d+$/.test(l));
    }
    const out = execSync(`pgrep -f ${JSON.stringify(profileDir)}`, { timeout: 5000 }).toString();
    return out.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
  } catch {
    return [];
  }
}

export async function launch({ port, kill_existing } = {}) {
  const cdpPort = port || 9222;
  // 2026-07-30 full-system checkup: connection.js/tab.js each hardcoded their
  // own separate CDP_PORT = 9222 constant, never reading whatever port was
  // actually passed here -- launching on a non-default port (e.g. 9222 already
  // in use) would silently connect/reconnect on the wrong port with no error
  // pointing at the real cause. Setting this env var lets both of those
  // modules follow the real port for the rest of this process's lifetime.
  process.env.TV_MCP_CDP_PORT = String(cdpPort);
  // Default false (unlike the old TradingView-Desktop version of this function):
  // chrome.exe is also the user's everyday browser, so force-killing it must be
  // opt-in, never the default behavior of launching TradingView.
  const killFirst = kill_existing === true;
  const platform = process.platform;
  const profileDir = debugProfileDir(platform);

  const pathMap = {
    darwin: [
      '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    ],
    win32: [
      `${process.env.PROGRAMFILES}\\Google\\Chrome\\Application\\chrome.exe`,
      `${process.env['PROGRAMFILES(X86)']}\\Google\\Chrome\\Application\\chrome.exe`,
      `${process.env.LOCALAPPDATA}\\Google\\Chrome\\Application\\chrome.exe`,
    ],
    linux: [
      '/usr/bin/google-chrome',
      '/usr/bin/google-chrome-stable',
      '/usr/bin/chromium-browser',
      '/usr/bin/chromium',
      '/snap/bin/chromium',
    ],
  };

  let chromePath = null;
  const candidates = pathMap[platform] || pathMap.linux;
  for (const p of candidates) {
    if (p && existsSync(p)) { chromePath = p; break; }
  }

  if (!chromePath) {
    try {
      const cmd = platform === 'win32'
        ? 'where chrome.exe'
        : 'which google-chrome || which google-chrome-stable || which chromium-browser || which chromium';
      chromePath = execSync(cmd, { timeout: 3000, shell: platform === 'win32' ? undefined : '/bin/bash' })
        .toString().trim().split('\n')[0];
      if (chromePath && !existsSync(chromePath)) chromePath = null;
    } catch { /* ignore */ }
  }

  if (!chromePath) {
    throw new Error(`Google Chrome not found on ${platform}. Searched: ${candidates.join(', ')}. Launch manually with: chrome --remote-debugging-port=${cdpPort} --user-data-dir="${profileDir}" https://www.tradingview.com/chart/`);
  }

  if (killFirst) {
    const pids = findDebugChromePids(profileDir, platform);
    for (const pid of pids) {
      try {
        if (platform === 'win32') execSync(`taskkill /F /PID ${pid}`, { timeout: 5000 });
        else execSync(`kill ${pid}`, { timeout: 5000 });
      } catch { /* already gone */ }
    }
    if (pids.length) await new Promise(r => setTimeout(r, 1500));
  }

  const child = spawn(chromePath, [
    `--remote-debugging-port=${cdpPort}`,
    `--user-data-dir=${profileDir}`,
    '--no-first-run',
    '--no-default-browser-check',
    'https://www.tradingview.com/chart/',
  ], { detached: true, stdio: 'ignore' });
  child.unref();

  for (let i = 0; i < 15; i++) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const http = await import('http');
      const ready = await new Promise((resolve) => {
        http.get(`http://localhost:${cdpPort}/json/version`, (res) => {
          let data = '';
          res.on('data', (chunk) => data += chunk);
          res.on('end', () => resolve(data));
        }).on('error', () => resolve(null));
      });
      if (ready) {
        const info = JSON.parse(ready);
        return {
          success: true, platform, binary: chromePath, pid: child.pid,
          cdp_port: cdpPort, cdp_url: `http://localhost:${cdpPort}`, profile_dir: profileDir,
          browser: info.Browser, user_agent: info['User-Agent'],
          note: 'First launch only: log into TradingView in the opened window -- the session persists in this dedicated profile after that.',
        };
      }
    } catch { /* retry */ }
  }

  return {
    success: true, platform, binary: chromePath, pid: child.pid, cdp_port: cdpPort, cdp_ready: false,
    profile_dir: profileDir,
    warning: 'Chrome launched but CDP not responding yet. It may still be loading. Try tv_health_check in a few seconds.',
  };
}
