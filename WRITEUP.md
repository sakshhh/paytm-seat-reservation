# Write-up

## 1. The atomic decision

**The mechanism:** a row lock on each requested `seats` row (`SELECT … ORDER BY label FOR
UPDATE`), and the "is it available?" check runs *while those locks are held*, inside the
same transaction that flips the seats to `confirmed`. (`app/reservations.py::_reserve_tx`)

**Why it can't race.** Take 500 transactions that all want seat A12. The first one locks the row.
The other 499 block on the same lock. When the winner commits, Postgres (at READ COMMITTED)
lets the next waiter take the lock and re-read the row's *latest committed* version, which
is now `confirmed`. That waiter declines with 409 and rolls back. There is no gap between
"check" and "take", because the check runs on a row that nobody else can change until we
commit. Two schema rules back this up: `CHECK (status IN (...))` and
`CHECK ((status='available') = (reservation_id IS NULL))`. Together they make a seat that
is "half-taken", or owned by nobody, impossible to store.

**Multi-seat and deadlock.** Every transaction takes its locks in the same global order:

1. `idempotency_keys(user_id, key)`: reserve only
2. `user_show_usage(show_id, user_id)`
3. `seats(show_id, label)`, **sorted by label**

A deadlock needs a cycle, and transactions that all lock in one total order can't form
one. Cancel locks its own `reservations` row first. Reserve never locks an existing
reservation row, so that can't create a cycle either. To verify this, 300 users ask for
`{B1,B2}` and `{B2,B1}` concurrently; there are 0 deadlocks in the Postgres log, both in
the DB-level storm and in the HTTP burst. `DeadlockDetectedError` would still be retried
(up to 3 attempts) as a safety net, but it has never fired.

**Partial requests are all-or-nothing.** If any requested seat is taken, the whole
transaction rolls back (seats, usage counter and idempotency key) and the response is a 409
listing the unavailable seats. It holds under concurrency because the decision is made only
after *all* the requested rows are locked.

**Per-user limit.** This is a single conditional statement:
`UPDATE user_show_usage SET seat_count = seat_count + n WHERE … AND seat_count + n <= limit`.
Ten parallel requests from one user serialise on that one row, and the 5th through 10th
match zero rows. There is also `CHECK (seat_count >= 0)`. Cancel decrements the counter by
the number of seats it actually freed.

**Pre-check fast path.** During a hot-seat storm, nearly every request loses. Before
opening a write transaction, a plain `SELECT` checks whether any requested seat is already
taken, and if so returns 409 straight away. This shortcut can only ever say **no**. A
**yes** always goes on to the locked decision. A stale "no" (the seat was freed a moment
ago) is no different from arriving a moment earlier. To show that correctness doesn't
depend on it, `DISABLE_PRECHECK=1` sends every request through the locks, and the 20k
burst still passes every check (324 vs 455 req/s locally).

## 2. Idempotency

- **Storage:** the `idempotency_keys` table, `PRIMARY KEY (user_id, key)`. Keys are scoped
  per user, so one user can't collide with or replay another user's key. The row stores
  `request_hash = sha256(show_id + sorted seats)` and the `reservation_id`.
- **Exactly once:** the key row is inserted **in the same transaction** as the reservation,
  as its first step (`INSERT … ON CONFLICT DO NOTHING`). Its FK to the reservation is
  `DEFERRABLE INITIALLY DEFERRED`, because the reservation row is created later in that
  transaction. As a result:
  - Commit means the key and the reservation exist together. Rollback (a decline) means
    neither exists, so a retry after a decline is re-evaluated. Declines are not cached.
  - Two concurrent requests with the same key: the second one's INSERT blocks on the
    unique index until the first commits. It then sees the conflict and replays the stored
    reservation. In the burst, about 10% of storm requests are sent twice concurrently,
    and no key ever produced two reservations.
- **Replay:** returns `200` with the original body and `Idempotent-Replayed: true`. I chose
  200 rather than 201 so that "exactly one 201 per seat" stays literally true. If the
  reservation has since been cancelled, the replay returns it with `status:"cancelled"`. A
  retry never silently re-books.
- **Same key, different body:** hash mismatch, so `409 idempotency_key_mismatch`. This is
  checked before anything else, so it wins even when the seats are now taken.

## 3. Holds & expiry

The model is **confirm on reserve, plus an explicit owner-only cancel**. That matches the
spec's response (`status: "confirmed"`), and there is no payment step to wait on. `held`
exists in the schema and in every count, so a time-boxed hold can be added without
changing the invariant:

- reserve writes `status='held', hold_expires_at=now()+ttl`
- `POST /reservations/{id}/confirm` flips held to confirmed, guarded by
  `WHERE status='held' AND hold_expires_at > now()`
