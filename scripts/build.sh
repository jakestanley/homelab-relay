#!/usr/bin/env bash
# Build the broadcast-path images once, under a tag that is never overwritten.
#
#   scripts/build.sh [TAG]      TAG defaults to today's UTC date, e.g. 2026-09-23
#
# This is the only place images are built. up.sh runs whatever RELAY_IMAGE_TAG
# in .env names and never builds, so the image that passed the acceptance
# checks is the image that runs on the night. A rebuild is a deliberate act:
#
#   scripts/build.sh 2026-10-01
#   test/acceptance.sh 2026-10-01          # the sink checks, against that tag
#   set RELAY_IMAGE_TAG=2026-10-01 in .env, then scripts/up.sh
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-$(date -u +%F)}"

if [[ ! "${TAG}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "Invalid tag: ${TAG}" >&2
  exit 1
fi

for image in "homelab-relay:${TAG}" "homelab-relay-mediamtx:${TAG}"; do
  if docker image inspect "${image}" >/dev/null 2>&1; then
    echo "${image} already exists. Tags are never overwritten: pass a new one," >&2
    echo "e.g. scripts/build.sh ${TAG}-2" >&2
    exit 1
  fi
done

revision="$(git -C "${ROOT_DIR}" rev-parse HEAD 2>/dev/null || echo unknown)"
if [[ -n "$(git -C "${ROOT_DIR}" status --porcelain 2>/dev/null)" ]]; then
  echo "WARNING: working tree has uncommitted changes; labelling as ${revision}-dirty." >&2
  revision="${revision}-dirty"
fi

echo "== build ${TAG} (revision ${revision}): START"
docker build --label "org.opencontainers.image.revision=${revision}" \
  -t "homelab-relay:${TAG}" "${ROOT_DIR}"
docker build --label "org.opencontainers.image.revision=${revision}" \
  -t "homelab-relay-mediamtx:${TAG}" "${ROOT_DIR}/mediamtx"

echo "== build ${TAG}: END"
echo "ffmpeg:   $(docker run --rm "homelab-relay:${TAG}" ffmpeg -version | head -1)"
echo "mediamtx: $(docker run --rm "homelab-relay-mediamtx:${TAG}" --version 2>&1 | head -1)"
echo
echo "Next: test/acceptance.sh ${TAG}, then set RELAY_IMAGE_TAG=${TAG} in .env and run scripts/up.sh."
