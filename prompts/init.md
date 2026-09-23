# Spec prompt — `homelab-relay`

Status: **ready once the registry entry is committed.** All design decisions
are settled. The `relay` entry in `homelab-infra/registry.yaml` must exist,
committed and pushed, before an agent starts — see Prerequisites.

---

## Task

Create the `homelab-relay` service repository: an RTMP relay that accepts one
inbound stream and fans it out to multiple streaming platforms.

Start from `homelab-service-template`. Do not create the repository structure
from scratch.

Concretely: clone the template, discard its `.git`, and copy its contents into
the `homelab-relay` working directory — so the repo has the template's
structure and its own history, with no upstream link back to the template.

If that directory already exists and holds files (this prompt, for instance),
**preserve them**; the template fills in around them rather than replacing the
directory. Commit this prompt alongside the code: the reasoning behind the
constraints is worth keeping next to what implements them.

Canonical agent behaviour is defined in `homelab-standards/AGENTS.md`. Read it
before proposing an implementation. Where this document conflicts with it, the
deviations below are the explicit exceptions; everything not listed as a
deviation still applies.

---

## Where the standards live

Three repositories are referenced throughout this document:

| Repository | Remote | Holds |
| --- | --- | --- |
| `homelab-standards` | `git@github.com:jakestanley/homelab-standards.git` | `AGENTS.md`, `PATTERNS/` |
| `homelab-infra` | `git@github.com:jakestanley/homelab-infra.git` | `registry.yaml` — ports, DNS, exposure |
| `homelab-service-template` | `git@github.com:jakestanley/homelab-service-template.git` | The repository skeleton to start from |

Dev checkouts follow `~/git/<forge>/<owner>/<repo>`, per
`PATTERNS/checkout-topology.md`, so all three sit as siblings of this service's
checkout:

```
~/git/github.com/jakestanley/
├── homelab-standards/
├── homelab-infra/
└── homelab-relay/          <- this service
```

Read them at those paths. If any is missing, clone it from the remote above
rather than proceeding without it.

- `../homelab-standards/AGENTS.md` is canonical and MUST be read first.
- `../homelab-standards/PATTERNS/` holds the pattern documents this spec cites.
- `../homelab-infra/registry.yaml` MAY be read **at authoring time only** to
  look up this service's declared name, ports and exposure. Service code MUST
  NOT read or parse it at runtime; values reach the service through
  environment or config.
- Agent-facing docs are vendored into consumer repos under `imported/` for tool
  visibility, synced by `homelab-standards/scripts/sync_imports.py`. Vendored
  copies are **not** committed to this repo; the canonical versions above win
  in any disagreement.

Per the preflight requirement in `AGENTS.md`, both `homelab-standards` and
`homelab-infra` must be git repositories with no uncommitted changes, at
default branch HEAD, before this work starts. Do not pull or reset them
automatically — warn and stop.

---

## Why this service exists

A live performance is broadcast from a macOS laptop running OBS, Ableton Live
and a 60 fps display capture simultaneously. That machine has no headroom to
mux and upload three copies of the stream.

So OBS sends **one** stream to this relay, and the relay does the fan-out. The
alternative — `obs-multi-rtmp` on the laptop — puts that work on the busiest
machine in the room, and was rejected for that reason. See "The relay is on the
LAN" for what this does and does not save.

The consuming application (`batw`) needs no knowledge of this service beyond
one address. It points at a single RTMP endpoint either way.

---

## Prerequisites (not part of the implementation)

1. **A `relay` entry must exist in `homelab-infra/registry.yaml`** before this
   work starts. Per `AGENTS.md`, agents align to the registry rather than
   inventing values, so service name, ports and exposure come from there.
2. That entry MUST declare **two** ports:
   - The RTMP port, as an explicit out-of-range exception. The homelab port
     range is reserved for HTTP services; 1935 is a native port and `AGENTS.md`
     requires out-of-range ports to be declared rather than assumed.
   - An HTTP port **in** the homelab range, for the status page.
3. Two names, both resolving to the relay host: `adler.stanley.arpa` for
   ingest and `stream-relay.stanley.arpa` for the status page. Ingest explains
   why.

