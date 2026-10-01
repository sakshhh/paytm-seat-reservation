# Seat Reservation at Scale

A JSON API that sells assigned seats under on-sale stampede load without ever
double-selling a seat, exceeding a per-user limit, or double-booking a retried request.

**Stack:** Python 3.12 · FastAPI · asyncpg (raw SQL, no ORM) · PostgreSQL 16 · Prometheus client
**Live URL:** `https://<your-service>.onrender.com` ← _fill in after deploy_
**Design & trade-offs:** [WRITEUP.md](WRITEUP.md)

---

## Run it

```bash
docker compose up --build                        # API on :8000, Postgres on :5432
ADMIN_KEY=dev-admin-key ./burst.sh http://localhost:8000
```

The image is the same one Render builds. Migrations run automatically on boot.

## One-command burst

```bash
ADMIN_KEY=<admin key> ./burst.sh <BASE_URL>                  # ~20,000 reserve calls
ADMIN_KEY=<admin key> ./burst.sh <BASE_URL> --requests 5000 --concurrency 100
```

The script needs Python 3.8+ and uses the standard library only. It creates a
fresh 500-seat show, then fires everything at once, interleaved:

| Scenario | What it checks |
|---|---|
| Hot-seat storm: 5 seats × 500 different users | Exactly one 201 per seat, every other buyer gets 409 |
| ~10% of storm requests re-sent concurrently with the same key | Never two reservations for one key; the winner's retry gets a 200 replay |
| Same key, different seats | 409 `idempotency_key_mismatch` |
| 20 greedy users × 10 parallel reserves (limit 4) | Each ends with exactly 4 |
| Crowd: random 1–2 seat requests | No seat ever ends up with two owners |
| Spoofed `user_id` in the body | Reservation belongs to the token's user |
| Background poller on `GET /shows/{id}` during the burst | `available + held + confirmed == total` at every poll |
| Cancel / re-race / stale cancel / cross-user cancel | Freed seat has exactly one new winner; a stale cancel never frees someone else's seat; only the owner can cancel |
| `/metrics` before vs after | Confirmed counter delta == 201s seen; seat gauge == API counts |

It prints the outcome distribution and the PASS/FAIL checks, and exits non-zero on any failure.
Sample output (local, 20k requests):

```
all: {'confirmed': 347, '409 seat_taken': 19493, '409 per_user_limit': 120, 'idempotent_replay': 4, '409 idempotency_key_mismatch': 26}
  [PASS] zero 5xx / transport errors (got 0)
  [PASS] hot seat F1: 500 buyers -> exactly one 201, every other buyer 409, winner's retries 200 (1 x 201, 552 x 409, 1 x 200)
  ...
  [PASS] reconciliation during burst (67 polls)
  [PASS] greedy users (10 parallel each) ended with exactly 4: [4]
ALL CHECKS PASSED
```

