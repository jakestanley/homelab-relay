import os
import tempfile
import unittest
from unittest import mock

from relay.config import ConfigError, check_ingest_key, load, read_dotenv

GOOD_ENV = """\
# comment
SERVICE_PORT=20040
RTMP_PORT=1935
INGEST_KEY=Acc3pt_ingest-key.v1
RECORD_DIR=D:\\RelayRecordings
TWITCH_URL=rtmps://ingest.global-contribute.live-video.net/app
STREAM_KEY_TWITCH="live_123?bandwidthtest=true"
YOUTUBE_URL=rtmps://a.rtmps.youtube.com/live2
STREAM_KEY_YOUTUBE=
SINK_A_URL=rtmps://127.0.0.1:19361/sink
SINK_A_TLS_VERIFY=false
"""

YAML = """\
renditions:
  small:
    width: 1280
    height: 720
    fps: 60
    video_kbps: 4000
    audio_kbps: 160
    keyframe_seconds: 2
outputs:
  - name: twitch
    url_env: TWITCH_URL
    key_env: STREAM_KEY_TWITCH
    rendition: small
    max_kbps: 6000
  - name: youtube
    url_env: YOUTUBE_URL
    key_env: STREAM_KEY_YOUTUBE
    rendition: copy
  - name: sink-a
    url_env: SINK_A_URL
    tls_verify_env: SINK_A_TLS_VERIFY
    rendition: small
"""


