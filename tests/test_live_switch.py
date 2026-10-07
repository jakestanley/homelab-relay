import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from relay.common import read_armed, write_armed
from relay.status import derive_output_state, make_handler


class ArmedFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_record_only_when_never_set(self):
        self.assertEqual(read_armed(self.dir), (False, None))

    def test_armed_since_this_boot(self):
        write_armed(self.dir, True)
        armed, since = read_armed(self.dir, booted=time.time() - 60)
        self.assertTrue(armed)
        self.assertIsNotNone(since)

    def test_reboot_returns_to_record_only(self):
        write_armed(self.dir, True)
        self.assertEqual(read_armed(self.dir, booted=time.time() + 60), (False, None))

    def test_disarm(self):
        write_armed(self.dir, True)
        write_armed(self.dir, False)
        self.assertFalse(read_armed(self.dir, booted=0)[0])


class StandbyStateTests(unittest.TestCase):
    def test_standby_with_ingest_live(self):
        now = time.time()
        report = {"enabled": True, "phase": "standby", "heartbeat": now}
        self.assertEqual(derive_output_state(report, True, now - 30, now)[0], "standby")

    def test_idle_without_ingest(self):
        now = time.time()
        report = {"enabled": True, "phase": "standby", "heartbeat": now}
        self.assertEqual(derive_output_state(report, False, None, now)[0], "idle")


class SwitchEndpointTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        env = {"RELAY_STATE_DIR": self.dir, "RELAY_API_URL": "http://127.0.0.1:1", "RELAY_INGEST_PATH": "live/x"}
        with mock.patch.dict(os.environ, env):
            from relay.status import StatusApp

            app = StatusApp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, body, ctype="application/json", path="/api/live"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        conn.request("POST", path, body=body, headers={"Content-Type": ctype})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")

    def test_arm_and_disarm(self):
        status, body = self.post(json.dumps({"armed": True}))
        self.assertEqual((status, body["armed"]), (200, True))
        self.assertTrue(read_armed(self.dir)[0])
        status, body = self.post(json.dumps({"armed": False}))
        self.assertEqual((status, body["armed"]), (200, False))

    def test_form_post_refused(self):
        status, _ = self.post("armed=true", ctype="application/x-www-form-urlencoded")
        self.assertEqual(status, 415)
        self.assertFalse(read_armed(self.dir)[0])

    def test_bad_body_refused(self):
        self.assertEqual(self.post(json.dumps({"armed": "yes"}))[0], 400)
        self.assertEqual(self.post("not json")[0], 400)

    def test_other_paths_still_read_only(self):
        self.assertEqual(self.post(json.dumps({"armed": True}), path="/api/status")[0], 405)
