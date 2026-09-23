#!/usr/bin/env bash
# Acceptance checks from prompts/init.md, run against one image tag, locally,
# against the test sinks -- never against a platform.
#
#   test/acceptance.sh TAG [--idle SECONDS]
#
# Runs an isolated compose project (relayaccept) on spare ports, so it can run
# on adler beside the real relay. Needs docker, ffmpeg, ffprobe, curl, python3.
# Prints PASS/FAIL per check with the measured numbers, writes the same to
# test/reports/<TAG>-<UTC time>.txt (not committed), and exits non-zero if
# anything failed.
#
# Not covered here (see TESTING.md): host reboot, the real platforms, and
# re-running up.sh against a running relay.
set -uo pipefail

TAG="${1:?usage: test/acceptance.sh TAG [--idle SECONDS]}"
IDLE=0
[[ "${2:-}" == "--idle" ]] && IDLE="${3:?--idle needs seconds}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT=relayaccept
RTMP_PORT=11935
HTTP_PORT=20940
INGEST_KEY=Acc3pt_ingest-key.v1   # every character class the key check allows
# Fake platform keys: every check below must never find these in any output.
KEY_TW="live_ACCEPTLEAK_tw1?bandwidthtest=true"
KEY_YT="ACCEPTLEAK-yt2-xxxx"
WORK="$(mktemp -d)"
mkdir -p "${WORK}/rec" "${ROOT}/test/reports"
REPORT="${ROOT}/test/reports/${TAG}-$(date -u +%Y%m%dT%H%M%SZ).txt"
PUB_PID=""
FAILS=0

cat >"${WORK}/env" <<EOF
RELAY_IMAGE_TAG=${TAG}
SERVICE_PORT=${HTTP_PORT}
RTMP_PORT=${RTMP_PORT}
INGEST_KEY=${INGEST_KEY}
RECORD_DIR=${WORK}/rec
RECORD_MIN_FREE_GB=0.01
RELAY_UID=$(id -u)
RELAY_GID=$(id -g)
TWITCH_URL=rtmp://nonexistent.invalid/app
STREAM_KEY_TWITCH=${KEY_TW}
YOUTUBE_URL=rtmps://sink-b:1936/yt
STREAM_KEY_YOUTUBE=${KEY_YT}
FACEBOOK_URL=rtmps://live-api-s.facebook.com:443/rtmp
STREAM_KEY_FACEBOOK=
SINK_A_URL=rtmp://sink-a:1935/sink
SINK_B_URL=rtmps://sink-b:1936/sink
SINK_B_TLS_VERIFY=false
EOF

cat >"${WORK}/tmpfs.yml" <<EOF
services:
  recorder:
    volumes: [rectmp:/recordings]
  status:
    volumes: [rectmp:/recordings]
volumes:
  rectmp:
    driver: local
    driver_opts: {type: tmpfs, device: tmpfs, o: "size=20m,uid=$(id -u),gid=$(id -g)"}
EOF

dc() { docker compose -p "${PROJECT}" --env-file "${WORK}/env" -f "${ROOT}/docker-compose.yml" --profile sinks "$@"; }

log() { echo "$*" | tee -a "${REPORT}"; }
pass() { log "PASS  $1${2:+  ($2)}"; }
fail() { log "FAIL  $1${2:+  ($2)}"; FAILS=$((FAILS + 1)); }

cleanup() {
  [[ -n "${PUB_PID}" ]] && kill "${PUB_PID}" 2>/dev/null
  dc -f "${WORK}/tmpfs.yml" down -v >/dev/null 2>&1
  rm -rf "${WORK}"
}
trap cleanup EXIT

