# homelab-relay

An RTMP relay on adler. OBS sends **one** stream to it; it forwards that stream,
unmodified, to Twitch, YouTube and Facebook, and keeps an archive copy.

- Ingest: `rtmp://adler.stanley.arpa/live` with the stream key from batw's
  `STREAM_KEY_LIVE`.
- Status page: `https://stream-relay.stanley.arpa` (proxied to port 20040).

The design spec, with the reasoning behind every constraint, is
[prompts/init.md](prompts/init.md). Recovery and pre-broadcast checks are in
[RECOVERY.md](RECOVERY.md).

## Why it exists

The broadcast laptop runs OBS, Ableton Live and a 60 fps display capture at
once. Muxing and uploading three copies of the stream there
(`obs-multi-rtmp`) puts extra CPU and thermal load on the busiest machine in
the room. The relay takes that work off the laptop.

It does **not** save bandwidth. The relay is on the same LAN, so the house
uplink still carries three copies, exactly as it would with `obs-multi-rtmp`.
The benefit is the laptop's CPU and thermal headroom, and nothing else.

## How it works

```
OBS ──RTMP──▶ mediamtx (:1935, ingest only)
                 │ read by
                 ├─▶ out-twitch    wrapper + ffmpeg ──RTMPS──▶ Twitch
                 ├─▶ out-youtube   wrapper + ffmpeg ──RTMPS──▶ YouTube
                 ├─▶ out-facebook  wrapper + ffmpeg ──RTMPS──▶ Facebook
                 ├─▶ out-sink-a/b  wrapper + ffmpeg ──▶ local test sinks (off by default)
                 └─▶ recorder      wrapper + ffmpeg ──▶ $RECORD_DIR/<UTC time>.ts
               status (:20040)  reads the MediaMTX API + wrapper reports → page, gap log
```

- **MediaMTX** accepts ingest and nothing else. It allows publishing only on
  `live/<INGEST_KEY>`, so a publish with the wrong key is rejected.
- **Each output is its own long-running process**: a small Python wrapper
  (`relay/output.py`) running ffmpeg, which re-encodes to that output's
  rendition, or copies for a `copy` rendition and for the recorder.
- **The wrapper waits for ingest itself**, polling the MediaMTX API every
  second, and starts ffmpeg as soon as ingest appears. When ffmpeg exits,
  whether because ingest stopped, the platform dropped the connection or the
  I/O timeout fired, the wrapper goes back to waiting (with a retry delay of
  at most 10 s while ingest is live). It never exits because ffmpeg exited,
  so Docker's restart backoff never builds up during idle time.
- **Who restarts what:** the wrapper restarts ffmpeg. Docker (`restart:
  always`, one container per output) restarts a wrapper that crashes. The
  status glue restarts nothing.
- **Stalls become exits.** Every push sets `-rw_timeout`. The wrapper also
  kills ffmpeg if its byte counter stops advancing. Both are needed: in
  testing, ffmpeg's timeout fired but ffmpeg then hung while writing its
  trailer, and the wrapper's watchdog is what ended it.
- **"Connected" means bytes are moving.** Each wrapper writes its ffmpeg
  `-progress` byte counter to a JSON file in a shared volume. The status page
  shows *connected* only while that counter is advancing.