If the registry entry is absent, stop and ask rather than choosing values.

---

## Fixed constraints

### Ingest

- The relay MUST accept RTMP ingest at `rtmp://adler.stanley.arpa/live`.
- This address is committed in the consuming repository. Changing it is a
  coordinated change in both places, not a unilateral one.
- Ingest is plain RTMP, not RTMPS, and is LAN-only. See deviation 2.

**The ingest stream key is part of that contract.** `rtmp://…/live` is only the
server and application; OBS also sends a key, which batw supplies from
`STREAM_KEY_LIVE` in its own `.env`.

- The relay MUST accept a configured ingest key, supplied by environment, and
  that value MUST match what batw sends. It is a shared secret between the two,
  not a platform credential.
- The relay SHOULD reject a publish with the wrong key rather than accepting
  anonymous ingest, so a misconfigured publisher fails loudly on the LAN rather
  than silently becoming the broadcast.

**One host, two names, on purpose.** nginx runs on the relay host, so both
names resolve to the same box — but they say different things, and publishers
MUST use the first:

- `adler.stanley.arpa` is the **machine**. RTMP on 1935 goes straight to the
  relay process, not through nginx. Naming the host makes that directness
  explicit, and means the ingest path does not silently depend on a service
  vhost that exists for something else.
- `stream-relay.stanley.arpa` is the **service vhost** for the status page,
  terminated by nginx.

The practical benefit: if the status page vhost is ever renamed, moved behind a
different proxy, or taken down, ingest is unaffected, because it never
depended on that name.

Ingest MUST NOT be proxied through any other host. See "Why ingest is not
proxied" below.

### The relay is on the LAN

It sits on the same network as the laptop. This means the fan-out saves the
**laptop's CPU and thermal headroom**, which is the entire justification — the
house uplink still carries three copies, exactly as `obs-multi-rtmp` would.
`README.md` MUST state the benefit in those terms and MUST NOT claim a
bandwidth saving.

### Fan-out

- The relay MUST forward to each configured platform **without re-encoding**
  (stream copy).
- **The relay host has no GPU.** Any transcoding would be software, on CPU, in
  real time, three times over — moving the cost this service exists to remove
  from one machine to another. Stream copy is therefore not a default to be
  revisited; it is a property of the hardware. An agent proposing transcoding
  for any reason MUST stop and ask.
- Consequence to document, not to solve: every platform receives OBS's single
  bitrate and resolution, and OBS must therefore be configured to whatever the
  most restrictive platform accepts — currently Facebook. Per-platform
  renditions are a non-goal. See "Portability" for the circumstance under which
  that would be revisited.
- The relay MUST support at least three simultaneous outputs.
- **An output is a name, a URL and an optional key** — not a key alone.
  Platform ingest URLs change, the local test sink needs an arbitrary one, and
  Facebook's is RTMPS rather than RTMP, so a config model of "one key per
  known platform" does not survive contact. The URL is configuration.
- **An output is enabled only when its URL is set.** An empty URL means
  disabled. This is the rule, because it is the only one that works for every
  output: a local sink legitimately has no key, so "empty key means disabled"
  would make sinks unable to run at all.
- **An output that names a key variable is also disabled when that variable is
  empty.** Platforms name one; sinks do not. This preserves the per-broadcast
  switch — clear `STREAM_KEY_FACEBOOK`, Facebook is out — while leaving
  keyless sinks workable.
- **Sinks default to disabled in the committed configuration** (empty URL).
  They are a testing tool; a sink left enabled on the night is a wasted output
  and a confusing status page.
- A disabled output MUST NOT attempt to connect. It MAY remain running and
  idle — see the wrapper in deviation 1 — which is simpler and more robust
  than trying to make the runtime skip a service based on an empty variable.
  What it MUST NOT do is retry, log failures, or report as anything other than
  disabled.
- Outputs are a **fixed set of named slots** — Twitch, YouTube, Facebook and at
  least one sink — declared in the runtime configuration, with data supplying
  each slot's URL and key. Adding a *new* platform is therefore an edit to the
  service definition, which is accepted: it is rare and explicit.
