"""Configuration: relay.yaml plus .env, compiled into each process's environment.

relay.yaml (committed) names the output slots and the rendition each is
encoded to. .env (never committed) holds ports, paths, URLs and keys. This
module validates both and turns them into one flat environment per service.

It runs at install time (relay.install on Windows, test/acceptance.py) and
at container start (relay.run on Linux). The
broadcast-path processes never read either file: each gets a flat
environment, so what a running output does is fixed by what was applied to
its service, and nothing changes under it until that service is restarted.
"""

import os
import re
import sys

import yaml

from relay.common import COPY, describe_rendition

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT, "relay.yaml")
EXE = ".exe" if os.name == "nt" else ""

# MediaMTX refuses any other character in a path name, before it ever
# compares the key, so a key outside this set is refused on every publish.
# "/" is allowed by MediaMTX but would split the key into path segments.
INGEST_KEY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
INGEST_KEY_PLACEHOLDER = "change-me-to-match-batw-STREAM_KEY_LIVE"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
RESERVED_NAMES = {"mediamtx", "recorder", "status"}
# "copy" makes a host forward ingest untouched to every output, whatever
# relay.yaml's renditions say: a relay with no encoder to spare (the adler
# fallback). It is a property of the host, so it lives in .env, not relay.yaml.
VIDEO_ENCODERS = ("h264_nvenc", "libx264", COPY)

RENDITION_FIELDS = ("width", "height", "fps", "video_kbps", "audio_kbps", "keyframe_seconds")
OUTPUT_FIELDS = {"name", "url_env", "key_env", "tls_verify_env", "rendition", "max_kbps"}


class ConfigError(Exception):
    """Every problem found, so one run of up.ps1 reports them all."""

    def __init__(self, problems):
        super().__init__("\n".join(problems))
        self.problems = problems