- **Recording** uses the same wrapper, writing MPEG-TS with `-c copy`. Each
  ingest session gets a new file named by UTC start time, and ffmpeg runs with
  `-n`, so nothing is ever overwritten. MPEG-TS survives the process being
  killed, losing only the tail. A full disk stops the recorder and nothing
  else. See [Archive](#archive).

### Output slots and on/off

Outputs are named slots in `relay.yaml`. Each slot has a URL and an optional
key, both set in `.env`, and a rendition (below):

| Slot | URL variable | Key variable | Disabled when |
| --- | --- | --- | --- |
| twitch | `TWITCH_URL` | `STREAM_KEY_TWITCH` | URL or key empty |
| youtube | `YOUTUBE_URL` | `STREAM_KEY_YOUTUBE` | URL or key empty |
| facebook | `FACEBOOK_URL` | `STREAM_KEY_FACEBOOK` | URL or key empty |
| sink-a | `SINK_A_URL` | none | URL empty (default) |
| sink-b | `SINK_B_URL` | none | URL empty (default) |

**To leave a platform out of a broadcast, clear its key and re-run up**
(`scripts\up.ps1` or `scripts/up.sh`). A disabled output logs one "disabled"
line, makes no connection and shows as *disabled* on the page. Adding a new
platform means a slot in `relay.yaml`, and on Linux an `out-<slot>` service in
`docker-compose.yml` (`up.sh` refuses to start if the two disagree).

Each output is sent its own rendition from `relay.yaml`: OBS sends one
high-bitrate master, and each platform gets it re-encoded to its own limit
(`VIDEO_ENCODER`: `h264_nvenc` on the GPU, `libx264` without one). An output
whose rendition is `copy` is forwarded untouched instead. `VIDEO_ENCODER=copy`
in a host's `.env` does that for every output, whatever `relay.yaml` says:
that is the adler fallback, the same checkout of `main` as shrike, with no
encoder in the path and OBS setting every platform's bitrate (`up` prints each
output's `max_kbps` as a reminder). The recorder always copies.

### Go live or record only

The status page has one switch for every push output. **Record only** (the
default) records ingest and keeps every platform output on *standby*:
nothing reaches a platform. **Go live** starts every enabled output, within
a few seconds (about 9 s on shrike, where NVENC has to start). Switching back
to record only mid-stream stops the pushes cleanly, as *standby* rather than
*failed*, while recording carries on.

It is the page's only control (`POST /api/live`, `{"armed": true|false}`,
JSON only so a plain cross-site form cannot set it), stored as `armed.json` in
the state directory. A reboot always comes back record only. A crashed or
redeployed service keeps the switch as it was, so a status-page restart
mid-show cannot take you off air. This replaces the spec's "read-only status
page" non-goal (decided 2026-10-07). Per-platform on/off is still a matter of
clearing a key.

### When ingest drops

