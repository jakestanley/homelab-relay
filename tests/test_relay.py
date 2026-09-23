import os
import tempfile
import unittest
from unittest import mock

from relay.common import join_destination, key_secrets, redact
from relay.status import derive_output_state

NOW = 1_000_000.0


def report(**overrides):
    base = {
        "enabled": True,
        "disabled_reason": None,
        "phase": "waiting",
        "heartbeat": NOW - 1,
        "run_started": None,
        "last_progress": None,
        "last_exit": None,
    }
    base.update(overrides)
    return base


class DeriveStateTests(unittest.TestCase):
    def state(self, rep, live=True, since=NOW - 120):
        return derive_output_state(rep, live, since if live else None, NOW)[0]

    def test_disabled_wins_even_with_ingest(self):
        rep = report(enabled=False, disabled_reason="stream key not set", phase="disabled")
        self.assertEqual(self.state(rep, live=True), "disabled")
        self.assertEqual(self.state(rep, live=False), "disabled")

    def test_no_ingest_is_idle_whatever_the_process_is_doing(self):
        for phase in ("waiting", "running", "retrying"):
            rep = report(phase=phase, last_exit={"reason": "boom"}, run_started=NOW - 100)
            self.assertEqual(self.state(rep, live=False), "idle", phase)

    def test_connected_requires_advancing_bytes(self):
        rep = report(phase="running", run_started=NOW - 60, last_progress=NOW - 1)
        self.assertEqual(self.state(rep), "connected")

    def test_attached_but_not_delivering_is_failed(self):
        # Process is running and attached, but the counter stopped: a stall.
        rep = report(phase="running", run_started=NOW - 60, last_progress=NOW - 30)
        self.assertEqual(self.state(rep), "failed")

    def test_new_push_gets_a_grace_period(self):
        rep = report(phase="running", run_started=NOW - 3)
        self.assertEqual(self.state(rep), "connecting")

    def test_push_that_never_delivers_fails_after_grace(self):
        rep = report(phase="running", run_started=NOW - 60)
        self.assertEqual(self.state(rep), "failed")

    def test_ingest_just_returned_uses_grace_not_old_progress(self):
        # Previous run's progress is old; ingest came back 2s ago and the
        # wrapper has not started ffmpeg yet.
        rep = report(phase="waiting", last_progress=NOW - 300)
        self.assertEqual(self.state(rep, since=NOW - 2), "connecting")

    def test_retry_after_failure_stays_failed_not_connecting(self):
        # Failed 5s ago during this ingest session, new run just started.
        rep = report(
            phase="running",
            run_started=NOW - 2,
            last_progress=NOW - 20,
            last_exit={"at": NOW - 5, "reason": "No space left on device"},
        )
        state, detail = derive_output_state(rep, True, NOW - 120, NOW)
        self.assertEqual(state, "failed")
        self.assertIn("No space left", detail)

    def test_exit_from_previous_session_does_not_count(self):
        # Last exit was when the previous ingest ended; ingest is new.
        rep = report(phase="running", run_started=NOW - 2, last_exit={"at": NOW - 60, "reason": "EOF"})
        self.assertEqual(self.state(rep, since=NOW - 3), "connecting")

    def test_recovered_after_failure_is_connected(self):
        rep = report(
            phase="running",
            run_started=NOW - 10,
            last_progress=NOW - 1,
            last_exit={"at": NOW - 12, "reason": "boom"},
        )
        self.assertEqual(self.state(rep), "connected")

    def test_retrying_while_ingest_live_is_failed(self):
        rep = report(phase="retrying", last_exit={"reason": "Connection refused"})
        state, detail = derive_output_state(rep, True, NOW - 120, NOW)
        self.assertEqual(state, "failed")
        self.assertIn("Connection refused", detail)

    def test_missing_or_stale_report(self):
        self.assertEqual(self.state(None, live=True), "failed")
        self.assertEqual(self.state(None, live=False), "unknown")
        stale = report(heartbeat=NOW - 60)
        self.assertEqual(self.state(stale, live=False), "unknown")


