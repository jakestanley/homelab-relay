"""Output wrapper: one long-running process per output slot, and the recorder.

Its jobs are fixed by deviation 1b in prompts/init.md, and it does nothing else:

- idle when disabled, and wait for ingest rather than exiting when enabled;
- run ffmpeg with the configured destination, key and I/O timeout;
- expose a progress counter the status glue can read (a JSON file in
  RELAY_STATE_DIR);
- redact the key from anything it logs.

It never exits because ffmpeg exited. Whether ffmpeg ended because ingest
stopped, the platform dropped it or the I/O timeout fired, the wrapper goes
back to waiting for ingest. The runtime's restart policy (Docker
`restart: always`) only covers the wrapper itself crashing.
"""

import os
import signal
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone

from relay.common import (
    env_bool,
    env_float,
    env_str,
    fetch_ingest,
    join_destination,
    key_secrets,
    redact,
    setup_logging,
    write_json_atomic,
)

POLL_SECONDS = 1.0
DISABLED_HEARTBEAT_SECONDS = 5.0
MAX_RETRY_DELAY = 10.0
# A run that delivered data for this long counts as healthy, so the next
# failure starts the retry delay from the bottom again.
HEALTHY_RUN_SECONDS = 30.0
API_WARN_AFTER_SECONDS = 5.0

stop_event = threading.Event()


def _handle_stop(signum, frame):
    stop_event.set()


