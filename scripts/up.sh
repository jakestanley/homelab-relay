#!/usr/bin/env bash
# Canonical entrypoint: build and (re)start the relay. Safe to re-run against
# a running service -- `docker compose up -d` only recreates containers whose
# configuration or image changed.
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

# Fails fast, with a named variable, if INGEST_KEY or RECORD_DIR is unset.
docker compose config -q

# The archive directory must exist and be owned by the relay user before
# Docker bind-mounts it, or Docker creates it root-owned and the recorder
# cannot write.
record_dir="$(docker compose config --format json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["status"]["environment"]["RECORD_DIR_LABEL"])')"
if [[ ! -d "${record_dir}" ]]; then
  echo "Creating archive directory ${record_dir}"
  mkdir -p "${record_dir}"
fi
if [[ ! -w "${record_dir}" ]]; then
  echo "WARNING: ${record_dir} is not writable by $(id -un); the recorder may fail." >&2
fi

# Reproducible image IDs: without a fixed timestamp every build gets a new
# ID even when nothing changed, and compose recreates every container --
# dropping every output if this is re-run mid-broadcast. Fixed rather than
# the commit time, so a docs-only commit does not recreate anything either.
export SOURCE_DATE_EPOCH=0

docker compose up -d --build --remove-orphans

echo "== homelab-relay up: END"
docker compose ps --format 'table {{.Service}}\t{{.Status}}'
