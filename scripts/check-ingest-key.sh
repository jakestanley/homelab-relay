#!/usr/bin/env bash
# Reads an ingest key on stdin (never argv, so it stays out of `ps`) and exits
# non-zero, naming the rule, if MediaMTX would refuse it.
#
# The key is the last segment of the publish path (live/<key>), and MediaMTX
# rejects any path containing characters other than letters, digits, "_", "."
# and "-" -- before it ever compares the key -- so OBS sees a refused
# connection. "/" is also allowed by MediaMTX but is excluded here: it would
# split the key into extra path segments. Base64 keys fail on "=" and "+".
#
# Generate a suitable key with:
#   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
set -euo pipefail

IFS= read -r key || true

if [[ -z "${key}" ]]; then
  echo "INGEST_KEY is empty." >&2
  exit 1
fi
if [[ ! "${key}" =~ ^[A-Za-z0-9._-]+$ ]]; then
  bad="$(printf '%s' "${key}" | tr -d 'A-Za-z0-9._-' | fold -w1 | sort -u | tr -d '\n')"
  echo "INGEST_KEY contains characters MediaMTX refuses in a path: '${bad}'." >&2
  echo "Use only letters, digits, '_', '.' and '-' (and the same value in batw's STREAM_KEY_LIVE)." >&2
  echo 'Generate one with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"' >&2
  exit 1
fi
