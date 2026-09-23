# RECOVERY — homelab-relay

A broadcast happens once, at a fixed time. A relay that is down then cannot be
fixed afterwards. Check it **before** the show, and during the show prefer the
bail-out over debugging.

## Bail-out: go live without the relay

**The relay is not a single point of failure for the show.** batw keeps a
`twitch` target that points at Twitch's own ingest:

```sh
./batw.sh --target twitch
```

This goes live on Twitch directly from the laptop. It loses YouTube, Facebook
and the relay's archive copy, so keep OBS's local recording on. If the relay
is not healthy shortly before a broadcast, use this rather than
debugging it minutes before going live.

## Prerequisites

- adler is up, with Docker and containerd enabled at boot (they are).
- The repo checkout (or a fresh clone) with a filled-in `.env`. Required:
  `SERVICE_PORT`, `RTMP_PORT`, `INGEST_KEY`, `RECORD_DIR`. See `.env.example`.
- `INGEST_KEY` equals batw's `STREAM_KEY_LIVE`.
- `RECORD_DIR` exists, is writable by `RELAY_UID` (default 1000), and has
  free space. `up.sh` creates it if missing.
- `homelab-infra` is deployed: `stream-relay.stanley.arpa` → adler:20040
  (nginx), and 1935/tcp declared for `stream-relay`. `adler.stanley.arpa`
  resolves to adler.
- Internet egress from adler to the platforms on 443/tcp (RTMPS).

## Recovery order

1. **Is it running?** `docker compose -p homelab-relay ps` from the repo.
   Expect `mediamtx`, `status`, `recorder` and `out-*` all `Up`.
2. **Start or repair:** `./scripts/up.sh`. It is safe to re-run. If nothing
   changed it recreates nothing; if the code or `.env` changed it recreates
   only what changed.
3. **Check the page:** <https://stream-relay.stanley.arpa/> (or
   `http://adler:20040/`). Before a broadcast, expect **No ingest**, each
   platform you intend to use **idle**, the others **disabled**, and no
   storage warning.
4. **Logs:** `docker compose -p homelab-relay logs --since 10m <service>`.
   Output failures are logged as `output <name> failed while ingest is live:
   <reason>`. Keys are shown as `<redacted>`.
5. **Order of dependency:** `mediamtx` first (ingest). The `out-*` wrappers
   and `recorder` wait for it on their own. `status` is observation only;
   restarting it never affects the broadcast.

## Pre-broadcast verification

The page proves the relay is up and configured. It does **not** prove that a
platform will accept the stream: outputs connect only once ingest arrives.
Do both of the following well before the show.

### 1. Relay accepts ingest (any time)

From any LAN machine with ffmpeg, publish a test pattern with the real ingest
key. Nothing reaches a platform unless a platform key is set, so clear the
platform keys first or use the sinks.

```sh
ffmpeg -re -f lavfi -i testsrc2=size=1280x720:rate=30 -f lavfi -i sine=frequency=440 \
  -c:v libx264 -preset veryfast -g 60 -b:v 2500k -c:a aac -t 60 \
  -f flv "rtmp://adler.stanley.arpa/live/$STREAM_KEY_LIVE"
```

Expect the page to show **Ingest live**, the recorder **recording**, and a
new file in `RECORD_DIR`. A wrong key fails with `authentication failed`.
Alternatively, point OBS at `rtmp://adler.stanley.arpa/live` via batw and
start streaming.

To exercise fan-out without any platform, set `SINK_A_URL` / `SINK_B_URL` as in
README "Tests", run `docker compose --profile sinks up -d` then
`./scripts/up.sh`. **Clear the sink URLs and re-run `up.sh` before the
show.**

### 2. Each platform accepts the relay's stream, privately

Mechanisms checked on 2026-09-23. Platforms change these, so re-check if
one does not behave as described.

- **Twitch: bandwidth test.** Append `?bandwidthtest=true` to the key:
  `STREAM_KEY_TWITCH=live_xxxxx?bandwidthtest=true`, run `./scripts/up.sh`,
  then publish. Twitch accepts the stream but does not make it viewable.
  Confirm it arrived at <https://inspector.twitch.tv/>. **Remove the suffix
  and re-run `up.sh` afterwards**, or the real broadcast will not be
  viewable.
