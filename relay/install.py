"""Install, update, restart, stop and remove the relay's NSSM services.

Run by scripts/up.ps1 and scripts/install-service.ps1 from the repo's venv,
elevated. Every process the relay runs is its own NSSM service (see
relay.config.Config.services), so NSSM supervises each output separately and a
failing output never takes another down with it.

  python -m relay.install apply   [--prefix P] [--no-start]
  python -m relay.install restart [--prefix P]
  python -m relay.install stop    [--prefix P]
  python -m relay.install remove  [--prefix P]
  python -m relay.install status  [--prefix P]
  python -m relay.install ports

`apply` is what up.ps1 runs, and it is safe to re-run during a broadcast: it
installs missing services, starts stopped ones, and restarts only services
whose configuration changed. Changing one platform's key restarts that output
and nothing else. `restart` restarts everything, and is never for use during
a broadcast.

Change detection: each service's environment carries RELAY_SPEC_SHA256, a
hash of everything this module would apply to it. If the hash matches, the
service is left alone. Re-applying everything regardless is `remove` then
`apply`.

Never prints environment values: they include platform keys.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys

from relay.config import ROOT, ConfigError, load

SPEC_VAR = "RELAY_SPEC_SHA256"
DEFAULT_PREFIX = os.path.basename(ROOT)
LOG_ROTATE_BYTES = 10 * 1024 * 1024

# NSSM settings every service gets, beyond its program, arguments and
# environment. Part of the hash, so changing one here re-applies it.
COMMON_SETTINGS = [
    ("Start", ["SERVICE_AUTO_START"]),
    # Restart whatever the exit code. The wrappers never exit because ffmpeg
    # did, so an exit here is a crash and restarting is the right response.
    ("AppExit", ["Default", "Restart"]),
    ("AppRestartDelay", ["2000"]),
    # NSSM sends Ctrl+C first. The wrapper then stops ffmpeg (up to ~7 s)
    # before exiting, so allow it longer than NSSM's 1.5 s default.
    ("AppStopMethodConsole", ["15000"]),
    ("AppKillProcessTree", ["1"]),
    ("AppRotateFiles", ["1"]),
    ("AppRotateOnline", ["1"]),
    ("AppRotateBytes", [str(LOG_ROTATE_BYTES)]),
]


class NssmError(Exception):
    pass


def _decode(data):
    # nssm writes UTF-16 when its output is redirected, UTF-8/ANSI otherwise.
    if b"\x00" in data:
        return data.decode("utf-16-le", errors="replace").replace("﻿", "")
    return data.decode(errors="replace")


class Nssm:
    """The nssm.exe calls this module makes. Replaced by a fake in tests."""

    def __init__(self, exe="nssm"):
        self.exe = exe

    def _run(self, *args, check=True):
        proc = subprocess.run([self.exe, *args], capture_output=True)
        out = (_decode(proc.stdout) + _decode(proc.stderr)).strip()
        if check and proc.returncode != 0:
            # args[2:] may hold environment values; name the call, not them.
            raise NssmError("nssm {} {} failed: {}".format(args[0], " ".join(args[1:3]), out))
        return proc.returncode, out

    def status(self, name):
        """SERVICE_RUNNING, SERVICE_STOPPED, ..., or None if not installed."""
        code, out = self._run("status", name, check=False)
        return out.split()[0] if code == 0 and out else None

    def get_env(self, name):
        code, out = self._run("get", name, "AppEnvironmentExtra", check=False)
        if code != 0:
            return {}
        env = {}
        for line in out.splitlines():
            key, sep, value = line.strip().partition("=")
            if sep:
                env[key] = value
        return env

    def install(self, name, exe):
        self._run("install", name, exe)

    def set(self, name, param, *values):
        self._run("set", name, param, *values)

    def start(self, name):
        self._run("start", name)

    def restart(self, name):
        self._run("restart", name)

    def stop(self, name):
        self._run("stop", name, check=False)

    def remove(self, name):
        self._run("remove", name, "confirm")

    def installed_with_prefix(self, prefix):
        """Service names starting with "<prefix>-", including ones this
        configuration no longer defines (a slot removed from relay.yaml)."""
        import winreg  # Windows only; never reached elsewhere.

        names = []
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services") as key:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                if sub.lower().startswith(prefix.lower() + "-"):
                    names.append(sub)
                i += 1
        return names


def settings_for(service, logs_dir):
    """Every `nssm set` for one service, in order, as (param, [values])."""
    log = os.path.join(logs_dir, service["name"] + ".log")
    settings = [
        ("Application", [service["exe"]]),
        ("AppParameters", [subprocess.list2cmdline(service["args"])]),
        ("AppDirectory", [ROOT]),
        ("DisplayName", [service["name"]]),
        ("Description", [service["description"]]),
        ("AppStdout", [log]),
        ("AppStderr", [log]),
    ] + COMMON_SETTINGS
    return settings


def spec_hash(service, settings):
    doc = {"settings": settings, "env": sorted(service["env"].items())}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def plan(services, nssm, logs_dir):
    """What apply would do to each service: install, update, start or nothing.

    Pure apart from nssm reads, so the decisions are testable off Windows.
    """
    steps = []
    for service in services:
        settings = settings_for(service, logs_dir)
        digest = spec_hash(service, settings)
        status = nssm.status(service["name"])
        if status is None:
            action = "install"
        elif nssm.get_env(service["name"]).get(SPEC_VAR) != digest:
            action = "update"
        elif status != "SERVICE_RUNNING":
            action = "start"
        else:
            action = "unchanged"
        steps.append({"service": service, "settings": settings, "hash": digest, "status": status, "action": action})
    return steps


def apply(services, nssm, logs_dir, start=True, out=print):
    for step in plan(services, nssm, logs_dir):
        service, name, action = step["service"], step["service"]["name"], step["action"]
        if action in ("install", "update"):
            if action == "install":
                nssm.install(name, service["exe"])
            for param, values in step["settings"]:
                nssm.set(name, param, *values)
            env = dict(service["env"], **{SPEC_VAR: step["hash"]})
            # Never pass an empty value: NSSM drops every entry after a
            # "KEY=" one when it builds the process environment, so a
            # disabled output (empty OUTPUT_KEY) lost OUTPUT_URL, OUTPUT_WIDTH
            # and the rest, and crash-looped. Seen on shrike, NSSM 2.24-101.
            # The wrappers read a missing variable as empty, so omitting it
            # is the same value.
            nssm.set(name, "AppEnvironmentExtra", *["{}={}".format(k, v) for k, v in sorted(env.items()) if v != ""])
        if not start:
            out("{:<32} {}".format(name, action + (" (not started)" if action != "unchanged" else "")))
            continue
        if action == "update" and step["status"] == "SERVICE_RUNNING":
            nssm.restart(name)
            out("{:<32} configuration changed; restarted".format(name))
        elif action in ("install", "update", "start"):
            nssm.start(name)
            out("{:<32} {}; started".format(name, {"install": "installed", "update": "updated", "start": "was stopped"}[action]))
        else:
            out("{:<32} unchanged".format(name))


def remove_orphans(services, nssm, prefix, out=print):
    """Stop and remove services this configuration no longer defines."""
    wanted = {s["name"].lower() for s in services}
    for name in nssm.installed_with_prefix(prefix):
        if name.lower() not in wanted and nssm.status(name) is not None:
            nssm.stop(name)
            nssm.remove(name)
            out("{:<32} no longer configured; removed".format(name))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m relay.install")
    parser.add_argument("action", choices=["apply", "restart", "stop", "remove", "status", "ports"])
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="service name prefix (default: repo folder name)")
    parser.add_argument("--env", default=os.path.join(ROOT, ".env"))
    parser.add_argument("--no-start", action="store_true", help="apply: install/update only")
    parser.add_argument("--nssm", default="nssm")
    args = parser.parse_args(argv)

    try:
        config = load(args.env)
    except ConfigError as exc:
        for problem in exc.problems:
            print("config: " + problem, file=sys.stderr)
        return 1

    if args.action == "ports":
        print(json.dumps({"rtmp": config.rtmp_port, "http": config.service_port}))
        return 0
    if os.name != "nt":
        print("relay.install manages NSSM services and runs on Windows only.", file=sys.stderr)
        return 1

    services = config.services(args.prefix, sys.executable)
    nssm = Nssm(args.nssm)
    logs_dir = os.path.join(ROOT, "logs")
    try:
        if args.action == "apply":
            for directory in (logs_dir, config.state_dir, config.record_dir):
                os.makedirs(directory, exist_ok=True)
            apply(services, nssm, logs_dir, start=not args.no_start)
            remove_orphans(services, nssm, args.prefix)
        elif args.action == "restart":
            for s in services:
                if nssm.status(s["name"]) == "SERVICE_RUNNING":
                    nssm.restart(s["name"])
                else:
                    nssm.start(s["name"])
                print("{:<32} restarted".format(s["name"]))
        elif args.action == "stop":
            for s in reversed(services):
                nssm.stop(s["name"])
                print("{:<32} stopped".format(s["name"]))
        elif args.action == "remove":
            for name in nssm.installed_with_prefix(args.prefix):
                nssm.stop(name)
                nssm.remove(name)
                print("{:<32} removed".format(name))
        elif args.action == "status":
            for s in services:
                print("{:<32} {}".format(s["name"], nssm.status(s["name"]) or "not installed"))
    except NssmError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
