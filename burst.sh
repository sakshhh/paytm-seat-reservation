#!/usr/bin/env bash
# One-command on-sale stampede:  ADMIN_KEY=... ./burst.sh https://<your-app>.onrender.com [--requests 20000]
set -euo pipefail
BASE_URL=${1:?usage: ADMIN_KEY=... ./burst.sh <BASE_URL> [burst.py options]}
shift
exec python3 "$(dirname "$0")/scripts/burst.py" "$BASE_URL" "$@"