- **Egress MUST support RTMPS**, not only RTMP. Facebook requires it; Twitch
  and YouTube accept it. Confirm each platform's current requirement at
  implementation time rather than trusting this line.
- This constrains the server choice. `nginx-rtmp` cannot push RTMPS natively
  and would need an `stunnel` sidecar per output; a server with native RTMPS
  egress avoids that. Choose with this in mind and record the reasoning in
  `README.md`.
- Disabling MUST be reflected on the status page as **disabled** rather than
  failed.

### Failure isolation

This is the requirement most likely to be got wrong:

- A platform refusing, dropping or stalling its connection MUST NOT interrupt
  ingest from OBS.
- A failing output MUST NOT interrupt the other outputs.
- A failed output SHOULD be retried while ingest continues.
- Loss of an output MUST be visible in logs with the platform named.

What this rules out is **one process multiplexing all outputs** — an `ffmpeg`
`tee` muxer, or equivalent — where a single output failing takes the whole
process down with it.

It does not rule out `ffmpeg` as such. One process per output, supervised and
restarted independently, satisfies it.

**Name the supervisor explicitly.** A hook that fires once per path — MediaMTX's
`runOnReady`, `nginx-rtmp`'s `exec` — starts *a* command, not one per output,
and leaves open who restarts each output when it dies. That gap must not be
filled by the status glue, which deviation 3 keeps out of the broadcast path.

The shape that satisfies this:

- The RTMP server handles ingest only — and recording too, if the recorder is
  not built as a separate consumer (see Archive recording).
- **Each output is its own long-running process** pulling from the server and
  pushing to one platform.
- Supervision belongs to the runtime: under Docker, one compose service per
  output with `restart: always`; under Windows, one NSSM service per output.

This gives failure isolation, retry and a Windows story for free. An
alternative shape is acceptable if it meets the same requirements, but the
implementation MUST state which component restarts a dead output.

**Each output process MUST wait for ingest itself.** It MUST NOT exit when
ingest is absent and rely on the restart policy to try again.

This is not a style preference. Docker's `restart: always` applies exponential
backoff to a container that keeps exiting quickly — doubling from a fraction of
a second, and only resetting once the container has stayed up for a while.
NSSM throttles restarts similarly. An output left idle for the hour before a
broadcast will therefore be sitting at its maximum backoff when you go live,
and could take that long to connect. The same delay applies to recovery after a
mid-show ingest gap, which is worse.

So each output process stays alive across an idle period, polling for ingest —
the server's API, or a short fixed retry — and connects promptly when ingest
appears. The restart policy then covers only real crashes, which is what it is
for. This also removes the log noise: without it, every idle retry writes an
output-failure line naming the platform, and real failures are lost in the
churn.

**A stalled push MUST become an exit.** An output whose platform socket hangs
will otherwise sit there indefinitely — never exiting, never retried, and
reported as connected while nothing is being delivered. This is the failure
mode "stalling" above refers to, and it needs handling explicitly:

- Every output MUST set an I/O timeout on the push side (for `ffmpeg`,
  `-rw_timeout` or equivalent) so a stall terminates `ffmpeg` rather than
  hanging indefinitely. The wrapper then returns to waiting for ingest and
  reconnects — see deviation 1b. The runtime's restart policy is not involved.
- **"Connected" MUST be evidenced by progress, not by attachment.** Appearing
  as a reader on the RTMP server only proves the pull side works. Use the
  output's own byte or frame counters — `ffmpeg`'s `-progress` output, or
  equivalent — and treat a counter that has stopped advancing as failed.
  Without this the status page cannot detect the very failure this section
  exists to catch.

### Availability

- The relay **process** MUST be running before the broadcast begins and MUST
  survive restarts of the producing application. The laptop-side application is
  expected to come and go.
- The service MUST start automatically on host boot.

**What "survives" means here, precisely.** Outgoing platform connections exist
only while a publisher is connected. When OBS disconnects, the outputs drop
too — that is normal and is not a failure to handle.

- The relay MUST remain running through an ingest gap and MUST re-establish
  every enabled output when ingest returns, without manual intervention.
