"""Status glue: a read-only status page and JSON endpoint, plus the ingest gap log.

Observation only, and off the broadcast path: it reads the MediaMTX API, the
wrappers' report files and the filesystem. If it crashes, the page and the gap
log stop and the broadcast carries on.

It is never given platform keys, so it cannot leak one.
"""

import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from relay.common import env_float, env_str, fetch_ingest, read_json, setup_logging

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

# A report older than this means the wrapper is not running.
REPORT_STALE_SECONDS = 15.0
# "connected" requires the byte counter to have advanced this recently.
PROGRESS_WINDOW_SECONDS = 5.0
# How long an output may take to start delivering before it counts as failed.
CONNECT_GRACE_SECONDS = 15.0

log = setup_logging("status")


def iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def derive_output_state(report, ingest_live, ingest_since, now):
    """Reduce a wrapper report to one state for the page.

    Ingest is checked first: with no ingest every enabled output is idle,
    whatever its process is doing. The page is mostly read before a
    broadcast, when idling is normal, and must not report it as failure.

    Returns (state, detail). States: disabled, idle, connecting, connected,
    failed, unknown.
    """
    if report is None or now - report.get("heartbeat", 0) > REPORT_STALE_SECONDS:
        if ingest_live:
            return "failed", "output process is not reporting"
        return "unknown", "output process is not reporting"

    if not report.get("enabled"):
        return "disabled", report.get("disabled_reason") or "disabled"

    if not ingest_live:
        return "idle", "waiting for ingest"

    last_progress = report.get("last_progress")
    if report.get("phase") == "running" and last_progress and now - last_progress <= PROGRESS_WINDOW_SECONDS:
        return "connected", "data flowing"

    started = report.get("run_started") if report.get("phase") == "running" else None
    recent_start = max(t for t in (started, ingest_since, 0) if t is not None)
    no_progress_since_start = last_progress is None or last_progress < recent_start

    # An output that already failed during this ingest session stays failed
    # while it retries, rather than flickering back to "connecting".
    last_exit = report.get("last_exit") or {}
    failed_this_session = ingest_since is not None and last_exit.get("at", 0) >= ingest_since
    if failed_this_session and no_progress_since_start:
        return "failed", "retrying after: {}".format(last_exit.get("reason") or "error")

    # Allow a new push (or a freshly arrived ingest) time to start delivering
    # before calling it failed.
    if no_progress_since_start and now - recent_start <= CONNECT_GRACE_SECONDS:
        return "connecting", "starting"

    if report.get("phase") == "running":
        return "failed", "no data delivered for {:.0f}s".format(now - (last_progress or started or now))
    return "failed", last_exit.get("reason") or "not running"


class IngestWatcher(threading.Thread):
    """Polls ingest continuously and logs every gap with start, end and duration."""

    def __init__(self, api_url, ingest_path, gap_log_path):
        super().__init__(daemon=True)
        self.api_url = api_url
        self.ingest_path = ingest_path
        self.gap_log_path = gap_log_path
        self.lock = threading.Lock()
        self.doc = None
        self.api_ok = False
        self.live = False
        self.live_since = None
        self.gap_started = None
        self.last_gap = None

    def snapshot(self):
        with self.lock:
            return {
                "doc": self.doc,
                "api_ok": self.api_ok,
                "live": self.live,
                "live_since": self.live_since,
                "gap_started": self.gap_started,
                "last_gap": self.last_gap,
            }

    def run(self):
        while True:
            try:
                self.poll()
            except Exception:  # never let the watcher die silently
                log.exception("ingest watcher poll failed")
            time.sleep(1.0)

    def poll(self):
        doc = fetch_ingest(self.api_url, self.ingest_path)
        now = time.time()
        with self.lock:
            self.api_ok = doc is not None
            self.doc = doc or None
            live = bool(doc)
            if live and not self.live:
                self.live_since = now
                if self.gap_started is not None:
                    gap = {"start": self.gap_started, "end": now, "duration": now - self.gap_started}
                    self.last_gap = gap
                    self.gap_started = None
                    self._log_gap_end(gap)
                else:
                    log.info("ingest started")
            elif not live and self.live:
                self.gap_started = now
                self.live_since = None
                log.warning("ingest gap started at %s", iso(now))
                self._append_gap_log("gap start={}".format(iso(now)))
            self.live = live

    def _log_gap_end(self, gap):
        line = "gap start={} end={} duration={:.1f}s".format(
            iso(gap["start"]), iso(gap["end"]), gap["duration"]
        )
        log.warning("ingest resumed: %s", line)
        self._append_gap_log(line)

    def _append_gap_log(self, line):
        if not self.gap_log_path:
            return
        try:
            with open(self.gap_log_path, "a", encoding="utf-8") as fh:
                fh.write("{} {}\n".format(iso(time.time()), line))
        except OSError as exc:
            # A full archive disk costs the gap file, not the stdout log.
            log.warning("could not append to %s: %s", self.gap_log_path, exc)


