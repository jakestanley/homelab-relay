"""Helpers shared by the output wrapper and the status glue.

Standard library only, and no POSIX-only calls, so the same code runs under
Docker today and under NSSM on Windows if the service ever moves there.
"""

import json
import logging
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

REDACTED = "<redacted>"


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


def write_json_atomic(path, data):
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def fetch_ingest(api_url, ingest_path, timeout=2.0):
    """Ask MediaMTX whether the ingest path has a ready publisher.

    Returns the path document when ingest is live, {} when the path exists
    but nothing is publishing, and None when the API cannot be reached.
    """
    url = "{}/v3/paths/get/{}".format(
        api_url.rstrip("/"), urllib.parse.quote(ingest_path, safe="/")
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            doc = json.load(resp)
    except urllib.error.HTTPError as exc:
        # 404: no publisher has created the path yet. That is "no ingest",
        # not an API failure.
        return {} if exc.code == 404 else None
    except (OSError, ValueError):
        return None
    return doc if doc.get("ready") else {}