status() { curl -s --max-time 2 "localhost:${HTTP_PORT}/api/status"; }
# state NAME -> state of an output ("recorder" included), or of ingest.
state() {
  status | python3 -c '
import json, sys
try: s = json.load(sys.stdin)
except ValueError: print("unreachable"); sys.exit()
n = sys.argv[1]
if n == "ingest": print(s["ingest"]["state"]); sys.exit()
for o in s["outputs"] + [s["recording"]]:
    if o["name"] == n: print(o["state"]); break' "$1"
}
field() { status | python3 -c 'import json,sys; s=json.load(sys.stdin); o=[o for o in s["outputs"]+[s["recording"]] if o["name"]==sys.argv[1]][0]; print(o.get(sys.argv[2]))' "$1" "$2"; }
# wait_for SECONDS CMD... -> prints elapsed seconds on success
wait_for() {
  local limit="$1"; shift
  local t0; t0=$(date +%s.%N)
  while true; do
    if "$@" >/dev/null 2>&1; then python3 -c "import time; print(round(time.time()-$t0,1))"; return 0; fi
    if python3 -c "import sys,time; sys.exit(0 if time.time()-$t0 > $limit else 1)"; then return 1; fi
    sleep 0.25
  done
}
is() { [[ "$(state "$1")" == "$2" ]]; }
all_connected() { is sink-a connected && is sink-b connected && is recorder connected; }

publish() {
  ffmpeg -hide_banner -loglevel error -re \
    -f lavfi -i testsrc2=size=1280x720:rate=30 -f lavfi -i sine=frequency=440:sample_rate=48000 \
    -c:v libx264 -preset veryfast -profile:v high -g 60 -b:v 2500k -maxrate 2500k -bufsize 5000k \
    -c:a aac -b:a 128k -t 3600 -f flv "rtmp://127.0.0.1:${RTMP_PORT}/live/$1" >"${WORK}/pub.log" 2>&1 &
  PUB_PID=$!
}
unpublish() { kill "${PUB_PID}" 2>/dev/null; wait "${PUB_PID}" 2>/dev/null; PUB_PID=""; }
restarts() { docker inspect -f '{{.RestartCount}}' "${PROJECT}-$1-1"; }
probe() {
  ffprobe -v error -show_entries stream=codec_name,profile,level,width,height,r_frame_rate,pix_fmt,sample_rate,channels \
    -of compact=p=0 "$1" 2>/dev/null | sort
}
video_kbps() {
  local b; b=$(timeout 30 ffmpeg -hide_banner -loglevel error -i "$1" -map 0:v -c copy -t 10 -f h264 - 2>/dev/null | wc -c)
  echo $((b * 8 / 10 / 1000))
}

log "homelab-relay acceptance: tag ${TAG}, $(date -u +%FT%TZ), host $(hostname)"
log "images: $(docker image inspect -f '{{index .RepoTags 0}} {{.Id}} rev={{index .Config.Labels "org.opencontainers.image.revision"}}' "homelab-relay:${TAG}" "homelab-relay-mediamtx:${TAG}" 2>&1 | tr '\n' ' ')"
log "ffmpeg: $(docker run --rm "homelab-relay:${TAG}" ffmpeg -version 2>&1 | head -1)"
log

if ! dc up -d --build >/dev/null 2>&1; then
  fail "stack starts" "docker compose up failed"; exit 1
fi
# Every output must have written its first report before states are judged;
# until then it is legitimately "unknown".
all_reported() {
  status | python3 -c 'import json,sys; s=json.load(sys.stdin); sys.exit(any(o["state"]=="unknown" for o in s["outputs"]+[s["recording"]]))'
}
wait_for 30 all_reported >/dev/null || { fail "stack starts" "outputs still unknown after 30s"; exit 1; }

# --- Ingest key check (scripts/check-ingest-key.sh, run by up.sh) -------------
bad_ok=0
for k in 'base64pad==' 'with+plus' 'with/slash' 'with space' ''; do
  printf '%s\n' "${k}" | "${ROOT}/scripts/check-ingest-key.sh" 2>/dev/null && bad_ok=$((bad_ok + 1))
done
if [[ "${bad_ok}" -eq 0 ]] && printf '%s\n' "${INGEST_KEY}" | "${ROOT}/scripts/check-ingest-key.sh"; then
  pass "ingest key check refuses = + / space empty, accepts letters digits _ . -"
else
  fail "ingest key check" "${bad_ok} bad keys accepted, or the good key refused"
fi

