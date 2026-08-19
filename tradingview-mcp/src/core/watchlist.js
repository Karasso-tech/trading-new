/**
 * Core watchlist logic.
 * Uses TradingView's internal widget API with DOM fallback.
 */
import { evaluate, evaluateAsync, getClient } from '../connection.js';

export async function get() {
  // Try internal API first — reads from the active watchlist widget
  const symbols = await evaluate(`
    (function() {
      // Method 1: Try the watchlist widget's internal data
      try {
        var rightArea = document.querySelector('[class*="layout__area--right"]');
        if (!rightArea || rightArea.offsetWidth < 50) return { symbols: [], source: 'panel_closed' };
      } catch(e) {}

      // Method 2: Read data-symbol-full attributes from watchlist rows
      var results = [];
      var seen = {};
      var container = document.querySelector('[class*="layout__area--right"]');
      if (!container) return { symbols: [], source: 'no_container' };

      // Find all elements with symbol data attributes
      var symbolEls = container.querySelectorAll('[data-symbol-full]');
      for (var i = 0; i < symbolEls.length; i++) {
        var sym = symbolEls[i].getAttribute('data-symbol-full');
        if (!sym || seen[sym]) continue;
        seen[sym] = true;

        // Find the row and extract price data
        var row = symbolEls[i].closest('[class*="row"]') || symbolEls[i].parentElement;
        var cells = row ? row.querySelectorAll('[class*="cell"], [class*="column"]') : [];
        var nums = [];
        for (var j = 0; j < cells.length; j++) {
          var t = cells[j].textContent.trim();
          if (t && /^[\\-+]?[\\d,]+\\.?\\d*%?$/.test(t.replace(/[\\s,]/g, ''))) nums.push(t);
        }
        results.push({ symbol: sym, last: nums[0] || null, change: nums[1] || null, change_percent: nums[2] || null });
      }

      if (results.length > 0) return { symbols: results, source: 'data_attributes' };

      // Method 3: Scan for ticker-like text in the right panel
      var items = container.querySelectorAll('[class*="symbolName"], [class*="tickerName"], [class*="symbol-"]');
      for (var k = 0; k < items.length; k++) {
        var text = items[k].textContent.trim();
        if (text && /^[A-Z][A-Z0-9.:!]{0,20}$/.test(text) && !seen[text]) {
          seen[text] = true;
          results.push({ symbol: text, last: null, change: null, change_percent: null });
        }
      }

      return { symbols: results, source: results.length > 0 ? 'text_scan' : 'empty' };
    })()
  `);

  return {
    success: true,
    count: symbols?.symbols?.length || 0,
    source: symbols?.source || 'unknown',
    symbols: symbols?.symbols || [],
  };
}