- The relay MUST NOT generate filler or keepalive frames to hold platform
  connections open across a gap. Producing frames is encoding, which the no-GPU
  constraint forbids, and it would put a black screen on air.
- An ingest gap MUST be visible on the status page as "no ingest" rather than
  as three failed outputs.

**The viewer-facing consequence, stated so nobody expects otherwise.** When
ingest drops, the platform outputs drop with it, and viewers see the stream
stop. When ingest returns the relay re-pushes, and each platform treats that as
a new session — which, depending on the platform and the length of the gap, can
end the broadcast, split the VOD, or drop viewers back to an offline channel.

**This build does not smooth it over.** Bridging a gap means generating frames
continuously, which requires full-time transcoding and a GPU this host does not
have — the reasoning is in "Portability", and it is a scope decision rather
than an impossibility. Within this build:

- Do not add reconnect grace periods, buffering, held-open sessions or filler
  in an attempt to hide an ingest drop. Half-measures here do not work, and
  they add delay to every broadcast to serve the exceptional case.
- If bridging is wanted later, read "Portability" first. It is an architecture
  change, not a feature to bolt on.
- Keeping ingest up is the publisher's job and the LAN's, not the relay's.
- Every ingest gap MUST be logged with its start, end and duration, so "did we
  drop, and for how long?" is answerable afterwards. **The status glue owns
  this**, since it is the only component watching ingest state continuously.
  It is observation only and stays off the broadcast path — a crash in the glue
  costs the gap log and the page, never the broadcast.

### Archive recording

The relay keeps the archive copy of each broadcast. The laptop currently
records locally to a disk that is already tight; once this exists, that local
recording becomes a fallback rather than the only copy.

- The relay MUST write a recording of the ingest stream, without re-encoding.
- Recording MUST NOT be able to take ingest or fan-out down. A full disk, a
  write error or a slow volume costs the archive, never the broadcast. This is
  the ranking whenever the two conflict.
- **The recorder SHOULD be another consumer process**, using the same wrapper
  as an output (deviation 1b) with a file as its destination instead of a URL:
  pulling from the server and writing with `-c copy`. Then a full disk kills
  one process by design, rather than depending on the RTMP server handling a
  write error gracefully — which is the kind of assumption that is only tested
  on the night. It also gives recording state the same progress-based evidence
  the outputs use, rather than a separate mechanism.
- **Every ingest session MUST write to a new destination, and nothing may ever
  be overwritten.** The wrapper starts a fresh `ffmpeg` each time ingest
  returns, so a fixed output path means the second session destroys the first:
  a ten-second drop mid-show would leave you with the second half and nothing
  else. Name each session by timestamp, or write a per-session segment set.
  This is the single most destructive failure available to this service, and
  it is silent.
- The storage root, and any retention rule, MUST come from configuration, not
  be hardcoded.
- **The required property is crash survival, not a particular container.** What
  is written MUST remain playable if the process is killed mid-broadcast,
  losing at most the tail. Whatever the chosen server records natively and
  meets that is acceptable: FLV, segmented fMP4 and MPEG-TS all do, losing at
  most the final segment. A single non-segmented MP4 does not, because a
  truncated one is unplayable — do not choose it.
- Any conversion to a friendlier container MUST happen **after** the broadcast
  ends, as a remux with no re-encode, never during.
- The relay has no idea when a broadcast starts, so "check free space
  beforehand" is not something it can implement. Instead: a configurable
  free-space threshold, surfaced as a warning on the status page whenever it is
  breached.

### Status page

A minimal HTML page, served over HTTP on the registry port.

- It MUST show whether ingest is currently arriving, and per configured
  platform the output's state.
- Per-platform state MUST distinguish at least: **disabled** (URL empty, or
  the named key empty), **idle** (no ingest, so nothing to push — not an
  error),
  **connected**, and **failed** (ingest is arriving but this output is not
  working). Collapsing idle and failed into one "not connected" makes the page
  useless for the job it exists to do, because before a broadcast every output
  is legitimately idle.
- State MUST be derived by checking ingest first: with no ingest, every enabled
  output is *idle*, whatever its process is doing. The page is read mostly
  during the idle period before a broadcast, so anything that reports normal
  idling as failure makes it cry wolf exactly when it matters.
