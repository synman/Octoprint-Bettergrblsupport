#!/usr/bin/env python3
"""``_viewer.cached``: a normalised copy that would pass the byte cap is refused (the viewer then serves the
original) and leaves nothing behind; one under it is built as before.

Needs OctoPrint importable (the package ``__init__`` imports it): run with a rig's own interpreter,

    PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest test_viewer_cache -v
"""
import importlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_viewer = importlib.import_module("octoprint_bettergrblsupport._viewer")


class CopyCap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = os.path.join(self.tmp.name, "cache")
        self.cap = _viewer.MAX_COPY_BYTES
        self.addCleanup(setattr, _viewer, "MAX_COPY_BYTES", self.cap)
        _viewer._failed.clear()

    def source(self, name, text):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_a_copy_past_the_cap_is_refused_and_leaves_no_files(self):
        src = self.source("arcs.gcode", "M3 S1000\n" + "G2 X0 Y0 I50 J0 F500\n" * 200)
        _viewer.MAX_COPY_BYTES = 100000
        self.assertIsNone(_viewer.cached(self.cache, "local", "arcs.gcode", src))
        self.assertEqual(os.listdir(self.cache), [])
        # remembered as failed: the second request does not rebuild it
        self.assertIsNone(_viewer.cached(self.cache, "local", "arcs.gcode", src))

    def test_a_copy_under_the_cap_is_built(self):
        src = self.source("small.gcode", "M3 S1000\nG0 X0 Y0\nG1 X10 Y10 F500\n")
        _viewer.MAX_COPY_BYTES = 100000
        built = _viewer.cached(self.cache, "local", "small.gcode", src)
        self.assertIsNotNone(built)
        self.assertTrue(all(os.path.isfile(p) for p in built))

    def test_the_shipped_cap_is_below_the_cache_budget(self):
        # one copy may not be able to evict everything else, nor sit above the whole budget
        self.assertLess(self.cap, _viewer.KEEP_BYTES)


if __name__ == "__main__":
    unittest.main()
