"""Structured JSON logs with a per-request correlation id, plus Prometheus metrics."""
import contextvars
import json
import logging
import sys
import time
import uuid

from prometheus_client import Counter, Gauge, Histogram

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


# ------------------------------------------------------------------ logs ----
class JsonFormatter(logging.Formatter):
    RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "color_message"}

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for k, v in record.__dict__.items():
            if k not in self.RESERVED:
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # route uvicorn's server logs through the JSON handler too
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # uvicorn's own access log is replaced by ours (which carries request_id)
    logging.getLogger("uvicorn.access").disabled = True


def new_request_id(incoming: str | None) -> str:
    if incoming and 0 < len(incoming) <= 128 and incoming.isprintable():
        return incoming
    return uuid.uuid4().hex


# --------------------------------------------------------------- metrics ----
RESERVATIONS_CONFIRMED = Counter(
    "reservations_confirmed_total", "Reservations newly confirmed (201)")
RESERVATIONS_DECLINED = Counter(
    "reservations_declined_total",
    "Reserve requests that did not create a reservation, by reason "
    "(seat_taken, per_user_limit, idempotent_replay, idempotency_key_mismatch, ...)",
    ["reason"])
SEATS_CONFIRMED = Counter(
    "seats_confirmed_total", "Seats moved available -> confirmed")
RESERVATIONS_CANCELLED = Counter(
    "reservations_cancelled_total", "Reservations cancelled by their owner")
SEATS_RELEASED = Counter(
    "seats_released_total", "Seats moved confirmed -> available by a cancel")

# Read from the database at scrape time, so it is the system of record, not a
# process-local estimate: it reconciles with GET /shows/{id} by construction.
SEATS = Gauge("seats", "Seats per show by status (from DB at scrape time)", ["show_id", "status"])
SEATS_TOTAL = Gauge("seats_total", "Total seats per show", ["show_id"])
SEATS_RECONCILED = Gauge(
    "seats_reconciled", "1 if available+held+confirmed == total_seats for the show", ["show_id"])

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "Request latency", ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30))
DB_POOL_SIZE = Gauge("db_pool_connections", "DB pool connections", ["state"])
