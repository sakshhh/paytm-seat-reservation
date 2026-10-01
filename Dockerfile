FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=8000
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
COPY burst.sh .

RUN useradd --system --uid 10001 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=2)"

# One worker per container: Prometheus counters are per-process, so one
# process keeps /metrics exact. Scale out with more containers, not workers.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log --proxy-headers --forwarded-allow-ips='*' --backlog 4096 --timeout-keep-alive 30"]