# --- Before ingest -----------------------------------------------------------
s="$(state ingest) twitch=$(state twitch) youtube=$(state youtube) facebook=$(state facebook) sink-a=$(state sink-a) sink-b=$(state sink-b) recorder=$(state recorder)"
if [[ "${s}" == "no_ingest twitch=idle youtube=idle facebook=disabled sink-a=idle sink-b=idle recorder=idle" ]]; then
  pass "no ingest: enabled outputs idle, keyless platform disabled" "${s}"
else
  fail "no ingest: enabled outputs idle, keyless platform disabled" "${s}"
fi

out=$(timeout 10 ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc2=size=320x240:rate=30 -c:v libx264 -t 2 \
  -f flv "rtmp://127.0.0.1:${RTMP_PORT}/live/wrong-key" 2>&1); rc=$?
if [[ ${rc} -ne 0 && "${out}" == *"authentication failed"* ]]; then
  pass "wrong ingest key rejected" "exit ${rc}: $(echo "${out}" | head -1)"
else
  fail "wrong ingest key rejected" "exit ${rc}"
fi

if [[ "${IDLE}" -gt 0 ]]; then
  log "idling ${IDLE}s before go-live"
  sleep "${IDLE}"
fi

# MediaMTX's log must stay readable while idle: the consumers poll the API
# every second and none of that may be logged as an error.
sleep 5
errs=$(dc logs --no-log-prefix mediamtx 2>&1 | grep -c ' ERR ')
if [[ "${errs}" -eq 0 ]]; then
  pass "idle: no ERR lines in the mediamtx log"
else
  fail "idle: no ERR lines in the mediamtx log" "${errs} ERR lines"
fi

# --- Go-live -----------------------------------------------------------------
publish "${INGEST_KEY}"
if t=$(wait_for 20 all_connected); then
  pass "go-live after ${IDLE}s idle: sinks and recorder connected" "${t}s from publisher start"
else
  fail "go-live after ${IDLE}s idle" "sink-a=$(state sink-a) sink-b=$(state sink-b) recorder=$(state recorder) after 20s"
fi
sleep 3

# --- Nothing re-encoded ------------------------------------------------------
pi=$(probe "rtmp://127.0.0.1:${RTMP_PORT}/live/${INGEST_KEY}")
pa=$(probe "rtmp://127.0.0.1:19351/sink")
pb=$(probe "rtmp://127.0.0.1:19352/sink")
if [[ -n "${pi}" && "${pi}" == "${pa}" && "${pi}" == "${pb}" ]]; then
  pass "stream parameters identical at ingest, sink-a, sink-b (rtmps)" "$(echo "${pi}" | tr '\n' ' ')"
else
  fail "stream parameters identical" "ingest=[${pi}] a=[${pa}] b=[${pb}]"
fi
ki="${WORK}/ki"; ka="${WORK}/ka"; kb="${WORK}/kb"
video_kbps "rtmp://127.0.0.1:${RTMP_PORT}/live/${INGEST_KEY}" >"${ki}" & p1=$!
video_kbps "rtmp://127.0.0.1:19351/sink" >"${ka}" & p2=$!
video_kbps "rtmp://127.0.0.1:19352/sink" >"${kb}" & p3=$!
wait "${p1}" "${p2}" "${p3}"   # not bare `wait`: that would wait on the publisher
bi=$(cat "${ki}"); ba=$(cat "${ka}"); bb=$(cat "${kb}")
if python3 -c "import sys; i,a,b=$bi,$ba,$bb; sys.exit(0 if i and abs(a-i)/i<0.02 and abs(b-i)/i<0.02 else 1)"; then
  pass "video bitrate matches within 2%" "ingest ${bi}, sink-a ${ba}, sink-b ${bb} kbit/s over 10s"
else
  fail "video bitrate matches within 2%" "ingest ${bi}, sink-a ${ba}, sink-b ${bb} kbit/s"
fi

# --- Platform-shaped failures while ingest is live ---------------------------
sleep 5
if is twitch failed && is youtube failed; then
  pass "failing platforms show failed while ingest is live" "twitch: $(field twitch detail)"
else
  fail "failing platforms show failed" "twitch=$(state twitch) youtube=$(state youtube)"
fi
if dc logs --no-log-prefix out-youtube 2>&1 | grep -q "Peer certificate failed verification"; then
  pass "TLS verified on platform outputs (self-signed peer refused)"
