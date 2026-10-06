# TESTING — homelab-relay

What has been run, against what, and what was seen, on adler (Linux) and
shrike (Windows). Anything not observed is listed under
[Not verified](#not-verified), not stated as fact elsewhere.

## How to re-run

```sh
python3 -m unittest discover -s tests -t .      # unit tests
python test/acceptance.py [--encoder E] [--small] [--idle S] [--full-disk-dir D]
```

`test/acceptance.py` is one suite for both platforms. It runs the relay's own
processes, compiled by the same `relay.config` both platforms deploy from, as
children of the script on spare ports (RTMP 11935, status 20940, API 19997,
sinks 19351/19352 and 19361/19362), so it runs beside a live relay. Nothing
in it contacts a platform: "twitch" is unresolvable and "youtube" is a local
self-signed sink. Only the opt-in `--public-tls` reaches one.

| Host | Command | What it certifies |
| --- | --- | --- |
| shrike | `.venv\Scripts\python.exe test\acceptance.py --ffmpeg tools\ffmpeg\bin\ffmpeg.exe --mediamtx tools\mediamtx\mediamtx.exe` | the show path: NVENC, full-size renditions, 30 Mbps master |
| adler | `docker run --rm --network host -v "$PWD":/app -w /app homelab-relay:<tag> python test/acceptance.py --encoder copy --small` | the stream-copy fallback, in the image that will run |
| any | `--encoder libx264 --small` | a smoke test of the transcoding path without a GPU |

Full size, the publisher sends what batw sends on show day: 1080p60 at
30000 kbps with 320 kbps 48 kHz stereo AAC. `--small` sends 720p30 at
2.5 Mbps so a CPU-only host keeps up. Reports go to `test/reports/` (not
committed). Run it on every new tag (Linux) or deploy (Windows) before a
show depends on it, and add the result below. NSSM's own behaviour (boot
start, restart after a crash) is not covered: the harness stands in for NSSM.

## Record

### 2026-10-06: `test/acceptance.py`, both platforms (current)

Replaces `test/acceptance.sh` (bash and Docker, adler only), which was
removed with the move to one layout for both hosts.

- **shrike**, h264_nvenc, full size, RTX 3070 Ti (driver 610.88), ffmpeg
  9.0.2 (Gyan essentials), MediaMTX 1.21.1: **33/33 passed**, full-disk and
  public-TLS skipped. sink-a 1664x936 60 fps at 5507 kbit/s (target 5500),
  sink-b 1920x1080 60 fps at 8991 (target 9000), keyframes exactly 2.0 s,
  go-live 6.4 s with no reconnect, recording a copy of the 30 Mbps master.
- **The first shrike run failed 6 checks**: every transcoding output was
  dropped by MediaMTX ("reader is too slow, discarding frames") and
  reconnected through the first ~15 s of go-live. A transcoding ffmpeg takes
  ~10 s to start reading at full speed, and at the 30 Mbps master the default
  512-packet write queue overflowed first. Fixed with `writeQueueSize: 4096`
  in `mediamtx/mediamtx.yml`. The check "go-live is clean: no output
  reconnected" was added so it cannot come back unseen. The Linux runs below
  never saw it because `--small` is a far lighter input.
- **adler**, in the relay image (Debian ffmpeg 7.1.5, MediaMTX 1.21.1),
  `--small --full-disk-dir` on a 24 MB tmpfs: **34/34** with
  `--encoder copy` (each sink receives ingest untouched) and **34/34** with
  `--encoder libx264`.


### 2026-09-23: tag `2026-09-23-2` (current)

- Images: `homelab-relay:2026-09-23-2` `sha256:4809c3b8…` and
  `homelab-relay-mediamtx:2026-09-23-2` `sha256:20581eb0…`, revision
  `0772583`, built from a clean clone. ffmpeg `7.1.5-0+deb13u1`, MediaMTX
  `v1.21.1`.
- Changes from `2026-09-23`: the consumers poll `/v3/paths/list` (no ERR log
  spam while idle); `up.sh` refuses an ingest key MediaMTX cannot use.

`test/acceptance.sh 2026-09-23-2`: **26/26 passed**. Same checks as below,
plus three new ones:

