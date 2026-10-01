#!/usr/bin/env bash
# DB-level concurrency proof: storms the reserve transaction with parallel psql sessions.
# usage: PGURL=postgres://postgres@localhost/seats tests/db/storm.sh
set -euo pipefail
PGURL=${PGURL:-postgres://postgres@localhost/seats}
P=${P:-150}   # parallel sessions
q() { psql "$PGURL" -qtAX -c "$1"; }

psql "$PGURL" -qX -f "$(dirname "$0")/reserve_harness.sql"
SHOW=$(q "SELECT gen_random_uuid()")
q "INSERT INTO shows VALUES ('$SHOW','storm',25000,4,60)"
q "INSERT INTO seats(show_id,label) SELECT '$SHOW', r||n FROM unnest(ARRAY['A','B','C']) r, generate_series(1,20) n"
call() { q "SELECT test_reserve('$SHOW','$1','$2','$3','$4')"; }
export -f q call; export PGURL SHOW

echo "== 1. hot seat: 500 users race for A12"
seq 1 500 | xargs -P "$P" -I{} bash -c 'call u{} k{} h "{A12}"' | sort | uniq -c

echo "== 2. multi-seat, opposite orders (deadlock check): 300 users want {B1,B2} or {B2,B1}"
seq 1 300 | xargs -P "$P" -I{} bash -c 'if (( {} % 2 )); then s="{B1,B2}"; else s="{B2,B1}"; fi; call m{} k{} h "$s"' | sort | uniq -c

echo "== 3. per-user limit: one user, 10 parallel requests for different seats (limit 4)"
seq 1 10 | xargs -P 10 -I{} bash -c 'call greedy g{} h "{C{}}"' | sort | uniq -c

echo "== 4. idempotency: 50 parallel retries, same key + same body"
seq 1 50 | xargs -P 50 -I{} bash -c 'call retry same-key h1 "{C15}"' | sort | uniq -c
echo "   same key, different body:"; call retry same-key h2 "{C16}"

echo "== invariants"
q "SELECT 'seats confirmed to >1 reservation: ' || count(*) FROM (SELECT s.label FROM reservations r, unnest(r.seats) s(label) WHERE r.show_id='$SHOW' AND r.status='confirmed' GROUP BY s.label HAVING count(*)>1) x"
q "SELECT 'available+held+confirmed=' || sum(c) || ' total=' || max(t) || '  [' || string_agg(status||':'||c, ' ') || ']' FROM (SELECT status, count(*) c, (SELECT total_seats FROM shows WHERE id='$SHOW') t FROM seats WHERE show_id='$SHOW' GROUP BY status) x"
q "SELECT 'greedy holds: ' || seat_count FROM user_show_usage WHERE show_id='$SHOW' AND user_id='greedy'"
q "SELECT 'usage == confirmed seats: ' || ((SELECT sum(seat_count) FROM user_show_usage WHERE show_id='$SHOW') = (SELECT count(*) FROM seats WHERE show_id='$SHOW' AND status='confirmed'))"
