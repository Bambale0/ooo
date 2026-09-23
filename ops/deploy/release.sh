#!/usr/bin/env bash
# Run from the checked-out release directory. .env and nginx/ssl are provisioned separately.
set -euo pipefail
: "${NEIRONYCH_IMAGE:?required}"
: "${APP_REVISION:?required}"
PREVIOUS_IMAGE="${PREVIOUS_IMAGE:-}"
PREVIOUS_REVISION="${PREVIOUS_REVISION:-}"
[[ -f .backup.env ]] || { echo "Configure encrypted off-site backups in .backup.env before release" >&2; exit 1; }
compose=(docker compose -f docker-compose.prod.yml -f docker-compose.pitr.yml)
"${compose[@]}" config -q
"${compose[@]}" pull app worker webhook_worker
"${compose[@]}" up -d postgres redis
"${compose[@]}" run --rm --no-deps app alembic upgrade head
"${compose[@]}" up -d app worker webhook_worker
for attempt in {1..12}; do
  if "${compose[@]}" exec -T app python -c 'import json,os,urllib.request; r=json.load(urllib.request.urlopen("http://localhost:8000/api/v1/readiness",timeout=4)); assert r["revision"] == os.environ["APP_REVISION"] and r["status"] == "ready"'; then
    printf '%s\n' "$NEIRONYCH_IMAGE" > DEPLOYED_IMAGE
    printf '%s\n' "$APP_REVISION" > REVISION
    exit 0
  fi
  sleep 5
done
if [[ -n "$PREVIOUS_IMAGE" && -n "$PREVIOUS_REVISION" ]]; then
  export NEIRONYCH_IMAGE="$PREVIOUS_IMAGE" APP_REVISION="$PREVIOUS_REVISION"
  "${compose[@]}" up -d app worker webhook_worker
fi
# Schema is intentionally not downgraded: releases must use additive migrations.
echo 'Release failed readiness; inspect services and validate the previous revision.' >&2
exit 1
