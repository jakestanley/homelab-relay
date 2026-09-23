# TESTING — homelab-relay

What has been run, against what, and what was seen. Everything here was
observed on adler. Anything not observed is listed under
[Not verified](#not-verified), not stated as fact elsewhere.

## How to re-run

```sh
python3 -m unittest discover -s tests -t .      # unit tests, no Docker
test/acceptance.sh <tag> [--idle SECONDS]       # sink checks against a built tag
```

`test/acceptance.sh` writes its report to `test/reports/` (not committed).
Run it on every new tag before pointing `RELAY_IMAGE_TAG` at it, and add the
result below.

The publisher in all runs is ffmpeg, not OBS: `testsrc2` 1280x720 at 30 fps,
x264 High profile with a 2 s GOP at 2.5 Mbit/s, and AAC audio. "From publisher
start" times therefore include roughly 1–2 s of x264 start-up on the
publishing side.

## Record

### 2026-09-23: tag `2026-09-23`

- Images: `homelab-relay:2026-09-23` `sha256:0de871bb…` and
  `homelab-relay-mediamtx:2026-09-23` `sha256:35ad5f04…`, revision
  `c04c4d1`, built from a clean clone.
- Versions: ffmpeg `7.1.5-0+deb13u1`, MediaMTX `v1.21.1`.

`test/acceptance.sh 2026-09-23`: **24/24 passed** (3 min 39 s).

| Check | Seen |
| --- | --- |
| No ingest: enabled outputs idle, keyless platform disabled | ingest `no_ingest`; twitch, youtube, sinks, recorder `idle`; facebook `disabled` |
| Wrong ingest key rejected | publisher exit 255, `Server error: authentication failed` |
| Go-live (no idle) | sinks and recorder connected **7.8 s** from publisher start |
| Nothing re-encoded: stream parameters | identical at ingest, sink-a, sink-b (over RTMPS): h264 High, level 31, 1280x720, 30/1, yuv420p; aac LC 48 kHz |
| Nothing re-encoded: bitrate | ingest 2431, sink-a 2431, sink-b 2431 kbit/s (video, 10 s) |
| Failing platforms show failed while ingest is live | twitch (unresolvable host) and youtube (self-signed peer) `failed`, with the reason |
| TLS verified on platform outputs | out-youtube logged `Peer certificate failed verification` |
| Isolation | sink-a killed; ingest live, sink-b kept delivering (9.5 MB → 12.8 MB in 10 s), publisher unaffected |
| Killed output shown and logged by name | page `failed`; log `output sink-a failed while ingest is live` |
| Recovery after restore | sink-a connected **9.9 s** after restore, container restarts 0 |
| Stall (sink frozen with `docker pause`) | page `failed` **20.1 s** after freeze (see below) |
| Stall ended and retried by the wrapper | new ffmpeg run 23.4 s after `failed`, container restarts 0 |
| Stall recovery | connected **4.0 s** after unfreeze |
| Ingest gap | `no_ingest`, every output `idle`, none `failed` |
| Publisher restart | every output delivering **8.2 s** from publisher start |
| Both sessions recorded | 2 files; the first unchanged after the second session |
| Gap log | `gap start=19:56:52 end=19:57:00 duration=8.0s` |
| Recording after SIGKILL mid-write | 9.2 s file probes and decodes |
| Crashed recorder | Docker restarted it (restarts 1); a new file already existed when checked, about 3 s after the kill |
| Key leaks | 0 hits for platform or ingest keys in wrapper/status logs, all endpoints and state files; 48 `<redacted>` lines |
| Disabled output | `disabled`, one log line, 0 TCP connections |
| Full archive disk (20 MB tmpfs) | recorder `failed: … No space left on device`; ingest, both sinks and publisher unaffected |
| Free-space warning | `threshold_breached: true` |

Why a stall takes about 20 s to show as `failed`: the byte counter kept
advancing for about 15 s after the sink froze, while kernel socket buffers
absorbed the stream. The page reports `failed` 5 s after the counter stops.

`up.sh` against that tag, by hand, in an isolated project:

- Re-run against a running relay: nothing recreated.
- One platform key changed in `.env`: only that output recreated.
- `RELAY_IMAGE_TAG` set to a tag that does not exist: `up.sh` exits 1 naming
  both missing images, and running containers are untouched.
- A tag that already exists: `scripts/build.sh` refuses to overwrite it.

Go-live after at least an hour idle, against this tag: *running,
result to be added*.

### 2026-09-23: by hand, before the tag existed

These were run on the same code paths before the build model was added. They
are kept because they are not repeated by the script.

- **ffmpeg TLS default.** `ffmpeg -h protocol=tls` shows `tls_verify`
  default 0. With the default, a push to a self-signed RTMPS sink succeeded.
  With `-tls_verify 1` it was refused (`Peer certificate failed
  verification`).
- **Public certificates verify.** With `-tls_verify 1`, pushes to
  `a.rtmps.youtube.com`, `ingest.global-contribute.live-video.net` and
  `live-api-s.facebook.com:443` completed the TLS and RTMP handshakes. Each
  was then rejected at publish, because the key was bogus. This proves
  verification passes against public roots, not that a real key would be
  accepted.
- **Ingest key redacted from input errors.** A wrapper pointed at a refused
  source port logged `Error opening input file
  rtmp://mediamtx:1999/live/<redacted>.`, with 0 raw occurrences.
- **Shorter probing is unsafe.** `-analyzeduration 500000 -probesize 262144`
  lost the H.264 profile and resolution, so probing stays at ffmpeg's default
  (about 3.2 s to first output bytes when joining a live ingest).
- **A full stop/start (`docker compose stop` then `start`)** with platform
  keys empty: each platform logged only its `disabled` line and held 0 TCP
  connections.
- **Go-live after 62 minutes idle** (a separate stack, started 19:12:36Z,
  publishing at 20:14:45Z): sink-a, sink-b and the recorder connected
  **7.95 s** from publisher start, the same as with no idle. All 10
  containers still had `RestartCount=0`, so no restart backoff had built up.
- **Docker at boot.** `docker`, `docker.socket` and `containerd` are
  `enabled`. Every broadcast-path container has restart policy `always`.

## Not verified

- **Host reboot.** Not done. Boot start rests on the enabled units and
  `restart: always` above. The spec's "disabled output across a reboot" check
  was approximated by a Compose stop/start, which is not the same thing.
- **Delivery to the real platforms.** No stream with a real key has been sent
  from this relay. The private routes in RECOVERY.md (Twitch bandwidth test,
  private YouTube stream, Facebook "Only me") have not been exercised. For a
  Facebook **Page**, no private route has been confirmed.
- **OBS as the publisher.** All runs used ffmpeg. OBS's own message for a
  wrong key has not been seen.
- **A stall caused by a firewall** rather than a frozen process. Only
  `docker pause` was used.
