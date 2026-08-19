"""Shared pytest setup for this project's test suite (2026-08-02).

One job: keep test runs from leaking into the real world -- the real log
files, and (2026-08-03) the real Telegram chat.

Found the hard way. `process_queue.py`'s `_log()` writes to
`PROJECT_ROOT/process_queue.log` -- the file a human opens when something looks
broken -- and the tests call the same handlers, so every `pytest` run appended
lines like:

    /playbook: claude -p exit=0 output_len=2  -> {}
    /maxadd SCHW: fetch_maxadd_data.py failed rc=1: FAILED: TradingView connection lost

Those are synthetic test fixtures behaving exactly as designed. Read in the log
hours later, next to real entries, with real timestamps, they look precisely
like a broken /playbook and a dropped TradingView connection -- and were
diagnosed as exactly that during a live review, costing a real detour before
someone noticed the timestamps lined up with test runs rather than with
anything the user had sent.

Autouse, so it applies to every test without each file remembering to opt in.

Same class of leak, found 2026-08-03, this time visible to the user rather
than buried in a log: every deliver_*.py script calls
telegram_send.send_failure_alert() on its failure path, and NO test ever
mocked it -- only send_text/send_photo/send_document were mocked. So each
`pytest` run of the deliberately-failing delivery tests fired REAL Telegram
messages at the real chat from the real bot token in .env:

    ⚠️ Report failed: position status report
    send_text did not confirm ok=True
    ⚠️ Report failed: result missing ticker/headline: {'ticker': 'NVDA'}
    ⚠️ Report failed: CRM screener report        (x3 -- the parametrized
    one or more Telegram sends failed             send_photo/document/text case)

NVDA and CRM are test fixture values, not real tickers in trouble; the counts
match the failure-path tests exactly. Read on a phone hours later they are
indistinguishable from a genuinely broken overnight report run, which is
exactly how they were reported ("every day is the same thing").

Fixed in two independent layers below, deliberately not one:
  1. _block_real_network -- nothing in this suite may reach the network at
     all, ever, whether or not anyone remembered to mock it. Catches this
     whole class of bug for every future test and every future script,
     including sends nobody has written yet.
  2. _silence_failure_alerts -- names the specific function, so a test
     exercising a failure path reads as intentionally silent rather than
     relying on layer 1 to swallow it.

Third leak of the same family, found 2026-08-07 while wiring the chart redraw
into /filled, /add and /exit: TVClient doesn't use `requests` at all -- it
spawns the vendored tradingview-mcp `node` server over stdio and drives the
user's OWN live TradingView window. So layer 1 never saw it. The very first
run of the existing /add happy-path test spent 3.1 seconds inside a real
TVClient session against the real chart, with fixture tickers. Same shape as
the Telegram spam: synthetic test data, indistinguishable on screen from
something the user actually asked for. _block_tradingview below is the third
layer, and it blocks the connection itself rather than any one call site.
"""

import sys

import pytest

_LOG_MODULES = (
    "ack_listener",
    "backup_db",
    "process_queue",
    "refresh_pending",
    "score_shadow",
)


@pytest.fixture(autouse=True)
def _isolate_log_files(tmp_path, monkeypatch):
    """Point every module-level LOG_FILE at the test's own temp directory.

    Imports are attempted individually and skipped if unavailable -- a module
    that grows or loses a LOG_FILE should never break collection of unrelated
    tests, and a missing one is not a failure worth reporting here."""
    for name in _LOG_MODULES:
        try:
            module = __import__(name)
        except Exception:
            continue
        if hasattr(module, "LOG_FILE"):
            monkeypatch.setattr(module, "LOG_FILE", tmp_path / f"{name}.log")


# Every HTTP verb `requests` exposes at module level. telegram_send.py,
# download_photo.py and fetch_x_feed.py all call these as module attributes
# (`requests.post(...)`, or `_request_with_retry(requests.post, ...)`), looked
# up at call time -- so patching the attribute here covers all of them without
# each module needing to know it's under test.
_BLOCKED_VERBS = ("request", "get", "post", "put", "patch", "delete", "head", "options")


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """Hard-fail any unmocked outbound HTTP instead of letting it through.

    Raises rather than returning a canned fake response: a test that reaches
    the network is a test with a hole in its mocking, and it should say so out
    loud at the exact call site. Note this alone would NOT have made the
    Telegram-spam tests fail -- send_failure_alert() catches every exception by
    design (it runs inside an already-failing except block and must never
    itself raise) -- but it does stop the message from ever leaving the
    machine, which is the part that matters.

    A test that genuinely wants to drive requests still can: it just has to
    monkeypatch it in the test body, which runs after this fixture and wins."""
    import requests

    def _blocked(*args, **kwargs):
        target = args[0] if args else kwargs.get("url", "<unknown url>")
        raise RuntimeError(
            f"blocked a real network call from a test: {target}\n"
            "Nothing in this suite may talk to a live API (Telegram, Yahoo, X). "
            "Mock the call in the test, or in bot/conftest.py if it's shared."
        )

    for verb in _BLOCKED_VERBS:
        monkeypatch.setattr(requests, verb, _blocked, raising=False)
    # Anything holding its own Session (or a library doing so internally) goes
    # through this one method rather than the module-level verbs above.
    monkeypatch.setattr(requests.sessions.Session, "request", _blocked)


@pytest.fixture(autouse=True)
def _block_tradingview(monkeypatch):
    """Hard-fail any unmocked TVClient connection instead of letting it drive
    the user's real chart.

    Patches the connect() METHOD on the class, not the name TVClient: every
    caller does `from tv_data import TVClient` and so holds its own reference
    to the same class object -- patching the method reaches all of them at
    once, including modules not yet written. Raising (rather than returning a
    no-op client) means a test with a hole in its mocking says so at the exact
    call site, same posture as _block_real_network above.

    tv_data is imported lazily inside the fixture: a test that never touches
    TradingView shouldn't pay the import cost of the MCP client stack.

    A test that genuinely wants to drive a fake client still can -- it
    monkeypatches in the test body, which runs after this fixture and wins."""
    try:
        import tv_data
    except Exception:
        return

    async def _blocked(self, *args, **kwargs):
        raise RuntimeError(
            "blocked a real TradingView connection from a test.\n"
            "TVClient drives the user's own live chart (watchlist edits, drawn "
            "lines) -- nothing in this suite may do that. Mock the client in "
            "the test, or the draw helper the code under test calls."
        )

    monkeypatch.setattr(tv_data.TVClient, "connect", _blocked)


# Every module that reaches for send_failure_alert. They use
# `from telegram_send import send_failure_alert`, so each holds its OWN
# reference -- patching telegram_send alone would miss all of them.
_ALERT_MODULES = (
    "telegram_send",
    "deliver_report",
    "deliver_monitor_report",
    "deliver_auto_monitor_report",
    "deliver_playbook_report",
    "deliver_position_status_report",
    "trigger_auto_monitor",
    "trigger_position_status",
    "ack_listener",
    "process_queue",
)


@pytest.fixture(autouse=True)
def _silence_failure_alerts(monkeypatch):
    """Replace send_failure_alert with a no-op wherever it's already imported.

    Reads sys.modules rather than importing: a test that never touched
    deliver_playbook_report shouldn't pay for importing it (Playwright, the
    TradingView client) just to stub one name on it. By the time a fixture
    runs, any module its test file imports is already in sys.modules."""
    for name in _ALERT_MODULES:
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "send_failure_alert"):
            monkeypatch.setattr(module, "send_failure_alert", lambda *a, **k: None)
