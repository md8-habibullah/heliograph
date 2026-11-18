"""Live terminal status of the service: scan, details, thumbnails, conversions, resource governor."""

import shutil
import socket
import sys
import time
from pathlib import Path

from .client import call_api, server_info
from .util import fmt_duration, human


def bar(frac, width):
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return "█" * n + "░" * (width - n)


def render(st, prev, width):
    def c(code, s):
        return f"\x1b[{code}m{s}\x1b[0m"

    home = str(Path.home())

    def tilde(p):
        return "~" + p[len(home):] if p.startswith(home) else p

    barw = max(10, min(40, width - 62))
    lines = [c("1", "Heliograph") + c("2", "  live status      q = quit"), ""]
    ui = "window open" if st["ui_alive"] else "no window open"
    idle = "" if st["idle_exit_in"] is None else f" · stops itself in {fmt_duration(st['idle_exit_in'])} if left idle"
    lines.append(f"Service    running · pid {st['pid']} · up {fmt_duration(st['uptime'])} · "
                 f"{st['rss_kb'] / 1024:.0f} MB RAM · {ui}{idle}")
    g = st["gov"]
    colour = {"idle": "32", "normal": "32", "busy": "33", "paused": "36"}.get(g["state"], "0")
    why = f" ({', '.join(g['reasons'])})" if g["reasons"] else ""
    lines.append(f"Machine    {c(colour, g['state'])}{why} · CPU {g['cpu'] * 100:.0f}% · "
                 f"{g['mem_available_mb']:,} MB free · {'on battery' if g['battery'] else 'on power'}")
    lines.append(f"Library    {st['files']:,} files in {len(st['roots'])} folder(s)")
    sc = st["scan"]
    if sc["state"] == "scanning":
        rate = ""
        if prev and prev["scan"]["state"] == "scanning" and st["now"] > prev["now"]:
            rate = f" · {max(0, (sc['files'] - prev['scan']['files']) / (st['now'] - prev['now'])):,.0f} files/s"
        lines.append(f"Scan       {c('33', 'scanning')} {tilde(sc['root'] or '')}: {sc['files']:,} files, "
                     f"{sc['dirs']:,} folders{rate}")
    else:
        last = f"last scan {sc['last_files']:,} files in {sc['last_seconds']}s" if sc["last_files"] else "no scan yet"
        lines.append(f"Scan       {c('32', 'idle')} · {last}")
    en = st["enrich"]
    done, rem = en["done"], en["remaining"]
    if en["state"] in ("running", "waiting", "paused") and (done + rem):
        frac = done / (done + rem)
        label = {"running": c("33", "reading"), "waiting": c("36", "waiting"), "paused": c("36", "paused")}[en["state"]]
        extra = f" · {en['reason']}" if en["reason"] else ""
        lines.append(f"Details    {label} {bar(frac, barw)} {frac * 100:5.1f}%  {done:,} read, {rem:,} to go{extra}")
        lines.append(c("2", "           (sizes, dates, durations - read in small bursts, on-screen files first)"))
    else:
        lines.append(f"Details    {c('32', 'idle')} · every file's details are up to date")
    th = st["thumbs"]
    avg = th["ms_total"] / max(1, th["made"] + th["failed"])
    ci = st.get("cache_info") or {}
    lines.append(f"Thumbs     made {th['made']:,} · from cache {th['cached']:,} · failed {th['failed']:,} · "
                 f"generating {th['inflight']} (+{th['queued']} waiting) · avg {avg:,.0f} ms · "
                 f"{ci.get('thumbs', 0):,} stored, {human(ci.get('thumbs_bytes', 0))}")
    j = st["jobs"]
    lines.append(f"Convert    {j['running']} running · {j['queued']} queued    "
                 f"Playback streams {st['stream']['active']}")
    lines.append("")
    lines.append(c("1", "Reading") + "    " + (", ".join(
        tilde(r["path"]) + ("" if r["online"] else c("31", " (offline)")) + ("" if r["persist"] else c("2", " (opened)"))
        for r in st["roots"]) or "nothing yet"))
    lines.append(c("1", "Writing") + f"    {tilde(st['db'])} ({human(st['db_bytes'])}) · {tilde(st['cache'])}")
    return "\n".join(ln[:width + 40] for ln in lines)


def run(once=False, interval=1.0):
    def frame(prev):
        info = server_info()
        if not info:
            return None, "Heliograph service is not running.\nStart it with:  heliograph\n"
        st = call_api(info, "GET", "/api/status")
        return st, render(st, prev, shutil.get_terminal_size((100, 30)).columns)

    if once:
        st, text = frame(None)
        print(text)
        return 0 if st else 1
    import select
    tty_ok = sys.stdin.isatty()
    old = None
    if tty_ok:
        import termios
        import tty
        old = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
    prev = None
    try:
        sys.stdout.write("\x1b[?25l")
        while True:
            try:
                prev, text = frame(prev)
            except (RuntimeError, OSError, ConnectionError, socket.timeout) as e:
                prev, text = None, f"Cannot read status: {e}\n"
            sys.stdout.write("\x1b[H\x1b[J" + text + "\n")
            sys.stdout.flush()
            if tty_ok:
                if select.select([sys.stdin], [], [], interval)[0] and sys.stdin.read(1).lower() == "q":
                    break
            else:
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h\n")
        if old is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
    return 0