- **connected** MUST mean the output's byte counter is advancing, not that a
  process exists or is attached to the server. See Failure isolation.
- It SHOULD show free space at the storage root, whether the configured
  threshold is breached, and whether recording is running.
- Static HTML is sufficient and is what `AGENTS.md` prefers at this scale; do
  not introduce Vue for it.
- `PATTERNS/api.md` applies to any HTTP endpoint the page is built on.
- Platform stream keys MUST NOT be rendered on the page or exposed by any
  endpoint behind it.

It sits behind nginx at `stream-relay.stanley.arpa`, which terminates TLS and
sends `Cache-Control: no-store`. Therefore:

- The service MUST serve plain HTTP on the registry port and MUST NOT manage
  TLS itself.
- The page MUST refresh by **polling** on a timer, not by SSE or WebSocket. The
  proxy is not configured with `proxy_buffering off`, so a streamed response
  would be buffered and the page would appear to hang. Introducing SSE later is
  a coordinated change in the proxy config too, not a free choice here.
- The service SHOULD NOT set its own caching headers; the proxy owns that.

Its purpose is pre-broadcast verification: "is the relay healthy?" should be a
page to open, not logs to read on another host.

**What the page cannot tell you, and what to do instead.** Outputs only connect
once ingest arrives, so an idle page proves the relay is up and configured — it
does not prove any platform will accept the stream. Proving that needs a real
push, which on Twitch or Facebook means going live publicly.

`RECOVERY.md` MUST document a way to verify each platform without broadcasting
to an audience. Confirm the current mechanism for each platform at
implementation time; Twitch's bandwidth-test ingest and an unlisted or private
scheduled stream on YouTube are the usual routes. If a platform has no private
path, say so plainly rather than implying it has been tested.

---

## Deviations from `homelab-standards` defaults

State these in the repository's `README.md` as documented exceptions.

1. **The relay itself is not Python.** `AGENTS.md` prefers Python / venv / `requirements.txt`
   / Flask for new services. This service is a media relay — an existing RTMP
   server or forwarder under configuration, not application code. Do not build
   the relay itself as a Python application.

   **Two pieces of code are pre-approved. Do not stop to ask for either.**

   **(a) Glue for the status page.** Free space, recording state and
   per-platform state will not come out of the RTMP server's own stats
   endpoint in the shape the page needs, so a small script or sidecar that
   reads that endpoint plus the filesystem and serves JSON is expected and in
   scope. Keep it bounded: one process, no database, no framework beyond a
   minimal HTTP server.

   **(b) One small wrapper per output.** Bare `ffmpeg` cannot meet the
   requirements in Failure isolation — it exits immediately when its source is
   not ready, reports progress only on its own terms, and prints the full push
   URL, key included, on error. So each output is `ffmpeg` inside a wrapper
   whose **only** jobs are:

   - idle when disabled, and wait for ingest rather than exiting when enabled;
   - run `ffmpeg` with the configured destination, key and I/O timeout;
   - expose a progress counter the status glue can read;
   - redact the key from anything it logs.

   **The wrapper does not exit when `ffmpeg` exits.** Whether `ffmpeg` ends
   because ingest stopped, the platform dropped it or the I/O timeout fired,
   the wrapper returns to waiting for ingest and runs it again. If the wrapper
   exited instead, every ingest drop would become a container exit and the
   restart backoff this design exists to avoid would be back. The runtime's
   restart policy covers only the wrapper itself crashing.

   The **recorder uses this same wrapper**, with a file as its destination
   rather than a URL (see Archive recording).

   How the progress counter reaches the glue is the implementer's choice, with
   one constraint: a file on a shared volume or a local HTTP endpoint, **not**
   the Docker socket. The glue must not be given `docker.sock` to inspect
   containers.

   It MUST NOT make routing decisions, transform media, or hold state beyond
   what those jobs need. This *is* broadcast-path code, which is why its scope
   is fixed here rather than left to judgement.

   **Python for both**, per the portability rules: a shell loop does not carry
   over to NSSM, and the Windows port is explicitly not ruled out. This is the
   part of the service that genuinely is application code, so the
   `homelab-standards` default language applies to it after all.

