"""The resource governor: background work asks it before running and yields when the PC is busy.

It looks at what the *user* would feel, not at our own numbers:
  * overall CPU use (/proc/stat), and pressure-stall information (/proc/pressure/*) for CPU,
    disk I/O and memory - the kernel's own "something is waiting" signal;
  * free memory (MemAvailable);
  * whether the laptop runs on battery.
States: "idle" (plenty of headroom), "normal", "busy" (background work pauses) and "paused"
(the user switched background indexing off). Work that the user is waiting for - thumbnails and
details of what is on screen - is never blocked by the governor, only limited in parallelism.
"""

import glob
import os
import threading
import time

BUSY_CPU = 0.78            # overall CPU use above this = busy
IDLE_CPU = 0.35            # below this (and quiet disk) = idle
BUSY_PSI_CPU = 25.0        # percent of the last 10 s with tasks waiting for a CPU
BUSY_PSI_IO = 20.0
BUSY_PSI_MEM = 4.0
LOW_MEM_MB = 700


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _psi(name):
    """avg10 of the 'some' line of /proc/pressure/<name>, or 0.0."""
    for line in _read(f"/proc/pressure/{name}").splitlines():
        if line.startswith("some"):
            for part in line.split():
                if part.startswith("avg10="):
                    try:
                        return float(part[6:])
                    except ValueError:
                        return 0.0
    return 0.0


def _meminfo_available_mb():
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemAvailable:"):
            try:
                return int(line.split()[1]) // 1024
            except (ValueError, IndexError):
                break
    return 99999


def _on_battery():
    try:
        for p in glob.glob("/sys/class/power_supply/*/"):
            if _read(p + "type").strip() == "Mains" and _read(p + "online").strip() == "1":
                return False
        for p in glob.glob("/sys/class/power_supply/BAT*/"):
            return _read(p + "status").strip().lower() == "discharging"
    except OSError:
        pass
    return False


class Governor:
    def __init__(self, get_setting=None):
        self._get_setting = get_setting or (lambda k, d=None: d)
        self._lock = threading.Lock()
        self._last = None
        self._at = 0.0
        self._cpu_prev = None
        self._cores = os.cpu_count() or 1

    def _cpu_util(self):
        parts = _read("/proc/stat").split("\n", 1)[0].split()[1:]
        try:
            vals = [int(x) for x in parts[:8]]
        except ValueError:
            return 0.0
        total, idle = sum(vals), vals[3] + vals[4]
        prev, self._cpu_prev = self._cpu_prev, (total, idle)
        if not prev or total <= prev[0]:
            return 0.0
        return max(0.0, min(1.0, 1.0 - (idle - prev[1]) / (total - prev[0])))

    def snapshot(self, max_age=1.5):
        """Current machine state (cached for max_age seconds)."""
        with self._lock:
            now = time.monotonic()
            if self._last and now - self._at < max_age:
                return self._last
            cpu = self._cpu_util()
            psi = {"cpu": _psi("cpu"), "io": _psi("io"), "memory": _psi("memory")}
            mem = _meminfo_available_mb()
            battery = _on_battery()
            reasons = []
            if cpu > BUSY_CPU:
                reasons.append(f"CPU {cpu * 100:.0f}% busy")
            if psi["cpu"] > BUSY_PSI_CPU:
                reasons.append("CPU contention")
            if psi["io"] > BUSY_PSI_IO:
                reasons.append("disk busy")
            if psi["memory"] > BUSY_PSI_MEM:
                reasons.append("memory pressure")
            if mem < LOW_MEM_MB:
                reasons.append(f"low memory ({mem} MB free)")
            if self._get_setting("bg_index", "auto") == "paused":
                state, reasons = "paused", ["paused by you"]
            elif reasons:
                state = "busy"
            elif cpu < IDLE_CPU and psi["io"] < 5 and not battery:
                state = "idle"
            else:
                state = "normal"
            self._last = {"state": state, "reasons": reasons, "cpu": round(cpu, 3), "psi": psi,
                          "mem_available_mb": mem, "battery": battery, "cores": self._cores}
            self._at = now
            return self._last

    # ---- decisions
    def background_ok(self):
        """May low-priority background work run right now?"""
        return self.snapshot()["state"] in ("idle", "normal")

    def expensive_ok(self):
        """May expensive background work (starting ffprobe for every file) run right now?"""
        s = self.snapshot()
        return s["state"] == "idle"

    def pause_ratio(self):
        """After a burst of work that took t seconds, sleep t * ratio (a duty cycle)."""
        s = self.snapshot()
        if s["state"] == "idle":
            return 0.35
        return 1.5 if s["battery"] else 1.0

    def foreground_workers(self):
        """How many thumbnail jobs may run at once for what is on screen."""
        s = self.snapshot()
        n = 3 if self._cores >= 6 else 2
        if s["mem_available_mb"] < LOW_MEM_MB or s["psi"]["memory"] > BUSY_PSI_MEM:
            n = 1
        return n
