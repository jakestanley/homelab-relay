"""Acceptance checks from prompts/init.md, run locally against test sinks, never
against a platform.

    python test/acceptance.py [--encoder h264_nvenc|libx264|copy] [--small]
                              [--idle SECONDS] [--full-disk-dir PATH] [--public-tls]

Runs the relay's own processes (MediaMTX, every output wrapper, the recorder,
the status page) exactly as relay.config compiles them for NSSM, but as
children of this script, on spare ports (RTMP 11935, status 20940, API 19997),
beside two throwaway MediaMTX sinks. So it can run on shrike next to the real
relay, and on any host with Python, ffmpeg and MediaMTX.

- On shrike: run from the repo's .venv with the defaults (h264_nvenc, the
  committed renditions). That is the run a show depends on.
- Elsewhere: --encoder libx264 --small. Same code path, smaller numbers.
  A smoke test, not a substitute for the shrike run.
- A stream-copy host (VIDEO_ENCODER=copy, the adler fallback): --encoder
  copy. Every sink must then receive ingest untouched, and no bitrate
  ceiling applies (the publisher sets it).

Nothing here contacts a platform: the "twitch" target is unresolvable and
"youtube" is a local sink. Only the opt-in --public-tls reaches one.

The harness stands in for NSSM: where a check needs a crashed process
restarted, the harness restarts it. NSSM's own behaviour (boot start, restart
after a crash, up.ps1 against a running relay) is a manual list in
TESTING.md.

Prints PASS/FAIL/SKIP per check with measured numbers, writes the same to
test/reports/ (not committed), and exits non-zero if anything failed.
"""

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

from relay.config import EXE, check_ingest_key, load  # noqa: E402

RTMP_PORT, HTTP_PORT, API_PORT = 11935, 20940, 19997
SINKS = {"sink-a": (19351, 19361), "sink-b": (19352, 19362)}  # rtmp, rtmps
INGEST_KEY = "Acc3pt_ingest-key.v1"  # every character class the key check allows
# Fake platform keys: no log, endpoint or state file may ever contain these.
KEY_TW = "live_ACCEPTLEAK_tw1?bandwidthtest=true"
KEY_YT = "ACCEPTLEAK-yt2-xxxx"
LEAK = re.compile(r"ACCEPTLEAK|" + re.escape(INGEST_KEY))


# -- processes ------------------------------------------------------------------


class Proc:
    def __init__(self, name, cmd, env, log_dir):
        self.name, self.cmd, self.env = name, cmd, env
        self.log_path = os.path.join(log_dir, name + ".log")
        self.popen = None

    def start(self):
        full_env = dict(os.environ)
        full_env.update(self.env)
        log = open(self.log_path, "ab")
        self.popen = subprocess.Popen(
            self.cmd, env=full_env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL
        )
        log.close()
        return self

    def alive(self):
        return self.popen is not None and self.popen.poll() is None

    def kill(self):
        if self.alive():
            self.popen.kill()
            self.popen.wait()

    def stop(self):
        if self.alive():
            self.popen.terminate()
            try:
                self.popen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.kill()

    def suspend(self):
        if os.name == "nt":
            import ctypes

            ctypes.windll.ntdll.NtSuspendProcess(int(self.popen._handle))
        else:
            os.kill(self.popen.pid, signal.SIGSTOP)

    def resume(self):
        if os.name == "nt":
            import ctypes

            ctypes.windll.ntdll.NtResumeProcess(int(self.popen._handle))
        else:
            os.kill(self.popen.pid, signal.SIGCONT)

    def log(self):
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""


# -- the run --------------------------------------------------------------------


