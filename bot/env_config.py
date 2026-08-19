"""Single shared .env reader (2026-07-16).

Found in review: persistence.get_circuit_breaker_threshold(),
telegram_send._load_credentials(), and ack_listener._load_allowed_user_id() each
independently re-implemented the same naive line-based .env parse. Any future
quirk in the file (a quoted value, a trailing comment) would silently parse
differently across the three, not consistently. One shared reader instead --
still no new dependency (python-dotenv), just the same simple approach written
once.
"""

from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = PROJECT_ROOT / ".env"


def get_env_value(key: str, env_path: Optional[Path] = None) -> Optional[str]:
    """Returns the value of KEY= from .env, or None if the file or the key
    doesn't exist. Not cached -- .env is tiny and read rarely enough (a
    handful of calls per command) that re-reading fresh every time is simpler
    than ever risking a stale value surviving a manual edit.

    env_path defaults to this project's real .env, but persistence.py's
    get_circuit_breaker_threshold() passes DB_PATH.parent / ".env" explicitly
    -- that path is dynamic (DB_PATH itself is monkeypatched in tests to an
    isolated temp DB), and its own test suite (test_circuit_breaker.py)
    depends on writing a throwaway .env alongside that temp DB, not this
    project's real one."""
    path = env_path if env_path is not None else _ENV_PATH
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None
