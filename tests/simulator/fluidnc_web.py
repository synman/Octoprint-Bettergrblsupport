"""FluidNC's WebUI file endpoint for FluidNCVirtual, so HTTP config uploads can be tested like a real board.

Port of ``WebUI/WebUIServer.cpp`` @ 9b8236f, ``/files`` only (``on("/files", HTTP_ANY, handleFileList,
LocalFSFileupload)``, line 387), backed by the simulator's LocalFS (``FluidNCVirtual._localfs``):

* POST multipart: every file part is written to LocalFS under its part filename (``FluidPath{filename,
  LocalFS}``, ``uploadStart`` lines 1366-1476).  Opening the file (``FileStream(fpath, "w")``, line 1448)
  truncates an existing one.  An optional ``<filename>S`` form field carries the expected size, and only when it
  is present and non-zero is there a space pre-check (``filesize > available + existing`` -> "Upload rejected, not
  enough space", nothing opened, the old file untouched, lines 1411-1418) and a size check after the write
  (``uploadEnd`` 1534-1542).  Every failure replies HTTP 500 ``text/xml`` with the error text and no
  ``Cache-Control`` header (``pushError``, lines 1045-1057, ``request->send(web_error, "text/xml", st)``).
* The failed file is NOT removed on the paths a config upload can take: ``uploadCheck`` (1568-1578) removes a file
  only while ``_uploadFile`` is still set, and ``uploadEnd`` (1499-1556) has already deleted that stream and
  set it to null by then.  So a size mismatch leaves the complete file in place, and a write that runs out of
  space in the final chunk leaves the truncated partial one.  The port treats a part as one final chunk (a real
  upload arrives in TCP-segment chunks, and a failure in an earlier chunk would be removed by ``uploadCheck``).
* GET/POST: ``path`` picks the directory; ``action=delete|rename`` with ``filename`` (and ``newname``) act on it;
  ``dontlist=yes`` skips the listing (``handleFileOps``, lines 1244-1356).
* The reply is the file list JSON: ``files[{name, shortname, size, datetime:""}]``, then ``path``, ``total``,
  ``used``, ``occupation``, ``status`` (``advance_file_list_chunk`` / ``append_file_entry``), with
  ``Cache-Control: no-cache``.

Not modelled: authentication (``is_authenticated`` is LEVEL_ADMIN with authentication disabled, the default
build), the other WebUI routes, directories other than the LocalFS root tree, and the SD ``/upload`` route.
The LocalFS capacity is a fixed ``LOCALFS_TOTAL`` (a 192 KB LittleFS partition); ``formatBytes`` is
NutsBolts.cpp:145.
"""
import email.parser
import email.policy
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fluidnc_virtual import LOCALFS_PREFIX, canon_path

LOCALFS_TOTAL = 192 * 1024


def format_bytes(n):
    """NutsBolts.cpp:formatBytes @ 9b8236f (lines 145-163)."""
    if n < 1024:
        return "%d B" % n
    b = n / 1024.0
    for unit in ("KB", "MB", "GB"):
        if b < 1024 or unit == "GB":
            return "%.2f %s" % (b, unit)
        b /= 1024.0


