#!/usr/bin/env python3
"""On-sale stampede against a live deployment. Standard library only.

    ADMIN_KEY=... python3 scripts/burst.py https://your-app.onrender.com [--requests 20000]

Fires, all at once and interleaved:
  * hot-seat storm   : --hot-seats seats, each raced by --storm distinct users
  * duplicate retries: ~10% of storm requests re-sent concurrently with the same key
  * key reuse        : same key, different seats -> must be 409
  * greedy users     : each fires 10 parallel reserves on a limit-4 show
  * general crowd    : random users, random 1-2 seat requests (lots of collisions)
  * spoofing         : body says user_id=<victim>; reservation must belong to token user
and polls GET /shows/{id} during the burst to check the reconciliation invariant.
Afterwards: cancel/rebook checks, cross-owner cancel check, and metrics reconciliation.
Exit code is non-zero if any correctness check fails.
"""
import argparse
import collections
import http.client
import json
import os
import random
import ssl
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse


# ----------------------------------------------------------------- http -----
class Client:
    """One keep-alive connection per thread."""

    def __init__(self, base: str, timeout: float):
        u = urlparse(base)
        self.https = u.scheme == "https"
        self.host = u.netloc
        self.prefix = u.path.rstrip("/")
        self.timeout = timeout
        self.local = threading.local()
        self.ctx = ssl.create_default_context()

    def _conn(self):
        c = getattr(self.local, "conn", None)
        if c is None:
            c = (http.client.HTTPSConnection(self.host, timeout=self.timeout, context=self.ctx)
                 if self.https else http.client.HTTPConnection(self.host, timeout=self.timeout))
            self.local.conn = c
        return c

    def req(self, method, path, body=None, headers=None):
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        data = json.dumps(body) if body is not None else None
        for attempt in range(2):  # one reconnect on a dropped keep-alive socket
            c = self._conn()
            try:
                c.request(method, self.prefix + path, body=data, headers=hdrs)
                r = c.getresponse()
                raw = r.read()
                try:
                    payload = json.loads(raw) if raw else None
                except ValueError:
                    payload = raw.decode(errors="replace")
                return r.status, payload, dict(r.getheaders())
            except (http.client.HTTPException, OSError) as e:
                c.close()
                self.local.conn = None
                if attempt == 1:
                    return 0, {"error": f"transport: {e!r}"}, {}


