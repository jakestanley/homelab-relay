"""Helpers shared by the output wrapper and the status glue.

Standard library only, and no POSIX-only calls: these run under NSSM on
Windows, and the acceptance checks also run them on Linux.
"""

import json
import logging
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request

REDACTED = "<redacted>"
COPY = "copy"

# On Windows a file cannot be replaced while another process has it open, and
# the status glue reads each report every poll. Each side retries briefly
# rather than treating that moment as a failure.
REPLACE_ATTEMPTS = 10
REPLACE_RETRY_SECONDS = 0.02


def env_str(name, default=""):
    return os.environ.get(name, default).strip()


def env_bool(name, default):
    raw = env_str(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def env_float(name, default):
    raw = env_str(name)
    return float(raw) if raw else default


def setup_logging(name):
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    return logging.getLogger(name)


def redact(text, secrets):
    """Replace every form of each secret that might appear in a log line."""
    for secret in secrets:
        if not secret:
            continue
        # Longest forms first, so a raw key does not break up its own
        # URL-encoded variant before that variant is replaced.
        forms = {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}
        for form in sorted(forms, key=len, reverse=True):
            text = text.replace(form, REDACTED)
    return text


def key_secrets(key):
    """The strings to redact for a platform key.

    A key may carry a query string (Twitch's `?bandwidthtest=true`), and
    a server may log the path without it, so the bare key is redacted too.
    """
    if not key:
        return []
    return [key, key.split("?", 1)[0]]


def join_destination(url, key):
    """Platform URL plus key, the way OBS joins "Server" and "Stream Key"."""
    if not key:
        return url
    return url.rstrip("/") + "/" + key


def describe_rendition(env):
    """One line for an output's rendition, safe to log and to show on the page."""
    if env.get("OUTPUT_VIDEO_ENCODER", COPY) == COPY:
        return "copy of ingest"
    return "{}x{} {}fps {}k+{}k {}".format(
        env["OUTPUT_WIDTH"], env["OUTPUT_HEIGHT"], env["OUTPUT_FPS"],
        env["OUTPUT_VIDEO_KBPS"], env["OUTPUT_AUDIO_KBPS"], env["OUTPUT_VIDEO_ENCODER"],
    )


def write_json_atomic(path, data):
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(REPLACE_RETRY_SECONDS)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path):
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except PermissionError:
            # Mid-replace on Windows; see REPLACE_ATTEMPTS.
            time.sleep(REPLACE_RETRY_SECONDS)
        except (OSError, ValueError):
            return None
    return None


def fetch_ingest(api_url, ingest_path, timeout=2.0):
    """Ask MediaMTX whether the ingest path has a ready publisher.

    Returns the path document when ingest is live, {} when nothing is
    publishing, and None when the API cannot be reached.

    Uses the list endpoint rather than /v3/paths/get/<path>: while there is
    no ingest, "get" answers 404 and MediaMTX logs every 404 at ERR level,
    which at one poll per second per consumer buries the log lines that
    matter. Only the ingest path can be published to, so the list is tiny.
    """
    url = "{}/v3/paths/list?itemsPerPage=1000".format(api_url.rstrip("/"))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            doc = json.load(resp)
    except (OSError, ValueError):
        return None
    for item in doc.get("items") or []:
        if item.get("name") == ingest_path:
            return item if item.get("ready") else {}
    return {}
