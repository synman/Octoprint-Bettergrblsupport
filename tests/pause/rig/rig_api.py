"""Helpers for driving a throwaway OctoPrint rig over its REST API and reading its serial.log / octoprint.log.

The rig is described by two environment variables: ``RIG`` (the rig directory: ``<RIG>/basedir``, ``<RIG>/apikey``)
and ``RIG_PORT`` (its HTTP port on 127.0.0.1).  See ``docs/simulator-testing.md`` section 5 for building one.
Standard library only.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid

RIG = os.environ["RIG"]
BASE = "http://127.0.0.1:" + os.environ["RIG_PORT"]
KEY = open(RIG + "/apikey").read().strip()
SERIAL_LOG = RIG + "/basedir/logs/serial.log"
OCTOPRINT_LOG = RIG + "/basedir/logs/octoprint.log"
_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d+) - (Send: |>>> |Recv: |<<< )(.*)$")


def api(method, path, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"X-Api-Key": KEY, "Content-Type": ctype})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode()
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "_body": error.read().decode()[:300]}
    return json.loads(text) if text.strip()[:1] in "{[" and text.strip() else text


def _size(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


def _tail(path, offset):
    with open(path, "rb") as file:
        file.seek(offset if _size(path) >= offset else 0)
        return file.read().decode("utf-8", "replace")


def mark():
    """A position in both logs: pass it to ``serial_rows`` / ``plugin_log`` to read only what came after."""
    return (_size(SERIAL_LOG), _size(OCTOPRINT_LOG))


def serial_rows(since, keep_status=False):
    """``[(epoch seconds, ">" sent | "<" received, text)]`` since ``mark()``; status polls and acks left out unless asked."""
    rows = []
    for line in _tail(SERIAL_LOG, since[0]).splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        stamp = time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")) + int(match.group(2)) / 1000.0
        text = match.group(4)
        if not keep_status and (text == "?" or text.startswith("<") or text in ("ok", "")):
            continue
        rows.append((stamp, ">" if match.group(3) in ("Send: ", ">>> ") else "<", text))
    return rows


def plugin_log(since, pattern):
    return [line[:220] for line in _tail(OCTOPRINT_LOG, since[1]).splitlines() if re.search(pattern, line)]


def format_rows(rows, t0):
    return "\n".join("%+8.2fs %s %s" % (t - t0, d, repr(x)) for t, d, x in rows)


def command(text):
    return api("POST", "/api/printer/command", {"command": text})


def plugin_command(name, **kw):
    kw["command"] = name
    return api("POST", "/api/plugin/bettergrblsupport", kw)


def connection_state():
    return api("GET", "/api/connection")["current"]["state"]


def job_state():
    return api("GET", "/api/job")["state"]


def wait_for(predicate, timeout=30, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def connect(port, settle=22):
    api("POST", "/api/connection", {"command": "disconnect"})
    wait_for(lambda: connection_state() == "Closed", 15)
    api("POST", "/api/connection", {"command": "connect", "port": port, "baudrate": 115200, "printerProfile": "_bgs"})
    wait_for(lambda: connection_state().startswith("Operational"), 60)
    time.sleep(settle)          # the plugin's connect handshake ($I, $G, $#, $CD, $S) has to finish


def upload(name):
    boundary = uuid.uuid4().hex
    head = ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\nContent-Type: application/octet-stream\r\n\r\n'
            % (boundary, name)).encode()
    body = head + open(RIG + "/" + name, "rb").read() + ("\r\n--%s--\r\n" % boundary).encode()
    return api("POST", "/api/files/local", raw=body, ctype="multipart/form-data; boundary=" + boundary)


def select(name):
    return api("POST", "/api/files/local/" + name, {"command": "select", "print": False})


def job(name, **kw):
    kw["command"] = name
    return api("POST", "/api/job", kw)


def wait_job(prefix, timeout=60):
    return wait_for(lambda: job_state().startswith(prefix) and job_state(), timeout)