class Wrapper:
    def __init__(self):
        self.name = env_str("OUTPUT_NAME")
        if not self.name:
            raise SystemExit("OUTPUT_NAME is required")
        self.log = setup_logging(self.name)

        self.kind = env_str("OUTPUT_KIND", "push")
        self.url = env_str("OUTPUT_URL")
        self.key = env_str("OUTPUT_KEY")
        self.key_required = env_bool("OUTPUT_KEY_REQUIRED", False)
        self.tls_verify = env_bool("OUTPUT_TLS_VERIFY", True)
        self.record_dir = env_str("RECORD_DIR")

        self.source = env_str("RELAY_SOURCE_URL")
        self.api_url = env_str("RELAY_API_URL")
        self.ingest_path = env_str("RELAY_INGEST_PATH")
        self.state_dir = env_str("RELAY_STATE_DIR")
        for var, value in (
            ("RELAY_SOURCE_URL", self.source),
            ("RELAY_API_URL", self.api_url),
            ("RELAY_INGEST_PATH", self.ingest_path),
            ("RELAY_STATE_DIR", self.state_dir),
        ):
            if not value:
                raise SystemExit("{} is required".format(var))

        self.io_timeout = env_float("OUTPUT_IO_TIMEOUT", 10.0)
        self.secrets = key_secrets(self.key)
        self.report_path = os.path.join(self.state_dir, self.name + ".json")

        self.phase = "starting"
        self.runs = 0
        self.run_started = None
        self.bytes = 0
        self.last_progress = None
        self.last_exit = None
        self.current_file = None
        self.last_error = None
        self._first_progress = None
        self.api_down_since = None
        self.api_warned = False

    # -- configuration ----------------------------------------------------

    def disabled_reason(self):
        if self.kind == "record":
            return None if self.record_dir else "RECORD_DIR not set"
        if not self.url:
            return "no URL configured"
        if self.key_required and not self.key:
            return "stream key not set"
        return None

    def destination_label(self):
        """What the logs and status page may show: never includes the key."""
        if self.kind == "record":
            return self.current_file or self.record_dir
        return self.url

    # -- reporting --------------------------------------------------------

    def write_report(self):
        report = {
            "name": self.name,
            "kind": self.kind,
            "enabled": self.disabled_reason() is None,
            "disabled_reason": self.disabled_reason(),
            "destination": self.destination_label(),
            "phase": self.phase,
            "heartbeat": time.time(),
            "runs": self.runs,
            "run_started": self.run_started,
            "bytes": self.bytes,
            "last_progress": self.last_progress,
            "last_exit": self.last_exit,
            "current_file": self.current_file,
        }
        try:
            write_json_atomic(self.report_path, report)
        except OSError as exc:
            # The report is for the status page. Failing to write it must
            # never stop the output.
            self.log.warning("could not write status report: %s", exc)

    def sleep(self, seconds):
        """Sleep while keeping the heartbeat fresh. False if told to stop."""
        deadline = time.monotonic() + seconds
        while not stop_event.is_set():
            self.write_report()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            stop_event.wait(min(POLL_SECONDS, remaining))
        return False

    # -- main loop --------------------------------------------------------

    def run(self):
        reason = self.disabled_reason()
        if reason:
            # Disabled: no connection attempts, no retries, no failure logs.
            self.phase = "disabled"
            self.log.info("output %s disabled (%s); not connecting", self.name, reason)
            while not stop_event.is_set():
                self.write_report()
                stop_event.wait(DISABLED_HEARTBEAT_SECONDS)
            return

        self.log.info(
            "output %s enabled; destination %s; waiting for ingest",
            self.name,
            self.destination_label(),
        )
        failures = 0
        while not stop_event.is_set():
            self.phase = "waiting"
            if not self.wait_for_ingest():
                return
            delivered_for = self.run_ffmpeg()
            if stop_event.is_set():
                return

            if fetch_ingest(self.api_url, self.ingest_path):
                # Ingest is still live, so this output failed on its own.
                failures = 1 if delivered_for >= HEALTHY_RUN_SECONDS else failures + 1
                delay = min(2 ** (failures - 1), MAX_RETRY_DELAY)
                self.log.warning(
                    "output %s failed while ingest is live: %s; retrying in %.0fs",
                    self.name,
                    self.last_exit["reason"],
                    delay,
                )
                self.phase = "retrying"
                if not self.sleep(delay):
                    return
            else:
                failures = 0
                self.log.info("output %s stopped: ingest ended; waiting for ingest", self.name)

    def wait_for_ingest(self):
        while not stop_event.is_set():
            ingest = fetch_ingest(self.api_url, self.ingest_path)
            self._note_api(ingest is not None)
            if ingest:
                return True
            self.write_report()
            stop_event.wait(POLL_SECONDS)
        return False

    def _note_api(self, reachable):
        # Warn once the API has been down for a while, not on the normal
        # race with MediaMTX at startup.
        now = time.monotonic()
        if reachable:
            if self.api_warned:
                self.log.info("RTMP server API reachable again")
            self.api_down_since = None
            self.api_warned = False
        elif self.api_down_since is None:
            self.api_down_since = now
        elif not self.api_warned and now - self.api_down_since > API_WARN_AFTER_SECONDS:
            self.log.warning("RTMP server API unreachable; treating as no ingest")
            self.api_warned = True

    # -- one ffmpeg run -------------------------------------------------------

    def new_recording_path(self):
        """A fresh file per ingest session. Nothing is ever overwritten."""
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        base = os.path.join(self.record_dir, stamp)
        path = base + ".ts"
        n = 1
        while os.path.exists(path):
            path = "{}-{}.ts".format(base, n)
            n += 1
        return path

    def build_command(self):
        timeout_us = str(int(self.io_timeout * 1_000_000))
        cmd = [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-nostats", "-progress", "pipe:1", "-stats_period", "1",
        ]
        if self.kind == "record":
            # -n: refuse to overwrite, as a second guard behind the unique name.
            cmd.append("-n")
        cmd += ["-rw_timeout", timeout_us, "-i", self.source, "-map", "0", "-c", "copy"]

        if self.kind == "record":
            path = self.new_recording_path()
            self.current_file = os.path.basename(path)
            # MPEG-TS stays playable when the process is killed mid-write;
            # at most the tail is lost.
            return cmd + ["-f", "mpegts", path], path

        cmd += ["-rw_timeout", timeout_us]
        if urllib.parse.urlsplit(self.url).scheme == "rtmps":
            # ffmpeg does NOT verify TLS peers by default. Verification is
            # on for every output unless OUTPUT_TLS_VERIFY=false is set on
            # that one slot (the self-signed test sink only).
            cmd += ["-tls_verify", "1" if self.tls_verify else "0"]
        return cmd + ["-f", "flv", join_destination(self.url, self.key)], None

    def run_ffmpeg(self):
        """Run ffmpeg until it exits. Returns seconds of delivery observed."""
        cmd, record_path = self.build_command()
        self.runs += 1
        self.run_started = time.time()
        self.bytes = 0
        self.last_progress = None
        self._first_progress = None
        self.last_error = None
        self.phase = "running"
        verb = "recording to" if self.kind == "record" else "starting push to"
        self.log.info("output %s %s %s", self.name, verb, self.destination_label())

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            bufsize=1,
        )
        readers = [
            threading.Thread(target=self._read_progress, args=(proc.stdout,), daemon=True),
            threading.Thread(target=self._read_stderr, args=(proc.stderr,), daemon=True),
        ]
        for t in readers:
            t.start()

        watchdog_reason = None
        while proc.poll() is None:
            self.write_report()
            if stop_event.is_set():
                break
            watchdog_reason = self._watchdog()
            if watchdog_reason:
                self.log.warning("output %s %s; stopping ffmpeg", self.name, watchdog_reason)
                break
            stop_event.wait(POLL_SECONDS)

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for t in readers:
            t.join(timeout=2)

        first_progress = self._first_progress
        delivered_for = (
            (self.last_progress - first_progress) if first_progress and self.last_progress else 0.0
        )
        reason = watchdog_reason or self.last_error or "ffmpeg exited with code {}".format(proc.returncode)
        self.last_exit = {"at": time.time(), "code": proc.returncode, "reason": reason}
        self.phase = "waiting"
        self.run_started = None

        if record_path:
            self._discard_if_empty(record_path)
        self.write_report()
        return delivered_for

    def _watchdog(self):
        """Belt and braces behind -rw_timeout: a stall must become an exit."""
        now = time.time()
        if self.last_progress is None:
            limit = self.io_timeout + 20
            if now - self.run_started > limit:
                return "delivered no data within {:.0f}s of starting".format(limit)
        else:
            limit = self.io_timeout + 5
            if now - self.last_progress > limit:
                return "stalled: no data delivered for {:.0f}s".format(limit)
        return None

    def _read_progress(self, stream):
        block = {}
        for line in stream:
            k, _, v = line.strip().partition("=")
            if k != "progress":
                block[k] = v
                continue
            size = block.get("total_size", "")
            if size.isdigit() and int(size) > self.bytes:
                self.bytes = int(size)
                self.last_progress = time.time()
                if self._first_progress is None:
                    self._first_progress = self.last_progress
            block = {}

    def _read_stderr(self, stream):
        for line in stream:
            line = redact(line.rstrip(), self.secrets)
            if line:
                self.last_error = line
                self.log.warning("output %s ffmpeg: %s", self.name, line)

    def _discard_if_empty(self, path):
        # A run that wrote nothing (full disk, ingest gone before the first
        # packet) leaves an empty file. Removing an empty file loses nothing.
        try:
            if os.path.getsize(path) == 0:
                os.remove(path)
                self.current_file = None
                return
        except OSError:
            return
        self.log.info("recording %s closed", os.path.basename(path))


def main():
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    Wrapper().run()


if __name__ == "__main__":
    main()