2. **`PATTERNS/internal-ca-trust.md` does not govern this service.** Neither leg
   is internal HTTPS. Ingest is plain RTMP on the LAN from OBS; egress is RTMPS
   to public platforms validated against public root CAs. The homelab CA is not
   involved in either direction. This is not a licence to disable TLS
   verification on egress — public roots MUST be verified normally.

   **One scoped exception**, if a TLS test sink is built (see Acceptance):
   certificate verification MAY be disabled for that one sink output, because
   a self-signed local sink cannot validate. It MUST be scoped to that output
   alone and never applied globally. `adler` is also the host that broadcasts
   for real, so this is not a "test host only" rule — the sink is simply
   disabled (empty URL) whenever a real broadcast runs, like every other sink.

3. **Two ports, one of them out of range.** The status page is an ordinary HTTP
   service on a registry port in the homelab range. The RTMP port is the
   declared exception. The service's primary interface is RTMP; the page is
   secondary and MUST NOT become a dependency of the broadcast.

No indexed pattern in `PATTERNS/` covers a media relay. `systemd-service.md` is
the closest by shape if the service ends up running directly under systemd;
consult it in that case. Docker remains the default runtime for Linux hosts per
`AGENTS.md` — pick one and document which in `README.md`.

---

## Secrets

Two different kinds of secret, with different handling.

**Platform stream keys** — Twitch, YouTube, Facebook.

- Each is **optional**. An empty value means that platform is disabled for now,
  which is the per-broadcast on/off switch described under Fan-out.
- They are real credentials: anyone holding one can broadcast as Jake.
- They MUST NOT appear in logs, on the status page, or in any status endpoint,
  including inside RTMP URLs on error paths. Redact before logging. Some
  servers log the whole push URL on failure — this is checked in Acceptance.

**The ingest key** — shared between the relay and batw, which sends it from
`STREAM_KEY_LIVE`.

- It is required, and the relay checks it.
- It is a LAN shared secret, not a platform credential. The worst an attacker
  on the LAN can do with it is publish to the relay.
- It is therefore **acceptable for it to appear in logs**, which matters
  because some servers carry the stream key as the path name and will then name
  it in every line about the stream. Do not contort the design to hide it, and
  do not confuse it with a platform key when applying the rule above.

Both kinds:

- MUST come from environment configuration and MUST NOT be committed.
- `.env.example` MUST list every variable with placeholder values, marking
  which are optional.
- `.gitignore` MUST exclude `.env` and `.env.*`.

---

## Repository structure

Per `AGENTS.md`, unless documented otherwise:

```
.
├── docker-compose.yml        # if Docker-based
├── .env.example
├── .gitignore
├── scripts/
│   └── up.sh
├── README.md
├── RECOVERY.md
```

`scripts/up.sh` is the canonical entrypoint and MUST be executable and
idempotent. Startup logic MUST NOT live only in README instructions.

`README.md` MUST state what the service does, how it is run, that ingress and
ports are defined in `homelab-infra`, and the three deviations above.

`RECOVERY.md` matters more here than for most services, because the failure is
time-boxed: a broadcast happens once, at a fixed time, and a relay that is down
cannot be fixed afterwards. It MUST cover prerequisites, recovery order, and
how to verify the relay is accepting ingest and reaching each platform
**before** a broadcast rather than during one.

It MUST also state the bail-out: **the publisher can bypass the relay
entirely.** batw keeps a `twitch` target pointing at Twitch's own ingest, so
`./batw.sh --target twitch` goes ahead without this service at all, losing the
other platforms and the archive copy. The relay is therefore not a single point
of failure for the show, and recovery under time pressure should say so plainly
rather than inviting debugging minutes before a broadcast.

---

## Portability — Windows is not ruled out

Decided 2026-09-23: **build this on Linux.** That host is stable, and stream
copy needs no GPU, so the GPU on the Windows box buys the service as specified
precisely nothing.

It is deferred, not rejected. Two things would reopen it, in this order.

