#!/usr/bin/env bash
# Stop the relay on this host and keep it stopped, reboots included, until
# scripts/start.sh. For a relay that only runs on show days (the adler
# stream-copy fallback).
#
#   scripts/stop.sh            refuses while ingest is live
#   scripts/stop.sh --force    stops anyway, ending any broadcast through it
#
# Works on the containers by Compose project name, not from a compose file,
# so it stops whichever layout is running (the 2026-09-23 two-image relay or
# the current one) without recreating anything. The containers are kept;
# start.sh brings the same, tested containers back.
#
# Docker restarts a `restart: always` container when the daemon starts even
# if it was stopped by hand, so the policy is set to `no` before stopping
# and back to `always` by start.sh. Windows equivalent:
# scripts\install-service.ps1 -Stop.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${COMPOSE_PROJECT_NAME:-homelab-relay}"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

ids="$(docker ps -aq --filter "label=com.docker.compose.project=${PROJECT}")"
if [[ -z "${ids}" ]]; then
  echo "No containers in Compose project ${PROJECT}; nothing to stop." >&2
  exit 1
fi

# The status page knows whether OBS is publishing. Unreachable (already
# stopped, or starting) is not a reason to refuse.
port="$(sed -n 's/^SERVICE_PORT=//p' "${ROOT_DIR}/.env" 2>/dev/null | tail -1)"
state="$(curl -fsS -m 3 "http://127.0.0.1:${port:-20040}/api/status" 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["ingest"]["state"])' 2>/dev/null || echo unknown)"
if [[ "${state}" == "live" && "${FORCE}" -ne 1 ]]; then
  echo "Ingest is live on ${PROJECT}: stopping would end the broadcast." >&2
  echo "Re-run with --force if that is what you want." >&2
  exit 1
fi

echo "== homelab-relay stop (${PROJECT}, ingest: ${state}): START"
# shellcheck disable=SC2086
docker update --restart=no ${ids} >/dev/null
# shellcheck disable=SC2086
docker stop ${ids} >/dev/null
echo "== homelab-relay stop: END"
docker ps -a --filter "label=com.docker.compose.project=${PROJECT}" \
  --format 'table {{.Label "com.docker.compose.service"}}\t{{.Status}}'
