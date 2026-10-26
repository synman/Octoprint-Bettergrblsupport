"""Settings migration: the `_bgs` printer profile survives an upgrade from a v8 config (released 2.3.2).

Needs OctoPrint importable: run with a rig's own interpreter, from this directory,

    PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest test_migration -v
"""
import importlib
import logging
import os
import shutil
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
bgs = importlib.import_module("octoprint_bettergrblsupport")


class Settings(object):
    """Just enough of OctoPrint's plugin settings for on_settings_migrate."""

    def __init__(self, folder):
        self.folder = folder
        self.values = {}
        self.globals = {("appearance", "components", "order", "tab"): ["control", "terminal"],
                        ("appearance", "components", "disabled", "tab"): []}

    def get_boolean(self, path):
        return bool(self.values.get(tuple(path)))

    def set_boolean(self, path, value):
        self.values[tuple(path)] = bool(value)

    def get(self, path):
        return self.values.get(tuple(path))

    def set(self, path, value):
        self.values[tuple(path)] = value

    def remove(self, path):
        self.values.pop(tuple(path), None)

    def global_get(self, path):
        return self.globals.get(tuple(path))

    def global_set(self, path, value):
        self.globals[tuple(path)] = value

    def global_get_basefolder(self, name):
        return self.folder

    def save(self):
        pass


class Migration(unittest.TestCase):
    def migrate(self, current):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        profile = os.path.join(folder, "_bgs.profile")
        with open(profile, "w") as f:
            f.write("name: My Laser\n")
        p = bgs.BetterGrblSupportPlugin()
        p._logger = logging.getLogger("test")
        p._settings = Settings(folder)
        p._plugin_manager = types.SimpleNamespace(send_plugin_message=lambda *a, **k: None)
        p._identifier = "bettergrblsupport"
        p.on_settings_migrate(9, current)
        return os.path.exists(profile), p._settings.get_boolean(["profile_fixed"])

    def test_a_v8_config_keeps_the_users_profile(self):
        # released 2.3.2 is v8 and never persisted profile_fixed; its profile may carry the user's own edits
        self.assertEqual(self.migrate(8), (True, True))

    def test_a_pre_v8_config_still_drops_the_stale_profile(self):
        self.assertEqual(self.migrate(7), (False, True))

    def test_a_fresh_config_drops_the_stale_profile(self):
        self.assertEqual(self.migrate(None), (False, True))


if __name__ == "__main__":
    unittest.main()