// 2026-07-11: selects a named watchlist via the "watchlists-button" dropdown
// (top of the watchlist panel, shows the current list's name and a "RECENTLY USED"
// quick-switch section). Confirmed live against a real TradingView Desktop session:
// clicking the button opens a dropdown; the target list's name appears as a leaf
// text node distinct from (and positioned well below) the button's own label, which
// also shows the current list name -- must explicitly exclude the button itself or
// this matches on its own label instead of a real dropdown entry. Only searches the
// "RECENTLY USED" quick list -- a list not recently used needs "Open list..." (Shift+W)
// instead, deliberately not built here since it needs a name-search flow of its own;
// throws a clear, honest error rather than silently no-op'ing on a list it can't find.
export async function selectList({ name }) {
  const c = await getClient();

  const alreadyActive = await evaluate(`
    (function() {
      var btn = document.querySelector('[data-name="watchlists-button"]');
      return btn ? btn.textContent.trim() : null;
    })()
  `);
  if (alreadyActive === name) return { success: true, list: name, action: 'already_active' };

  const btnRect = await evaluate(`
    (function() {
      var btn = document.querySelector('[data-name="watchlists-button"]');
      if (!btn) return null;
      var r = btn.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2, top: r.y };
    })()
  `);
  if (!btnRect) throw new Error('watchlists-button not found -- is the watchlist panel open?');

  await c.Input.dispatchMouseEvent({ type: 'mouseMoved', x: btnRect.x, y: btnRect.y });
  await c.Input.dispatchMouseEvent({ type: 'mousePressed', x: btnRect.x, y: btnRect.y, button: 'left', buttons: 1, clickCount: 1 });
  await c.Input.dispatchMouseEvent({ type: 'mouseReleased', x: btnRect.x, y: btnRect.y, button: 'left' });

  const escaped = name.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
  const findEntry = () => evaluate(`
    (function() {
      var btn = document.querySelector('[data-name="watchlists-button"]');
      var btnTop = btn ? btn.getBoundingClientRect().y : 0;
      var all = document.querySelectorAll('*');
      for (var i = 0; i < all.length; i++) {
        var e = all[i];
        if (e.children.length === 0 && e.textContent.trim() === '${escaped}') {
          if (btn && btn.contains(e)) continue;
          var r = e.getBoundingClientRect();
          if (r.y > btnTop + 10 && r.width > 0) return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
        }
      }
      return null;
    })()
  `);

  // Dropdown-open animation isn't instant; a single fixed wait proved flaky under
  // back-to-back automated calls (confirmed live, 2026-07-11: worked reliably with a
  // human-paced manual test, intermittently failed when chained immediately after
  // another CDP call in the same process) -- poll instead of trusting one wait.
  let entry = null;
  for (let attempt = 0; attempt < 5 && !entry; attempt++) {
    await new Promise(r => setTimeout(r, 300));
    entry = await findEntry();
  }
  if (!entry) {
    // Close whatever dropdown may still be open rather than leaving the UI stuck.
    await c.Input.dispatchKeyEvent({ type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
    await c.Input.dispatchKeyEvent({ type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
    throw new Error(`Watchlist "${name}" not found in the "RECENTLY USED" quick-switch list -- open it manually at least once first (Shift+W to search all lists).`);
  }

  await c.Input.dispatchMouseEvent({ type: 'mouseMoved', x: entry.x, y: entry.y });
  await c.Input.dispatchMouseEvent({ type: 'mousePressed', x: entry.x, y: entry.y, button: 'left', buttons: 1, clickCount: 1 });
  await c.Input.dispatchMouseEvent({ type: 'mouseReleased', x: entry.x, y: entry.y, button: 'left' });
  await new Promise(r => setTimeout(r, 300));

  const nowActive = await evaluate(`
    (function() {
      var btn = document.querySelector('[data-name="watchlists-button"]');
      return btn ? btn.textContent.trim() : null;
    })()
  `);
  if (nowActive !== name) {
    throw new Error(`Selected watchlist "${name}" but the active list now reads "${nowActive}" -- selection may not have taken effect.`);
  }

  return { success: true, list: name };
}

// 2026-07-11: removes a symbol from whichever watchlist is currently active. There is
// no dedicated "remove" affordance exposed as a simple button/menu item -- confirmed
// live that TradingView's own right-click context menu on a watchlist row does NOT
// offer a text "Remove" option in this version. What does work, confirmed live: a
// real (CDP-level, not a synthetic DOM event) left-click on the row to select it,
// immediately followed by the Delete key. Matches by data-symbol-full (e.g.
// "NASDAQ:AAPL") OR data-symbol-short (e.g. "AAPL") since callers pass a bare ticker.
export async function remove({ symbol }) {
  const c = await getClient();
  const escaped = symbol.replace(/\\/g, '\\\\').replace(/'/g, "\\'").toUpperCase();

  const rowRect = await evaluate(`
    (function() {
      var wrap = document.querySelector('[data-name="symbol-list-wrap"]');
      if (!wrap) return null;
      var rows = wrap.querySelectorAll('[data-symbol-full]');
      for (var i = 0; i < rows.length; i++) {
        var full = (rows[i].getAttribute('data-symbol-full') || '').toUpperCase();
        var short = (rows[i].getAttribute('data-symbol-short') || '').toUpperCase();
        if (full === '${escaped}' || short === '${escaped}' || full === 'NASDAQ:${escaped}' || full.endsWith(':${escaped}')) {
          var r = rows[i].getBoundingClientRect();
          return { x: r.x + 80, y: r.y + r.height / 2 };
        }
      }
      return null;
    })()
  `);
  if (!rowRect) {
    return { success: true, symbol, action: 'not_present' };
  }

  await c.Input.dispatchMouseEvent({ type: 'mouseMoved', x: rowRect.x, y: rowRect.y });
  await c.Input.dispatchMouseEvent({ type: 'mousePressed', x: rowRect.x, y: rowRect.y, button: 'left', buttons: 1, clickCount: 1 });
  await c.Input.dispatchMouseEvent({ type: 'mouseReleased', x: rowRect.x, y: rowRect.y, button: 'left' });
  await new Promise(r => setTimeout(r, 200));

  await c.Input.dispatchKeyEvent({ type: 'keyDown', key: 'Delete', code: 'Delete', windowsVirtualKeyCode: 46 });
  await c.Input.dispatchKeyEvent({ type: 'keyUp', key: 'Delete', code: 'Delete', windowsVirtualKeyCode: 46 });
  await new Promise(r => setTimeout(r, 300));

  const stillThere = await evaluate(`
    (function() {
      var wrap = document.querySelector('[data-name="symbol-list-wrap"]');
      if (!wrap) return false;
      var rows = wrap.querySelectorAll('[data-symbol-full]');
      for (var i = 0; i < rows.length; i++) {
        var full = (rows[i].getAttribute('data-symbol-full') || '').toUpperCase();
        var short = (rows[i].getAttribute('data-symbol-short') || '').toUpperCase();
        if (full === '${escaped}' || short === '${escaped}' || full.endsWith(':${escaped}')) return true;
      }
      return false;
    })()
  `);
  if (stillThere) throw new Error(`Removal of ${symbol} did not take effect -- row still present after click+Delete.`);

  return { success: true, symbol, action: 'removed' };
}

export async function add({ symbol, list }) {
  const c = await getClient();
  if (list) await selectList({ name: list });

  // Use keyboard shortcut to open symbol search in watchlist, type symbol, press Enter
  // First ensure watchlist panel is open
  const panelState = await evaluate(`
    (function() {
      var btn = document.querySelector('[data-name="base-watchlist-widget-button"]')
        || document.querySelector('[aria-label*="Watchlist"]');
      if (!btn) return { error: 'Watchlist button not found' };
      var isActive = btn.getAttribute('aria-pressed') === 'true'
        || btn.classList.toString().indexOf('Active') !== -1
        || btn.classList.toString().indexOf('active') !== -1;
      if (!isActive) { btn.click(); return { opened: true }; }
      return { opened: false };
    })()
  `);

  if (panelState?.error) throw new Error(panelState.error);
  if (panelState?.opened) await new Promise(r => setTimeout(r, 500));

  // Click the "Add symbol" button (various selectors)
  const addClicked = await evaluate(`
    (function() {
      var selectors = [
        '[data-name="add-symbol-button"]',
        '[aria-label="Add symbol"]',
        '[aria-label*="Add symbol"]',
        'button[class*="addSymbol"]',
      ];
      for (var s = 0; s < selectors.length; s++) {
        var btn = document.querySelector(selectors[s]);
        if (btn && btn.offsetParent !== null) { btn.click(); return { found: true, selector: selectors[s] }; }
      }
      // Fallback: find + button in right panel
      var container = document.querySelector('[class*="layout__area--right"]');
      if (container) {
        var buttons = container.querySelectorAll('button');
        for (var i = 0; i < buttons.length; i++) {
          var ariaLabel = buttons[i].getAttribute('aria-label') || '';
          if (/add.*symbol/i.test(ariaLabel) || buttons[i].textContent.trim() === '+') {
            buttons[i].click();
            return { found: true, method: 'fallback' };
          }
        }
      }
      return { found: false };
    })()
  `);

  if (!addClicked?.found) throw new Error('Add symbol button not found in watchlist panel');
  await new Promise(r => setTimeout(r, 300));

  // Type the symbol into the search input
  await c.Input.insertText({ text: symbol });
  await new Promise(r => setTimeout(r, 500));

  // Press Enter to select the first result
  await c.Input.dispatchKeyEvent({ type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
  await c.Input.dispatchKeyEvent({ type: 'keyUp', key: 'Enter', code: 'Enter' });
  await new Promise(r => setTimeout(r, 300));

  // Press Escape to close search
  await c.Input.dispatchKeyEvent({ type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await c.Input.dispatchKeyEvent({ type: 'keyUp', key: 'Escape', code: 'Escape' });
  await new Promise(r => setTimeout(r, 300));

  // 2026-07-11: verify the row actually landed rather than trusting the keystroke
  // sequence blindly -- a stale symbol-search result or a slow network lookup could
  // leave nothing added despite every step above reporting success.
  const escapedSym = symbol.replace(/\\/g, '\\\\').replace(/'/g, "\\'").toUpperCase();
  const landed = await evaluate(`
    (function() {
      var wrap = document.querySelector('[data-name="symbol-list-wrap"]');
      if (!wrap) return false;
      var rows = wrap.querySelectorAll('[data-symbol-full]');
      for (var i = 0; i < rows.length; i++) {
        var full = (rows[i].getAttribute('data-symbol-full') || '').toUpperCase();
        var short = (rows[i].getAttribute('data-symbol-short') || '').toUpperCase();
        if (full === '${escapedSym}' || short === '${escapedSym}' || full.endsWith(':${escapedSym}')) return true;
      }
      return false;
    })()
  `);
  if (!landed) throw new Error(`"${symbol}" was not found in the watchlist after the add sequence -- it may not have resolved to a real symbol, or the list changed underneath it.`);

  return { success: true, symbol, action: 'added' };
}
