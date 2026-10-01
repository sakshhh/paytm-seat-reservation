import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator

from . import auth, config, db, reservations, shows
from .errors import DomainError


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    await db.migrate()
    yield
    await db.close()


app = FastAPI(title="seat-reservation", lifespan=lifespan)


# ---------------------------------------------------------------- errors ----
@app.exception_handler(DomainError)
async def domain_error(_: Request, exc: DomainError):
    return JSONResponse(exc.body(), status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    errs = [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse({"error": "invalid_request", "message": "validation failed",
                         "details": errs}, status_code=400)


def _parse_uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise DomainError(404, f"{what}_not_found", f"no such {what}")


# ---------------------------------------------------------------- models ----


class TokenRequest(BaseModel):
    user_id: str


class CreateShow(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    seats: list[str] = Field(min_length=1, max_length=config.MAX_SEATS_PER_SHOW)
    price_paise: StrictInt = Field(ge=0)          # StrictInt: 250.0 is rejected, never coerced
    per_user_limit: StrictInt = Field(default=config.DEFAULT_PER_USER_LIMIT, ge=1, le=100)

    @field_validator("seats")
    @classmethod
    def unique_labels(cls, v):
        for s in v:
            if not (1 <= len(s) <= 16) or not s.replace("_", "").replace("-", "").isalnum():
                raise ValueError(f"invalid seat label {s!r}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate seat labels")
        return v


class ReserveRequest(BaseModel):
    # NOTE: deliberately no user_id field. Any "user_id" in the body is ignored.
    seats: list[str] = Field(min_length=1, max_length=config.MAX_SEATS_PER_REQUEST)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("seats")
    @classmethod
    def unique_seats(cls, v):
        if len(set(v)) != len(v):
            raise ValueError("duplicate seats in request")
        return v


# ---------------------------------------------------------------- routes ----
@app.post("/auth/token")
async def token(body: TokenRequest):
    """Demo identity provider: mints a signed token for a user id."""
    return {"user_id": body.user_id, "token": auth.issue_token(body.user_id), "token_type": "Bearer"}


@app.post("/shows", status_code=201, dependencies=[Depends(auth.require_admin)])
async def create_show(body: CreateShow):
    return await shows.create_show(body.name, body.seats, body.price_paise, body.per_user_limit)


@app.get("/shows/{show_id}")
async def get_show(show_id: str):
    return await shows.get_show(_parse_uuid(show_id, "show"))


@app.post("/shows/{show_id}/reserve")
async def reserve(show_id: str, body: ReserveRequest,
                  user_id: str = Depends(auth.current_user),
                  idempotency_key: str | None = Header(default=None)):
    key = idempotency_key or body.idempotency_key
    if not key:
        raise DomainError(400, "idempotency_key_required",
                          "send Idempotency-Key header or idempotency_key in body")
    if idempotency_key and body.idempotency_key and idempotency_key != body.idempotency_key:
        raise DomainError(400, "idempotency_key_conflict", "header and body keys differ")
    if len(key) > 200:
        raise DomainError(400, "invalid_request", "idempotency key too long")

    result = await reservations.reserve(_parse_uuid(show_id, "show"), user_id, body.seats, key)
    if result.replayed:
        return JSONResponse(result.reservation, status_code=200,
                            headers={"Idempotent-Replayed": "true"})
    return JSONResponse(result.reservation, status_code=201)


@app.post("/reservations/{reservation_id}/cancel")
async def cancel(reservation_id: str, user_id: str = Depends(auth.current_user)):
    return await reservations.cancel(_parse_uuid(reservation_id, "reservation"), user_id)
