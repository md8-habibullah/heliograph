import unittest
from unittest import mock

from helpers import IsolatedHome  # noqa: F401

from heliograph import governor


def make(cpu=0.1, io=0.0, mem_psi=0.0, cpu_psi=0.0, mem_mb=4000, battery=False, setting="auto", cores=8):
    g = governor.Governor(get_setting=lambda k, d=None: setting if k == "bg_index" else d)
    g._cores = cores
    patches = [mock.patch.object(g, "_cpu_util", return_value=cpu),
               mock.patch.object(governor, "_psi", side_effect=lambda n: {"cpu": cpu_psi, "io": io, "memory": mem_psi}[n]),
               mock.patch.object(governor, "_meminfo_available_mb", return_value=mem_mb),
               mock.patch.object(governor, "_on_battery", return_value=battery)]
    for p in patches:
        p.start()
    return g, patches


class GovernorTest(unittest.TestCase):
    def state(self, **kw):
        g, patches = make(**kw)
        try:
            return g, g.snapshot(max_age=0)
        finally:
            for p in patches:
                p.stop()

    def test_idle_machine(self):
        g, s = self.state()
        self.assertEqual(s["state"], "idle")
        self.assertEqual(s["reasons"], [])

    def test_normal_when_somewhat_loaded_or_on_battery(self):
        self.assertEqual(self.state(cpu=0.5)[1]["state"], "normal")
        self.assertEqual(self.state(battery=True)[1]["state"], "normal")

    def test_busy_reasons(self):
        for kw, word in [(dict(cpu=0.95), "CPU"), (dict(io=40), "disk"), (dict(mem_psi=10), "memory pressure"),
                         (dict(cpu_psi=60), "contention"), (dict(mem_mb=300), "low memory")]:
            s = self.state(**kw)[1]
            self.assertEqual(s["state"], "busy", kw)
            self.assertTrue(any(word in r for r in s["reasons"]), (kw, s["reasons"]))

    def test_user_pause_wins(self):
        s = self.state(setting="paused")[1]
        self.assertEqual(s["state"], "paused")
        self.assertEqual(s["reasons"], ["paused by you"])

    def test_decisions(self):
        for kw, bg, expensive in [({}, True, True), (dict(cpu=0.5), True, False), (dict(cpu=0.95), False, False),
                                  (dict(setting="paused"), False, False)]:
            g, patches = make(**kw)
            try:
                self.assertEqual(g.background_ok(), bg, kw)
                self.assertEqual(g.expensive_ok(), expensive, kw)
            finally:
                for p in patches:
                    p.stop()

    def test_duty_cycle_and_workers(self):
        g, patches = make()
        try:
            self.assertLess(g.pause_ratio(), 0.5, "an idle machine can work almost continuously")
            self.assertEqual(g.foreground_workers(), 3)
        finally:
            for p in patches:
                p.stop()
        g, patches = make(cpu=0.5, battery=True, cores=4, mem_mb=500)
        try:
            self.assertGreater(g.pause_ratio(), 1.0, "on battery the duty cycle is gentler")
            self.assertEqual(g.foreground_workers(), 1, "low memory: one thumbnail at a time")
        finally:
            for p in patches:
                p.stop()

    def test_snapshot_is_cached_briefly(self):
        g, patches = make()
        try:
            a = g.snapshot()
            self.assertIs(g.snapshot(), a)
        finally:
            for p in patches:
                p.stop()


if __name__ == "__main__":
    unittest.main()