- **YouTube: private stream.** In YouTube Studio → Go live, create or
  schedule a stream with visibility **Private**, and use that stream's key as
  `STREAM_KEY_YOUTUBE`. Publish; the Live Control Room preview shows whether
  data is arriving. Only you can see a private stream. Check which stream
  key the real broadcast will use: a reusable key carries the settings of
  the stream it is bound to.
- **Facebook: no confirmed private route for a Page.** On a personal
  profile, Live Producer lets you set the audience to **Only me**. That is a
  real live broadcast visible only to you. For a Page, "Only me" has not been
  confirmed. The fallback is Live Producer's preview: with the key set and
  the relay publishing, Live Producer shows a preview before you press
  **Go live**. Whether a given key goes live automatically depends on its
  settings, so check that in Live Producer before relying on the preview.
  **This path has not been tested from this relay.**

Each test proves RTMPS egress for that platform too: the relay verifies each
platform's certificate against public roots.

### T-minus checklist (the day)

- [ ] Page: **No ingest**. Intended platforms **idle**, others
      **disabled**, sinks **disabled**.
- [ ] No storage warning. Free space at `RECORD_DIR` covers the show.
- [ ] Any `?bandwidthtest=true` removed from `STREAM_KEY_TWITCH`.
- [ ] Keys set only for the platforms in this broadcast (clear a key to drop
      a platform, then `./scripts/up.sh`).
- [ ] No deploys to adler until after the show.

## Symptoms

| Page / log shows | Meaning | Do |
| --- | --- | --- |
| Status page unreachable | `status` or nginx down; broadcast unaffected | `docker compose -p homelab-relay ps`; `./scripts/up.sh` |
| RTMP server unreachable | `mediamtx` down; no ingest possible | `./scripts/up.sh`; if still down within minutes, **bail out** |
| No ingest while OBS says live | OBS pointed elsewhere, or wrong key | Check batw target and `STREAM_KEY_LIVE` = `INGEST_KEY`; `mediamtx` logs show `authentication failed` for a wrong key |
| One platform **failed**, others connected | That platform refused or stalled | Reason on the page and in `logs out-<name>`. It retries every ≤10 s. Wrong or expired key is the usual cause: fix the key, `./scripts/up.sh` (restarts only that output) |
| All platforms **failed**, ingest live | adler's internet egress, or all keys wrong | Check egress from adler; if not quickly fixable, **bail out** |
| Output **unknown** | Its wrapper is not reporting | `./scripts/up.sh` |
| Recording **failed**: `No space left on device` | Archive disk full; broadcast unaffected | Free space in `RECORD_DIR`; it resumes into a new file on its own |
| A platform shows **disabled** you wanted | Its URL or key is empty in `.env` | Set it, `./scripts/up.sh` |

After an ingest drop mid-show, outputs reconnect on their own once OBS is
back. Each platform sees a new session, which may end the broadcast or split
the VOD. That is expected; this build does not bridge gaps. Gaps are listed
in `$RECORD_DIR/ingest-gaps.log`.

## Do not change casually

- **The ingest address `rtmp://adler.stanley.arpa/live` and `INGEST_KEY`.**
  Both are committed in batw; changing either is a coordinated change there
  too.
- **Stream copy.** adler has no GPU. Any transcoding (a slate, filler,
  per-platform renditions) is an architecture change: read the spec's
  Portability section first.
- **`-tls_verify`.** ffmpeg defaults to not verifying. Verification may be off
  only for the self-signed test sink slot.
- **The wrapper's loop.** It must wait for ingest itself and never exit
  because ffmpeg exited; otherwise Docker's restart backoff delays go-live
  after idle time. Its jobs are fixed by the spec (deviation 1b).
- **Recording file naming and `-n`.** A fixed output path would let the second
  session overwrite the first, silently.
- **The `authInternalUsers` order in `mediamtx/mediamtx.yml`.** The ingest key
  is injected by position (`MTX_AUTHINTERNALUSERS_0_PERMISSIONS_0_PATH`).
- **Proxying ingest.** RTMP goes straight to adler:1935, never through nginx.
  See "Why ingest is not proxied" in the spec.