class ConfigTests(unittest.TestCase):
    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def load(self, env=GOOD_ENV, yaml_text=YAML):
        return load(self.write(".env", env), self.write("relay.yaml", yaml_text), root=self.tmp)

    def problems(self, **kw):
        with self.assertRaises(ConfigError) as cm:
            self.load(**kw)
        return "\n".join(cm.exception.problems)

    def test_dotenv_quotes_and_comments(self):
        values = read_dotenv(self.write(".env", GOOD_ENV))
        self.assertEqual(values["STREAM_KEY_TWITCH"], "live_123?bandwidthtest=true")
        self.assertEqual(values["RECORD_DIR"], "D:\\RelayRecordings")
        self.assertEqual(values["STREAM_KEY_YOUTUBE"], "")

    def test_ingest_key_rules(self):
        for bad in ("base64pad==", "with+plus", "with/slash", "with space", "", "change-me-to-match-batw-STREAM_KEY_LIVE"):
            self.assertIsNotNone(check_ingest_key(bad), bad)
        self.assertIsNone(check_ingest_key("Acc3pt_ingest-key.v1"))

    def test_output_env_for_a_rendition(self):
        config = self.load()
        env = config.output_env(config.outputs[0])
        self.assertEqual(env["OUTPUT_KEY"], "live_123?bandwidthtest=true")
        self.assertEqual(env["OUTPUT_KEY_REQUIRED"], "true")
        self.assertEqual(env["OUTPUT_VIDEO_ENCODER"], "h264_nvenc")
        self.assertEqual((env["OUTPUT_WIDTH"], env["OUTPUT_HEIGHT"], env["OUTPUT_FPS"]), ("1280", "720", "60"))
        self.assertEqual(env["RELAY_SOURCE_URL"], "rtmp://127.0.0.1:1935/live/Acc3pt_ingest-key.v1")
        self.assertEqual(env["RELAY_API_URL"], "http://127.0.0.1:9997")

    def test_copy_output_has_no_rendition(self):
        config = self.load()
        env = config.output_env(config.outputs[1])
        self.assertEqual(env["OUTPUT_VIDEO_ENCODER"], "copy")
        self.assertNotIn("OUTPUT_WIDTH", env)

    def test_sink_is_keyless_and_may_skip_verification(self):
        config = self.load()
        env = config.output_env(config.outputs[2])
        self.assertEqual(env["OUTPUT_KEY_REQUIRED"], "false")
        self.assertEqual(env["OUTPUT_TLS_VERIFY"], "false")

    def test_platforms_always_verify(self):
        config = self.load()
        self.assertEqual(config.output_env(config.outputs[0])["OUTPUT_TLS_VERIFY"], "true")

    def test_encoder_override_for_test_hosts(self):
        config = self.load(env=GOOD_ENV + "VIDEO_ENCODER=libx264\n")
        self.assertEqual(config.output_env(config.outputs[0])["OUTPUT_VIDEO_ENCODER"], "libx264")
        self.assertIn("VIDEO_ENCODER", self.problems(env=GOOD_ENV + "VIDEO_ENCODER=hevc_nvenc\n"))

    def test_copy_host_forwards_every_output_untouched(self):
        config = self.load(env=GOOD_ENV + "VIDEO_ENCODER=copy\n")
        for output in config.outputs:
            env = config.output_env(output)
            self.assertEqual(env["OUTPUT_VIDEO_ENCODER"], "copy")
            self.assertNotIn("OUTPUT_WIDTH", env)

    def test_copy_host_reports_ceilings_instead_of_refusing(self):
        from relay.config import summary

        config = self.load(env=GOOD_ENV + "VIDEO_ENCODER=copy\n")
        twitch = [line for line in summary(config) if line.startswith("output twitch")][0]
        self.assertIn("copy of ingest", twitch)
        self.assertIn("under 6000 kbps", twitch)

    def test_status_never_gets_keys(self):
        config = self.load()
        text = repr(config.status_env())
        self.assertNotIn("live_123", text)
        self.assertEqual(config.status_env()["RELAY_OUTPUTS"], "twitch,youtube,sink-a")

    def test_recorder_copies(self):
        env = self.load().recorder_env()
        self.assertEqual((env["OUTPUT_KIND"], env["OUTPUT_VIDEO_ENCODER"]), ("record", "copy"))

    def test_services_one_per_slot_plus_fixed(self):
        names = [s["name"] for s in self.load().services("homelab-relay", "py")]
        self.assertEqual(
            names,
            [
                "homelab-relay-mediamtx",
                "homelab-relay-out-twitch",
                "homelab-relay-out-youtube",
                "homelab-relay-out-sink-a",
                "homelab-relay-recorder",
                "homelab-relay-status",
            ],
        )

    def test_mediamtx_env(self):
        env = self.load().mediamtx_env()
        self.assertEqual(env["MTX_AUTHINTERNALUSERS_0_PERMISSIONS_0_PATH"], "live/Acc3pt_ingest-key.v1")
        self.assertEqual(env["MTX_RTMPADDRESS"], ":1935")
        self.assertEqual(env["MTX_APIADDRESS"], "127.0.0.1:9997")

    def test_over_platform_ceiling_is_refused(self):
        text = self.problems(yaml_text=YAML.replace("video_kbps: 4000", "video_kbps: 5900"))
        self.assertIn("6060 kbps", text)
        self.assertIn("over the platform's 6000", text)

    def test_every_problem_reported_at_once(self):
        env = GOOD_ENV.replace("SERVICE_PORT=20040", "SERVICE_PORT=http").replace("INGEST_KEY=Acc3pt_ingest-key.v1", "INGEST_KEY=a==")
        text = self.problems(env=env, yaml_text=YAML.replace("height: 720", "height: 721"))
        self.assertIn("SERVICE_PORT", text)
        self.assertIn("INGEST_KEY", text)
        self.assertIn("even", text)

    def test_typos_are_errors_not_ignored(self):
        self.assertIn("unknown keys", self.problems(yaml_text=YAML.replace("max_kbps: 6000", "max_kpbs: 6000")))
        self.assertIn("not defined", self.problems(yaml_text=YAML.replace("rendition: small\n    max", "rendition: smal\n    max")))

    def test_reserved_and_duplicate_names(self):
        self.assertIn("name must be", self.problems(yaml_text=YAML.replace("name: sink-a", "name: recorder")))
        self.assertIn("duplicate", self.problems(yaml_text=YAML.replace("name: sink-a", "name: twitch")))

    def test_missing_env_file(self):
        with self.assertRaises(ConfigError) as cm:
            load(os.path.join(self.tmp, "nope.env"), self.write("relay.yaml", YAML), root=self.tmp)
        self.assertIn("copy .env.example", cm.exception.problems[0])

    def test_committed_relay_yaml_is_valid(self):
        from relay.config import DEFAULT_CONFIG

        config = load(self.write(".env", GOOD_ENV), DEFAULT_CONFIG, root=self.tmp)
        self.assertEqual([o["name"] for o in config.outputs], ["twitch", "youtube", "facebook", "sink-a", "sink-b"])