**1. Blackout continuity — the stronger driver.** When ingest drops, viewers
see the stream stop (see Availability). Holding the platform connections open
against a black or holding slate instead would mean viewers see a caption
rather than an offline channel. That is wanted; it is out of scope here
because of what it costs, not because it lacks value.

The cost is not the black frames — encoding static black at 1080p60 is well
under a core even in software. The cost is that **filler forces the relay to
transcode all the time.** A platform output is one continuous stream with
consistent codec parameters and monotonic timestamps, so the relay cannot copy
bytes while ingest is present and generate frames when it is not. It must
decode and re-encode continuously, then swap its input to black during a gap.

That turns a near-free stream copy into a permanent 1080p60 encode:

- On a CPU-only host, several cores at a fast preset, plus latency and a
  generation of quality loss — reintroducing on this host exactly the cost the
  service exists to move off the laptop.
- On a GPU host, one hardware encode session, which is trivial. All platforms
  share a single rendition, so it is one encode, not three.

So blackout continuity and per-platform renditions want the same architecture,
and neither is reachable without a GPU. **Treat "add a slate" as an
architecture change, not a feature**, whenever it is next raised.

There is a middle option — a pre-encoded black loop whose codec parameters are
byte-identical to the publisher's output, spliced at container level without
transcoding. It works, and any encoder setting change on the publisher breaks
it silently. Not suitable for a service whose failure mode is a broadcast.

Before building any of it, confirm how long a gap each platform actually
tolerates. Some resume the same broadcast after a short reconnect, which may
already cover the realistic case of a publisher restarting.

**2. Rendition quality.** A single rendition capped at the most restrictive
platform visibly costing quality on the others. Judge that by watching a real
broadcast — the video montage rather than the playing — not by predicting it.
Two cheaper answers come first: disable Facebook for broadcasts where quality
matters (a config change, per the empty-key rule above), or accept the cap.

If it does move to Windows, `AGENTS.md` requires a different shape: NSSM with
`scripts/install-service.ps1`, `scripts/up.ps1`, an idempotent inbound firewall
rule, and no Docker. nginx would also no longer be co-located with the relay,
which reopens the host-and-vhost arrangement recorded under Ingest.

**Therefore, where it is free to do so, do not paint this into a Linux
corner:**

- **Prefer a relay with first-class Windows builds.** This is the choice that
  decides whether a port is a weekend or a rewrite. MediaMTX ships Windows
  binaries; `nginx-rtmp` effectively does not. Record the Windows story of
  whatever is chosen in `README.md`, even though Linux is the target today.
- Platform list, keys and storage root MUST come from environment or a config
  file that is not itself Docker-specific, so the same configuration can drive
  a non-containerised run.
- The status glue MUST NOT shell out to Linux-only tools for free space or
  process state. Use the language's own APIs.
- No systemd-specific logic inside the service. Supervision belongs to the
  runtime layer, not the code.
- No hardcoded POSIX paths.

None of this justifies abstraction layers or a compatibility shim. It is a
short list of things not to do, not a feature.

---

## Non-goals

- Per-platform transcoding or renditions.
- Anything beyond a **read-only** status page. No mutating endpoints, no
  dashboard framework, no build step, no authentication UI. Per-platform
  on/off is achieved by clearing a key in configuration, not by a control on
  the page.
- Any change to the producing application beyond the endpoint address.
- Editing, trimming or publishing the archive recording. It writes a file.
- Authentication on ingest beyond checking the configured ingest key. No user
  accounts, tokens or signed URLs — ingest is LAN-only. Checking the key is in
  scope and is required; see Ingest.
- Exposure outside the LAN. Neither the RTMP port nor the status page is
  reachable from the internet.

---

## Decisions on record

No open questions remain. Recorded below is the reasoning behind the one that
stayed open longest, so it is not re-litigated.

### Why ingest is not proxied

Settled 2026-09-23. nginx runs on the relay host, so both names resolve to that
box: the status page through nginx on `stream-relay.stanley.arpa`, RTMP
straight to the relay on 1935 via `adler.stanley.arpa`. See Ingest for why the
publisher names the host rather than the vhost.

