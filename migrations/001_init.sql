-- Seat reservation schema.
-- Every correctness rule that matters under concurrency is enforced by the
-- database (row locks, conditional updates, CHECK and UNIQUE constraints),
-- never by a read-then-write in application code.

CREATE TABLE IF NOT EXISTS shows (
    id             UUID PRIMARY KEY,
    name           TEXT        NOT NULL,
    price_paise    BIGINT      NOT NULL CHECK (price_paise >= 0),   -- integer minor units, never float
    per_user_limit INT         NOT NULL DEFAULT 4 CHECK (per_user_limit > 0),
    total_seats    INT         NOT NULL CHECK (total_seats > 0),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reservations (
    id           UUID PRIMARY KEY,
    show_id      UUID        NOT NULL REFERENCES shows(id),
    user_id      TEXT        NOT NULL,
    seats        TEXT[]      NOT NULL,
    amount_paise BIGINT      NOT NULL CHECK (amount_paise >= 0),
    status       TEXT        NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS reservations_show_user ON reservations (show_id, user_id);

-- One row per physical seat. A seat can only ever be in ONE status, so
-- available + held + confirmed == total_seats holds by construction.
CREATE TABLE IF NOT EXISTS seats (
    show_id        UUID NOT NULL REFERENCES shows(id),
    label          TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'available'
                   CHECK (status IN ('available', 'held', 'confirmed')),
    reservation_id UUID REFERENCES reservations(id),
    PRIMARY KEY (show_id, label),
    -- a taken seat must point at its owner; a free seat must point at nobody
    CHECK ((status = 'available') = (reservation_id IS NULL))
);

-- Per-user seat count for a show. The conditional increment
--   UPDATE ... SET seat_count = seat_count + n WHERE seat_count + n <= limit
-- is the atomic per-user-limit check; the CHECK is a backstop.
CREATE TABLE IF NOT EXISTS user_show_usage (
    show_id    UUID NOT NULL REFERENCES shows(id),
    user_id    TEXT NOT NULL,
    seat_count INT  NOT NULL DEFAULT 0 CHECK (seat_count >= 0),
    PRIMARY KEY (show_id, user_id)
);

-- Idempotency keys are scoped to the authenticated user: user B can never
-- collide with, or replay, user A's key. Written in the same transaction as
-- the reservation, so a key exists if and only if its reservation exists.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    user_id        TEXT        NOT NULL,
    key            TEXT        NOT NULL,
    request_hash   TEXT        NOT NULL,
    -- the key row is inserted FIRST (to claim the key), before the
    -- reservation row exists, so the FK is checked at commit time
    reservation_id UUID        NOT NULL REFERENCES reservations(id)
                               DEFERRABLE INITIALLY DEFERRED,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);