class RedactionTests(unittest.TestCase):
    def test_redacts_raw_and_encoded_forms(self):
        key = "live_123/abc?bandwidthtest=true"
        line = "rtmps://x/app/{} and {}".format(key, "live_123%2Fabc%3Fbandwidthtest%3Dtrue")
        out = redact(line, key_secrets(key))
        self.assertNotIn("live_123", out)
        self.assertIn("<redacted>", out)

    def test_bare_key_redacted_when_query_is_stripped(self):
        key = "live_123?bandwidthtest=true"
        out = redact("publish live_123 failed", key_secrets(key))
        self.assertEqual(out, "publish <redacted> failed")

    def test_no_key_no_change(self):
        self.assertEqual(redact("hello", key_secrets("")), "hello")

    def test_join_destination(self):
        self.assertEqual(join_destination("rtmps://h/app/", "k"), "rtmps://h/app/k")
        self.assertEqual(join_destination("rtmp://sink:1935/sink", ""), "rtmp://sink:1935/sink")


BASE_ENV = {
    "RELAY_SOURCE_URL": "rtmp://mediamtx:1935/live/ingest",
    "RELAY_API_URL": "http://mediamtx:9997",
    "RELAY_INGEST_PATH": "live/ingest",
}


class WrapperTests(unittest.TestCase):
    def make(self, **env):
        from relay.output import Wrapper

        self.tmp = tempfile.mkdtemp()
        full = dict(BASE_ENV, RELAY_STATE_DIR=self.tmp, **env)
        with mock.patch.dict(os.environ, full, clear=True):
            return Wrapper()

    def test_disabled_rules(self):
        self.assertEqual(self.make(OUTPUT_NAME="t").disabled_reason(), "no URL configured")
        w = self.make(OUTPUT_NAME="t", OUTPUT_URL="rtmps://h/app", OUTPUT_KEY_REQUIRED="true")
        self.assertEqual(w.disabled_reason(), "stream key not set")
        # A sink names no key variable, so it runs without one.
        self.assertIsNone(self.make(OUTPUT_NAME="s", OUTPUT_URL="rtmp://sink/x").disabled_reason())

    def test_rtmps_verifies_by_default(self):
        w = self.make(OUTPUT_NAME="t", OUTPUT_URL="rtmps://h/app", OUTPUT_KEY="SECRET")
        cmd, _ = w.build_command()
        self.assertEqual(cmd[cmd.index("-tls_verify") + 1], "1")
        self.assertEqual(cmd[-1], "rtmps://h/app/SECRET")
        self.assertIn("-rw_timeout", cmd[cmd.index("-c"):])

    def test_tls_verify_off_is_per_output(self):
        w = self.make(OUTPUT_NAME="s", OUTPUT_URL="rtmps://sink:1936/x", OUTPUT_TLS_VERIFY="false")
        cmd, _ = w.build_command()
        self.assertEqual(cmd[cmd.index("-tls_verify") + 1], "0")

    def test_plain_rtmp_has_no_tls_option(self):
        cmd, _ = self.make(OUTPUT_NAME="s", OUTPUT_URL="rtmp://sink/x").build_command()
        self.assertNotIn("-tls_verify", cmd)

    def test_state_report_never_contains_key(self):
        w = self.make(OUTPUT_NAME="t", OUTPUT_URL="rtmps://h/app", OUTPUT_KEY="SECRETKEY")
        w.write_report()
        with open(w.report_path) as fh:
            self.assertNotIn("SECRETKEY", fh.read())

    def test_recording_paths_never_collide(self):
        rec_dir = tempfile.mkdtemp()
        w = self.make(OUTPUT_NAME="recorder", OUTPUT_KIND="record", RECORD_DIR=rec_dir)
        seen = set()
        for _ in range(3):
            path = w.new_recording_path()
            self.assertNotIn(path, seen)
            open(path, "w").close()
            seen.add(path)
        cmd, path = w.build_command()
        self.assertIn("-n", cmd)
        self.assertTrue(path.endswith(".ts"))


if __name__ == "__main__":
    unittest.main()
