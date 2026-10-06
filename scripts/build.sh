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
#   run the acceptance checks against that tag (TESTING.md)
#   set RELAY_IMAGE_TAG=2026-10-01 in .env, then scripts/up.sh
#
# One image holds every process, MediaMTX included (see Dockerfile).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-$(date -u +%F)}"

if [[ ! "${TAG}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "Invalid tag: ${TAG}" >&2
  exit 1
fi

image="homelab-relay:${TAG}"
if docker image inspect "${image}" >/dev/null 2>&1; then
  echo "${image} already exists. Tags are never overwritten: pass a new one," >&2
  echo "e.g. scripts/build.sh ${TAG}-2" >&2
  exit 1
fi

revision="$(git -C "${ROOT_DIR}" rev-parse HEAD 2>/dev/null || echo unknown)"
if [[ -n "$(git -C "${ROOT_DIR}" status --porcelain 2>/dev/null)" ]]; then
  echo "WARNING: working tree has uncommitted changes; labelling as ${revision}-dirty." >&2
  revision="${revision}-dirty"
fi

echo "== build ${TAG} (revision ${revision}): START"
docker build --label "org.opencontainers.image.revision=${revision}" \
  -t "${image}" "${ROOT_DIR}"

echo "== build ${TAG}: END"
echo "ffmpeg:   $(docker run --rm "${image}" ffmpeg -version | head -1)"
echo "mediamtx: $(docker run --rm "${image}" mediamtx --version 2>&1 | head -1)"
echo "encoders: $(docker run --rm "${image}" ffmpeg -hide_banner -encoders 2>/dev/null | awk '$2 ~ /^(libx264|h264_nvenc)$/ {print $2}' | tr '\n' ' ')"
echo
echo "Next: the acceptance checks against ${TAG}, then set RELAY_IMAGE_TAG=${TAG} in .env and run scripts/up.sh."
