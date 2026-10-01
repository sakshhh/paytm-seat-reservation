"""Identity is derived ONLY from a signed bearer token. Nothing in a request
body can change who the caller is."""
import hmac
import re
import time

import jwt
from fastapi import Header

from . import config
from .errors import DomainError

USER_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


def issue_token(user_id: str) -> str:
    if not USER_ID_RE.match(user_id):
        raise DomainError(400, "invalid_user_id", "user_id must match [A-Za-z0-9_.@-]{1,64}")
    now = int(time.time())
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + config.TOKEN_TTL_SECONDS},
        config.JWT_SECRET,
        algorithm="HS256",
    )


def current_user(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise DomainError(401, "unauthenticated", "missing bearer token")
    token = authorization[7:].strip()
    try:
        claims = jwt.decode(token, config.JWT_SECRET, algorithms=["HS256"],
                            options={"require": ["sub", "exp"]})
    except jwt.PyJWTError:
        raise DomainError(401, "unauthenticated", "invalid or expired token")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not USER_ID_RE.match(sub):
        raise DomainError(401, "unauthenticated", "invalid subject")
    return sub


def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    if not x_admin_key or not hmac.compare_digest(x_admin_key, config.ADMIN_KEY):
        raise DomainError(403, "forbidden", "admin key required")