BASE_ENV = {
    "OUTPUT_NAME": "t",
    "OUTPUT_URL": "rtmps://h/app",
    "OUTPUT_KEY": "SECRET",
    "RELAY_SOURCE_URL": "rtmp://127.0.0.1:1935/live/ingest",
    "RELAY_API_URL": "http://127.0.0.1:9997",
    "RELAY_INGEST_PATH": "live/ingest",
    "OUTPUT_WIDTH": "1664",
    "OUTPUT_HEIGHT": "936",
    "OUTPUT_FPS": "60",
    "OUTPUT_VIDEO_KBPS": "5500",
    "OUTPUT_AUDIO_KBPS": "160",
    "OUTPUT_KEYFRAME_SECONDS": "2",
}


class RenditionCommandTests(unittest.TestCase):
    def make(self, **env):
        from relay.output import Wrapper

        full = dict(BASE_ENV, RELAY_STATE_DIR=tempfile.mkdtemp(), **env)
        with mock.patch.dict(os.environ, full, clear=True):
            return Wrapper()

    def arg(self, cmd, flag):
        return cmd[cmd.index(flag) + 1]

    def test_nvenc_decodes_scales_and_encodes_on_gpu(self):
        cmd, _ = self.make(OUTPUT_VIDEO_ENCODER="h264_nvenc").build_command()
        self.assertEqual(self.arg(cmd, "-hwaccel"), "cuda")
        self.assertLess(cmd.index("-hwaccel"), cmd.index("-i"))
        self.assertEqual(self.arg(cmd, "-vf"), "fps=60,scale_cuda=1664:936")
        self.assertEqual(self.arg(cmd, "-c:v"), "h264_nvenc")
        self.assertEqual(self.arg(cmd, "-rc"), "cbr")
        self.assertEqual(self.arg(cmd, "-b:v"), "5500k")
        self.assertEqual(self.arg(cmd, "-g"), "120")
        self.assertEqual(self.arg(cmd, "-b:a"), "160k")
        self.assertNotIn("copy", cmd)
        # Key and TLS verification behave exactly as for a copy.
        self.assertEqual(cmd[-1], "rtmps://h/app/SECRET")
        self.assertEqual(self.arg(cmd, "-tls_verify"), "1")

    def test_libx264_for_test_hosts(self):
        cmd, _ = self.make(OUTPUT_VIDEO_ENCODER="libx264").build_command()
        self.assertNotIn("-hwaccel", cmd)
        self.assertEqual(self.arg(cmd, "-c:v"), "libx264")
        self.assertEqual(self.arg(cmd, "-sc_threshold"), "0")

    def test_copy_is_unchanged(self):
        cmd, _ = self.make(OUTPUT_VIDEO_ENCODER="copy").build_command()
        self.assertEqual(self.arg(cmd, "-c"), "copy")
        self.assertNotIn("-c:v", cmd)

    def test_rendition_in_report_and_never_the_key(self):
        w = self.make(OUTPUT_VIDEO_ENCODER="h264_nvenc")
        self.assertEqual(w.rendition_label, "1664x936 60fps 5500k+160k h264_nvenc")
        w.write_report()
        with open(w.report_path) as fh:
            text = fh.read()
        self.assertIn("1664x936", text)
        self.assertNotIn("SECRET", text)

    def test_ffmpeg_path_from_env(self):
        cmd, _ = self.make(OUTPUT_VIDEO_ENCODER="copy", FFMPEG_EXE="C:\\tools\\ffmpeg.exe").build_command()
        self.assertEqual(cmd[0], "C:\\tools\\ffmpeg.exe")

    def test_recorder_refuses_to_encode(self):
        with self.assertRaises(SystemExit):
            self.make(OUTPUT_KIND="record", RECORD_DIR=tempfile.mkdtemp(), OUTPUT_VIDEO_ENCODER="h264_nvenc")

    def test_incomplete_rendition_refused(self):
        with self.assertRaises(SystemExit):
            self.make(OUTPUT_VIDEO_ENCODER="h264_nvenc", OUTPUT_FPS="")


if __name__ == "__main__":
    unittest.main()
