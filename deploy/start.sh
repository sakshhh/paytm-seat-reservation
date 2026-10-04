#!/usr/bin/env bash
# (Re)start the production stack on an EC2 host. Safe to run repeatedly; runs at every boot
# via seat-reservation.service. Pulls the latest code, works out the public hostname,
# creates secrets on first run, and brings the stack up.
set -euo pipefail
cd "$(dirname "$0")"

git -C .. pull --ff-only || echo "git pull failed; continuing with current checkout"

# --- secrets: generated once, kept in deploy/.env (never committed) --------------------
if [ ! -f .env ]; then
  umask 077
  cat > .env <<EOF
DB_PASSWORD=$(openssl rand -hex 16)
JWT_SECRET=$(openssl rand -hex 32)
ADMIN_KEY=${ADMIN_KEY:-$(openssl rand -hex 16)}
EOF
fi

# --- public hostname: <ip-with-dashes>.sslip.io resolves to the instance's own IP -------
TOKEN=$(curl -sf --max-time 2 -X PUT http://169.254.169.254/latest/api/token \
          -H "X-aws-ec2-metadata-token-ttl-seconds: 300" || true)
IP=$(curl -sf --max-time 2 -H "X-aws-ec2-metadata-token: $TOKEN" \
          http://169.254.169.254/latest/meta-data/public-ipv4 || true)
if [ -n "${SITE_ADDRESS:-}" ]; then
  :                                   # explicit override, e.g. SITE_ADDRESS=:80
elif [[ "$IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  SITE_ADDRESS="${IP//./-}.sslip.io"
else
  SITE_ADDRESS=":80"
fi
grep -v '^SITE_ADDRESS=' .env > .env.tmp || true
echo "SITE_ADDRESS=$SITE_ADDRESS" >> .env.tmp
mv .env.tmp .env

docker compose -f docker-compose.prod.yml --env-file .env up -d --build --remove-orphans
echo "service URL: https://${SITE_ADDRESS}   (admin key: sudo grep ADMIN_KEY $(pwd)/.env)"
