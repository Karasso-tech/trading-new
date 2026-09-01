"""Mechanical photo download for /playbook automation (2026-07-09). Downloads a
Telegram photo by file_id to a fixed local path so the automated claude -p
invocation can read it directly (Claude Code's Read tool supports images). No
judgment here -- reading/interpreting the screenshot's contents is Category B and
stays with whichever Claude session does it.

Usage: python bot/download_photo.py FILE_ID OUTPUT_PATH
"""

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from telegram_send import _load_credentials


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: python bot/download_photo.py FILE_ID OUTPUT_PATH", file=sys.stderr)
        sys.exit(1)
    file_id, output_path = sys.argv[1], sys.argv[2]
    token, _ = _load_credentials()

    resp = requests.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id}, timeout=15)
    resp.raise_for_status()
    info = resp.json()
    if not info.get("ok"):
        print(f"FAILED: getFile did not return ok=True: {info}", file=sys.stderr)
        sys.exit(1)
    telegram_path = info["result"]["file_path"]

    file_resp = requests.get(f"https://api.telegram.org/file/bot{token}/{telegram_path}", timeout=30)
    file_resp.raise_for_status()
    Path(output_path).write_bytes(file_resp.content)
    print(f"OK: downloaded {len(file_resp.content)} bytes to {output_path}")


if __name__ == "__main__":
    main()