Routing ingest through the *other* nginx — the one fronting `stream-ipad` and
`stream-editor` — was considered and rejected. It is technically possible with
`ngx_stream` as a plain TCP proxy (**not** `nginx-rtmp`, which would mean a
second RTMP server re-publishing the stream), but it buys nothing here:

- It adds a hop and a second box to the most critical path in the system. If
  that proxy is down or restarting, the broadcast cannot start, for no gain.
- It adds no TLS. RTMP over a TCP proxy is still plaintext; ingest is LAN-only
  and does not need it.
- The publisher is a single machine on the same flat LAN, so there is nothing
  to consolidate — one publisher, one destination.

A TCP proxy in front of ingest would only earn its place if OBS could not reach
the relay host directly, for example across a segmented network. That is not
the case, and if it becomes the case the answer is `ngx_stream`, not
`nginx-rtmp`.

---

## Acceptance

Each of these names how it is checked, because several of them pass by
inspection and fail in practice.

**Most of it runs against local sinks, not the platforms.** You cannot drop a
Twitch connection on demand, and what a platform serves back has been through
its own transcode, so probing it proves nothing about what was sent.

**Two sinks are required**, both configured exactly as platform outputs so the
path under test is the real one: a second path on the same server, or
`ffmpeg -rtmp_listen 1`. Two, not one — with a single sink, killing it cannot
show that the *other* outputs kept running, which is the whole point of the
isolation test.

### Against the sinks

- **Nothing is re-encoded.** `ffprobe` ingest and a sink: codec, profile,
  level, resolution and framerate match exactly; bitrate agrees within
  measurement tolerance, since it is sampled over time. Low CPU is
  corroboration, not proof.
- **Isolation.** Kill sink A. Ingest continues, sink B keeps receiving
  throughout, and the failure is logged naming the output. Restore sink A and
  it recovers unaided.
- **Stall detection.** Make a sink hang rather than close — pause its process,
  or firewall its port. The output must time out and be retried by the
  wrapper (not a container restart — see deviation 1b), and the
  status page must show it as **failed**, not connected. An output that sits
  there reported as healthy while delivering nothing is the failure this whole
  design is built against.
- **Go-live latency after a long idle.** Leave the relay idle for at least an
  hour, then start publishing. Every enabled output must connect within a few
  seconds. This is the check that catches restart backoff: an implementation
  that exits and relies on its restart policy will pass every other test here
  and still take a minute to come up when it matters.
- **Publisher restart.** Stop and restart the publisher: every enabled output
  re-establishes unaided, and the page reads "no ingest" in between rather than
  showing failures. **Both sessions' recordings must exist afterwards** —
  nothing overwritten. This is the check for the worst bug available here, and
  it passes silently if you only look at whether a file was written.
- **Recording.** A file is written; filling the storage volume mid-broadcast
  stops the recording without interrupting ingest or fan-out. Use a small
  loopback or tmpfs mount as the storage root — do not fill the real disk to
  find out. The written file is playable after an abrupt kill.

### Against the platforms

These cannot be done locally and use the private routes documented in
`RECOVERY.md` rather than broadcasting to an audience.

- A stream published to `rtmp://adler.stanley.arpa/live` with the configured
  ingest key reaches each enabled platform.
- **RTMPS egress works.** The local sinks are plain RTMP, so nothing above
  exercises TLS on the push side. Either verify RTMPS through the private
  platform routes, or build a TLS sink — MediaMTX with a self-signed
  certificate, with verification disabled for that one output under the scoped
  exception in deviation 2.

### Independent of both

- A publish with the wrong ingest key is rejected.
- The status page distinguishes disabled, idle, connected and failed, and is
  reachable at the registry HTTP port.
- A disabled output attempts no connection, logs no failures and shows as
  disabled. **Check this across a reboot**, not just after clearing the value:
  the failure worth catching is an output that comes back and starts pushing
  because the runtime restarted it from an earlier state.
- Restarting the host brings the relay back without manual intervention.
- **No platform key leaks.** Force an output failure and read the logs: some
  servers log the whole push URL, key included, on error. Check the committed
  tree, the service logs and every status endpoint. The ingest key is exempt —
  see Secrets.
- `scripts/up.sh` is safe to re-run against a running service.
