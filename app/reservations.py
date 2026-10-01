"""The reservation state machine. All concurrency decisions live here.

Lock order (identical in every transaction, so no deadlock cycle is possible):
    1. idempotency_keys(user_id, key)       -- reserve only
    2. user_show_usage(show_id, user_id)
    3. seats(show_id, label) sorted by label -- FOR UPDATE
Cancel additionally locks its own reservations row first; reserve never locks
an existing reservation row, so that cannot form a cycle either.
"""
import hashlib
import json
import uuid
from dataclasses import dataclass

import asyncpg

from . import config, db
from .errors import DomainError


@dataclass
class ReserveResult:
    reservation: dict
    replayed: bool


def request_hash(show_id: str, seats: list[str]) -> str:
    # Seat order is irrelevant to the meaning of a request, so normalise it.
    canonical = json.dumps({"show_id": show_id, "seats": sorted(seats)}, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _reservation_dict(row) -> dict:
    return {
        "reservation_id": str(row["id"]),
        "show_id": str(row["show_id"]),
        "user_id": row["user_id"],
        "seats": list(row["seats"]),
        "amount_paise": row["amount_paise"],
        "status": row["status"],
    }


async def _replay(conn, user_id: str, key: str, req_hash: str) -> ReserveResult | None:
    row = await conn.fetchrow(
        """SELECT k.request_hash, r.*
             FROM idempotency_keys k JOIN reservations r ON r.id = k.reservation_id
            WHERE k.user_id = $1 AND k.key = $2""",
        user_id, key,
    )
    if row is None:
        return None
    if row["request_hash"] != req_hash:
        raise DomainError(409, "idempotency_key_mismatch",
                          "idempotency key was already used with a different request")
    return ReserveResult(_reservation_dict(row), replayed=True)


async def reserve(show_id: uuid.UUID, user_id: str, seats: list[str], key: str) -> ReserveResult:
    req_hash = request_hash(str(show_id), seats)
    labels = sorted(seats)
    n = len(labels)

    async with db.acquire() as conn:
        # --- fast paths (plain reads, no locks) -------------------------------
        # A retry must return the original result even if the seat is now
        # taken, so the idempotency lookup comes before anything else.
        replay = await _replay(conn, user_id, key, req_hash)
        if replay:
            return replay

        show = await conn.fetchrow(
            "SELECT price_paise, per_user_limit FROM shows WHERE id = $1", show_id)
        if show is None:
            raise DomainError(404, "show_not_found", "no such show")
        if n > show["per_user_limit"]:
            raise DomainError(409, "per_user_limit", "request exceeds the per-user seat limit",
                              limit=show["per_user_limit"])

        # Optimistic pre-check: during a hot-seat storm almost everyone is a
        # loser; turn them away without opening a write transaction. This is
        # ONLY a shortcut for "no" — a "yes" is always re-decided under lock.
        taken = [] if config.DISABLE_PRECHECK else await conn.fetch(
            "SELECT label FROM seats WHERE show_id = $1 AND label = ANY($2::text[]) AND status <> 'available'",
            show_id, labels)
        if taken:
            raise DomainError(409, "seat_taken", "seat(s) not available",
                              seats=sorted(r["label"] for r in taken))

        # --- the atomic decision ---------------------------------------------
        for attempt in range(3):
            try:
                return await _reserve_tx(conn, show_id, user_id, labels, key, req_hash, show)
            except asyncpg.DeadlockDetectedError:  # should be impossible; belt & braces
                if attempt == 2:
                    raise
    raise AssertionError("unreachable")


async def _reserve_tx(conn, show_id, user_id, labels, key, req_hash, show) -> ReserveResult:
    n = len(labels)
    res_id = uuid.uuid4()
    async with conn.transaction():
        # 1. Claim the key. A concurrent request with the same key blocks here
        #    on the primary-key index until the first one commits or rolls back.
        claimed = await conn.fetchval(
            """INSERT INTO idempotency_keys (user_id, key, request_hash, reservation_id)
               VALUES ($1, $2, $3, $4) ON CONFLICT (user_id, key) DO NOTHING
               RETURNING 1""",
            user_id, key, req_hash, res_id)
        if not claimed:
            replay = await _replay(conn, user_id, key, req_hash)
            assert replay is not None
            return replay

        # 2. Per-user limit: one conditional UPDATE is the whole check.
        await conn.execute(
            """INSERT INTO user_show_usage (show_id, user_id) VALUES ($1, $2)
               ON CONFLICT DO NOTHING""", show_id, user_id)
        ok = await conn.fetchval(
            """UPDATE user_show_usage SET seat_count = seat_count + $3
                WHERE show_id = $1 AND user_id = $2 AND seat_count + $3 <= $4
            RETURNING seat_count""",
            show_id, user_id, n, show["per_user_limit"])
        if ok is None:
            raise DomainError(409, "per_user_limit", "per-user seat limit reached",
                              limit=show["per_user_limit"])

        # 3. Lock the seats in sorted order, then decide while holding the locks.
        rows = await conn.fetch(
            """SELECT label, status FROM seats
                WHERE show_id = $1 AND label = ANY($2::text[])
                ORDER BY label FOR UPDATE""",
            show_id, labels)
        if len(rows) != n:
            unknown = sorted(set(labels) - {r["label"] for r in rows})
            raise DomainError(400, "unknown_seat", "seat(s) do not exist in this show", seats=unknown)
        taken = [r["label"] for r in rows if r["status"] != "available"]
        if taken:  # all-or-nothing
            raise DomainError(409, "seat_taken", "seat(s) not available", seats=taken)

        row = await conn.fetchrow(
            """INSERT INTO reservations (id, show_id, user_id, seats, amount_paise, status)
               VALUES ($1, $2, $3, $4, $5, 'confirmed') RETURNING *""",
            res_id, show_id, user_id, labels, show["price_paise"] * n)
        updated = await conn.execute(
            """UPDATE seats SET status = 'confirmed', reservation_id = $3
                WHERE show_id = $1 AND label = ANY($2::text[]) AND status = 'available'""",
            show_id, labels, res_id)
        if updated != f"UPDATE {n}":  # cannot happen while we hold the locks
            raise RuntimeError(f"seat update mismatch: {updated}")
        return ReserveResult(_reservation_dict(row), replayed=False)


async def get(reservation_id: uuid.UUID, user_id: str) -> dict:
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM reservations WHERE id = $1", reservation_id)
    # Someone else's reservation is indistinguishable from a missing one.
    if row is None or row["user_id"] != user_id:
        raise DomainError(404, "reservation_not_found", "no such reservation")
    return _reservation_dict(row)


async def cancel(reservation_id: uuid.UUID, user_id: str) -> tuple[dict, int]:
    """Returns (reservation, seats_freed). seats_freed == 0 for a repeat cancel."""
    async with db.acquire() as conn, conn.transaction():
        res = await conn.fetchrow(
            "SELECT * FROM reservations WHERE id = $1 FOR UPDATE", reservation_id)
        if res is None:
            raise DomainError(404, "reservation_not_found", "no such reservation")
        if res["user_id"] != user_id:
            raise DomainError(403, "not_owner", "only the owner can cancel this reservation")
        if res["status"] == "cancelled":  # cancelling twice is a no-op
            return _reservation_dict(res), 0

        await conn.execute(
            "SELECT 1 FROM user_show_usage WHERE show_id = $1 AND user_id = $2 FOR UPDATE",
            res["show_id"], user_id)
        await conn.execute(
            """SELECT 1 FROM seats WHERE show_id = $1 AND reservation_id = $2
                ORDER BY label FOR UPDATE""", res["show_id"], reservation_id)
        # Guarded on reservation_id: a release can only ever free seats that
        # belong to THIS reservation, never a seat now owned by someone else.
        freed = await conn.fetchval(
            """WITH f AS (
                 UPDATE seats SET status = 'available', reservation_id = NULL
                  WHERE show_id = $1 AND reservation_id = $2 RETURNING 1)
               SELECT count(*) FROM f""", res["show_id"], reservation_id)
        await conn.execute(
            """UPDATE user_show_usage SET seat_count = seat_count - $3
                WHERE show_id = $1 AND user_id = $2""", res["show_id"], user_id, freed)
        row = await conn.fetchrow(
            """UPDATE reservations SET status = 'cancelled', cancelled_at = now()
                WHERE id = $1 RETURNING *""", reservation_id)
        return _reservation_dict(row), freed