class Run:
    def __init__(self, args):
        self.args = args
        self.work = tempfile.mkdtemp(prefix="relay-accept-")
        self.rec = os.path.join(self.work, "rec")
        self.state_dir = os.path.join(self.work, "state")
        self.logs = os.path.join(self.work, "logs")
        for d in (self.rec, self.state_dir, self.logs):
            os.makedirs(d)
        os.makedirs(os.path.join(ROOT, "test", "reports"), exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.report = os.path.join(ROOT, "test", "reports", "{}-{}.txt".format(stamp, args.encoder))
        self.fails = 0
        self.procs = {}
        self.publisher = None

        tools = os.path.join(ROOT, "tools")
        self.ffmpeg = args.ffmpeg or _found(os.path.join(tools, "ffmpeg", "bin", "ffmpeg" + EXE)) or shutil.which("ffmpeg")
        self.ffprobe = os.path.join(os.path.dirname(self.ffmpeg), "ffprobe" + EXE) if self.ffmpeg else None
        if self.ffprobe and not os.path.exists(self.ffprobe):
            self.ffprobe = shutil.which("ffprobe")
        self.mediamtx = args.mediamtx or _found(os.path.join(tools, "mediamtx", "mediamtx" + EXE)) or shutil.which("mediamtx")
        if not (self.ffmpeg and self.ffprobe and self.mediamtx):
            raise SystemExit("need ffmpeg, ffprobe and mediamtx: run scripts/fetch-tools.ps1, or pass --ffmpeg/--mediamtx")

    # -- reporting ---------------------------------------------------------------

    def log(self, line=""):
        print(line, flush=True)
        with open(self.report, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def check(self, ok, name, detail=""):
        self.log("{}  {}{}".format("PASS" if ok else "FAIL", name, "  ({})".format(detail) if detail else ""))
        if not ok:
            self.fails += 1
        return ok

    def skip(self, name, why):
        self.log("SKIP  {}  ({})".format(name, why))

    # -- configuration -----------------------------------------------------------

    def config(self, record_dir=None):
        """relay.config compiled for this run: same code as up.ps1 uses."""
        env_path = os.path.join(self.work, ".env")
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write(
                "\n".join(
                    [
                        "SERVICE_PORT={}".format(HTTP_PORT),
                        "RTMP_PORT={}".format(RTMP_PORT),
                        "RELAY_API_PORT={}".format(API_PORT),
                        "INGEST_KEY={}".format(INGEST_KEY),
                        "RECORD_DIR={}".format(record_dir or self.rec),
                        "RECORD_MIN_FREE_GB=0.01",
                        "RELAY_STATE_DIR={}".format(self.state_dir),
                        "VIDEO_ENCODER={}".format(self.args.encoder),
                        "FFMPEG_EXE={}".format(self.ffmpeg),
                        "MEDIAMTX_EXE={}".format(self.mediamtx),
                        "OUTPUT_IO_TIMEOUT=10",
                        # Unresolvable: a platform that cannot be reached.
                        "TWITCH_URL=rtmp://nonexistent.invalid/app",
                        "STREAM_KEY_TWITCH={}".format(KEY_TW),
                        # A self-signed peer: platforms must refuse it.
                        "YOUTUBE_URL=rtmps://127.0.0.1:{}/yt".format(SINKS["sink-b"][1]),
                        "STREAM_KEY_YOUTUBE={}".format(KEY_YT),
                        # No key: disabled.
                        "FACEBOOK_URL=rtmps://live-api-s.facebook.com:443/rtmp",
                        "STREAM_KEY_FACEBOOK=",
                        "SINK_A_URL=rtmp://127.0.0.1:{}/sink".format(SINKS["sink-a"][0]),
                        "SINK_B_URL=rtmps://127.0.0.1:{}/sink".format(SINKS["sink-b"][1]),
                        "SINK_B_TLS_VERIFY=false",
                    ]
                )
                + "\n"
            )
        config_path = os.path.join(ROOT, "relay.yaml")
        if self.args.small:
            # Same slots and keyframe rules, smaller pictures and bitrates,
            # so a CPU-only host keeps up.
            with open(config_path, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            for r in doc["renditions"].values():
                scale = 640 / r["width"]
                r.update(width=640, height=int(r["height"] * scale) // 2 * 2, fps=30)
                r["video_kbps"] = max(800, r["video_kbps"] // 4)
            config_path = os.path.join(self.work, "relay.yaml")
            with open(config_path, "w", encoding="utf-8") as fh:
                yaml.safe_dump(doc, fh)
        return load(env_path, config_path)

    def start(self, cfg):
        for svc in cfg.services("accept", sys.executable):
            name = svc["name"].split("-", 1)[1]
            self.procs[name] = Proc(name, [svc["exe"]] + svc["args"], svc["env"], self.logs).start()
        sink_dir = os.path.join(ROOT, "test", "sink")
        for name, (rtmp, rtmps) in SINKS.items():
            env = {
                "MTX_RTMPADDRESS": "127.0.0.1:{}".format(rtmp),
                "MTX_RTMPSADDRESS": "127.0.0.1:{}".format(rtmps),
                "MTX_RTMPSERVERKEY": os.path.join(sink_dir, "server.key"),
                "MTX_RTMPSERVERCERT": os.path.join(sink_dir, "server.crt"),
            }
            self.procs[name] = Proc(name, [self.mediamtx, os.path.join(sink_dir, "mediamtx.yml")], env, self.logs).start()

    def restart(self, name, env=None):
        proc = self.procs[name]
        proc.kill()
        if env is not None:
            proc.env = env
        proc.start()

    def stop_all(self):
        self.unpublish()
        for proc in self.procs.values():
            proc.stop()

    # -- status page ---------------------------------------------------------------

    def status(self):
        try:
            with urllib.request.urlopen("http://127.0.0.1:{}/api/status".format(HTTP_PORT), timeout=2) as resp:
                return json.load(resp)
        except (OSError, ValueError):
            return None

    def output(self, name):
        s = self.status()
        if not s:
            return {}
        for o in s["outputs"] + [s["recording"]]:
            if o["name"] == name:
                return o
        return {}

    def state(self, name):
        if name == "ingest":
            s = self.status()
            return s["ingest"]["state"] if s else "unreachable"
        return self.output(name).get("state", "unreachable")

    def states(self, *names):
        return " ".join("{}={}".format(n, self.state(n)) for n in names)

    def set_live(self, armed):
        """Flip the go-live switch through the status page, as the page does."""
        req = urllib.request.Request(
            "http://127.0.0.1:{}/api/live".format(HTTP_PORT),
            data=json.dumps({"armed": armed}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp)

    def wait_for(self, seconds, predicate):
        """Seconds taken, or None on timeout."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            if predicate():
                return round(time.monotonic() - t0, 1)
            time.sleep(0.25)
        return None

    def connected(self, *names):
        return lambda: all(self.state(n) == "connected" for n in names)

    # -- publishing and probing -------------------------------------------------

    def publish_cmd(self, key, seconds=3600):
        """The test publisher. Full size, it sends what batw sends on show day
        (1080p60, 30000 kbps video, 320 kbps 48 kHz stereo audio: batw
        ROADMAP, decided 2026-10-06), encoded with NVENC on shrike as OBS
        does with VideoToolbox."""
        if self.args.small:
            size, rate, video, enc = "1280x720", 30, "2500k", ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high"]
        elif self.args.encoder == "h264_nvenc":
            size, rate, video, enc = "1920x1080", 60, "30000k", ["-c:v", "h264_nvenc", "-preset", "p4", "-profile:v", "high"]
        else:
            size, rate, video, enc = "1920x1080", 60, "30000k", ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high"]
        audio = ["-c:a", "aac", "-b:a", "128k"] if self.args.small else ["-c:a", "aac", "-b:a", "320k", "-ac", "2"]
        return (
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-re",
             "-f", "lavfi", "-i", "testsrc2=size={}:rate={}".format(size, rate),
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate={}".format(44100 if self.args.small else 48000)]
            + enc
            + ["-g", str(rate * 2), "-b:v", video, "-maxrate", video, "-bufsize", video]
            + audio
            + ["-t", str(seconds),
               "-f", "flv", "rtmp://127.0.0.1:{}/live/{}".format(RTMP_PORT, key)]
        )

    def publish(self):
        self.publisher = Proc("publisher", self.publish_cmd(INGEST_KEY), {}, self.logs).start()

    def unpublish(self):
        if self.publisher:
            self.publisher.stop()
            self.publisher = None

    def probe(self, url):
        out = subprocess.run(
            [self.ffprobe, "-v", "error", "-rw_timeout", "10000000",
             "-show_entries", "stream=codec_type,codec_name,profile,width,height,r_frame_rate,sample_rate,channels",
             "-of", "json", url],
            capture_output=True, text=True, timeout=30,
        )
        try:
            return json.loads(out.stdout).get("streams", [])
        except ValueError:
            return []

    def sample(self, url, seconds=10):
        """(video kbit/s, keyframe intervals in s) over `seconds` of a live stream."""
        out = subprocess.run(
            [self.ffprobe, "-v", "error", "-rw_timeout", "10000000", "-read_intervals", "%+{}".format(seconds),
             "-select_streams", "v:0", "-show_entries", "packet=pts_time,size,flags", "-of", "csv=p=0", url],
            capture_output=True, text=True, timeout=seconds + 30,
        )
        times, total, keys = [], 0, []
        for line in out.stdout.splitlines():
            parts = line.split(",")
            if len(parts) < 3 or parts[0] in ("", "N/A"):
                continue
            t = float(parts[0])
            times.append(t)
            total += int(parts[1])
            if "K" in parts[2]:
                keys.append(t)
        span = (max(times) - min(times)) if len(times) > 1 else 0
        kbps = round(total * 8 / span / 1000) if span else 0
        intervals = [round(b - a, 2) for a, b in zip(keys, keys[1:])]
        return kbps, intervals

    def versions(self):
        def first(cmd):
            try:
                return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
            except (OSError, IndexError, subprocess.SubprocessError):
                return "unknown"

        gpu = first(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
        rev = first(["git", "-C", ROOT, "describe", "--always", "--dirty", "--tags"])
        return [
            "git: " + rev,
            "ffmpeg: {} ({})".format(first([self.ffmpeg, "-version"]), self.ffmpeg),
            "mediamtx: {} ({})".format(first([self.mediamtx, "--version"]), self.mediamtx),
            "encoder: {}{}".format(self.args.encoder, " --small" if self.args.small else ""),
            "gpu: " + gpu,
        ]


def _shape(streams):
    """What a probe says about a stream, minus what a remux may change."""
    return sorted(
        (s.get("codec_type"), s.get("codec_name"), s.get("profile"), s.get("width"), s.get("height"), s.get("sample_rate"), s.get("channels"))
        for s in streams
    )


def _found(path):
    return path if os.path.exists(path) else None


def _port_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


# -- the checks -----------------------------------------------------------------


def run_checks(r):
    a = r.args
    r.log("homelab-relay acceptance, {}, host {}".format(datetime.now(timezone.utc).isoformat(timespec="seconds"), socket.gethostname()))
    for line in r.versions():
        r.log(line)
    r.log()

    busy = [p for p in (RTMP_PORT, HTTP_PORT, API_PORT) + tuple(x for pair in SINKS.values() for x in pair) if not _port_free(p)]
    if busy:
        r.check(False, "test ports free", "in use: {}".format(busy))
        return

    cfg = r.config()
    r.start(cfg)
    reported = r.wait_for(30, lambda: r.status() and all(
        o["state"] != "unknown" for o in r.status()["outputs"] + [r.status()["recording"]]))
    if not r.check(reported is not None, "relay processes start and report"):
        return
    renditions = {o["name"]: cfg.output_env(o) for o in cfg.outputs}

    # -- ingest key ------------------------------------------------------------
    bad = [k for k in ("base64pad==", "with+plus", "with/slash", "with space", "") if check_ingest_key(k) is None]
    r.check(not bad and check_ingest_key(INGEST_KEY) is None,
            "ingest key check refuses = + / space empty, accepts letters digits _ . -")

    # -- before ingest ---------------------------------------------------------
    names = ("twitch", "youtube", "facebook", "sink-a", "sink-b", "recorder")
    want = "ingest=no_ingest twitch=idle youtube=idle facebook=disabled sink-a=idle sink-b=idle recorder=idle"
    got = r.states("ingest", *names)
    r.check(got == want, "no ingest: enabled outputs idle, keyless platform disabled", got)

    wrong = subprocess.run(r.publish_cmd("wrong-key", seconds=2), capture_output=True, text=True, timeout=30)
    r.check(wrong.returncode != 0 and "authentication failed" in (wrong.stderr + wrong.stdout).lower(),
            "wrong ingest key rejected", "exit {}".format(wrong.returncode))

    if a.idle:
        r.log("idling {}s before go-live".format(a.idle))
        time.sleep(a.idle)
    time.sleep(5)
    errs = [line for line in r.procs["mediamtx"].log().splitlines() if " ERR " in line]
    r.check(not errs, "idle: no ERR lines in the mediamtx log", "{} ERR lines".format(len(errs)))

    # -- record only, then go live --------------------------------------------------
    # The switch starts on record only: ingest arrives, the recorder records,
    # and no push output starts.
    pushes = ("twitch", "youtube", "sink-a", "sink-b")
    r.publish()
    t = r.wait_for(30, r.connected("recorder"))
    r.check(t is not None, "record only: recorder records when ingest arrives",
            "{}s from publisher start".format(t) if t else r.states("recorder"))
    time.sleep(4)
    got = r.states(*pushes)
    sent = sum(r.output(n).get("runs", 0) for n in pushes)
    r.check(all(r.state(n) == "standby" for n in pushes) and sent == 0,
            "record only: every push output on standby, none started", "{}; {} runs".format(got, sent))

    switched = r.set_live(True)
    t = r.wait_for(30, r.connected("sink-a", "sink-b"))
    r.check(switched.get("armed") is True and t is not None,
            "go live (switch on, after {}s idle): sinks connected".format(a.idle),
            "{}s from the switch".format(t) if t else r.states("sink-a", "sink-b"))
    time.sleep(3)

    # -- renditions -------------------------------------------------------------
    sink_url = {
        "sink-a": "rtmp://127.0.0.1:{}/sink".format(SINKS["sink-a"][0]),
        "sink-b": "rtmp://127.0.0.1:{}/sink".format(SINKS["sink-b"][0]),
    }
    ingest_url = "rtmp://127.0.0.1:{}/live/{}".format(RTMP_PORT, INGEST_KEY)
    copying = a.encoder == "copy"
    for sink in ("sink-a", "sink-b"):
        env = renditions[sink]
        if copying:
            # A copy host: the sink must carry exactly what was ingested.
            got, want = _shape(r.probe(sink_url[sink])), _shape(r.probe(ingest_url))
            r.check(bool(want) and got == want, "{} receives ingest untouched (copy host)".format(sink), str(got))
            _, keys = r.sample(sink_url[sink])
            r.check(bool(keys) and all(abs(k - 2) <= 0.1 for k in keys),
                    "{} keeps the publisher's 2s keyframes".format(sink), "intervals {}".format(keys))
            continue
        streams = r.probe(sink_url[sink])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
        fps = video.get("r_frame_rate", "0/1").split("/")
        fps = round(int(fps[0]) / max(1, int(fps[1])), 2) if len(fps) == 2 else 0
        shape = "{}x{} {}fps {} {}, audio {} {}Hz {}ch".format(
            video.get("width"), video.get("height"), fps, video.get("codec_name"), video.get("profile"),
            audio.get("codec_name"), audio.get("sample_rate"), audio.get("channels"))
        r.check(
            video.get("codec_name") == "h264"
            and (video.get("width"), video.get("height")) == (int(env["OUTPUT_WIDTH"]), int(env["OUTPUT_HEIGHT"]))
            and fps == int(env["OUTPUT_FPS"])
            and audio.get("codec_name") == "aac" and audio.get("sample_rate") == "48000" and audio.get("channels") == 2,
            "{} receives its rendition ({})".format(sink, r.output(sink).get("rendition")), shape)
        kbps, keys = r.sample(sink_url[sink])
        target = int(env["OUTPUT_VIDEO_KBPS"])
        # The ceiling is what a platform enforces, so over is a failure;
        # under is reported (an encoder may undershoot on simple content).
        r.check(0 < kbps <= target * 1.10, "{} video bitrate within its ceiling".format(sink),
                "{} kbit/s over 10s, target {}".format(kbps, target))
        gop = float(env["OUTPUT_KEYFRAME_SECONDS"])
        r.check(bool(keys) and all(abs(k - gop) <= 0.1 for k in keys),
                "{} keyframe every {:g}s".format(sink, gop), "intervals {}".format(keys))

    # Every output must have connected once and stayed connected. A reader
    # MediaMTX drops as "too slow" while it starts reconnects, which a
    # platform shows viewers as the stream stopping (writeQueueSize).
    runs = {n: r.output(n).get("runs", 0) for n in ("sink-a", "sink-b", "recorder")}
    r.check(all(v == 1 for v in runs.values()), "go-live is clean: no output reconnected", str(runs))

    ingest_streams = r.probe(ingest_url)
    newest = max((os.path.join(r.rec, f) for f in os.listdir(r.rec) if f.endswith(".ts")), key=os.path.getmtime, default=None)
    rec_streams = r.probe(newest) if newest else []
    r.check(bool(ingest_streams) and _shape(ingest_streams) == _shape(rec_streams),
            "recording is a copy of ingest (not re-encoded)", str(_shape(rec_streams)))

    # -- platform-shaped failures -------------------------------------------------
    time.sleep(5)
    r.check(r.state("twitch") == "failed" and r.state("youtube") == "failed",
            "failing platforms show failed while ingest is live",
            "twitch: {}; youtube: {}".format(r.output("twitch").get("detail"), r.output("youtube").get("detail")))
    # ffmpeg's GnuTLS backend (Gyan's Windows build, Debian, Ubuntu) says this.
    r.check("Peer certificate failed verification" in r.procs["out-youtube"].log(),
            "TLS verified on platform outputs (self-signed peer refused)")
    r.check(r.connected("sink-a", "sink-b", "recorder")(), "sinks unaffected by failing platforms")

    # -- isolation ------------------------------------------------------------------
    b0 = r.output("sink-b").get("bytes", 0)
    r.procs["sink-a"].kill()
    time.sleep(10)
    b1 = r.output("sink-b").get("bytes", 0)
    r.check(r.state("ingest") == "live" and r.state("sink-b") == "connected" and b1 > b0 and r.publisher.alive(),
            "isolation: sink-a killed, ingest and sink-b continue", "sink-b {} -> {} bytes".format(b0, b1))
    r.check(r.state("sink-a") == "failed" and "output sink-a failed while ingest is live" in r.procs["out-sink-a"].log(),
            "killed output shows failed and is logged by name", r.states("sink-a"))
    wrapper_pid = r.procs["out-sink-a"].popen.pid
    r.procs["sink-a"].start()
    t = r.wait_for(45, r.connected("sink-a"))
    r.check(t is not None, "sink-a recovers unaided", "{}s after restore".format(t) if t else r.states("sink-a"))

    # -- stall ------------------------------------------------------------------------
    runs0 = r.output("sink-a").get("runs", 0)
    r.procs["sink-a"].suspend()
    t = r.wait_for(60, lambda: r.state("sink-a") == "failed")
    r.check(t is not None, "stalled output shows failed, not connected", "{}s after sink froze".format(t) if t else r.states("sink-a"))
    t = r.wait_for(60, lambda: r.output("sink-a").get("runs", 0) > runs0)
    r.check(t is not None and r.procs["out-sink-a"].alive() and r.procs["out-sink-a"].popen.pid == wrapper_pid,
            "stall ended by the wrapper and retried (wrapper not restarted)", "new run {}s after failed".format(t))
    r.procs["sink-a"].resume()
    t = r.wait_for(60, r.connected("sink-a"))
    r.check(t is not None, "stalled output recovers", "{}s after unfreeze".format(t) if t else r.states("sink-a"))

    # -- publisher restart --------------------------------------------------------
    r.unpublish()
    if r.check(r.wait_for(5, lambda: r.state("ingest") == "no_ingest") is not None, "ingest gap detected"):
        time.sleep(2)
        got = r.states("sink-a", "sink-b", "recorder", "twitch")
        r.check(got == "sink-a=idle sink-b=idle recorder=idle twitch=idle",
                "ingest gap reads no_ingest, outputs idle (not failed)", got)
    time.sleep(3)
    files = sorted(f for f in os.listdir(r.rec) if f.endswith(".ts"))
    first = os.path.join(r.rec, files[0]) if files else None
    first_size = os.path.getsize(first) if first else -1
    r.publish()
    t = r.wait_for(30, r.connected("sink-a", "sink-b", "recorder"))
    r.check(t is not None, "every output re-established after publisher restart",
            "{}s from publisher start".format(t) if t else r.states("sink-a", "sink-b", "recorder"))
    files = sorted(f for f in os.listdir(r.rec) if f.endswith(".ts"))
    r.check(len(files) >= 2 and first and os.path.getsize(first) == first_size,
            "both sessions recorded, first file untouched", "{} files".format(len(files)))
    gaps = ""
    try:
        with open(os.path.join(r.rec, "ingest-gaps.log"), encoding="utf-8") as fh:
            gaps = fh.read()
    except OSError:
        pass
    r.check("duration=" in gaps, "gap logged with start, end, duration", gaps.strip().splitlines()[-1] if gaps.strip() else "")

    # -- crash: the recorder's wrapper killed outright --------------------------------
    time.sleep(5)
    current = max((os.path.join(r.rec, f) for f in os.listdir(r.rec) if f.endswith(".ts")), key=os.path.getmtime)
    r.procs["recorder"].kill()
    time.sleep(3)
    size = os.path.getsize(current)
    time.sleep(3)
    r.check(os.path.getsize(current) == size, "ffmpeg dies with a killed wrapper (no orphan writer)",
            "{} stable at {} bytes".format(os.path.basename(current), size))
    probe = r.probe(current)
    decoded = subprocess.run([r.ffmpeg, "-v", "error", "-i", current, "-f", "null", "-"], capture_output=True, timeout=300)
    r.check(bool(probe) and decoded.returncode == 0, "recording playable after the recorder was killed", os.path.basename(current))
    n = len(files)
    r.procs["recorder"].start()  # what NSSM does after a crash
    t = r.wait_for(30, lambda: len([f for f in os.listdir(r.rec) if f.endswith(".ts")]) > n)
    r.check(t is not None, "restarted recorder writes a new file", "{}s after restart (by the harness, standing in for NSSM)".format(t))

    # -- back to record only, mid-stream ------------------------------------------------
    rec0 = r.output("recorder").get("bytes", 0)
    r.set_live(False)
    t = r.wait_for(15, lambda: all(r.state(n) == "standby" for n in ("sink-a", "sink-b")))
    time.sleep(3)
    rec1 = r.output("recorder").get("bytes", 0)
    stopped_on_purpose = all((r.output(n).get("last_exit") or {}).get("reason") == "switched to record only"
                             for n in ("sink-a", "sink-b"))
    r.check(t is not None and stopped_on_purpose and r.state("ingest") == "live" and rec1 > rec0,
            "switch to record only mid-stream: pushes stop as standby (not failed), recording continues",
            "{}s; {}; recorder {} -> {} bytes".format(t, r.states("sink-a", "sink-b"), rec0, rec1))
    r.set_live(True)
    t = r.wait_for(30, r.connected("sink-a", "sink-b"))
    r.check(t is not None, "switch back to live: sinks reconnect", "{}s".format(t) if t else r.states("sink-a", "sink-b"))

    # -- keys never leak --------------------------------------------------------------
    leaks = [name for name, p in r.procs.items()
             if name not in ("mediamtx", "sink-a", "sink-b") and LEAK.search(p.log())]
    pages = ""
    for path in ("/", "/api/status", "/health", "/openapi.json"):
        try:
            with urllib.request.urlopen("http://127.0.0.1:{}{}".format(HTTP_PORT, path), timeout=2) as resp:
                pages += resp.read().decode(errors="replace")
        except OSError:
            pass
    state_text = ""
    for f in os.listdir(r.state_dir):
        with open(os.path.join(r.state_dir, f), encoding="utf-8", errors="replace") as fh:
            state_text += fh.read()
    redacted = sum(p.log().count("<redacted>") for name, p in r.procs.items() if name in ("out-twitch", "out-youtube"))
    r.check(not leaks and not LEAK.search(pages) and not LEAK.search(state_text) and redacted > 0,
            "no platform or ingest key in wrapper/status logs, endpoints, state files",
            "leaking logs {}, {} redacted lines".format(leaks or "none", redacted))

    # -- disabled output ----------------------------------------------------------------
    fb_log = [line for line in r.procs["out-facebook"].log().splitlines() if line.strip()]
    others = [line for line in fb_log if "disabled (stream key not set)" not in line]
    r.check(r.state("facebook") == "disabled" and not others and r.output("facebook").get("runs", 0) == 0,
            "disabled output: shows disabled, logs nothing else, never ran ffmpeg",
            "{} other log lines".format(len(others)))

    # -- full archive disk ------------------------------------------------------------------
    if not a.full_disk_dir:
        r.skip("full archive disk stops recording only", "pass --full-disk-dir on a small volume")
    else:
        full_cfg = r.config(record_dir=a.full_disk_dir)
        r.restart("recorder", full_cfg.recorder_env())
        r.restart("status", full_cfg.status_env())
        t = r.wait_for(300, lambda: r.state("recorder") == "failed")
        time.sleep(5)
        detail = r.output("recorder").get("detail", "")
        r.check(t is not None and "space" in detail.lower() and r.state("ingest") == "live"
                and r.connected("sink-a", "sink-b")() and r.publisher.alive(),
                "full archive disk stops recording only", "failed {}s after start: {}".format(t, detail))
        storage = (r.status() or {}).get("storage") or {}
        r.check(storage.get("threshold_breached") is True, "free-space warning raised")

    # -- RTMPS to a real platform's certificate ----------------------------------------------
    if not a.public_tls:
        r.skip("public platform certificate verifies", "pass --public-tls (needs internet)")
    else:
        push = subprocess.run(
            [r.ffmpeg, "-hide_banner", "-loglevel", "debug", "-re", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
             "-c:v", "libx264", "-t", "5", "-rw_timeout", "10000000", "-tls_verify", "1",
             "-f", "flv", "rtmps://a.rtmps.youtube.com/live2/acceptance-not-a-real-key"],
            capture_output=True, text=True, timeout=60)
        # The key is fake, so YouTube drops the connection at publish. Getting
        # as far as sending publish proves the certificate verified and the
        # RTMP handshake completed over TLS.
        r.check("Sending publish command" in push.stderr and "Peer certificate failed verification" not in push.stderr,
                "public platform certificate verifies (a.rtmps.youtube.com, -tls_verify 1)",
                "TLS and RTMP handshakes completed; publish refused, as expected for a fake key")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--encoder", choices=["h264_nvenc", "libx264", "copy"], default="h264_nvenc")
    parser.add_argument("--small", action="store_true", help="smaller renditions, for CPU-only hosts")
    parser.add_argument("--idle", type=int, default=0, help="seconds to idle before go-live (restart-backoff check)")
    parser.add_argument("--full-disk-dir", help="a directory on a small, nearly full volume")
    parser.add_argument("--public-tls", action="store_true", help="also verify a real platform's certificate")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--mediamtx")
    args = parser.parse_args()

    run = Run(args)
    try:
        run_checks(run)
    except BaseException:
        # A harness error is never a pass.
        run.fails += 1
        raise
    finally:
        run.stop_all()
        run.log()
        run.log("logs: {}".format(run.logs))
        run.log("report: {}".format(run.report))
        run.log("ALL PASSED" if run.fails == 0 else "{} FAILED".format(run.fails))
    sys.exit(1 if run.fails else 0)


if __name__ == "__main__":
    main()
