"""Where a ticker's bars live on disk, safely on Windows.

Windows still reserves a handful of names from the DOS era -- CON, PRN, AUX,
NUL, COM1-9, LPT1-9 -- and they are reserved with ANY extension. `CON.json` is
not a file; it is the console device. Opening it succeeds, `Path.exists()`
returns True, and READING it blocks forever waiting for someone to type at the
keyboard.

That is exactly what happened on 2026-09-02: the expanded-universe run froze
solid, with no CPU use and no error, every time it reached Continental Building
Products (ticker CON). It looked like a database lock for hours. It was the
operating system waiting for a keystroke.

So every read and write of a bar file goes through here. A reserved ticker gets
a trailing underscore on disk and keeps its real name everywhere else.
"""

from __future__ import annotations

from pathlib import Path

RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
}


def safe_name(ticker: str) -> str:
    """The on-disk stem for a ticker. Identical to the ticker unless Windows
    would treat it as a device."""
    return f"{ticker}_" if ticker.upper() in RESERVED else ticker


def bar_path(bars_dir: Path, ticker: str) -> Path:
    return bars_dir / f"{safe_name(ticker)}.json"


def ticker_of(path: Path) -> str:
    """The ticker a bar file belongs to, undoing safe_name."""
    stem = path.stem
    return stem[:-1] if stem.endswith("_") and stem[:-1].upper() in RESERVED else stem