- a sweeper, or lazy expiry inside reserve, runs
  `UPDATE seats … SET status='available' WHERE status='held' AND hold_expires_at < now()`
  in the same lock order

**Cancel can never resurrect a seat that belongs to someone else.** The release is
`UPDATE seats SET status='available' WHERE reservation_id = $this_reservation`. Once a
seat has been re-sold, its `reservation_id` points at the new owner, so a stale or
duplicate cancel matches zero rows. The burst tests exactly this: cancel, re-race, then
cancel the old reservation again, and the seat stays confirmed to the new winner.

## 4. Consistency vs availability under a partition

I chose **CP**. Postgres is the single source of truth, and if the service can't reach it,
it refuses to sell:

- `reserve` returns `503 db_unavailable` with `Retry-After`, and `/readyz` returns 503, so
  the load balancer stops routing traffic.
- `/healthz` stays 200, so the orchestrator doesn't restart-loop a healthy process whose
  dependency is down.
- If the DB is down at boot, the service retries in the background and becomes ready once
  the DB is back. All of this was tested by stopping Postgres mid-run.

Selling a seat twice is unrecoverable: two people turn up at A12. A few seconds of "try
again" during an outage is recoverable. If a partition hits *after* commit but before the
client sees the response, the client retries with the same key and gets a replay. That is
exactly why idempotency is enforced in the database and not in memory.

Scaling: more API instances are safe, because every decision is in Postgres. The limits
are the DB's write throughput and its connection count (each instance has
`DB_POOL_MAX=20`). Next steps would be PgBouncer and partitioning by show (one hot show
can't be sharded, but different shows can).

## 5. Observability: what pages me at 2am

| Alert | Query (sketch) | Why |
|---|---|---|
| **Invariant broken** | `min(seats_reconciled) == 0` | Data-integrity bug. Page immediately. |
| **Double-sell detector** | periodic SQL: a seat in >1 confirmed reservation | Should be impossible. Page. |
| **5xx rate** | `rate(http_requests_total{status=~"5.."}[1m]) > 0` during an on-sale | Declines are 4xx by design, so any 5xx is a bug or an outage |
| **Not ready** | `/readyz` failing for more than 1 min | DB unreachable means we're not selling |
| **Latency** | p99 of `http_request_duration_seconds{route=".../reserve"}` > 2s | Lock queues or pool saturation (`db_pool_connections{state="idle"} == 0`) |

**Dashboards, not pages:**
- `reservations_confirmed_total` vs `reservations_declined_total` by reason. A spike in
  `idempotency_key_mismatch` points at a client bug.
- `seats{status}` over time during an on-sale.

**Logs:** every line has a `request_id`. A user complaint ("I was charged twice") becomes:
grep their `user_id`, check that the `reservation_id` is the same on both lines and that
the second `outcome` is `idempotent_replay`.

## 6. AI usage

> **Sakshi — rewrite this section in your own words.** It's graded for honesty and you'll be
> asked about it. Below is a factual record of what happened; edit it to reflect what you decided.

I built this with Claude (Anthropic) in an agentic coding session, working step by step:
schema, then reserve and idempotency, then observability, then the burst, then deploy.

- **Decided by me** when Claude presented options: Python/FastAPI + Postgres; Render;
  all-or-nothing partial requests; confirm + cancel rather than TTL holds; 200 (not 201)
  for an idempotent replay; signed demo tokens for identity.
- **Proposed by Claude, which I reviewed:**
  - the lock order (key, then usage row, then sorted seats)
  - the conditional-UPDATE per-user limit
  - user-scoped idempotency keys with a deferred FK
  - the lock-free "no-only" pre-check
  - the scrape-time DB gauge
  - the burst script's scenarios
- **Verified rather than trusted:** a DB-level storm harness (parallel `psql`) before any
  HTTP code existed; the 20k burst with the pre-check both on and off; Postgres stopped
  mid-run to check fail-closed behaviour.
- **What the AI got wrong and the tests caught:**
  - the burst script initially miscounted hot-seat winners when a retry raced ahead of its
    original request; it now counts per buyer (idempotency key);
  - DB connection errors were surfacing as 500s; they are now 503 `db_unavailable`;
  - an exception class name that doesn't exist in asyncpg.

## 7. What I'd do next

- Time-boxed holds with a confirm step and payment-provider callbacks, as sketched in §3.
- A periodic reconciliation job that recomputes `user_show_usage` from `reservations`, and
  a double-sell detector exported as a metric.
- Expire idempotency keys (for example after 24h, with a TTL index or partition drop).
- Per-user and per-IP rate limiting at the edge so one client can't monopolise the DB pool.
- PgBouncer, plus multi-instance metrics aggregation (counters are per-process, which is
  why there is one worker per container).
- A proper identity provider (OIDC) instead of the demo token mint.