# ---------------------------------------------------------------- burst -----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("--requests", type=int, default=20000, help="approx total reserve calls")
    ap.add_argument("--concurrency", type=int, default=200)
    ap.add_argument("--rows", default="ABCDEFGHIJ")
    ap.add_argument("--per-row", type=int, default=50)
    ap.add_argument("--hot-seats", type=int, default=5)
    ap.add_argument("--storm", type=int, default=500, help="users racing each hot seat")
    ap.add_argument("--greedy", type=int, default=20, help="users firing 10 parallel reserves each")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--admin-key", default=os.getenv("ADMIN_KEY", "dev-admin-key"))
    a = ap.parse_args()

    cli = Client(a.base_url, a.timeout)
    pool = ThreadPoolExecutor(max_workers=a.concurrency)
    failures: list[str] = []

    def check(ok: bool, what: str):
        print(f"  [{'PASS' if ok else 'FAIL'}] {what}")
        if not ok:
            failures.append(what)

    # --- readiness ----------------------------------------------------------
    print(f"target: {a.base_url}")
    t0 = time.time()
    while True:
        st, body, _ = cli.req("GET", "/readyz")
        if st == 200:
            break
        if time.time() - t0 > 180:
            sys.exit(f"service not ready after 180s: {st} {body}")
        print(f"  waiting for /readyz ({st}) ...")
        time.sleep(3)
    print(f"ready in {time.time() - t0:.1f}s")

    # --- show ---------------------------------------------------------------
    seats = [f"{r}{n}" for r in a.rows for n in range(1, a.per_row + 1)]
    st, show, _ = cli.req("POST", "/shows", {"name": f"burst-{int(time.time())}", "seats": seats,
                                            "price_paise": 25000, "per_user_limit": 4},
                          {"X-Admin-Key": a.admin_key})
    if st != 201:
        sys.exit(f"create show failed: {st} {show}")
    sid, limit, price = show["id"], show["per_user_limit"], show["price_paise"]
    print(f"show {sid}: {len(seats)} seats, limit {limit}, price {price} paise")
    hot = seats[len(seats) // 2: len(seats) // 2 + a.hot_seats]  # the "good" middle seats

    def metrics_snapshot():
        st, text, _ = cli.req("GET", "/metrics")
        out = {}
        if st == 200 and isinstance(text, str):
            for line in text.splitlines():
                if line and not line.startswith("#"):
                    k, _, v = line.rpartition(" ")
                    out[k] = float(v)
        return out

    m0 = metrics_snapshot()

    # --- users & tokens -----------------------------------------------------
    run = uuid.uuid4().hex[:6]
    n_crowd = max(0, a.requests - a.hot_seats * a.storm * 11 // 10 - a.greedy * 10 - 50)
    n_users = a.hot_seats * a.storm + a.greedy + max(1, n_crowd // 3) + 20
    names = [f"u{run}-{i}" for i in range(n_users)]
    print(f"minting {n_users} tokens ...")
    tokens = dict(zip(names, pool.map(
        lambda u: cli.req("POST", "/auth/token", {"user_id": u})[1]["token"], names)))
    auth = lambda u: {"Authorization": f"Bearer {tokens[u]}"}

    # --- build the workload -------------------------------------------------
    jobs = []  # (kind, user, seats, key, extra_body)
    storm_users = iter(names[: a.hot_seats * a.storm])
    for seat in hot:
        for _ in range(a.storm):
            u = next(storm_users)
            k = uuid.uuid4().hex
            jobs.append(("storm", u, [seat], k, {}))
            if random.random() < 0.10:
                jobs.append(("storm-retry", u, [seat], k, {}))
    greedy_users = names[a.hot_seats * a.storm: a.hot_seats * a.storm + a.greedy]
    cold = [s for s in seats if s not in hot]
    random.shuffle(cold)
    if len(cold) < a.greedy * 10 + 20:
        sys.exit("not enough seats for the greedy scenario; raise --per-row")
    greedy_pool = iter(cold[: a.greedy * 10])  # reserved for greedy users only
    open_seats = cold[a.greedy * 10:]           # crowd + spoofers fight over these
    for u in greedy_users:
        for _ in range(10):
            jobs.append(("greedy", u, [next(greedy_pool)], uuid.uuid4().hex, {}))
    crowd = names[a.hot_seats * a.storm + a.greedy: -20]
    for _ in range(n_crowd):
        u = random.choice(crowd)
        want = random.sample(open_seats, random.choice((1, 1, 1, 2)))
        jobs.append(("crowd", u, want, uuid.uuid4().hex, {}))
    spoofers, victims = names[-20:-10], names[-10:]
    for s, v in zip(spoofers, victims):
        jobs.append(("spoof", s, [random.choice(open_seats)], uuid.uuid4().hex, {"user_id": v}))
    random.shuffle(jobs)

    results = []  # (kind, user, seats, key, status, body)
    lock = threading.Lock()

    def fire(job):
        kind, u, want, key, extra = job
        st, body, _ = cli.req("POST", f"/shows/{sid}/reserve",
                              {"seats": want, "idempotency_key": key, **extra}, auth(u))
        with lock:
            results.append((kind, u, want, key, st, body))

    # invariant poller runs throughout the burst
    stop = threading.Event()
    polls = []

    def poller():
        pc = Client(a.base_url, a.timeout)
        while not stop.is_set():
            st, s, _ = pc.req("GET", f"/shows/{sid}")
            if st == 200:
                c = s["counts"]
                polls.append(c["available"] + c["held"] + c["confirmed"] == s["total_seats"])
            time.sleep(0.25)

    pt = threading.Thread(target=poller, daemon=True)
    pt.start()
    print(f"firing {len(jobs)} reserve requests at concurrency {a.concurrency} ...")
    t1 = time.time()
    list(pool.map(fire, jobs))
    # same key, different body: must be rejected
    for kind, u, want, key, st, body in list(results)[:30]:
        if st == 201:
            other = [s for s in seats if s not in want][:1]
            st2, b2, _ = cli.req("POST", f"/shows/{sid}/reserve",
                                 {"seats": other, "idempotency_key": key}, auth(u))
            results.append(("key-reuse", u, other, key, st2, b2))
    dur = time.time() - t1
    stop.set()
    pt.join()

    # --- report -------------------------------------------------------------
    print(f"\ndone in {dur:.1f}s  ({len(results) / dur:.0f} req/s)\n")
    dist = collections.Counter()
    for kind, u, want, key, st, body in results:
        reason = body.get("error") if isinstance(body, dict) and st >= 300 else None
        label = {201: "confirmed", 200: "idempotent_replay"}.get(st, f"{st} {reason}")
        dist[(kind, label)] += 1
    print(f"{'kind':<12} {'outcome':<36} {'count':>7}")
    for (kind, label), n in sorted(dist.items()):
        print(f"{kind:<12} {label:<36} {n:>7}")
    totals = collections.Counter()
    for (_, label), n in dist.items():
        totals[label] += n
    print("\nall:", dict(totals))

    print("\nchecks")
    n5xx = sum(1 for r in results if r[4] >= 500 or r[4] == 0)
    check(n5xx == 0, f"zero 5xx / transport errors (got {n5xx})")

    winners = collections.defaultdict(set)
    for kind, u, want, key, st, body in results:
        if st == 201:
            for s in body["seats"]:
                winners[s].add(body["reservation_id"])
    check(all(len(v) == 1 for v in winners.values()),
          "no seat confirmed to two reservations (from API responses)")
    for seat in hot:
        rs = [r for r in results if r[0] in ("storm", "storm-retry") and r[2] == [seat]]
        n201 = sum(1 for r in rs if r[4] == 201)
        win_keys = {r[3] for r in rs if r[4] == 201}
        losers_ok = all(r[4] == 409 for r in rs if r[3] not in win_keys)
        replays_ok = all(r[4] in (200, 201) for r in rs if r[3] in win_keys)
        check(n201 == 1 and losers_ok and replays_ok,
              f"hot seat {seat}: {len({r[3] for r in rs})} buyers -> exactly one 201, every other "
              f"buyer 409, winner's retries 200 ({n201} x 201, "
              f"{sum(1 for r in rs if r[4] == 409)} x 409, {sum(1 for r in rs if r[4] == 200)} x 200)")

    by_key = collections.defaultdict(list)
    for r in results:
        if r[0] in ("storm", "storm-retry"):
            by_key[r[3]].append(r)
    retry_ok = True
    for rs in by_key.values():
        ids = {r[5]["reservation_id"] for r in rs if r[4] in (200, 201)}
        if len(ids) > 1 or sum(1 for r in rs if r[4] == 201) > 1:
            retry_ok = False
    check(retry_ok, "same idempotency key never produces two reservations")
    reuse = [r for r in results if r[0] == "key-reuse"]
    check(all(r[4] == 409 for r in reuse), f"same key + different seats -> 409 ({len(reuse)} tried)")

    st, final, _ = cli.req("GET", f"/shows/{sid}")
    c = final["counts"]
    check(c["available"] + c["held"] + c["confirmed"] == final["total_seats"],
          f"reconciliation after burst: {c} total={final['total_seats']}")
    check(all(polls) and len(polls) > 0, f"reconciliation during burst ({len(polls)} polls)")
    confirmed_api = {s for s, ids in winners.items()}
    confirmed_db = {s["seat"] for s in final["seats"] if s["status"] == "confirmed"}
    check(confirmed_api == confirmed_db,
          f"seats confirmed in DB == seats from 201 responses ({len(confirmed_db)})")

    held = collections.Counter()
    for kind, u, want, key, st, body in results:
        if st == 201:
            held[u] += len(body["seats"])
    check(max(held.values(), default=0) <= limit, f"no user above limit {limit} (max {max(held.values(), default=0)})")
    g = [held[u] for u in greedy_users]
    check(all(x == limit for x in g), f"greedy users (10 parallel each) ended with exactly {limit}: {sorted(set(g))}")

    spoof = [r for r in results if r[0] == "spoof" and r[4] == 201]
    check(all(r[5]["user_id"] == r[1] for r in spoof),
          f"spoofed body user_id ignored; reservation owned by token user ({len(spoof)} booked)")
    check(all(r[5]["amount_paise"] == price * len(r[5]["seats"]) and isinstance(r[5]["amount_paise"], int)
              for r in results if r[4] == 201), "amount_paise == price x seats, integer")

    # --- cancel / rebook / ownership ----------------------------------------
    print("\ncancel & rebook")
    win = next(r for r in results if r[0] in ("storm", "storm-retry") and r[4] == 201)
    owner, rid, seat = win[1], win[5]["reservation_id"], win[2][0]
    intruder = names[-1]
    st, b, _ = cli.req("POST", f"/reservations/{rid}/cancel", None, auth(intruder))
    check(st in (403, 404), f"another user cannot cancel ({st} {b.get('error') if isinstance(b, dict) else b})")
    st, b, _ = cli.req("POST", f"/reservations/{rid}/cancel", None, auth(owner))
    check(st == 200 and b["status"] == "cancelled", f"owner cancels ({st})")
    st, b, _ = cli.req("POST", f"/reservations/{rid}/cancel", None, auth(owner))
    check(st == 200, "second cancel is a harmless no-op")
    st, b, _ = cli.req("POST", f"/shows/{sid}/reserve",
                       {"seats": [seat], "idempotency_key": win[3]}, auth(owner))
    check(st == 200 and b["status"] == "cancelled",
          "retrying the original key after cancel replays (does not silently re-book)")
    # race the freed seat again
    rebook = list(pool.map(lambda u: cli.req(
        "POST", f"/shows/{sid}/reserve", {"seats": [seat], "idempotency_key": uuid.uuid4().hex},
        auth(u))[0], names[-10:]))
    check(rebook.count(201) == 1 and rebook.count(409) == 9,
          f"freed seat {seat} is re-raced: exactly one winner {collections.Counter(rebook)}")
    st, b, _ = cli.req("POST", f"/reservations/{rid}/cancel", None, auth(owner))
    st, final, _ = cli.req("GET", f"/shows/{sid}")
    seat_state = next(s["status"] for s in final["seats"] if s["seat"] == seat)
    check(seat_state == "confirmed", "a stale cancel never resurrects a seat now owned by someone else")

    # --- metrics reconciliation ---------------------------------------------
    print("\nmetrics")
    m1 = metrics_snapshot()
    d = lambda k: m1.get(k, 0) - m0.get(k, 0)
    n201 = sum(1 for r in results if r[4] == 201) + rebook.count(201)
    print(f"  reservations_confirmed_total +{d('reservations_confirmed_total'):.0f}, "
          f"201s observed {n201}")
    for reason in ("seat_taken", "per_user_limit", "idempotent_replay", "idempotency_key_mismatch"):
        key = 'reservations_declined_total{reason="%s"}' % reason
        print("  declined{%s} +%.0f" % (reason, d(key)))
    gauge = {s: m1.get('seats{show_id="%s",status="%s"}' % (sid, s)) for s in ("available", "held", "confirmed")}
    c = final["counts"]
    check(gauge == {k: float(v) for k, v in c.items()}, f"seats gauge == GET /shows counts {gauge}")
    if d("reservations_confirmed_total") != n201:
        print("  (counter delta differs from 201s: expected if other traffic hit the service, "
              "or if it runs >1 instance — counters are per-process)")

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} CHECK(S) FAILED'}  show={sid}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
