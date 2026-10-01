import os


def _env(name: str, default: str | None = None) -> str:
    val = os.getenv(name, default)
    if val is None:
        raise RuntimeError(f"missing required env var {name}")
    return val


DATABASE_URL = _env("DATABASE_URL", "postgresql://postgres@localhost:5432/seats")
JWT_SECRET = _env("JWT_SECRET", "dev-only-secret-change-me")
ADMIN_KEY = _env("ADMIN_KEY", "dev-admin-key")
TOKEN_TTL_SECONDS = int(_env("TOKEN_TTL_SECONDS", str(7 * 24 * 3600)))

DB_POOL_MIN = int(_env("DB_POOL_MIN", "2"))
DB_POOL_MAX = int(_env("DB_POOL_MAX", "20"))
# How long a request may queue for a DB connection during a burst before we
# give up. Long on purpose: queueing is better than failing a buyer.
DB_ACQUIRE_TIMEOUT = float(_env("DB_ACQUIRE_TIMEOUT", "60"))

DEFAULT_PER_USER_LIMIT = int(_env("DEFAULT_PER_USER_LIMIT", "4"))
MAX_SEATS_PER_SHOW = int(_env("MAX_SEATS_PER_SHOW", "20000"))
MAX_SEATS_PER_REQUEST = 10
LOG_LEVEL = _env("LOG_LEVEL", "INFO")