class FluidNCWebServer(object):
    """Serves ``/files`` for whichever FluidNCVirtual is current (``server.sim`` is swapped on reconnect)."""

    def __init__(self, host, port, sim=None):
        self.sim = sim
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer._handle(self, None)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer._handle(self, self.rfile.read(length) if length else b"")

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, name="fluidnc-web", daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ------------------------------------------------------------------ request handling
    def _send(self, req, code, ctype, body, no_cache=True):
        data = body.encode("utf-8")
        req.send_response(code)
        req.send_header("Content-Type", ctype)
        req.send_header("Content-Length", str(len(data)))
        if no_cache:
            req.send_header("Cache-Control", "no-cache")
        req.end_headers()
        req.wfile.write(data)

    def _handle(self, req, body):
        url = urllib.parse.urlsplit(req.path)
        if url.path != "/files":
            self._send(req, 404, "text/plain", "Not found")
            return
        sim = self.sim
        if sim is None:
            self._send(req, 503, "text/plain", "No controller")
            return
        params = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        status = "Ok"
        upload_dir = None
        ctype = req.headers.get("Content-Type", "")
        if body is not None and ctype.startswith("multipart/form-data"):
            fields, files = self._parse_multipart(ctype, body)
            params.update({k: v for k, v in fields.items() if k not in params})
            for filename, content in files:
                err = self._upload(sim, filename, content, fields.get(filename + "S"))
                if err:
                    self._send(req, 500, "text/xml", err, no_cache=False)   # pushError(..., web_error = 500)
                    return
                parent = filename.rsplit("/", 1)[0] if "/" in filename.strip("/") else ""
                upload_dir = parent.strip("/")
        path = params.get("path", upload_dir or "")
        path = path.replace("//", "/").strip("/") if path else ""
        root = canon_path(path) if path else LOCALFS_PREFIX
        if "action" in params and "filename" in params:
            status = self._action(sim, root, params)
        listing = params.get("dontlist") != "yes"
        self._send(req, 200, "application/json", self._file_list(sim, root, path, status, listing))

    def _parse_multipart(self, ctype, body):
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + ctype.encode("latin-1") + b"\r\n\r\n" + body)
        fields, files = {}, []
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files.append((filename, payload))
            elif name:
                fields[name] = payload.decode("utf-8", "replace")
        return fields, files

    def _upload(self, sim, filename, content, size_field):
        """uploadStart / uploadWrite / uploadEnd for one part; returns an error text or None."""
        key = canon_path(filename)
        try:
            expected = int(size_field) if size_field else 0
        except ValueError:
            expected = 0                                       # String::toInt() of junk is 0
        existing = len(sim._localfs[key].encode("utf-8")) if key in sim._localfs else 0
        others = sum(len(v.encode("utf-8")) for p, v in sim._localfs.items() if p != key)
        available = LOCALFS_TOTAL - others - existing
        if expected and expected > available + existing:
            return "Upload rejected, not enough space"         # before anything is opened or truncated
        room = LOCALFS_TOTAL - others
        data = content.encode("utf-8") if isinstance(content, str) else content
        if len(data) > room:
            # FileStream(fpath, "w") truncated the old file; only what fitted was written, and it stays
            sim._localfs[key] = data[:room].decode("utf-8", "ignore")
            return "File write failed"
        sim._localfs[key] = data.decode("utf-8", "replace")
        if expected and expected != len(data):
            return "File upload mismatch"                      # the complete file stays (see docstring)
        return None

    def _action(self, sim, root, params):
        action, filename = params["action"], params["filename"]
        target = root + "/" + filename.strip("/")
        if action == "delete":
            if target in sim._localfs:
                del sim._localfs[target]
                return filename + " deleted"
            return "Cannot delete " + filename + " No such file or directory"
        if action == "rename":
            if "newname" not in params:
                return "Missing new filename"
            if target not in sim._localfs:
                return "Cannot rename " + filename + " No such file or directory"
            sim._localfs[root + "/" + params["newname"].strip("/")] = sim._localfs.pop(target)
            return filename + " renamed to " + params["newname"]
        return "Ok"

    def _file_list(self, sim, root, path, status, listing):
        used = sum(len(v.encode("utf-8")) for v in sim._localfs.values())
        doc = {}
        if listing:
            entries = []
            for p in sorted(sim._localfs):
                if p.startswith(root + "/") and "/" not in p[len(root) + 1:]:
                    name = p[len(root) + 1:]
                    entries.append({"name": name, "shortname": name,
                                    "size": len(sim._localfs[p].encode("utf-8")), "datetime": ""})
            doc["files"] = entries
        doc["path"] = path
        doc["total"] = format_bytes(LOCALFS_TOTAL)
        doc["used"] = format_bytes(used)
        doc["occupation"] = used * 100 // LOCALFS_TOTAL
        doc["status"] = status
        return json.dumps(doc)