else
  fail "TLS verified on platform outputs" "no verification failure logged for out-youtube"
fi
if all_connected; then pass "sinks unaffected by failing platforms"; else fail "sinks unaffected by failing platforms"; fi

# --- Isolation ---------------------------------------------------------------
b0=$(field sink-b bytes)
docker kill "${PROJECT}-sink-a-1" >/dev/null
sleep 10
b1=$(field sink-b bytes)
if is ingest live && is sink-b connected && [[ "${b1}" -gt "${b0}" ]] && kill -0 "${PUB_PID}" 2>/dev/null; then
  pass "isolation: sink-a killed, ingest and sink-b continue" "sink-b ${b0} -> ${b1} bytes"
else
  fail "isolation" "ingest=$(state ingest) sink-b=$(state sink-b) bytes ${b0}->${b1}"
fi
if is sink-a failed && dc logs --no-log-prefix out-sink-a 2>&1 | grep -q "output sink-a failed while ingest is live"; then
  pass "killed output shows failed and is logged by name"
else
  fail "killed output shows failed and is logged by name" "sink-a=$(state sink-a)"
fi
docker start "${PROJECT}-sink-a-1" >/dev/null
if t=$(wait_for 30 is sink-a connected); then
  pass "sink-a recovers unaided" "${t}s after restore, container restarts=$(restarts out-sink-a)"
else
  fail "sink-a recovers unaided" "sink-a=$(state sink-a) after 30s"
fi

# --- Stall -------------------------------------------------------------------
runs0=$(field sink-a runs)
docker pause "${PROJECT}-sink-a-1" >/dev/null
if t=$(wait_for 60 is sink-a failed); then
  pass "stalled output shows failed, not connected" "${t}s after sink froze"
else
  fail "stalled output shows failed" "sink-a=$(state sink-a) after 60s"
fi
tr=$(wait_for 60 bash -c "[[ \$(curl -s localhost:${HTTP_PORT}/api/status | python3 -c 'import json,sys; print([o for o in json.load(sys.stdin)[\"outputs\"] if o[\"name\"]==\"sink-a\"][0][\"runs\"])') -gt ${runs0} ]]") \
  && pass "stall ended by the wrapper and retried (no container restart)" "new run ${tr}s after failed; restarts=$(restarts out-sink-a)" \
  || fail "stall ended by the wrapper and retried" "runs still ${runs0}"
docker unpause "${PROJECT}-sink-a-1" >/dev/null
if t=$(wait_for 60 is sink-a connected); then
  pass "stalled output recovers" "${t}s after unfreeze"
else
  fail "stalled output recovers" "sink-a=$(state sink-a)"
fi

# --- Publisher restart -------------------------------------------------------
unpublish
if t=$(wait_for 5 is ingest no_ingest); then
  sleep 2
  s="sink-a=$(state sink-a) sink-b=$(state sink-b) recorder=$(state recorder) twitch=$(state twitch)"
  if [[ "${s}" == "sink-a=idle sink-b=idle recorder=idle twitch=idle" ]]; then
    pass "ingest gap reads no_ingest, outputs idle (not failed)" "${s}"
  else
    fail "ingest gap reads no_ingest, outputs idle" "${s}"
  fi
else
  fail "ingest gap detected" "ingest=$(state ingest)"
