#!/usr/bin/env python3
"""Tests for fluidnc_web.py, the port of FluidNC's WebUI ``/files`` endpoint (WebUIServer.cpp @ 9b8236f).

Run:  cd tests/simulator && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_fluidnc_web -v

Each test starts the real server on an ephemeral localhost port in front of a real ``FluidNCVirtual`` and talks to
it with ``urllib``.  Expected strings and behaviours are quoted from WebUIServer.cpp (line numbers in the
``fluidnc_web.py`` docstring), not produced by running the port.
"""
import json
import unittest
import urllib.error
import urllib.request
import uuid

from fluidnc_virtual import FluidNCVirtual, canon_path
from fluidnc_web import LOCALFS_TOTAL, FluidNCWebServer, format_bytes

NEW_YAML = "name: Replaced\naxes:\n  y:\n    max_travel_mm: 300\n"


def multipart(files, fields=None):
    """-> (content_type, body) for ``files`` [(filename, bytes)] and ``fields`` {name: str}."""
    b = uuid.uuid4().hex
    out = b""
    for k, v in (fields or {}).items():
        out += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v)).encode()
    for name, data in files:
        out += ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n' % (b, name)).encode() + data + b"\r\n"
    out += ("--%s--\r\n" % b).encode()
    return "multipart/form-data; boundary=" + b, out


class WebTest(unittest.TestCase):
    def setUp(self):
        self.sim = FluidNCVirtual(http_enable=True)
        self.web = FluidNCWebServer("127.0.0.1", 0, self.sim)
        self.addCleanup(self.web.close)
        self.base = "http://127.0.0.1:%d" % self.web.port

    def req(self, path, data=None, ctype=None):
        r = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": ctype} if ctype else {})
        try:
            with urllib.request.urlopen(r, timeout=10) as x:
                return x.status, x.headers, x.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def post(self, files, fields=None, query=""):
        ctype, body = multipart(files, fields)
        return self.req("/files" + query, body, ctype)

    def fs(self, name):
        return self.sim._localfs.get(canon_path(name))


class Upload(WebTest):
    def test_upload_replaces_config_yaml(self):
        self.assertIn("name:", self.fs("config.yaml"))
        code, hdr, body = self.post([("config.yaml", NEW_YAML.encode())])
        self.assertEqual(code, 200)
        self.assertEqual(hdr["Content-Type"], "application/json")
        self.assertEqual(hdr["Cache-Control"], "no-cache")
        self.assertEqual(self.fs("config.yaml"), NEW_YAML)
        self.assertEqual(json.loads(body)["status"], "Ok")

    def test_replaced_config_is_what_a_restart_loads(self):
        self.post([("config.yaml", b"axes:\n  y:\n    max_travel_mm: 300\n")])
        self.sim.feed(b"$Bye\n")
        self.sim.drain()
        self.sim.feed(b"$131\n")
        self.assertEqual([x.decode() for x in self.sim.drain()], ["$131=300.000\r\n", "ok\r\n"])

    def test_upload_is_not_a_delete_first(self):
        # the file is replaced in place: after any single upload there is exactly one config.yaml entry
        self.post([("config.yaml", NEW_YAML.encode())])
        self.post([("config.yaml", b"name: Again\n")])
        self.assertEqual(self.fs("config.yaml"), "name: Again\n")
        self.assertEqual(len([p for p in self.sim._localfs if p.endswith("config.yaml")]), 1)

    def test_size_field_that_matches_succeeds(self):
        code, _, body = self.post([("a.yaml", b"12345")], {"a.yamlS": "5"})
        self.assertEqual(code, 200)
        self.assertEqual(self.fs("a.yaml"), "12345")

    def test_size_mismatch_is_http_500_and_the_file_stays(self):
        # uploadEnd (WebUIServer.cpp:1534-1542) pushes "File upload mismatch" with web_error 500, but has already
        # deleted _uploadFile, so uploadCheck (1568-1578) removes nothing: the complete file remains.
        code, hdr, body = self.post([("a.yaml", b"12345")], {"a.yamlS": "9"})
        self.assertEqual(code, 500)
        self.assertEqual(hdr["Content-Type"], "text/xml")
        self.assertIsNone(hdr["Cache-Control"])
        self.assertEqual(body, "File upload mismatch")
        self.assertEqual(self.fs("a.yaml"), "12345")

    def test_size_field_is_ignored_when_zero_or_junk(self):
        for field in ("0", "abc", ""):
            code, _, _ = self.post([("b.yaml", b"xy")], {"b.yamlS": field})
            self.assertEqual(code, 200, field)

    def test_not_enough_space_with_a_size_field_leaves_the_old_file(self):
        # uploadStart 1411-1418: filesize > available + existing -> rejected before the file is opened
        old = self.fs("config.yaml")
        code, hdr, body = self.post([("config.yaml", b"x")], {"config.yamlS": str(LOCALFS_TOTAL + 1)})
        self.assertEqual((code, body), (500, "Upload rejected, not enough space"))
        self.assertEqual(self.fs("config.yaml"), old)

    def test_a_replacement_that_only_fits_because_it_replaces_is_accepted(self):
        room = LOCALFS_TOTAL - len(self.fs("config.yaml").encode())     # what is left beside config.yaml
        self.assertEqual(self.post([("big.bin", b"a" * (room - 1000))])[0], 200)
        code, _, _ = self.post([("big.bin", b"b" * (room - 500))], {"big.binS": str(room - 500)})
        self.assertEqual(code, 200)       # 500 bytes bigger than the free space, but it replaces its old self
        self.assertEqual(len(self.fs("big.bin")), room - 500)

    def test_write_that_runs_out_of_space_without_a_size_field_destroys_the_old_file(self):
        # No size field -> no pre-check.  FileStream(fpath, "w") truncates config.yaml, uploadWrite fails
        # ("File write failed", 1490), uploadEnd then closes the stream so uploadCheck leaves the partial file.
        room = LOCALFS_TOTAL - len(self.fs("config.yaml").encode())
        self.assertEqual(self.post([("filler.bin", b"f" * (room - 100))])[0], 200)
        code, _, body = self.post([("config.yaml", NEW_YAML.encode() * 200)])
        self.assertEqual((code, body), (500, "File write failed"))
        left = self.fs("config.yaml")
        self.assertIsNotNone(left)
        self.assertLess(len(left), len(NEW_YAML) * 200)
        self.assertNotIn("name: Default", left)


