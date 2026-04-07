#!/bin/bash
# One-time migration: copy data from Render PostgreSQL to VPS PostgreSQL.
#
# Usage (run from the meshi-archive directory on the VPS):
#   RENDER_DATABASE_URL="postgresql://user:pass@host/db" \
#   POSTGRES_USER=meshi POSTGRES_PASSWORD=... POSTGRES_DB=meshi_archive \
#   ./deploy/migrate_db.sh

set -euo pipefail

: "${RENDER_DATABASE_URL:?Set RENDER_DATABASE_URL to the Render connection string}"
: "${POSTGRES_USER:?Set POSTGRES_USER}"
: "${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD}"
: "${POSTGRES_DB:?Set POSTGRES_DB}"

echo "[1/3] Waiting for local DB to be ready..."
until docker compose exec db pg_isready -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; do
  sleep 2
done
echo "      → ready"

echo "[2/3] Dumping from Render and restoring to VPS DB..."
# pg_dump runs in a temporary container that has internet access.
# The dump is piped directly into psql inside the local db container.
docker run --rm \
  --network host \
  postgres:16-alpine \
  pg_dump "${RENDER_DATABASE_URL}" --no-acl --no-owner -F p \
  | docker compose exec -T db \
    psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}"

echo "[3/3] Migration complete. Verify:"
echo "      docker compose exec db psql -U ${POSTGRES_USER} -d ${POSTGRES_DB} -c 'SELECT COUNT(*) FROM shops;'"
