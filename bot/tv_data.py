"""Raw TradingView data fetcher -- MCP stdio client to the vendored connector.

RAW DATA ONLY. No calculation of any kind lives here (see CLAUDE_CODE_INSTRUCTIONS.md,
"The non-negotiable architecture decision"). ATR/SMA/RS are computed via
indicators_core.py's pure-math functions; swing/wall/target-selection judgment is
Claude's own direct analysis (no automated per-message API/CLI call in this
architecture) -- from the raw bars this module returns, never invented here.

All fetches on one TVClient instance are sequential by construction (a single asyncio
task drives one MCP session at a time). Callers with multiple tickers (e.g. /playbook's
holdings, /screener with several symbols) MUST await each ticker's fetch before starting
the next -- never asyncio.gather them -- per fix #12 (concurrent CDP calls are the most
likely way to reproduce a hung connection).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENDORED_SERVER = PROJECT_ROOT / "tradingview-mcp" / "src" / "server.js"
NY_TZ = ZoneInfo("America/New_York")
TOOL_TIMEOUT_SECONDS = 30  # a cold CDP (re)connect can legitimately take ~15s (5 retries, backoff)
INDEX_CACHE_PATH = PROJECT_ROOT / "_index_bars_cache.json"
_INDEX_CACHE_LOCK_PATH = PROJECT_ROOT / "_index_bars_cache.lock"
_INDEX_CACHE_LOCK_RETRY_SECONDS = 0.05
_INDEX_CACHE_LOCK_MAX_WAIT_SECONDS = 5  # generous vs. how briefly this lock is ever actually held
INDEX_CACHE_TTL_SECONDS = 1800  # 30 min -- long enough that every ticker in one /playbook,
                                 # /screener batch, or /positions run reuses the same SPY/QQQ
                                 # pull instead of each paying its own ~10-15s CDP round-trip for
                                 # identical data; short enough that a run later the same day still
                                 # sees genuinely fresh bars.
INDEX_QUOTE_CACHE_TTL_SECONDS = 600  # 10 min -- see get_index_quote. Deliberately far
                                      # shorter than the bars TTL above: this one is a
                                      # live price, and callers are handed its age so a
                                      # cached number is never presented as current.
BOT_WATCHLIST_NAME = "Bot Watchlist"  # mirrors the pending list -- see deliver_report.py, _handle_drop
_NYSE = mcal.get_calendar("NYSE")


def _session_open_utc_ts(now_ny: datetime) -> tuple[float, str]:
    """B2: real NYSE trading-calendar version of "today's session open, or the prior
    trading day's if before today's open." The old logic just subtracted exactly one
    calendar day with zero weekend/holiday awareness -- it would misfire specifically
    on a pre-market check the day after a holiday (e.g. the actual July 3-6 gap seen
    in real use this week: a Monday pre-market /monitor check would have wrongly
    referenced "Sunday" instead of the correct prior Thursday session)."""
    schedule = _NYSE.schedule(start_date=(now_ny.date() - timedelta(days=10)), end_date=now_ny.date())
    if schedule.empty:
        raise RuntimeError(f"No NYSE trading sessions found in the 10 days before {now_ny.date()}")

    now_utc = now_ny.astimezone(timezone.utc)
    last_date = schedule.index[-1].date()
    last_open = schedule.iloc[-1]["market_open"].to_pydatetime()

    if last_date == now_ny.date() and now_utc < last_open:
        # today is a valid session but hasn't opened yet (pre-market) -- the correct
        # reference is the PREVIOUS actual trading session, which the calendar
        # already correctly skips weekends/holidays to find.
        if len(schedule) < 2:
            raise RuntimeError("Not enough NYSE trading-day history to find the prior session")
        row, session_date = schedule.iloc[-2], schedule.index[-2].date()
    else:
        row, session_date = schedule.iloc[-1], last_date

    return row["market_open"].timestamp(), session_date.isoformat()

logger = logging.getLogger("tv_data")
if not logger.handlers:
    _file_handler = logging.FileHandler(PROJECT_ROOT / "tv_data.log", encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_file_handler)
    logger.setLevel(logging.INFO)


# ----------------------------------------------------------------------
# Distinguishable failure modes (fix #6) -- each gets its own actionable
# Telegram message plus full raw detail in the local log.
# ----------------------------------------------------------------------

class TVError(Exception):
    kind = "unknown"

    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


class DesktopNotRunning(TVError):
    kind = "desktop_down"


class CDPHung(TVError):
    kind = "cdp_hung"


class BadSymbol(TVError):
    kind = "bad_symbol"


def _categorize(exc: Exception) -> TVError:
    if isinstance(exc, TVError):
        return exc
    if isinstance(exc, asyncio.TimeoutError):
        msg = (f"TradingView is open but isn't responding (a CDP call hung past "
               f"{TOOL_TIMEOUT_SECONDS}s) -- try restarting the TradingView Chrome window (tv_launch).")
        return CDPHung(msg, detail=repr(exc))

    text = str(exc)
    desktop_down_markers = (
        "No TradingView chart target found",
        "CDP connection failed after",
        "ECONNREFUSED",
        "fetch failed",
    )
    if any(marker in text for marker in desktop_down_markers):
        msg = "TradingView isn't open in Chrome (or Chrome wasn't started with --remote-debugging-port=9222)."
        return DesktopNotRunning(msg, detail=text)

    # These come from the connector's own data.js when a symbol resolves to a chart
    # with no bars/quote data -- confirmed live (fix, 2026-07-02): a garbage symbol
    # passes chart_set_symbol's chart_ready check often enough that _set_symbol's
    # validation alone isn't reliable, but the subsequent quote/OHLCV fetch reliably
    # fails with exactly this message. Wording is ambiguous ("still loading") but by
    # the time this fires, _set_symbol has already given the chart substantial time
    # to settle -- treating it as bad-symbol is the right call in practice.
    bad_symbol_markers = ("Could not retrieve quote", "Could not extract OHLCV data")
    if any(marker in text for marker in bad_symbol_markers):
        return BadSymbol(f"Symbol not recognized on TradingView (no data available): {text}", detail=text)

    return TVError(f"TradingView request failed: {text}", detail=text)


class TVClient:
    """One persistent MCP session over the vendored connector's stdio transport."""

    def __init__(self):
        self._session: Optional[ClientSession] = None
        self._stdio_ctx = None
        self._session_ctx = None
        self._last_bars_symbol: Optional[str] = None
        self._last_bars_signature: Optional[tuple] = None

    async def connect(self) -> None:
        server_params = StdioServerParameters(command="node", args=[str(VENDORED_SERVER)])
        self._stdio_ctx = stdio_client(server_params)
        read, write = await self._stdio_ctx.__aenter__()
        self._session_ctx = ClientSession(read, write)
        self._session = await self._session_ctx.__aenter__()
        await self._session.initialize()
        logger.info("Connected to vendored tradingview-mcp server (%s)", VENDORED_SERVER)

    async def close(self) -> None:
        if self._session_ctx is not None:
            await self._session_ctx.__aexit__(None, None, None)
        if self._stdio_ctx is not None:
            await self._stdio_ctx.__aexit__(None, None, None)
        self._session = None

    async def __aenter__(self) -> "TVClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def _call(self, tool: str, **kwargs) -> dict:
        if self._session is None:
            raise RuntimeError("TVClient not connected -- call connect() first")
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(tool, arguments=kwargs),
                timeout=TOOL_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 -- deliberately broad, categorized below
            err = _categorize(exc)
            logger.error("Tool call %s(%s) failed [%s]: %s", tool, kwargs, err.kind, err.detail or err)
            raise err from exc

        text = "".join(getattr(block, "text", "") for block in result.content)
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            err = TVError(f"Unexpected (non-JSON) response from TradingView tool {tool}.", detail=text)
            logger.error(str(err.detail))
            raise err

        if result.isError or payload.get("success") is False:
            err_text = payload.get("error", text or f"{tool} failed with no error detail")
            err = _categorize(Exception(err_text))
            logger.error("Tool call %s(%s) returned error [%s]: %s", tool, kwargs, err.kind, err_text)
            raise err

        return payload

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self) -> dict:
        """Wraps tv_health_check. Callers should call this proactively (e.g. at the
        start of a command) so a "TradingView not open in Chrome" failure surfaces
        immediately with its specific message, rather than after several other tool
        calls."""
        return await self._call("tv_health_check")

    # ------------------------------------------------------------------
    # Symbol / timeframe setup
    # ------------------------------------------------------------------

    @staticmethod
    def _same_symbol(requested: str, actual: str) -> bool:
        """Exact match after dropping the exchange prefix -- "NVDA" IS "BATS:NVDA".

        Deliberately strict, unlike the loose substring test _set_symbol uses to
        VERIFY a switch that already happened: this one decides whether to skip
        switching at all, and the loose test would happily accept the chart
        sitting on GME as "already showing GM" (substring) and hand back the
        wrong ticker's bars. A strict miss here only costs one redundant switch;
        a false hit would be the 2026-07-13 wrong-ticker-data bug all over
        again. Also normalizes case."""
        return requested.upper().split(":")[-1].strip() == actual.upper().split(":")[-1].strip()

    async def _set_symbol(self, symbol: str) -> None:
        # 2026-08-04: every public method here calls _set_symbol, so a caller
        # reading several timeframes off ONE ticker (fetch_monitor_data.py does
        # 2h -> 30m -> daily -> quote) paid a full chart switch four times for
        # the same symbol -- three of them changing nothing at all. Measured
        # ~2s each after the chart_ready fix (~11s before it), i.e. the single
        # largest remaining slice of a /monitorall run. chart_get_state is the
        # authoritative symbol read this function already trusted below and
        # costs one cheap round-trip, so ask it FIRST and skip the switch
        # entirely when the chart is provably already there.
        current = await self._call("chart_get_state")
        if self._same_symbol(symbol, str(current.get("symbol", ""))):
            await self._verify_bars_refreshed(symbol)
            return

        result = await self._call("chart_set_symbol", symbol=symbol)

        # Found real, 2026-07-13: an automated /monitorall run scanning many tickers
        # in one session got byte-for-byte IDENTICAL current_price/bars data for 4
        # DIFFERENT tickers in a row (CRM, PGR, SOFI, HOOD), all matching what was
        # actually SPY's price -- chart_set_symbol had reported chart_ready=True
        # (a false positive) for each switch, so this function used to return
        # immediately on that path with ZERO verification, trusting the DOM-polling
        # heuristic completely. The inner claude -p session caught it that time by
        # noticing the cross-ticker anomaly and refusing to act on it, but that's
        # LLM vigilance catching a hole that should be closed deterministically --
        # same "mechanical safety net, not judgment" principle report_lint.py
        # already applies elsewhere in this project. Always cross-check
        # chart_get_state()'s own `symbol` field now, on BOTH the chart_ready=True
        # and chart_ready=False paths, before trusting that any subsequent
        # quote/bars read actually reflects the requested ticker.
        state = await self._call("chart_get_state")
        actual_symbol = str(state.get("symbol", ""))
        symbol_matches = symbol.upper() in actual_symbol.upper() or actual_symbol.upper() in symbol.upper()

        if result.get("chart_ready", False):
            if symbol_matches:
                await self._verify_bars_refreshed(symbol)
                return
            raise BadSymbol(
                f'"{symbol}" appeared to switch successfully (chart_ready=true) but '
                f'chart_get_state reports the chart is actually still showing "{actual_symbol}" -- '
                f'refusing to proceed with a read that would silently return the wrong ticker\'s data.',
                detail=str(result),
            )

        # chart_ready=False from the vendored tool's own DOM-polling heuristic (10s,
        # checks for a stable bar count + a matching symbol string in the page) is NOT
        # authoritative -- confirmed empirically: it timed out on a plain valid symbol
        # (AAPL) right after a fresh TradingView launch, purely because the chart was
        # still cold/settling, not because the symbol was bad. Before concluding the
        # symbol itself is invalid, cross-check against chart_get_state's `symbol`
        # field, which reads the chart API's actual internal state directly rather
        # than scraping the DOM -- much less prone to this kind of timing flakiness.
        if not symbol_matches:
            raise BadSymbol(
                f'"{symbol}" was not recognized on TradingView (chart never became ready, and '
                f'the chart API reports symbol "{actual_symbol}" instead).',
                detail=str(result),
            )
        logger.warning(
            '%s: chart_ready reported false (DOM-polling timeout) but chart_get_state confirms '
            'the symbol actually switched correctly (got "%s") -- treating as a slow/cold chart '
            'load, not a bad symbol.', symbol, actual_symbol,
        )
        await self._verify_bars_refreshed(symbol)

    async def _verify_bars_refreshed(self, symbol: str) -> None:
        """Found real, 2026-07-26: a /positions run fetching ticker -> SPY -> QQQ in
        sequence on one chart session got byte-for-byte identical OHLCV bars for SPY
        and QQQ (same open/high/low/close/volume back to the oldest bar), even though
        chart_get_state had already confirmed the QQQ switch above. chart_get_state's
        `symbol` field flips as soon as the chart API accepts the new ticker, but the
        OHLCV bar buffer itself can lag behind that by a beat -- so the check above,
        while necessary, isn't sufficient on its own. This polls a cheap 2-bar probe
        until it stops matching the immediately-previous symbol's last-known bar
        (time+close+volume, effectively unique), bounded retries -- never silently
        hands back data that's provably still the old symbol's."""
        if symbol == self._last_bars_symbol or self._last_bars_signature is None:
            return
        for _ in range(6):
            page = await self._call("data_get_ohlcv", count=2, before=None)
            bars = page.get("bars", [])
            if not bars:
                return
            last = bars[-1]
            signature = (last["time"], last["close"], last["volume"])
            if signature != self._last_bars_signature:
                return
            await asyncio.sleep(0.5)
        raise BadSymbol(
            f'"{symbol}" switch was confirmed by chart_get_state, but the OHLCV bar buffer '
            f'still returns "{self._last_bars_symbol}"\'s exact last bar after 6 retries -- '
            f'refusing to proceed with data that is provably stale.',
        )

    async def _set_timeframe(self, timeframe: str) -> None:
        await self._call("chart_set_timeframe", timeframe=timeframe)

    # ------------------------------------------------------------------
    # Public raw-data methods
    # ------------------------------------------------------------------

    async def get_quote(self, symbol: str) -> dict:
        await self._set_symbol(symbol)  # validates the symbol; quote_get's own symbol
        return await self._call("quote_get", symbol=symbol)  # param is just a label, not a fetch

    async def get_daily_history(self, symbol: str, years: float = 5) -> list[dict]:
        """Raw daily OHLCV bars, oldest-first -- whatever TradingView currently has
        loaded in its in-memory bar buffer for this symbol/timeframe.

        `years` is a soft target, not a guarantee. Confirmed empirically (live testing,
        see tradingview-mcp/VENDORED_FROM.md): neither `chart_scroll_to_date` nor
        `chart_set_visible_range` actually trigger TradingView to fetch more history --
        both only reposition the viewport within bars already loaded (`total_available`
        measured flat across 15s of waiting after scrolling). TradingView's real
        lazy-loading is triggered by genuine user scroll input, which this connector
        doesn't currently simulate. In practice this means daily history is capped at
        whatever's preloaded for a freshly-set symbol -- observed ~500-700 bars
        (~2 years), not the full 5 years the protocol files ask for.

        This still pages through the `before` parameter (see VENDORED_FROM.md) in case
        more than one page's worth is ever already buffered, and never pads or
        fabricates bars to reach `years` -- callers/Claude are told the actual date
        range covered and should treat anything beyond it as "no data," not invented.
        """
        await self._set_symbol(symbol)
        await self._set_timeframe("D")

        target_bars = int(years * 252 * 1.05)  # small buffer for holidays etc.
        all_bars: list[dict] = []
        seen_times: set[float] = set()
        before: Optional[float] = None

        while len(all_bars) < target_bars:
            page = await self._call("data_get_ohlcv", count=500, before=before)
            bars = page.get("bars", [])
            new_bars = [b for b in bars if b["time"] not in seen_times]
            if not new_bars:
                break

            for b in new_bars:
                seen_times.add(b["time"])
            all_bars = new_bars + all_bars  # new_bars is strictly older than what we already have
            before = bars[0]["time"]

            if page.get("reached_start_of_buffer"):
                break

        all_bars.sort(key=lambda b: b["time"])
        logger.info("get_daily_history(%s, years=%s target): %d bars actually available, %s -> %s",
                    symbol, years, len(all_bars),
                    all_bars[0]["time"] if all_bars else None,
                    all_bars[-1]["time"] if all_bars else None)
        if all_bars:
            last = all_bars[-1]
            self._last_bars_symbol = symbol
            self._last_bars_signature = (last["time"], last["close"], last["volume"])
        return all_bars

    async def get_intraday_since_open(self, symbol: str, timeframe: str) -> list[dict]:
        """Raw bars since the correct trading session's open (9:30am ET), DST-aware
        via zoneinfo and weekend/holiday-aware via a real NYSE calendar (B2).
        `timeframe` is a TradingView resolution string, e.g. '120' (2H) or '30' (30min).
        A single 500-bar page is always enough for one session at these resolutions --
        no pagination needed intraday.
        """
        await self._set_symbol(symbol)
        await self._set_timeframe(timeframe)

        now_ny = datetime.now(NY_TZ)
        session_open_utc_ts, session_date = _session_open_utc_ts(now_ny)

        page = await self._call("data_get_ohlcv", count=500)
        bars = page.get("bars", [])
        since_open = [b for b in bars if b["time"] >= session_open_utc_ts]
        logger.info("get_intraday_since_open(%s, %s): %d bars since session %s",
                    symbol, timeframe, len(since_open), session_date)
        # 2026-07-30 full-system checkup: get_daily_history updates
        # _last_bars_symbol/_last_bars_signature after its own fetch, which is
        # what actually arms _verify_bars_refreshed's stale-buffer check on the
        # NEXT _set_symbol call -- this method never did, so that check was a
        # complete no-op for any caller doing intraday-only fetches across
        # multiple tickers on one client (no get_daily_history call in between
        # to arm it). Same real incident this check exists for (SPY/QQQ bars
        # leaking across a symbol switch) could otherwise recur silently on an
        # intraday-only path.
        if bars:
            last = bars[-1]
            self._last_bars_symbol = symbol
            self._last_bars_signature = (last["time"], last["close"], last["volume"])
        return since_open

    async def get_chart_screenshot(self, symbol: str, timeframe: str) -> Optional[Path]:
        """Optional/secondary -- qualitative visual context only (fix #3). Never the
        source of truth for a price level (rule 1, CONSISTENCY_RULES.md)."""
        await self._set_symbol(symbol)
        await self._set_timeframe(timeframe)
        result = await self._call("capture_screenshot", region="chart")
        file_path = result.get("file_path")
        return Path(file_path) if file_path else None

    # ------------------------------------------------------------------
    # Calendars (2026-07-26) -- closes the gap output_gate.py's earnings_verified
    # docstring flagged as a TODO ("no earnings calendar exists in this system yet").
    # Unlike every other method here, these two hit TradingView's public JSON
    # endpoints directly (tradingview-mcp/src/core/calendar.js) -- no chart symbol/
    # timeframe setup needed, no CDP-open Chrome tab required at all.
    # ------------------------------------------------------------------

    async def get_earnings_date(self, symbol: str) -> dict:
        """Real next/last earnings date + confirmed before/after-market session for
        one ticker, straight from TradingView -- the only thing that may ever set
        `earnings_verified: true` in output_gate.classify_output(). Returns the
        single-ticker result dict directly (never invented if not found: caller sees
        `error` + all-null fields, same as a real "not found" from the source)."""
        payload = await self._call("calendar_get_earnings", symbols=[symbol])
        results = payload.get("results", [])
        return results[0] if results else {"symbol": symbol, "error": "no result returned"}

    async def get_economic_calendar(self, days_ahead: int = 7, days_back: int = 0) -> list[dict]:
        """Upcoming (default 7 days ahead) US macro events pre-filtered to CPI, PPI,
        NFP, FOMC, Fed rate decisions, unemployment, GDP -- STRATEGY_v3.md section a's
        "CPI, NFP, FOMC" event check, now backed by real fetched data instead of
        model memory."""
        payload = await self._call("calendar_get_economic", days_ahead=days_ahead, days_back=days_back)
        return payload.get("events", [])

    # ------------------------------------------------------------------
    # Watchlist sync (2026-07-11) -- deliberately NOT "raw data", these are the
    # two write actions keeping a real TradingView watchlist mirroring the
    # pending list (see deliver_report.py and process_queue.py's _handle_drop).
    # UI automation against hashed CSS-module class names, not a stable API --
    # can break if TradingView changes its watchlist panel markup.
    # ------------------------------------------------------------------

    async def add_to_watchlist(self, symbol: str, list_name: Optional[str] = None) -> dict:
        return await self._call("watchlist_add", symbol=symbol, list=list_name)

    async def remove_from_watchlist(self, symbol: str) -> dict:
        return await self._call("watchlist_remove", symbol=symbol)

    # ------------------------------------------------------------------
    # Drawing (2026-07-13) -- deliberately NOT "raw data" either, same category as
    # watchlist sync above: these write to the chart via TradingView's own native
    # drawing-tools API (tradingview-mcp's src/core/drawing.js), not DOM/mouse
    # simulation. Thin passthroughs only -- colors/labels/which levels to draw are
    # presentation judgment and belong in chart_draw.py, not here (see this
    # module's own "RAW DATA ONLY" docstring at the top).
    # ------------------------------------------------------------------

    async def draw_shape(self, shape: str, point: dict, point2: Optional[dict] = None,
                          overrides: Optional[str] = None, text: Optional[str] = None) -> dict:
        """overrides is a JSON STRING, not a dict -- the vendored tool's own schema
        types it as a string and JSON.parses it server-side."""
        return await self._call("draw_shape", shape=shape, point=point,
                                 point2=point2, overrides=overrides, text=text)

    async def draw_clear(self) -> dict:
        return await self._call("draw_clear")

    async def prepare_chart(self, symbol: str, timeframe: str) -> None:
        """Public wrapper around the two private setup steps every other public
        method already does internally -- callers that need to draw between
        "chart is on the right symbol/timeframe" and "read/capture it" (i.e.
        chart_draw.py) need those two steps exposed without duplicating
        _set_symbol's validation logic."""
        await self._set_symbol(symbol)
        await self._set_timeframe(timeframe)