class Listing(WebTest):
    def test_list_json_shape_and_key_order(self):
        code, _, body = self.req("/files")
        self.assertEqual(code, 200)
        doc = json.loads(body)
        self.assertEqual(list(doc), ["files", "path", "total", "used", "occupation", "status"])
        self.assertEqual(doc["files"], [{"name": "config.yaml", "shortname": "config.yaml",
                                         "size": len(self.fs("config.yaml").encode()), "datetime": ""}])
        self.assertEqual(doc["path"], "")
        self.assertEqual(doc["total"], format_bytes(LOCALFS_TOTAL))
        self.assertEqual(doc["total"], "192.00 KB")
        self.assertEqual(doc["status"], "Ok")
        self.assertEqual(doc["occupation"], len(self.fs("config.yaml").encode()) * 100 // LOCALFS_TOTAL)

    def test_dontlist_omits_files(self):
        doc = json.loads(self.req("/files?dontlist=yes")[2])
        self.assertEqual(list(doc), ["path", "total", "used", "occupation", "status"])

    def test_format_bytes(self):
        self.assertEqual([format_bytes(n) for n in (0, 1023, 1024, 1536, 1048576)],
                         ["0 B", "1023 B", "1.00 KB", "1.50 KB", "1.00 MB"])

    def test_unknown_route_is_404_and_no_controller_is_503(self):
        self.assertEqual(self.req("/nope")[0], 404)
        self.web.sim = None
        self.assertEqual(self.req("/files")[0], 503)


class Operations(WebTest):
    def test_delete(self):
        doc = json.loads(self.req("/files?action=delete&filename=config.yaml")[2])
        self.assertEqual(doc["status"], "config.yaml deleted")
        self.assertEqual(doc["files"], [])
        self.assertIsNone(self.fs("config.yaml"))

    def test_delete_missing(self):
        doc = json.loads(self.req("/files?action=delete&filename=nope.yaml")[2])
        self.assertEqual(doc["status"], "Cannot delete nope.yaml No such file or directory")

    def test_rename(self):
        old = self.fs("config.yaml")
        doc = json.loads(self.req("/files?action=rename&filename=config.yaml&newname=old.yaml")[2])
        self.assertEqual(doc["status"], "config.yaml renamed to old.yaml")
        self.assertEqual([f["name"] for f in doc["files"]], ["old.yaml"])
        self.assertEqual(self.fs("old.yaml"), old)

    def test_rename_without_newname_and_of_a_missing_file(self):
        self.assertEqual(json.loads(self.req("/files?action=rename&filename=config.yaml")[2])["status"],
                         "Missing new filename")
        self.assertEqual(json.loads(self.req("/files?action=rename&filename=zz&newname=y")[2])["status"],
                         "Cannot rename zz No such file or directory")

    def test_path_parameter_selects_the_directory(self):
        code, _, body = self.post([("sub/x.txt", b"hi")])
        doc = json.loads(body)
        self.assertEqual(doc["path"], "sub")              # _uploadPath: the upload's parent dir is listed
        self.assertEqual([f["name"] for f in doc["files"]], ["x.txt"])
        self.assertEqual(self.fs("sub/x.txt"), "hi")
        doc = json.loads(self.req("/files?path=/sub//")[2])
        self.assertEqual((doc["path"], [f["name"] for f in doc["files"]]), ("sub", ["x.txt"]))
        root = json.loads(self.req("/files")[2])
        self.assertEqual([f["name"] for f in root["files"]], ["config.yaml"])
        doc = json.loads(self.req("/files?path=sub&action=delete&filename=x.txt")[2])
        self.assertEqual((doc["status"], doc["files"]), ("x.txt deleted", []))


if __name__ == "__main__":
    unittest.main()
