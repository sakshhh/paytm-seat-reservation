import uuid

from . import db
from .errors import DomainError


async def create_show(name: str, seats: list[str], price_paise: int, per_user_limit: int) -> dict:
    show_id = uuid.uuid4()
    async with db.acquire() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO shows (id, name, price_paise, per_user_limit, total_seats)
               VALUES ($1, $2, $3, $4, $5)""",
            show_id, name, price_paise, per_user_limit, len(seats))
        await conn.execute(
            "INSERT INTO seats (show_id, label) SELECT $1, unnest($2::text[])",
            show_id, seats)
    return await get_show(show_id)


async def get_show(show_id: uuid.UUID) -> dict:
    async with db.acquire() as conn:
        # Counts are derived from the single seat-list query (one snapshot), so
        # they are consistent even mid-burst; total_seats never changes.
        show = await conn.fetchrow("SELECT * FROM shows WHERE id = $1", show_id)
        if show is None:
            raise DomainError(404, "show_not_found", "no such show")
        seats = await conn.fetch(
            "SELECT label, status FROM seats WHERE show_id = $1 ORDER BY label", show_id)
    counts = {"available": 0, "held": 0, "confirmed": 0}
    for s in seats:
        counts[s["status"]] += 1
    return {
        "id": str(show["id"]),
        "name": show["name"],
        "price_paise": show["price_paise"],
        "per_user_limit": show["per_user_limit"],
        "total_seats": show["total_seats"],
        "counts": counts,
        "reconciled": sum(counts.values()) == show["total_seats"],
        "seats": [{"seat": s["label"], "status": s["status"]} for s in seats],
    }
