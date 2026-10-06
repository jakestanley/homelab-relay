import unittest

from relay.install import SPEC_VAR, _decode, apply, remove_orphans


class FakeNssm:
    """Records calls; services keep their status and environment."""

    def __init__(self):
        self.services = {}
        self.calls = []

    def status(self, name):
        return self.services.get(name, {}).get("status")

    def get_env(self, name):
        return dict(self.services.get(name, {}).get("env", {}))

    def install(self, name, exe):
        self.calls.append(("install", name))
        self.services[name] = {"status": "SERVICE_STOPPED", "env": {}}

    def set(self, name, param, *values):
        self.calls.append(("set", name, param))
        if param == "AppEnvironmentExtra":
            self.services[name]["env"] = dict(v.split("=", 1) for v in values)
            self.services[name]["env_values"] = values

    def start(self, name):
        self.calls.append(("start", name))
        self.services[name]["status"] = "SERVICE_RUNNING"

    def restart(self, name):
        self.calls.append(("restart", name))
        self.services[name]["status"] = "SERVICE_RUNNING"

    def stop(self, name):
        self.calls.append(("stop", name))
        self.services[name]["status"] = "SERVICE_STOPPED"

    def remove(self, name):
        self.calls.append(("remove", name))
        del self.services[name]

    def installed_with_prefix(self, prefix):
        return [n for n in self.services if n.startswith(prefix + "-")]

    def actions(self, *kinds):
        return [c[:2] for c in self.calls if c[0] in kinds]


def services(twitch_key="k1"):
    def svc(name, env):
        return {"name": name, "description": "d", "exe": "C:\\py.exe", "args": ["-m", "relay.output"], "env": env}

    return [
        svc("homelab-relay-mediamtx", {"MTX_X": "1"}),
        svc("homelab-relay-out-twitch", {"OUTPUT_KEY": twitch_key}),
        svc("homelab-relay-out-youtube", {"OUTPUT_KEY": "k2"}),
    ]


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.nssm = FakeNssm()
        self.lines = []
        apply(services(), self.nssm, "logs", out=self.lines.append)
        self.nssm.calls.clear()
        self.lines.clear()

    def test_first_apply_installs_and_starts_everything(self):
        nssm = FakeNssm()
        apply(services(), nssm, "logs", out=lambda line: None)
        self.assertEqual(len(nssm.actions("install")), 3)
        self.assertEqual(len(nssm.actions("start")), 3)
        for s in nssm.services.values():
            self.assertEqual(s["status"], "SERVICE_RUNNING")
            self.assertIn(SPEC_VAR, s["env"])

    def test_rerun_with_no_change_touches_nothing(self):
        apply(services(), self.nssm, "logs", out=self.lines.append)
        self.assertEqual(self.nssm.actions("install", "set", "start", "restart", "stop"), [])
        self.assertTrue(all(line.endswith("unchanged") for line in self.lines))

    def test_changing_one_key_restarts_only_that_output(self):
        apply(services(twitch_key="new"), self.nssm, "logs", out=self.lines.append)
        self.assertEqual(self.nssm.actions("restart", "start"), [("restart", "homelab-relay-out-twitch")])
        self.assertEqual(self.nssm.services["homelab-relay-out-twitch"]["env"]["OUTPUT_KEY"], "new")

    def test_stopped_service_is_started_not_reconfigured(self):
        self.nssm.services["homelab-relay-out-youtube"]["status"] = "SERVICE_STOPPED"
        apply(services(), self.nssm, "logs", out=self.lines.append)
        self.assertEqual(self.nssm.actions("set", "start", "restart"), [("start", "homelab-relay-out-youtube")])

    def test_no_start_installs_without_starting(self):
        nssm = FakeNssm()
        apply(services(), nssm, "logs", start=False, out=lambda line: None)
        self.assertEqual(nssm.actions("start", "restart"), [])

    def test_output_never_prints_values(self):
        apply(services(twitch_key="SECRETKEY"), self.nssm, "logs", out=self.lines.append)
        self.assertNotIn("SECRETKEY", "\n".join(self.lines))

    def test_empty_values_are_not_passed_to_nssm(self):
        nssm = FakeNssm()
        apply(services(twitch_key=""), nssm, "logs", out=lambda line: None)
        values = nssm.services["homelab-relay-out-twitch"]["env_values"]
        self.assertFalse([v for v in values if v.endswith("=")])
        self.assertNotIn("OUTPUT_KEY", nssm.services["homelab-relay-out-twitch"]["env"])

    def test_dropping_an_empty_value_is_a_change(self):
        nssm = FakeNssm()
        apply(services(twitch_key=""), nssm, "logs", out=lambda line: None)
        twitch = nssm.services["homelab-relay-out-twitch"]
        # A service applied before empty values were dropped carries them.
        twitch["env"] = dict(twitch["env"], OUTPUT_KEY="", **{SPEC_VAR: "old"})
        nssm.calls.clear()
        apply(services(twitch_key=""), nssm, "logs", out=lambda line: None)
        self.assertIn(("restart", "homelab-relay-out-twitch"), nssm.actions("restart"))

    def test_removed_slot_is_removed(self):
        remaining = services()[:2]
        remove_orphans(remaining, self.nssm, "homelab-relay", out=self.lines.append)
        self.assertEqual(self.nssm.actions("stop", "remove"), [
            ("stop", "homelab-relay-out-youtube"),
            ("remove", "homelab-relay-out-youtube"),
        ])

    def test_decode_utf16_from_nssm(self):
        self.assertEqual(_decode("SERVICE_RUNNING\r\n".encode("utf-16-le")).strip(), "SERVICE_RUNNING")
        self.assertEqual(_decode(b"SERVICE_STOPPED\r\n").strip(), "SERVICE_STOPPED")


if __name__ == "__main__":
    unittest.main()