def _load_index_cache() -> dict:
    if not INDEX_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(INDEX_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}  # a corrupt/partial cache file is a cache miss, never a crash


def _save_index_cache(cache: dict) -> None:
    try:
        INDEX_CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass  # the cache is a pure optimization -- a failed write must never break the fetch


@contextlib.contextmanager
def _index_cache_lock():
    """2026-07-30 full-system checkup: this cache used to be read-modify-written
    with no locking at all -- if two fetches for different symbols/tickers ever
    genuinely overlapped in time (e.g. an interactive session running alongside
    the automated queue), the second process's write could silently clobber the
    first's just-cached entry (a lost update), defeating the whole point of the
    cache. A plain exclusive-create lock file is enough here: the critical
    section is a fast local file read+write, never the slow network fetch
    itself, so contention (if it ever happens at all) is measured in
    milliseconds, not seconds. A lock file left behind by a process that died
    mid-write is stolen after _INDEX_CACHE_LOCK_MAX_WAIT_SECONDS rather than
    wedging the cache unusable forever."""
    deadline = time.time() + _INDEX_CACHE_LOCK_MAX_WAIT_SECONDS
    while True:
        try:
            fd = os.open(str(_INDEX_CACHE_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.time() > deadline:
                try:
                    _INDEX_CACHE_LOCK_PATH.unlink()
                except OSError:
                    pass
                continue
            time.sleep(_INDEX_CACHE_LOCK_RETRY_SECONDS)
    try:
        os.close(fd)
        yield
    finally:
        _INDEX_CACHE_LOCK_PATH.unlink(missing_ok=True)


async def get_index_bars(client: TVClient, symbol: str, years: float = 1) -> list[dict]:
    """Disk-cached wrapper around client.get_daily_history, for SPY/QQQ specifically.
    Every Category A script (fetch_analysis_data.py, fetch_monitor_data.py) refetches
    the exact same index history once per ticker in a batch run -- e.g. 6-7x per
    /playbook run for identical SPY/QQQ data -- each paying a real ~10-15s CDP
    round-trip for no new information. Cached to DISK, not in-process, since each
    ticker is its own subprocess (see this module's own docstring: fetches must stay
    sequential, never concurrent, so there's no shared process to hold an in-memory
    cache across tickers). Never used for a live quote (spy_quote in
    fetch_monitor_data.py) -- only for daily history, which doesn't need to be
    re-resolved more often than every half hour."""
    cache = _load_index_cache()
    key = f"{symbol}:{years}"
    entry = cache.get(key)
    now = time.time()
    if entry and (now - entry["fetched_at"]) < INDEX_CACHE_TTL_SECONDS:
        return entry["bars"]

    bars = await client.get_daily_history(symbol, years=years)
    # Locked read-modify-write (2026-07-30): re-reads the cache fresh under the
    # lock rather than reusing the `cache` var from above, in case another
    # process updated it while this fetch was in flight -- merging into a
    # stale in-memory copy would silently undo that other process's write.
    with _index_cache_lock():
        cache = _load_index_cache()
        cache[key] = {"fetched_at": now, "bars": bars}
        _save_index_cache(cache)
    return bars


async def get_index_quote(client: TVClient, symbol: str) -> tuple[dict, int]:
    """Disk-cached wrapper around client.get_quote for SPY/QQQ, same disk cache and
    lock as get_index_bars (2026-08-04).

    Why: fetch_monitor_data.py fetched a fresh SPY quote for EVERY ticker in a
    /monitorall run -- 20+ identical fetches of one number, each costing a real
    chart switch away from the ticker being analysed and back again. get_index_bars
    already solved exactly this for index HISTORY; the quote was simply never
    covered, and its own docstring said so ("Never used for a live quote").

    TTL is much shorter than the bars cache (10 min vs 30) because this IS a live
    price and a stale one read as current is the kind of thing rule 1 exists to
    prevent. Returns (quote, age_seconds) rather than the bare dict so the caller
    can publish how old the number actually is instead of implying it is live --
    a 0 means this call fetched it fresh.
    """
    cache = _load_index_cache()
    key = f"quote:{symbol}"
    entry = cache.get(key)
    now = time.time()
    if entry and (now - entry["fetched_at"]) < INDEX_QUOTE_CACHE_TTL_SECONDS:
        return entry["quote"], int(now - entry["fetched_at"])

    quote = await client.get_quote(symbol)
    with _index_cache_lock():
        cache = _load_index_cache()
        cache[key] = {"fetched_at": now, "quote": quote}
        _save_index_cache(cache)
    return quote, 0


async def get_economic_calendar_cached(client: TVClient, days_ahead: int = 10) -> list[dict]:
    """Disk-cached wrapper around client.get_economic_calendar, same cache and lock
    as get_index_bars/get_index_quote (2026-08-04).

    Unlike earnings, this result does not depend on the ticker AT ALL -- it is the
    same CPI/PPI/NFP/FOMC list for every symbol in a batch, verified identical
    across consecutive fetches. Every ticker in a /monitorall or /playbook run was
    paying its own round-trip for a byte-identical answer.

    Shares the bars TTL rather than the quote's shorter one: these are scheduled
    macro releases days out, not a live price, so half an hour of age cannot make
    one wrong. Deliberately no age returned for the same reason -- there is no
    "is this still live" question to answer, unlike get_index_quote.
    """
    cache = _load_index_cache()
    key = f"econ:{days_ahead}"
    entry = cache.get(key)
    now = time.time()
    if entry and (now - entry["fetched_at"]) < INDEX_CACHE_TTL_SECONDS:
        return entry["events"]

    events = await client.get_economic_calendar(days_ahead=days_ahead)
    with _index_cache_lock():
        cache = _load_index_cache()
        cache[key] = {"fetched_at": now, "events": events}
        _save_index_cache(cache)
    return events


@dataclass
class FreshnessResult:
    fresh: bool
    last_bar_date: Optional[str]
    most_recent_complete_session: str


def assert_data_fresh(bars: list[dict]) -> FreshnessResult:
    """Hardening Pass item 6: a single data path (TradingView in Chrome -> CDP ->
    vendored MCP) with no cross-check means one stale fetch poisons every number
    downstream. Fresh iff the last bar's date is >= the most recent NYSE trading
    session whose close has already passed -- the exact same real-calendar
    approach _session_open_utc_ts already uses above (reused deliberately, not
    reimplemented) rather than naive weekday arithmetic, which is exactly the bug
    that function itself was written to fix (see its own docstring: a Monday
    pre-market check wrongly referencing "Sunday" instead of the correct prior
    Thursday session after a holiday).

    Never raises on stale/empty data -- staleness is a fact to report, not an
    error to abort on (see fetch_analysis_data.py/fetch_monitor_data.py's own
    callers and output_gate.py's data_fresh condition, which downgrades a report
    to analysis_only rather than blocking it)."""
    now_ny = datetime.now(NY_TZ)
    schedule = _NYSE.schedule(start_date=(now_ny.date() - timedelta(days=10)), end_date=now_ny.date())
    if schedule.empty:
        raise RuntimeError(f"No NYSE trading sessions found in the 10 days before {now_ny.date()}")

    now_utc = now_ny.astimezone(timezone.utc)
    last_date = schedule.index[-1].date()
    last_close = schedule.iloc[-1]["market_close"].to_pydatetime()

    if last_date == now_ny.date() and now_utc < last_close:
        # Today is a valid session but hasn't closed yet -- the most recent
        # COMPLETE session is the prior one, same "hasn't happened yet" logic
        # _session_open_utc_ts already applies to the open side.
        if len(schedule) < 2:
            raise RuntimeError("Not enough NYSE trading-day history to find the prior complete session")
        most_recent_complete = schedule.index[-2].date()
    else:
        most_recent_complete = last_date

    if not bars:
        return FreshnessResult(fresh=False, last_bar_date=None,
                                most_recent_complete_session=most_recent_complete.isoformat())

    last_bar_date = datetime.fromtimestamp(bars[-1]["time"], tz=timezone.utc).date()
    return FreshnessResult(
        fresh=last_bar_date >= most_recent_complete,
        last_bar_date=last_bar_date.isoformat(),
        most_recent_complete_session=most_recent_complete.isoformat(),
    )


def describe_history_coverage(bars: list[dict], requested_years: float) -> dict:
    """B1: every report claiming "all-time high" or "no data above this level" must
    be scoped to what was actually fetched -- this computes the objective coverage
    facts (bar count, actual date range) so that claim can be made honestly instead
    of resting on a code comment nobody reading a Telegram report ever sees.

    `ath_verified` is deliberately NOT computed here -- whether the fetched window
    covers a stock's real listing history can't be determined from bar count alone.
    Callers must explicitly decide it true only with real reason (e.g. the fetched
    start date predates the stock's known IPO), otherwise leave it False and use the
    scoped "no qualifying target found within fetched history: [date_start] to
    [date_end]" wording instead of "all-time high" / "no data above this level" --
    the KRE full-vs-truncated-history lesson this rule (16, CONSISTENCY_RULES.md) is
    named for."""
    if not bars:
        return {"bars_received": 0, "date_start": None, "date_end": None, "history_complete": False}
    target_bars = int(requested_years * 252 * 1.05)
    return {
        "bars_received": len(bars),
        "date_start": datetime.fromtimestamp(bars[0]["time"], tz=timezone.utc).date().isoformat(),
        "date_end": datetime.fromtimestamp(bars[-1]["time"], tz=timezone.utc).date().isoformat(),
        # got essentially what was requested, not silently cut short by TradingView's
        # own lazy-load buffer cap (~2 years in practice, see get_daily_history's docstring)
        "history_complete": len(bars) >= target_bars * 0.95,
    }
