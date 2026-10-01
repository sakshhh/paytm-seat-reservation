-- DB-level proof harness (test-only).
-- Mirrors, statement for statement, the transaction in app/reservations.py so
-- the locking protocol can be stormed with parallel psql sessions without the
-- HTTP layer. Lock order everywhere: idempotency key -> user usage row -> seats (sorted).
CREATE OR REPLACE FUNCTION test_reserve(p_show UUID, p_user TEXT, p_key TEXT,
                                        p_hash TEXT, p_labels TEXT[])
RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE
  v_res   UUID := gen_random_uuid();
  v_n     INT  := cardinality(p_labels);
  v_limit INT;
  v_price BIGINT;
  v_row   RECORD;
  v_locked INT;
BEGIN
  BEGIN
    -- 1. claim the idempotency key (blocks behind a concurrent same-key txn)
    INSERT INTO idempotency_keys(user_id, key, request_hash, reservation_id)
    VALUES (p_user, p_key, p_hash, v_res)
    ON CONFLICT (user_id, key) DO NOTHING;
    IF NOT FOUND THEN
      SELECT request_hash, reservation_id INTO v_row
        FROM idempotency_keys WHERE user_id = p_user AND key = p_key;
      IF v_row.request_hash = p_hash THEN RETURN 'replay';
      ELSE RETURN 'key_mismatch'; END IF;
    END IF;

    SELECT per_user_limit, price_paise INTO v_limit, v_price FROM shows WHERE id = p_show;

    -- 2. per-user limit: atomic conditional increment
    INSERT INTO user_show_usage(show_id, user_id, seat_count)
    VALUES (p_show, p_user, 0) ON CONFLICT DO NOTHING;
    UPDATE user_show_usage SET seat_count = seat_count + v_n
     WHERE show_id = p_show AND user_id = p_user AND seat_count + v_n <= v_limit;
    IF NOT FOUND THEN RAISE EXCEPTION 'per_user_limit'; END IF;

    -- 3. lock requested seats in deterministic order, then decide under the lock
    PERFORM 1 FROM seats WHERE show_id = p_show AND label = ANY(p_labels)
     ORDER BY label FOR UPDATE;
    GET DIAGNOSTICS v_locked = ROW_COUNT;
    IF v_locked <> v_n THEN RAISE EXCEPTION 'unknown_seat'; END IF;
    SELECT count(*) INTO v_locked FROM seats
     WHERE show_id = p_show AND label = ANY(p_labels) AND status = 'available';
    IF v_locked <> v_n THEN RAISE EXCEPTION 'seat_taken'; END IF;

    INSERT INTO reservations(id, show_id, user_id, seats, amount_paise, status)
    VALUES (v_res, p_show, p_user, p_labels, v_price * v_n, 'confirmed');
    UPDATE seats SET status = 'confirmed', reservation_id = v_res
     WHERE show_id = p_show AND label = ANY(p_labels) AND status = 'available';
    RETURN 'confirmed';
  EXCEPTION WHEN raise_exception THEN
    RETURN SQLERRM;   -- sub-transaction rolled back: key, usage, seats all undone
  END;
END $$;
