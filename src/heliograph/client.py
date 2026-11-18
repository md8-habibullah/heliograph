"""Talking to a running service from the command line, and starting it when needed."""

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import APP_SLUG
from .paths import paths
from .server import TOKEN_HEADER


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def call_api(info, method, path, body=None, timeout=8):
    conn = http.client.HTTPConnection("127.0.0.1", info["port"], timeout=timeout)
    try:
        headers = {"Host": f"127.0.0.1:{info['port']}", TOKEN_HEADER: info.get("token", "")}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        obj = json.loads(raw) if raw else {}
        if resp.status >= 400:
            raise RuntimeError(obj.get("error", f"HTTP {resp.status}"))
        return obj
    finally:
        conn.close()


def server_info():
    """The running service's connection details, or None."""
    try:
        info = json.loads(paths().info.read_text())
        if not pid_alive(info.get("pid")):
            return None
        if call_api(info, "GET", "/api/ping", timeout=2).get("app") != APP_SLUG:
            return None
        return info
    except (OSError, ValueError, KeyError, RuntimeError, ConnectionError, socket.timeout):
        return None


def ensure_server():
    info = server_info()
    if info:
        return info
    p = paths()
    p.ensure()
    log = open(p.log, "ab")
    src = str(Path(__file__).resolve().parents[1])                     # lets `python -m heliograph` work from a checkout
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(x for x in (src, os.environ.get("PYTHONPATH", "")) if x))
    subprocess.Popen([sys.executable, "-m", "heliograph", "serve"], stdin=subprocess.DEVNULL, stdout=log,
                     stderr=log, start_new_session=True, close_fds=True, env=env)
    deadline = time.time() + 15
    while time.time() < deadline:
        info = server_info()
        if info:
            return info
        time.sleep(0.15)
    raise RuntimeError(f"the service did not start - see {p.log}")
