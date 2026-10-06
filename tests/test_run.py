import os
import unittest

from relay.config import ConfigError
from relay.run import resolve

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A container's environment: .env via env_file, container paths on top.
CONTAINER = {
    "PATH": "/usr/local/bin:/usr/bin",
    "HOME": "/home/relay",
    "SERVICE_PORT": "20040",
    "RTMP_PORT": "1935",
    "INGEST_KEY": "Acc3pt_ingest-key.v1",
    "RECORD_DIR": "/recordings",
    "RELAY_STATE_DIR": "/state",
    "FFMPEG_EXE": "/usr/bin/ffmpeg",
    "MEDIAMTX_EXE": "/usr/local/bin/mediamtx",
    "VIDEO_ENCODER": "libx264",
    "TWITCH_URL": "rtmps://ingest.example/app",
    "STREAM_KEY_TWITCH": "SECRETKEY",
    "STREAM_KEY_FACEBOOK": "",
    "UNRELATED": "not passed on",
}


class ResolveTests(unittest.TestCase):
    def test_mediamtx_runs_the_pinned_binary_with_the_repo_config(self):
        args, env = resolve(CONTAINER, "mediamtx", root=ROOT)
        self.assertEqual(args, ["/usr/local/bin/mediamtx", os.path.join(ROOT, "mediamtx", "mediamtx.yml")])
        self.assertEqual(env["MTX_AUTHINTERNALUSERS_0_PERMISSIONS_0_PATH"], "live/Acc3pt_ingest-key.v1")

    def test_output_gets_its_key_rendition_and_container_paths(self):
        args, env = resolve(CONTAINER, "out-twitch", root=ROOT)
        self.assertEqual(args[1:], ["-m", "relay.output"])
        self.assertEqual(env["OUTPUT_KEY"], "SECRETKEY")
        self.assertEqual(env["OUTPUT_VIDEO_ENCODER"], "libx264")
        self.assertEqual(env["FFMPEG_EXE"], "/usr/bin/ffmpeg")
        self.assertEqual(env["RELAY_STATE_DIR"], "/state")
        self.assertEqual(env["PATH"], CONTAINER["PATH"])

    def test_only_the_service_environment_is_passed_on(self):
        _, env = resolve(CONTAINER, "status", root=ROOT)
        self.assertNotIn("UNRELATED", env)
        self.assertNotIn("SECRETKEY", env.values())

    def test_empty_values_are_dropped_as_on_windows(self):
        _, env = resolve(CONTAINER, "out-facebook", root=ROOT)
        self.assertNotIn("OUTPUT_KEY", env)

    def test_unknown_service_is_refused(self):
        with self.assertRaises(KeyError):
            resolve(CONTAINER, "out-nope", root=ROOT)

    def test_invalid_config_is_refused(self):
        with self.assertRaises(ConfigError):
            resolve(dict(CONTAINER, INGEST_KEY="a=="), "status", root=ROOT)
