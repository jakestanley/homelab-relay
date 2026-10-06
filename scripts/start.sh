#!/usr/bin/env bash
# Start a relay that scripts/stop.sh stopped: the same containers, with
# `restart: always` restored. Changes nothing about what runs; to change the
# image or configuration, use scripts/up.sh. Windows equivalent:
# scripts\up.ps1 (it starts any stopped service).
set -euo pipefail

PROJECT="${COMPOSE_PROJECT_NAME:-homelab-relay}"

ids="$(docker ps -aq --filter "label=com.docker.compose.project=${PROJECT}")"
if [[ -z "${ids}" ]]; then
  echo "No containers in Compose project ${PROJECT}. Deploy with scripts/up.sh first." >&2
  exit 1
fi

echo "== homelab-relay start (${PROJECT}): START"
# shellcheck disable=SC2086
docker update --restart=always ${ids} >/dev/null
# MediaMTX first, so the outputs find ingest's API when they start polling.
mtx="$(docker ps -aq --filter "label=com.docker.compose.project=${PROJECT}" \
  --filter "label=com.docker.compose.service=mediamtx")"
[[ -n "${mtx}" ]] && docker start ${mtx} >/dev/null
# shellcheck disable=SC2086
docker start ${ids} >/dev/null
echo "== homelab-relay start: END"
docker ps -a --filter "label=com.docker.compose.project=${PROJECT}" \
  --format 'table {{.Label "com.docker.compose.service"}}\t{{.Status}}'