**Responses that never reached the service.** Every response the app sends carries
`X-Request-ID`. The burst script labels a response without it (for example a 429 or 502 from
Render's Cloudflare edge) as `edge`, and reports it separately from the service's own
outcomes, so a platform throttle can't be mistaken for an app 5xx. Pass `--retry-edge 5` to
retry those requests the way a real client would: with backoff and the **same idempotency
key**. The retry can never double-book, which is exactly what idempotency is for. The
`http_requests_total{status=...}` metric shows what the app itself returned.

On Render's free instance (~0.1 CPU), use a gentler burst:
`./burst.sh <URL> --requests 3000 --concurrency 50 --retry-edge 5`.

The DB-level storm (`tests/db/storm.sh`) runs the same lock protocol from parallel `psql`
sessions, without HTTP in between.

## API

All bodies are JSON. Money is integer paise; a float such as `250.0` is rejected.

| Method & path | Auth | Notes |
|---|---|---|
| `POST /auth/token` `{"user_id":"alice"}` | none | Demo identity provider: returns an HS256 JWT. Identity is read **only** from this token. |
| `POST /shows` `{"name","seats":[...],"price_paise",["per_user_limit"]}` | `X-Admin-Key` | 201 with every seat `available`. Default limit 4. |
| `GET /shows/{id}` | none | Status of each seat, `counts`, and `reconciled: true/false` |
| `POST /shows/{id}/reserve` `{"seats":[...],"idempotency_key":"..."}` | Bearer | The key can also go in the `Idempotency-Key` header. See outcomes below. |
| `POST /reservations/{id}/cancel` | Bearer (owner) | Frees the seats. A repeat cancel is a no-op 200. A non-owner gets 403. |
| `GET /reservations/{id}` | Bearer (owner) | 404 if the reservation isn't yours |
| `GET /healthz` | none | Liveness. Checks the process only. |
| `GET /readyz` | none | Readiness. Returns 503 unless the DB is reachable and migrated. |
| `GET /metrics` | none | Prometheus metrics |

**Reserve outcomes**

| Status | `error` | Meaning |
|---|---|---|
| 201 | — | Confirmed: `{reservation_id, show_id, user_id, seats, amount_paise, status:"confirmed"}` |
| 200 | — | Idempotent replay of the original reservation (header `Idempotent-Replayed: true`) |
| 409 | `seat_taken` | One or more seats are not available. **All-or-nothing**: nothing is booked. `seats` lists the culprits. |
| 409 | `per_user_limit` | The request would put you over the limit for this show |
| 409 | `idempotency_key_mismatch` | Key already used with different seats or a different show |
| 400 | `unknown_seat` / `invalid_request` / `idempotency_key_required` | Bad input |
| 401 / 404 | `unauthenticated` / `show_not_found` | — |
| 503 | `db_unavailable` | Database unreachable: fails closed, with `Retry-After` |

```bash
TOKEN=$(curl -s -XPOST $URL/auth/token -H 'content-type: application/json' -d '{"user_id":"alice"}' | jq -r .token)
SHOW=$(curl -s -XPOST $URL/shows -H "X-Admin-Key: $ADMIN_KEY" -H 'content-type: application/json' \
        -d '{"name":"friday-night","seats":["A1","A2","A3"],"price_paise":25000}' | jq -r .id)
curl -s -XPOST $URL/shows/$SHOW/reserve -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
     -d '{"seats":["A1"],"idempotency_key":"order-123"}'
```

## Observability

**Metrics** (`/metrics`):

- `reservations_confirmed_total`
- `reservations_declined_total{reason="seat_taken|per_user_limit|idempotent_replay|idempotency_key_mismatch|unknown_seat|…"}`
- `seats{show_id,status}`: a gauge **read from Postgres at scrape time**, so it always matches the API. It covers the 50 most recent shows.
- `seats_reconciled{show_id}`: 1 when the invariant holds
- `seats_confirmed_total`, `seats_released_total`, `reservations_cancelled_total`
- `http_requests_total{method,route,status}`, `http_request_duration_seconds`
- `db_pool_connections{state}`

**Logs** are one JSON object per line on stdout. Every line carries a `request_id`. The id
comes from the `X-Request-ID` header if you send one, and is echoed back in the response.
Reserve log lines also carry `user_id`, `show_id`, `seats`, `outcome` and `reservation_id`.
On Render, open *Logs* on the service; the response's `X-Request-ID` lets you grep a single
request.

## Deploy (Render)

1. Push this repo to GitHub.
2. In Render, choose **New → Blueprint** and select the repo. `render.yaml` creates the web
   service (Docker) and a Postgres database, and generates `JWT_SECRET` and `ADMIN_KEY`.
3. When the service shows *Live* (its health check is `/readyz`, so it only goes live once
   the DB is ready), copy `ADMIN_KEY` from the service's *Environment* tab.
4. `ADMIN_KEY=... ./burst.sh https://<service>.onrender.com`

`plan: starter` is recommended. The free plan works, but it has about 0.1 CPU and spins down
when idle, so a 20k burst against it is slow and the first request after idle waits for a
cold start.

## Configuration

| Env | Default | |
|---|---|---|
| `DATABASE_URL` | `postgresql://postgres@localhost:5432/seats` | |
| `JWT_SECRET`, `ADMIN_KEY` | dev values | **set in prod** |
| `DB_POOL_MAX` | 20 | connections per instance |
| `DB_ACQUIRE_TIMEOUT` | 60 s | how long a request can queue for a connection before a 503 |
| `DEFAULT_PER_USER_LIMIT` | 4 | |
| `DISABLE_PRECHECK` | 0 | test switch: forces every request through the locked path |

## Layout

```
app/reservations.py   the atomic decision (reserve / cancel): read this first
app/main.py           routes, error mapping, health, metrics endpoint
app/observability.py  JSON logging + metric definitions
migrations/           schema + constraints
scripts/burst.py      the stampede (burst.sh wraps it)
tests/db/             DB-level lock-protocol storm via parallel psql
```