When OBS disconnects, every output drops too, and viewers see the stream stop.
When OBS reconnects, every enabled output reconnects without intervention.
In testing, all outputs were delivering again 8 s after the publisher
restarted (see [TESTING.md](TESTING.md)). Each platform treats that as a new
session. The relay
does not yet generate filler to hold connections open (a slate is planned
now that outputs transcode; see batw's roadmap). During a gap the page reads **no
ingest**, not "failed".

Every gap is logged with start, end and duration by the status service
(`docker compose logs status`) and appended to
`$RECORD_DIR/ingest-gaps.log`.

## Status page

`http://adler:20040/`, and `https://stream-relay.stanley.arpa/` through nginx.
It polls `api/status` every 2 s.

| State | Meaning |
| --- | --- |
| disabled | URL empty, or the slot's key empty |
| idle | no ingest, so nothing to push (normal before a broadcast) |
| connecting | push started within the last 15 s, no data yet |
| connected | the output's byte counter is advancing |
| failed | ingest is live but this output is not delivering (reason shown) |
| unknown | the output process has not reported for 15 s and there is no ingest |

Ingest is checked first. With no ingest, every enabled output is *idle*,
whatever its process is doing. The page also shows free space at the storage
root, a warning below `RECORD_MIN_FREE_GB`, and whether recording is running.

Endpoints (read-only, plain HTTP, no auth, LAN only): `GET /`,
`GET /api/status`, `GET /health`, `GET /openapi.json`. The status service is
never given platform keys, so no endpoint can expose one. Caching headers are
left to the proxy.

## Running it

Two platforms, one configuration (see [Platforms](#platforms)). On Windows,
`scripts\up.ps1` from an elevated PowerShell; hosts-shrike's Ansible runs it.
On Linux, images are built once, tested, and then run by tag:

```sh
scripts/build.sh 2026-09-23            # builds homelab-relay:2026-09-23
test/acceptance.sh 2026-09-23          # the sink checks, against that exact image
cp .env.example .env                   # set RELAY_IMAGE_TAG=2026-09-23, INGEST_KEY,
                                       # RECORD_DIR and the platform keys
scripts/up.sh
```

**`scripts/up.sh` never builds.** It runs the preflight checks on
`homelab-standards` and `homelab-infra` (warn and ask, never pull), syncs
`imported/`, checks `.env`, checks that the image for `RELAY_IMAGE_TAG`
exists, validates the configuration inside it, creates `RECORD_DIR` if
missing, then runs `docker compose up -d`. So the
image that passed the acceptance checks is the image that runs on the night.
Re-running `up.sh` against a running relay recreates nothing unless `.env`
changed, and then only the affected container: changing one platform key
recreates only that output. If the tagged image is missing (fresh host,
prune), `up.sh` fails and leaves any running containers alone. It does not
build a replacement.

**Rebuilding is deliberate.** `apt-get install ffmpeg` resolves against the
live Debian mirror, so two builds of the same `Dockerfile` can contain
different ffmpeg versions. The tag records what was tested, not the file.
`scripts/build.sh` never overwrites a tag, labels images with the git revision,
and prints the ffmpeg and MediaMTX versions. Base images are pinned by digest.
Build from a clean clone; `build.sh` labels a dirty tree `-dirty`. Between
shows: build a new tag, run `test/acceptance.sh` on it, then point
`RELAY_IMAGE_TAG` at it. Changing the tag recreates every container, so
**never switch tags mid-broadcast**.

The Compose project name is fixed (`homelab-relay`), so it can be deployed
from an ephemeral clone (`PATTERNS/checkout-topology.md`). `relay.yaml` and
the MediaMTX config are baked into the image rather than bind-mounted, so
changing either means a new tag, and removing the clone
afterwards is safe. Runtime state lives outside the tree: recordings in
`RECORD_DIR`, wrapper reports in the `state` volume.

The service starts on boot through Docker's `restart: always` (Docker and
containerd are enabled units on adler).

**Stopping between shows** (the adler fallback runs only on show days):
`scripts/stop.sh` sets the containers' restart policy to `no` and stops
them, so they stay down across reboots; `scripts/start.sh` restores
`restart: always` and starts the same containers. Both work by Compose project
name, not from a compose file, so they handle whichever layout is running and
never recreate anything. `stop.sh` refuses while ingest is live unless given
`--force`. On Windows the equivalents are `scripts\install-service.ps1 -Stop`
and `scripts\up.ps1`; NSSM services are auto-start, so a stopped Windows
relay comes back at the next boot or Ansible deploy.

### Configuration

All configuration is in `.env`; `.env.example` lists every variable and marks
the optional ones. Required: `SERVICE_PORT`, `RTMP_PORT`, `INGEST_KEY`,
`RECORD_DIR`. Platform keys are real credentials. They are redacted from
every log line the wrapper writes (raw and URL-encoded forms, with and
without a query string such as `?bandwidthtest=true`), never written to the
report files, and never given to the status service. Anyone who can run
`docker inspect` on adler can still read them from the container environment.

The ingest key may contain only letters, digits, `_`, `.` and `-`. It becomes
a MediaMTX path name (`live/<key>`), and MediaMTX refuses any other character,
before it even compares the key. A base64 key with `=` or `+` is refused on
every publish. Both up scripts check this (through `relay.config`) before
starting anything. `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
generates a suitable key.

The ingest key is a LAN shared secret, not a platform credential. MediaMTX
names it in its own log lines (it is the path name), which is accepted. The
wrappers redact it anyway, so it stays out of `/api/status` (the last error
line is shown there) in case the status vhost is ever exposed beyond the LAN.

### Ingress and ports

Ports, DNS and exposure are defined in `homelab-infra` (`registry.yaml`,
service `stream-relay`), not here:

- `stream-relay.stanley.arpa` → nginx on adler → port **20040** (status page).
- **1935/tcp** RTMP, declared as a `native_ports` exception, reached directly
  as `adler.stanley.arpa:1935` and never proxied.

The registry entry is `stream-relay`, not `relay`. This deliberately overrides
the default rule that `homelab-<name>` maps to registry entry `<name>`,
because the entry name matches the status page's DNS name.

Neither port is exposed outside the LAN. The MediaMTX API (`RELAY_API_PORT`,
default 9997) listens on loopback only, on both platforms.

### Archive

- One MPEG-TS file per ingest session: `$RECORD_DIR/2026-09-23T19-13-09Z.ts`.
  A crash or a restart mid-show also starts a new file.
- No automatic deletion. Retention is manual. `RECORD_MIN_FREE_GB` (default
  20) only drives the warning on the status page. The relay cannot know when
  a broadcast is about to start, so check the page beforehand.
- To get an MP4, remux **after** the broadcast, never during it, and without
  re-encoding:

  ```sh
  ffmpeg -i 2026-09-23T19-13-09Z.ts -map 0 -c copy -movflags +faststart 2026-09-23T19-13-09Z.mp4
  ```

### Tests

Unit tests (standard library only):

```sh
python3 -m unittest discover -s tests -t .
```

Acceptance checks, against a built tag, on the local test sinks:

```sh
test/acceptance.sh 2026-09-23 [--idle 3600]
```

This runs an isolated Compose project (`relayaccept`, ports 11935/20940) beside
the real relay, with two throwaway MediaMTX sinks. Each sink accepts RTMP on
:1935 and RTMPS on :1936 with a self-signed certificate. The script checks
ingest, re-encoding, isolation, stalls, publisher restart, crash, full disk,
key leaks, TLS verification and disabled outputs, and prints PASS/FAIL with
measured numbers. `--idle` waits before going live, for the restart-backoff
check. What was run, and what was not, is in [TESTING.md](TESTING.md).

To use the sinks by hand, start them with `docker compose --profile sinks up -d`
and set the sink slots in `.env`:

```sh
SINK_A_URL=rtmp://sink-a:1935/sink
SINK_B_URL=rtmps://sink-b:1936/sink
SINK_B_TLS_VERIFY=false     # self-signed; permitted for a test sink only
```

Clear both URLs before a real broadcast.

## Deviations from homelab-standards

1. **The relay itself is not Python.** It is MediaMTX plus ffmpeg under
   configuration. The Python is limited to the two pieces the spec allows: the
   per-output wrapper (`relay/output.py`), whose four jobs are fixed in the
   spec, and the status glue (`relay/status.py`: one process, standard library
   `http.server`, no framework, no database). Both use the standard library
   only, so there is no `requirements.txt` and no venv. Everything runs inside
   the image.
2. **`PATTERNS/internal-ca-trust.md` does not govern this service.** Ingest is
   plain RTMP on the LAN; egress is RTMPS to public platforms, verified
   against the public roots in the image's `ca-certificates`. ffmpeg does
   **not** verify TLS peers by default (`tls_verify=0`), so the wrapper passes
   `-tls_verify 1` on every RTMPS output. It passes `-tls_verify 0` only when
   that one slot sets `OUTPUT_TLS_VERIFY=false`. The only permitted use is
   the self-signed test sink.
3. **Two ports, one of them out of range.** The status page uses registry port
   20040. RTMP uses 1935, the declared exception. The page is not a
   dependency of the broadcast: if `status` is down, ingest, fan-out and
   recording carry on.

## Why MediaMTX

- **RTMPS egress is not the server's job here.** Facebook accepts RTMPS only,
  and the platform defaults in `.env.example` are all RTMPS. `nginx-rtmp`
  cannot push RTMPS without an `stunnel` sidecar per output. In this design
  the server does not push at all: each output's ffmpeg pushes RTMPS
  natively. That leaves the server with ingest, key checking and one HTTP API
  call ("does `live/<key>` have a publisher?"), which MediaMTX does with no
  modules or sidecars.
- **Windows builds.** MediaMTX ships Windows binaries (`mediamtx_v1.21.1_windows_amd64.zip`);
  `nginx-rtmp` effectively has none.
- **MediaMTX's own `forward:` was considered and not used.** MediaMTX 1.21
  can forward a path to RTMP/RTMPS destinations itself. That would run every
  output inside the ingest process, with no per-output progress counter and
  no per-output supervision. The spec requires one process per output, with
  progress evidenced by the output's own counters.

## Platforms

The relay runs on Windows (shrike, NVIDIA, the show path from 2026-10-06) and
on Linux (Docker). Both take every process's program, arguments and
environment from `Config.services()` in `relay/config.py`, so renditions,
keys, validation and the never-a-key-in-the-status-page rule are one piece
of code:

| | Windows | Linux |
| --- | --- | --- |
| Entry point | `scripts\up.ps1` | `scripts/up.sh` |
| Supervision | one NSSM service per process (`relay.install`) | one container per process, `restart: always` |
| Config applied | at install, into each service's environment | at container start (`relay.run`) |
| Binaries | pinned MediaMTX and ffmpeg in `tools\` (`fetch-tools.ps1`) | pinned MediaMTX and Debian ffmpeg in the image |
| Networking | loopback between processes | host networking, so the same loopback addresses |

Two NSSM 2.24 behaviours shape `relay.install`: it never passes an empty
environment value (NSSM drops every entry after one), and logs rotate only at
service start (online rotation leaves every stop hung in `STOP_PENDING`).

On Windows, nginx is not on the same host, which reopens the host-and-vhost
decision recorded in the spec.