| Check | Seen |
| --- | --- |
| Ingest key check | refuses `=`, `+`, `/`, space and empty; accepts letters, digits, `_`, `.`, `-` |
| Idle MediaMTX log | 0 ERR lines |
| Outputs all reported before states are judged | (fixes the start-up race below) |

Figures: go-live 7.6 s; bitrate ingest 2419 / sinks 2430 kbit/s (within
0.5%, sampled over separate 10 s windows); sink-a recovery 5.0 s; stall shown
`failed` 21.6 s after freeze, retried 23.3 s later, recovered 5.2 s after
unfreeze; publisher restart 8.3 s; gap 8.0 s logged; SIGKILLed file plays
(9.5 s); full disk stops only the recorder.

`up.sh` by hand, in a throwaway project: with the rehearsal's original key
shape (base64 with `+` and `==`) it printed `INGEST_KEY contains characters
MediaMTX refuses in a path: '+='`, exited 1, and left the running containers
untouched.

### 2026-09-23: first rehearsal, recording only, tag `2026-09-23`

The first run with **OBS** as the publisher, from batw on the laptop
(10.92.8.114) to `rtmp://adler.stanley.arpa/live`. No platform keys were set,
so only the recorder ran.

- **Key incident.** batw's `STREAM_KEY_LIVE` was base64, ending in `==`.
  MediaMTX refused every publish with `invalid path name: can contain only
  alphanumeric characters, underscore, dot, minus, slash`, before comparing
  the key. The acceptance checks had only used alphanumeric keys, so they
  never hit this. It took two further mistakes to find:
  - the per-second API 404s logged at ERR buried the real line;
  - OBS kept retrying with the old key after `.env` changed, until the
    stream was restarted.

  Fixed for the rehearsal by removing the `==` on both sides. Fixed in
  `2026-09-23-2` by the key check and the quieter polling. Also, an
  unmasked log excerpt during diagnosis printed the ingest key into the
  session transcript. The spec allows the ingest key in logs, but rotating
  it costs nothing.
- **Recordings.** Four sessions, four files, none overwritten: 8.5 s,
  46.9 s, 24.8 s, then the rehearsal proper at **36 min 21 s** (1.8 GB,
  about 2.9 GB/hour).
- **Stream.** H.264 High 1920×1080 at 60 fps, AAC stereo 44.1 kHz, about
  6.5 Mbit/s. ffprobe reports `r_frame_rate=120/1` on the MPEG-TS; counting
  gave 3,596 video frames in 60 s, so 60 fps is what was recorded.
- **Integrity.** A stream-copy read of the whole 36-minute file: no container
  errors. A decode of a one-minute slice at 30:00: no errors. The whole file
  has not been decoded, and A/V sync over the full length has not been
  checked by playback.
- **Gaps** between sessions were logged: 19.0 s, 60.2 s, 138.4 s, and the end
  of the rehearsal as a gap start.
- **Recorder log.** Only start-of-session timestamp notes, and `Error during
  demuxing: Input/output error` each time OBS disconnected (ingest ending).

### 2026-09-23: tag `2026-09-23`, 61 minutes idle

`test/acceptance.sh 2026-09-23 --idle 3660`: **23/24**. Go-live after 3,660 s
idle: sinks and recorder connected **8.1 s** from publisher start, the same
as with no idle. The failure was in the script, not the relay: it judged the
pre-ingest states as soon as sink-a reported, while out-youtube had not yet
written its first report and correctly showed `unknown`. The script now waits
for every first report.

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

Go-live after at least an hour idle, against this tag: see the 61-minute
run above.

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
- **OBS through the platform outputs.** OBS has only been used with the
  recorder (first rehearsal). OBS's own message for a wrong key has not been
  seen.
- **A stall caused by a firewall** rather than a frozen process. Only a
  frozen process (`docker pause`, then SIGSTOP/NtSuspendProcess) was used.
- **Full archive disk on Windows.** Proved on Linux only (tmpfs); shrike has
  no small volume to fill.
- **The copy fallback at full size.** The copy runs used `--small`; adler's
  CPU cannot publish a 1080p60 30 Mbps test stream in real time, and the
  fallback is fed about 5500 kbps on show day anyway.