def read_dotenv(path):
    """KEY=VALUE lines; blank lines and # comments ignored.

    Matching single or double quotes around a value are removed. Nothing is
    expanded, so a value is exactly what is written.
    """
    values = {}
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def check_ingest_key(key):
    """A problem with the ingest key, or None."""
    if not key:
        return "INGEST_KEY is empty. Set it to batw's STREAM_KEY_LIVE."
    if key == INGEST_KEY_PLACEHOLDER:
        return "INGEST_KEY is still the .env.example placeholder. Set it to batw's STREAM_KEY_LIVE."
    if not INGEST_KEY_RE.match(key):
        bad = "".join(sorted({c for c in key if not INGEST_KEY_RE.match(c)}))
        return (
            "INGEST_KEY contains characters MediaMTX refuses in a path: {!r}. Use only letters, "
            "digits, '_', '.' and '-', and the same value in batw's STREAM_KEY_LIVE. Generate one "
            "with: python -c \"import secrets; print(secrets.token_urlsafe(32))\"".format(bad)
        )
    return None


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class Config:
    """Validated relay.yaml + .env. Construct with load()."""

    def __init__(self, env, doc, root):
        self.env = env
        self.root = root
        self.problems = []

        def need(name):
            value = env.get(name, "").strip()
            if not value:
                self.problems.append("{} must be set in .env".format(name))
            return value

        def port(name, default=None):
            raw = env.get(name, "").strip() or (default or "")
            if not raw:
                self.problems.append("{} must be set in .env".format(name))
                return 0
            if not raw.isdigit() or not 0 < int(raw) < 65536:
                self.problems.append("{} must be a port number, not {!r}".format(name, raw))
                return 0
            return int(raw)

        self.service_port = port("SERVICE_PORT")
        self.rtmp_port = port("RTMP_PORT")
        self.api_port = port("RELAY_API_PORT", "9997")
        self.ingest_key = env.get("INGEST_KEY", "").strip()
        problem = check_ingest_key(self.ingest_key)
        if problem:
            self.problems.append(problem)
        self.record_dir = need("RECORD_DIR")
        self.state_dir = env.get("RELAY_STATE_DIR", "").strip() or os.path.join(root, "state")
        self.record_min_free_gb = env.get("RECORD_MIN_FREE_GB", "").strip() or "20"
        self.io_timeout = env.get("OUTPUT_IO_TIMEOUT", "").strip() or "10"
        for name, value in (("RECORD_MIN_FREE_GB", self.record_min_free_gb), ("OUTPUT_IO_TIMEOUT", self.io_timeout)):
            try:
                if float(value) < 0:
                    raise ValueError
            except ValueError:
                self.problems.append("{} must be a non-negative number, not {!r}".format(name, value))

        self.video_encoder = env.get("VIDEO_ENCODER", "").strip() or "h264_nvenc"
        if self.video_encoder not in VIDEO_ENCODERS:
            self.problems.append(
                "VIDEO_ENCODER must be one of {}, not {!r}".format(", ".join(VIDEO_ENCODERS), self.video_encoder)
            )
        self.ffmpeg_exe = env.get("FFMPEG_EXE", "").strip() or os.path.join(root, "tools", "ffmpeg", "bin", "ffmpeg" + EXE)
        self.mediamtx_exe = env.get("MEDIAMTX_EXE", "").strip() or os.path.join(root, "tools", "mediamtx", "mediamtx" + EXE)

        self.renditions = self._renditions(doc.get("renditions"))
        self.outputs = self._outputs(doc.get("outputs"))
        unknown = set(doc) - {"renditions", "outputs"}
        if unknown:
            self.problems.append("relay.yaml: unknown top-level keys: {}".format(", ".join(sorted(unknown))))

    # -- relay.yaml -----------------------------------------------------------

    def _renditions(self, raw):
        renditions = {}
        if not isinstance(raw, dict) or not raw:
            self.problems.append("relay.yaml: renditions must be a non-empty mapping")
            return renditions
        for name, r in raw.items():
            where = "relay.yaml: rendition {}".format(name)
            if name == COPY:
                self.problems.append("{}: 'copy' is reserved for sending ingest untouched".format(where))
                continue
            if not isinstance(r, dict):
                self.problems.append("{}: must be a mapping".format(where))
                continue
            missing = [f for f in RENDITION_FIELDS if f not in r]
            unknown = sorted(set(r) - set(RENDITION_FIELDS))
            if missing:
                self.problems.append("{}: missing {}".format(where, ", ".join(missing)))
            if unknown:
                self.problems.append("{}: unknown keys {}".format(where, ", ".join(unknown)))
            if missing or unknown:
                continue
            bad = [f for f in RENDITION_FIELDS if not _positive_int(r[f])]
            if bad:
                self.problems.append("{}: {} must be positive whole numbers".format(where, ", ".join(bad)))
                continue
            if r["width"] % 2 or r["height"] % 2:
                self.problems.append("{}: width and height must be even for 4:2:0 H.264".format(where))
                continue
            if r["fps"] > 120:
                self.problems.append("{}: fps {} is not a streaming frame rate".format(where, r["fps"]))
                continue
            renditions[name] = dict(r)
        return renditions

    @property
    def host_copies(self):
        return self.video_encoder == COPY

    def rendition_of(self, output):
        """The rendition an output is actually sent: copy on a copy host."""
        return COPY if self.host_copies else output["rendition"]

    def _outputs(self, raw):
        outputs = []
        if not isinstance(raw, list) or not raw:
            self.problems.append("relay.yaml: outputs must be a non-empty list")
            return outputs
        seen = set()
        for o in raw:
            if not isinstance(o, dict):
                self.problems.append("relay.yaml: every output must be a mapping")
                continue
            name = o.get("name")
            where = "relay.yaml: output {}".format(name)
            if not isinstance(name, str) or not NAME_RE.match(name) or name in RESERVED_NAMES:
                self.problems.append(
                    "{}: name must be lower case letters, digits and '-', and not one of {}".format(
                        where, ", ".join(sorted(RESERVED_NAMES))
                    )
                )
                continue
            if name in seen:
                self.problems.append("{}: duplicate name".format(where))
                continue
            seen.add(name)
            unknown = sorted(set(o) - OUTPUT_FIELDS)
            if unknown:
                self.problems.append("{}: unknown keys {}".format(where, ", ".join(unknown)))
                continue
            if not o.get("url_env"):
                self.problems.append("{}: url_env is required".format(where))
                continue
            rendition = o.get("rendition")
            if rendition != COPY and rendition not in self.renditions:
                self.problems.append("{}: rendition {!r} is not defined (or is invalid)".format(where, rendition))
                continue
            max_kbps = o.get("max_kbps")
            if max_kbps is not None:
                if not _positive_int(max_kbps):
                    self.problems.append("{}: max_kbps must be a positive whole number".format(where))
                    continue
                # On a copy host the publisher sets every output's bitrate;
                # summary() repeats the ceiling instead of refusing.
                if self.host_copies:
                    outputs.append(dict(o))
                    continue
                if rendition == COPY:
                    self.problems.append(
                        "{}: max_kbps cannot be checked for 'copy'; the publisher sets that bitrate".format(where)
                    )
                    continue
                r = self.renditions[rendition]
                total = r["video_kbps"] + r["audio_kbps"]
                if total > max_kbps:
                    self.problems.append(
                        "{}: rendition {} is {} kbps (video {} + audio {}), over the platform's {}".format(
                            where, rendition, total, r["video_kbps"], r["audio_kbps"], max_kbps
                        )
                    )
                    continue
            outputs.append(dict(o))
        return outputs

    # -- compiled environments ------------------------------------------------

    @property
    def ingest_path(self):
        return "live/" + self.ingest_key

    def _consumer_env(self):
        return {
            "RELAY_API_URL": "http://127.0.0.1:{}".format(self.api_port),
            "RELAY_INGEST_PATH": self.ingest_path,
            "RELAY_SOURCE_URL": "rtmp://127.0.0.1:{}/{}".format(self.rtmp_port, self.ingest_path),
            "RELAY_STATE_DIR": self.state_dir,
            "OUTPUT_IO_TIMEOUT": self.io_timeout,
            "FFMPEG_EXE": self.ffmpeg_exe,
            "PYTHONUNBUFFERED": "1",
        }

    def output_env(self, output):
        env = self._consumer_env()
        key_env = output.get("key_env")
        tls_env = output.get("tls_verify_env")
        env.update(
            {
                "OUTPUT_NAME": output["name"],
                "OUTPUT_KIND": "push",
                "OUTPUT_URL": self.env.get(output["url_env"], "").strip(),
                "OUTPUT_KEY": self.env.get(key_env, "").strip() if key_env else "",
                "OUTPUT_KEY_REQUIRED": "true" if key_env else "false",
                "OUTPUT_TLS_VERIFY": (self.env.get(tls_env, "").strip() or "true") if tls_env else "true",
            }
        )
        rendition = self.rendition_of(output)
        if rendition == COPY:
            env["OUTPUT_VIDEO_ENCODER"] = COPY
        else:
            r = self.renditions[rendition]
            env.update(
                {
                    "OUTPUT_VIDEO_ENCODER": self.video_encoder,
                    "OUTPUT_WIDTH": str(r["width"]),
                    "OUTPUT_HEIGHT": str(r["height"]),
                    "OUTPUT_FPS": str(r["fps"]),
                    "OUTPUT_VIDEO_KBPS": str(r["video_kbps"]),
                    "OUTPUT_AUDIO_KBPS": str(r["audio_kbps"]),
                    "OUTPUT_KEYFRAME_SECONDS": str(r["keyframe_seconds"]),
                }
            )
        return env

    def recorder_env(self):
        env = self._consumer_env()
        env.update(
            {
                "OUTPUT_NAME": "recorder",
                "OUTPUT_KIND": "record",
                "OUTPUT_VIDEO_ENCODER": COPY,
                "RECORD_DIR": self.record_dir,
            }
        )
        return env

    def status_env(self):
        # Never given a platform key, so no endpoint behind it can expose one.
        return {
            "SERVICE_PORT": str(self.service_port),
            "RELAY_API_URL": "http://127.0.0.1:{}".format(self.api_port),
            "RELAY_INGEST_PATH": self.ingest_path,
            "RELAY_STATE_DIR": self.state_dir,
            "RELAY_OUTPUTS": ",".join(o["name"] for o in self.outputs),
            "RECORD_DIR": self.record_dir,
            "RECORD_MIN_FREE_GB": self.record_min_free_gb,
            "PYTHONUNBUFFERED": "1",
        }

    def mediamtx_env(self):
        return {
            # The one path a publisher may use. See mediamtx/mediamtx.yml.
            "MTX_AUTHINTERNALUSERS_0_PERMISSIONS_0_PATH": self.ingest_path,
            "MTX_RTMPADDRESS": ":{}".format(self.rtmp_port),
            # The API is for the consumers and the status glue on this host
            # only, so it listens on loopback.
            "MTX_APIADDRESS": "127.0.0.1:{}".format(self.api_port),
        }

    def services(self, prefix, python_exe):
        """Every process the relay runs, as {name, description, exe, args, env}.

        One per output slot whether enabled or not: a disabled output idles
        rather than being left out, so turning a platform back on is a
        change to its environment, not to which services exist.
        """
        py = lambda module: [python_exe, ["-m", module]]  # noqa: E731
        specs = [
            ("mediamtx", "RTMP ingest (MediaMTX)", [self.mediamtx_exe, [os.path.join(self.root, "mediamtx", "mediamtx.yml")]], self.mediamtx_env()),
        ]
        for o in self.outputs:
            specs.append(("out-" + o["name"], "output " + o["name"], py("relay.output"), self.output_env(o)))
        specs.append(("recorder", "archive recorder", py("relay.output"), self.recorder_env()))
        specs.append(("status", "status page", py("relay.status"), self.status_env()))
        return [
            {
                "name": "{}-{}".format(prefix, suffix),
                "description": "homelab-relay: {}".format(what),
                "exe": exe_args[0],
                "args": exe_args[1],
                "env": env,
            }
            for suffix, what, exe_args, env in specs
        ]


