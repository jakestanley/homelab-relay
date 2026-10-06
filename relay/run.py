"""Run one relay process in a container: `python -m relay.run <service>`.

The Linux (Docker) counterpart of relay.install. Both take each process's
program, arguments and environment from Config.services(), so an output is
encoded, keyed and validated the same way on both platforms; neither
platform has its own copy of that logic.

<service> is the suffix Config.services() names it by: mediamtx,
out-<slot>, recorder or status.

The container's environment stands in for .env (compose passes the file
with env_file, plus container paths through `environment:`). The process
is exec'd with only its own service environment plus PATH and HOME, as
NSSM gives it on Windows, so the status page never sees a platform key even
though every container is handed the whole .env.

`python -m relay.run --check` validates and prints the same summary as
`python -m relay.config`, for scripts/up.sh to run before touching anything.
"""

import os
import sys

from relay.config import DEFAULT_CONFIG, ROOT, ConfigError, load_env, summary
from relay.install import applied_env

# Kept from the container's own environment: needed to find and run the
# program, and none of them carries configuration.
PASSTHROUGH = ("PATH", "HOME", "LANG", "TZ")


def resolve(environ, suffix, config_path=DEFAULT_CONFIG, root=ROOT):
    """(argv, env) for one service. Raises ConfigError or KeyError."""
    config = load_env(dict(environ), config_path, root)
    for service in config.services("", sys.executable):
        if service["name"] == "-" + suffix:
            env = {k: environ[k] for k in PASSTHROUGH if k in environ}
            env.update(applied_env(service["env"]))
            return [service["exe"]] + list(service["args"]), env
    names = ", ".join(s["name"][1:] for s in config.services("", sys.executable))
    raise KeyError("no service {!r}; expected one of: {}".format(suffix, names))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m relay.run <service> | --check", file=sys.stderr)
        return 2
    try:
        if argv[0] == "--check":
            for line in summary(load_env(dict(os.environ))):
                print(line)
            return 0
        args, env = resolve(os.environ, argv[0])
    except ConfigError as exc:
        for problem in exc.problems:
            print("config: " + problem, file=sys.stderr)
        return 1
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return 2
    sys.stdout.flush()
    os.execvpe(args[0], args, env)


if __name__ == "__main__":
    sys.exit(main())
