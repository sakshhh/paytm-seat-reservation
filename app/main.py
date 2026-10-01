import asyncio

import asyncpg
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, StrictInt, field_validator

from . import auth, config, db, reservations, shows
from . import observability as obs
from .errors import DomainError

obs.setup_logging(config.LOG_LEVEL)
log = logging.getLogger("seatres")
state = {"migrated": False}


async def _bootstrap():
    """Connect + migrate in the background with retries, so the process comes
    up (liveness OK) even if the DB is slow on a cold start; readiness stays
    failed-closed until this succeeds."""
    delay = 0.5
    while True:
        try:
            if db.pool is None:
                await db.connect()
            await db.migrate()
            state["migrated"] = True
            log.info("database ready")
            return
        except Exception as e:  # noqa: BLE001
            log.warning("database not ready, retrying", extra={"error": repr(e), "retry_in_s": delay})
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_bootstrap())
    yield
    task.cancel()
    await db.close()


app = FastAPI(title="seat-reservation", lifespan=lifespan)


# ------------------------------------------------------- request logging ----
@app.middleware("http")
async def access_log(request: Request, call_next):
    rid = obs.new_request_id(request.headers.get("x-request-id"))
    token = obs.request_id_var.set(rid)
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        elapsed = time.perf_counter() - start
        route = getattr(request.scope.get("route"), "path", "unmatched")
        if route not in ("/metrics", "/healthz", "/readyz"):
            obs.HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
            obs.HTTP_LATENCY.labels(request.method, route).observe(elapsed)
            log.info("request", extra={
                "method": request.method, "path": request.url.path, "route": route,
                "status": status, "duration_ms": round(elapsed * 1000, 2),
                **getattr(request.state, "log_fields", {}),
            })
        obs.request_id_var.reset(token)


# ---------------------------------------------------------------- errors ----
@app.exception_handler(DomainError)
async def domain_error(request: Request, exc: DomainError):
    request.state.log_fields = {**getattr(request.state, "log_fields", {}), "outcome": exc.code}
    return JSONResponse(exc.body(), status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    errs = [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse({"error": "invalid_request", "message": "validation failed",
                         "details": errs}, status_code=400)


@app.exception_handler(asyncio.TimeoutError)
async def pool_timeout(_: Request, exc: asyncio.TimeoutError):
    # Only reachable if the DB pool stays saturated for DB_ACQUIRE_TIMEOUT.
    log.error("db pool acquire timed out")
    return JSONResponse({"error": "overloaded", "message": "try again"}, status_code=503,
                        headers={"Retry-After": "1"})


@app.exception_handler(OSError)
@app.exception_handler(asyncpg.PostgresConnectionError)
@app.exception_handler(asyncpg.InterfaceError)
@app.exception_handler(asyncpg.CannotConnectNowError)
async def db_unavailable(_: Request, exc: Exception):
    # Dependency down: fail closed and say so, rather than an opaque 500.
    log.error("database unavailable", extra={"error": repr(exc)})
    return JSONResponse({"error": "db_unavailable", "message": "try again"}, status_code=503,
                        headers={"Retry-After": "2"})


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse({"error": "internal", "request_id": obs.request_id_var.get()},
                        status_code=500)


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
async def reserve(request: Request, show_id: str, body: ReserveRequest,
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

    request.state.log_fields = {"user_id": user_id, "show_id": show_id, "seats": body.seats}
    try:
        result = await reservations.reserve(_parse_uuid(show_id, "show"), user_id, body.seats, key)
    except DomainError as e:
        obs.RESERVATIONS_DECLINED.labels(e.code).inc()
        raise
    request.state.log_fields["reservation_id"] = result.reservation["reservation_id"]
    if result.replayed:
        obs.RESERVATIONS_DECLINED.labels("idempotent_replay").inc()
        request.state.log_fields["outcome"] = "idempotent_replay"
        return JSONResponse(result.reservation, status_code=200,
                            headers={"Idempotent-Replayed": "true"})
    obs.RESERVATIONS_CONFIRMED.inc()
    obs.SEATS_CONFIRMED.inc(len(result.reservation["seats"]))
    request.state.log_fields["outcome"] = "confirmed"
    return JSONResponse(result.reservation, status_code=201)


@app.post("/reservations/{reservation_id}/cancel")
async def cancel(request: Request, reservation_id: str, user_id: str = Depends(auth.current_user)):
    request.state.log_fields = {"user_id": user_id, "reservation_id": reservation_id}
    res, freed = await reservations.cancel(_parse_uuid(reservation_id, "reservation"), user_id)
    if freed:
        obs.RESERVATIONS_CANCELLED.inc()
        obs.SEATS_RELEASED.inc(freed)
    request.state.log_fields["outcome"] = "cancelled" if freed else "already_cancelled"
    return res


@app.get("/reservations/{reservation_id}")
async def get_reservation(reservation_id: str, user_id: str = Depends(auth.current_user)):
    return await reservations.get(_parse_uuid(reservation_id, "reservation"), user_id)


# ---------------------------------------------------------- health/metrics --
@app.get("/healthz")
async def healthz():
    """Liveness: the process is up and serving. Deliberately no DB check, so a
    DB outage doesn't get the container restarted in a loop."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Readiness: fails closed (503) unless the DB is reachable and migrated."""
    if not state["migrated"] or db.pool is None:
        return JSONResponse({"status": "not_ready", "db": "not_initialised"}, status_code=503)
    try:
        async with db.pool.acquire(timeout=2) as conn:
            await conn.fetchval("SELECT 1", timeout=2)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"status": "not_ready", "db": repr(e)}, status_code=503)
    return {"status": "ready", "db": "ok"}


@app.get("/metrics")
async def metrics():
    if state["migrated"] and db.pool is not None:
        try:
            async with db.pool.acquire(timeout=2) as conn:
                rows = await conn.fetch(
                    """SELECT sh.id::text AS show_id, sh.total_seats,
                              count(*) FILTER (WHERE s.status = 'available') AS available,
                              count(*) FILTER (WHERE s.status = 'held')      AS held,
                              count(*) FILTER (WHERE s.status = 'confirmed') AS confirmed
                         FROM (SELECT * FROM shows ORDER BY created_at DESC LIMIT 50) sh
                         JOIN seats s ON s.show_id = sh.id
                        GROUP BY sh.id, sh.total_seats""", timeout=5)
            for r in rows:
                for st in ("available", "held", "confirmed"):
                    obs.SEATS.labels(r["show_id"], st).set(r[st])
                obs.SEATS_TOTAL.labels(r["show_id"]).set(r["total_seats"])
                obs.SEATS_RECONCILED.labels(r["show_id"]).set(
                    int(r["available"] + r["held"] + r["confirmed"] == r["total_seats"]))
            obs.DB_POOL_SIZE.labels("total").set(db.pool.get_size())
            obs.DB_POOL_SIZE.labels("idle").set(db.pool.get_idle_size())
        except Exception as e:  # noqa: BLE001
            log.warning("metrics db scrape failed", extra={"error": repr(e)})
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