def load(env_path, config_path=DEFAULT_CONFIG, root=ROOT, overrides=None):
    """Load and validate. Raises ConfigError listing every problem."""
    problems = []
    env = {}
    if not os.path.exists(env_path):
        problems.append("missing {}: copy .env.example to .env and fill it in".format(env_path))
    else:
        env = read_dotenv(env_path)
    env.update(overrides or {})
    return load_env(env, config_path, root, problems)


def load_env(env, config_path=DEFAULT_CONFIG, root=ROOT, problems=None):
    """Validate an environment already read (a container's, on Linux)."""
    problems = list(problems or [])
    try:
        with open(config_path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(problems + ["cannot read {}: {}".format(config_path, exc)])
    if not isinstance(doc, dict):
        raise ConfigError(problems + ["{}: must be a mapping".format(config_path)])
    config = Config(env, doc, root)
    problems += config.problems
    if problems:
        raise ConfigError(problems)
    return config


def main(argv=None):
    """`python -m relay.config [.env]`: validate, print a summary, exit non-zero on problems."""
    argv = sys.argv[1:] if argv is None else argv
    env_path = argv[0] if argv else os.path.join(ROOT, ".env")
    try:
        config = load(env_path)
    except ConfigError as exc:
        for problem in exc.problems:
            print("config: " + problem, file=sys.stderr)
        return 1
    for line in summary(config):
        print(line)
    return 0


def summary(config):
    """Which outputs are enabled and what each is sent; never a key."""
    lines = []
    for o in config.outputs:
        env = config.output_env(o)
        enabled = env["OUTPUT_URL"] and (env["OUTPUT_KEY"] or env["OUTPUT_KEY_REQUIRED"] == "false")
        line = "output {:<10} {:<9} {}".format(o["name"], "enabled" if enabled else "disabled", describe_rendition(env))
        if config.host_copies and o.get("max_kbps"):
            line += " (VIDEO_ENCODER=copy: OBS must stay under {} kbps)".format(o["max_kbps"])
        lines.append(line)
    lines.append("recorder   copy of ingest to {}".format(config.record_dir))
    return lines



if __name__ == "__main__":
    sys.exit(main())