fi
sleep 3
first=$(ls "${WORK}/rec"/*.ts | head -1); size_first=$(stat -c %s "${first}")
publish "${INGEST_KEY}"
if t=$(wait_for 20 all_connected); then
  pass "every output re-established after publisher restart" "${t}s from publisher start"
else
  fail "outputs re-established after publisher restart" "sink-a=$(state sink-a) sink-b=$(state sink-b) recorder=$(state recorder)"
fi
n=$(ls "${WORK}/rec"/*.ts | wc -l)
if [[ "${n}" -ge 2 && "$(stat -c %s "${first}")" -eq "${size_first}" ]]; then
  pass "both sessions recorded, first file untouched" "${n} files; first $(basename "${first}") ${size_first} bytes"
else
  fail "both sessions recorded" "${n} files"
fi
if grep -q "duration=" "${WORK}/rec/ingest-gaps.log" 2>/dev/null; then
  pass "gap logged with start, end, duration" "$(tail -1 "${WORK}/rec/ingest-gaps.log")"
else
  fail "gap logged with start, end, duration"
fi

# --- Crash: recorder wrapper SIGKILLed mid-write -----------------------------
sleep 5
current=$(ls -t "${WORK}/rec"/*.ts | head -1)
kill -9 "$(docker inspect -f '{{.State.Pid}}' "${PROJECT}-recorder-1")"
sleep 2
if dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "${current}") && [[ -n "${dur}" ]] \
   && ffmpeg -v quiet -i "${current}" -f null - ; then
  pass "recording playable after SIGKILL mid-write" "$(basename "${current}") ${dur}s"
else
  fail "recording playable after SIGKILL mid-write" "$(basename "${current}")"
fi
if t=$(wait_for 20 bash -c "[[ \$(ls ${WORK}/rec/*.ts | wc -l) -gt ${n} ]]"); then
  pass "crashed recorder restarted by Docker, new file" "restarts=$(restarts recorder), new file ${t}s later"
else
  fail "crashed recorder restarted" "restarts=$(restarts recorder)"
fi

# --- Keys never leak ---------------------------------------------------------
pat="ACCEPTLEAK|${INGEST_KEY}"
leaks=$(dc logs --no-log-prefix out-twitch out-youtube out-facebook out-sink-a out-sink-b recorder status 2>&1 | grep -cE "${pat}")
api=$(for p in / /api/status /health /openapi.json; do curl -s "localhost:${HTTP_PORT}${p}"; done | grep -cE "${pat}")
state_files=$(docker run --rm -v "${PROJECT}_state:/s" "homelab-relay:${TAG}" sh -c 'cat /s/*.json' | grep -cE "${pat}")
redacted=$(dc logs --no-log-prefix out-twitch out-youtube 2>&1 | grep -c '<redacted>')
if [[ "${leaks}" -eq 0 && "${api}" -eq 0 && "${state_files}" -eq 0 && "${redacted}" -gt 0 ]]; then
  pass "no platform or ingest key in wrapper/status logs, endpoints, state files" "${redacted} redacted lines"
else
  fail "keys never leak" "log hits ${leaks}, endpoint hits ${api}, state hits ${state_files}, redacted lines ${redacted}"
fi

# --- Disabled output ---------------------------------------------------------
lines=$(dc logs --no-log-prefix out-facebook 2>&1 | grep -vc "disabled (stream key not set)")
conns=$(docker exec "${PROJECT}-out-facebook-1" sh -c 'cat /proc/net/tcp /proc/net/tcp6 | awk "NR>1 && \$4==\"01\"" | wc -l')
if is facebook disabled && [[ "${lines}" -eq 0 && "${conns}" -eq 0 ]]; then
  pass "disabled output: shows disabled, logs nothing else, no connections"
else
  fail "disabled output" "state=$(state facebook) other log lines=${lines} tcp=${conns}"
fi

# --- Disk full ---------------------------------------------------------------
dc -f "${WORK}/tmpfs.yml" up -d recorder status >/dev/null 2>&1
if wait_for 30 is recorder connected >/dev/null && t=$(wait_for 150 is recorder failed); then
  sleep 5
  d=$(field recorder detail)
  if [[ "${d}" == *"No space left"* ]] && is ingest live && is sink-a connected && is sink-b connected && kill -0 "${PUB_PID}" 2>/dev/null; then
    pass "full archive disk stops recording only" "failed ${t}s after start: ${d}"
  else
    fail "full archive disk stops recording only" "ingest=$(state ingest) sink-a=$(state sink-a) sink-b=$(state sink-b) recorder: ${d}"
  fi
  if status | grep -q '"threshold_breached": true'; then pass "free-space warning raised"; else fail "free-space warning raised"; fi
else
  fail "full archive disk" "recorder=$(state recorder)"
fi

log
log "report: ${REPORT}"
if [[ "${FAILS}" -eq 0 ]]; then log "ALL PASSED"; else log "${FAILS} FAILED"; fi
exit $((FAILS > 0))