class StatusApp:
    def __init__(self):
        self.api_url = env_str("RELAY_API_URL")
        self.ingest_path = env_str("RELAY_INGEST_PATH")
        self.state_dir = env_str("RELAY_STATE_DIR")
        self.outputs = [n.strip() for n in env_str("RELAY_OUTPUTS").split(",") if n.strip()]
        self.record_dir = env_str("RECORD_DIR")
        self.record_dir_label = env_str("RECORD_DIR_LABEL") or self.record_dir
        self.min_free_bytes = int(env_float("RECORD_MIN_FREE_GB", 20.0) * 1024 ** 3)
        gap_log = os.path.join(self.record_dir, "ingest-gaps.log") if self.record_dir else ""
        self.watcher = IngestWatcher(self.api_url, self.ingest_path, gap_log)

    def report(self, name):
        return read_json(os.path.join(self.state_dir, name + ".json"))

    def output_entry(self, name, snap, now):
        report = self.report(name)
        state, detail = derive_output_state(report, snap["live"], snap["live_since"], now)
        report = report or {}
        return {
            "name": name,
            "state": state,
            "detail": detail,
            "destination": report.get("destination"),
            "rendition": report.get("rendition"),
            "bytes": report.get("bytes", 0),
            "runs": report.get("runs", 0),
            "last_exit": _public_exit(report.get("last_exit")),
        }

    def storage(self):
        if not self.record_dir:
            return None
        try:
            usage = shutil.disk_usage(self.record_dir)
        except OSError as exc:
            return {"path": self.record_dir_label, "error": str(exc)}
        return {
            "path": self.record_dir_label,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "min_free_bytes": self.min_free_bytes,
            "threshold_breached": usage.free < self.min_free_bytes,
        }

    def status(self):
        now = time.time()
        snap = self.watcher.snapshot()
        doc = snap["doc"] or {}
        if not snap["api_ok"]:
            ingest = {"state": "unknown", "detail": "RTMP server API unreachable"}
        elif snap["live"]:
            ingest = {
                "state": "live",
                "detail": "ingest arriving",
                "since": iso(snap["live_since"]),
                "tracks": doc.get("tracks", []),
                "bytes_received": doc.get("bytesReceived", 0),
            }
        else:
            ingest = {"state": "no_ingest", "detail": "no publisher connected"}
        ingest["gap_started"] = iso(snap["gap_started"])
        last_gap = snap["last_gap"]
        ingest["last_gap"] = (
            {"start": iso(last_gap["start"]), "end": iso(last_gap["end"]), "duration_seconds": round(last_gap["duration"], 1)}
            if last_gap
            else None
        )

        recording = self.output_entry("recorder", snap, now)
        recording["file"] = (self.report("recorder") or {}).get("current_file")
        return {
            "generated_at": iso(now),
            "ingest": ingest,
            "outputs": [self.output_entry(n, snap, now) for n in self.outputs],
            "recording": recording,
            "storage": self.storage(),
        }


def _public_exit(last_exit):
    if not last_exit:
        return None
    # The wrapper redacts keys before this reason is ever stored.
    return {"at": iso(last_exit.get("at")), "reason": last_exit.get("reason")}


def make_handler(app):
    static = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/openapi.json": ("openapi.json", "application/json"),
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "homelab-relay"

        def log_message(self, fmt, *args):
            # Read-only GETs polled every few seconds; access logs would
            # bury the output and gap lines that matter.
            pass

        def send_json(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            try:
                if path == "/api/status":
                    self.send_json(200, app.status())
                elif path == "/health":
                    self.send_json(200, {"status": "ok"})
                elif path in static:
                    name, ctype = static[path]
                    with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                        data = fh.read()
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_json(404, {"error": "not_found", "message": "no such endpoint"})
            except Exception:
                log.exception("error serving %s", path)
                self.send_json(500, {"error": "internal_error", "message": "internal error"})

        def reject(self):
            self.send_json(405, {"error": "method_not_allowed", "message": "this API is read-only"})

        do_POST = do_PUT = do_PATCH = do_DELETE = reject

    return Handler


def main():
    port = int(env_str("SERVICE_PORT", "20040"))
    app = StatusApp()
    app.watcher.start()
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    log.info("status page listening on :%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
