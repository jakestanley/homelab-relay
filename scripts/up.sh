#!/usr/bin/env bash
# Canonical entrypoint: (re)start the relay from the images named by
# RELAY_IMAGE_TAG in .env. It never builds (see scripts/build.sh), so it cannot
# change the image under a running relay, and re-running it against a running
# service recreates only containers whose .env configuration changed.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STANDARDS_DIR="${ROOT_DIR}/../homelab-standards"
INFRA_DIR="${ROOT_DIR}/../homelab-infra"
ENV_FILE="${ROOT_DIR}/.env"

echo "== homelab-relay up: START"

confirm() {
  local prompt="$1" answer=""
  if [[ -t 0 ]]; then
    read -r -p "${prompt}" answer || true
  else
    echo "${prompt}" >&2
    echo "Non-interactive session; refusing by default." >&2
    return 1
  fi
  [[ "${answer}" =~ ^([yY]|[yY][eE][sS])$ ]]
}

# Preflight (homelab-standards AGENTS.md): sibling repos should be git repos,
# clean, and on their default branch. Warn and ask; never pull or reset.
# A deploy from an ephemeral clone may have no siblings at all; that only
# warns, since nothing here reads them at runtime.
preflight_failed=0
check_repo() {
  local dir="$1" label="$2"
  if [[ ! -d "${dir}/.git" ]]; then
    echo "WARNING: ${label} not found at ${dir}; skipping its preflight check." >&2
    return
  fi
  if [[ -n "$(git -C "${dir}" status --porcelain)" ]]; then
    echo "WARNING: ${label} has uncommitted changes." >&2
    preflight_failed=1
  fi
  local branch default
  branch="$(git -C "${dir}" rev-parse --abbrev-ref HEAD)"
  default="$(git -C "${dir}" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/main)"
  if [[ "${branch}" != "${default#origin/}" ]]; then
    echo "WARNING: ${label} is on '${branch}', not '${default#origin/}'." >&2
    preflight_failed=1
  elif [[ "$(git -C "${dir}" rev-parse HEAD)" != "$(git -C "${dir}" rev-parse "${default}" 2>/dev/null)" ]]; then
    echo "WARNING: ${label} is not at ${default} (as last fetched)." >&2
    preflight_failed=1
  fi
}
check_repo "${STANDARDS_DIR}" "homelab-standards"
check_repo "${INFRA_DIR}" "homelab-infra"
if [[ "${preflight_failed}" -ne 0 ]] && ! confirm "Preflight checks failed. Continue anyway? [y/N] "; then
  exit 1
fi

# Vendored agent docs (imported/, not committed). Best effort.
if [[ -f "${STANDARDS_DIR}/scripts/sync_imports.py" ]]; then
  python3 "${STANDARDS_DIR}/scripts/sync_imports.py" "${ROOT_DIR}" \
    || echo "WARNING: syncing imported/ failed; continuing." >&2
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy .env.example to .env and fill it in." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found; cannot run docker compose." >&2
  exit 1
fi

cd "${ROOT_DIR}"

# Fails fast, with a named variable, if RELAY_IMAGE_TAG or RECORD_DIR is unset.
docker compose config -q

# The tagged image must already exist on this host. A missing image means a
# fresh host or a prune: build and re-check deliberately, not here.
image="$(docker compose config --images | sort -u | head -1)"
if ! docker image inspect "${image}" >/dev/null 2>&1; then
  echo "Missing image ${image}." >&2
  echo "Build it with scripts/build.sh <tag>, run the acceptance checks, then retry." >&2
  exit 1
fi

# Every configuration problem at once, before anything is touched, by the
# same validation up.ps1 runs on Windows (INGEST_KEY characters included).
# Prints which outputs are enabled and what each is sent; never a key.
summary="$(docker compose run --rm --no-deps -T status python -m relay.run --check)"
echo "${summary}"

# relay.yaml decides the output slots; docker-compose.yml has to run one
# container per slot. Refuse if they disagree rather than leave a slot dark.
wanted="$(echo "${summary}" | awk '$1 == "output" { print "out-" $2 }' | sort)"
have="$(docker compose config --services | grep '^out-' | sort)"
if [[ "${wanted}" != "${have}" ]]; then
  echo "relay.yaml output slots and docker-compose.yml out-* services differ:" >&2
  diff <(echo "${wanted}") <(echo "${have}") >&2 || true
  exit 1
fi

# The archive directory must exist and be writable by the relay user before
# Docker bind-mounts it, or Docker creates it root-owned and the recorder
# cannot write.
record_dir="$(docker compose config --format json \
  | python3 -c 'import json,sys; print(next(v["source"] for v in json.load(sys.stdin)["services"]["recorder"]["volumes"] if v["target"] == "/recordings"))')"
if [[ ! -d "${record_dir}" ]]; then
  echo "Creating archive directory ${record_dir}"
  mkdir -p "${record_dir}"
fi
if [[ ! -w "${record_dir}" ]]; then
  echo "WARNING: ${record_dir} is not writable by $(id -un); the recorder may fail." >&2
fi

docker compose up -d --remove-orphans

echo "== homelab-relay up: END"
docker compose ps --format 'table {{.Service}}\t{{.Status}}'
