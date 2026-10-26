#!/usr/bin/env python3
"""The position estimate ``hook_gcode_sending`` keeps while a job or a jog runs (``grblX``/``grblY``/``grblZ``).

OctoPrint suppresses status requests while a job runs, so the sent lines are the only live source: a line's axis words
are added to the estimate when the line is relative and replace it when it is absolute.  A ``$J=`` jog carries its own
distance mode (``G90`` / ``G91`` inside the line) and leaves the parser's mode alone, so for a jog the estimate must
follow the jog's word, falling back to the parser's mode only when the jog names none.

Needs OctoPrint importable (the plugin class derives from it): run with a rig's own interpreter, from this directory,

    PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m unittest test_jog_estimate -v
"""
import logging
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import octoprint_bettergrblsupport as bgs

GRBL = "[VER:1.1h.20190830:] [OPT:V,15,128]"


def plugin(positioning, x=2.0, y=3.0, z=-4.0):
    p = bgs.BetterGrblSupportPlugin()
    p._logger = logging.getLogger("test")
    p._printer_profile_manager = types.SimpleNamespace(get_current_or_default=lambda: {"id": "_bgs"})
    p._printer = types.SimpleNamespace(commands=lambda *a, **k: None, is_printing=lambda: False, is_operational=lambda: True)
    p._plugin_manager = types.SimpleNamespace(send_plugin_message=lambda *a, **k: None)
    p._identifier = "bettergrblsupport"
    p._settings = types.SimpleNamespace(get=lambda key: None, get_boolean=lambda key: False)
    p.grblVersion = GRBL
    p.positioning = positioning
    p.grblX, p.grblY, p.grblZ = x, y, z
    return p


def after(positioning, line, **kw):
    p = plugin(positioning, **kw)
    result = p.hook_gcode_sending(None, "sending", line, None, None)
    assert result is not None and result[0] is not None, "the hook dropped %r" % line
    return p


class JogEstimate(unittest.TestCase):
    def test_a_relative_jog_adds_whatever_the_parser_mode_is(self):
        for mode in (0, 1):
            p = after(mode, "$J=G91 G21 X5 Y-1 Z0.5 F500")
            self.assertEqual((p.grblX, p.grblY, p.grblZ), (7.0, 2.0, -3.5), mode)

    def test_an_absolute_jog_replaces_whatever_the_parser_mode_is(self):
        for mode in (0, 1):
            p = after(mode, "$J=G90 G21 X5 Y-1 Z0.5 F500")
            self.assertEqual((p.grblX, p.grblY, p.grblZ), (5.0, -1.0, 0.5), mode)

    def test_a_jog_that_names_no_distance_mode_follows_the_parsers(self):
        p = after(1, "$J=G21 X5 F500")
        self.assertEqual(p.grblX, 7.0)
        p = after(0, "$J=G21 X5 F500")
        self.assertEqual(p.grblX, 5.0)

    def test_a_jog_leaves_the_parser_distance_mode_alone(self):
        self.assertEqual(after(0, "$J=G91 G21 X5").positioning, 0)
        self.assertEqual(after(1, "$J=G90 G21 X5").positioning, 1)

    def test_an_ordinary_line_follows_the_parser_mode(self):
        p = after(1, "G1 X5 Y-1 F500")
        self.assertEqual((p.grblX, p.grblY), (7.0, 2.0))
        p = after(0, "G1 X5 Y-1 F500")
        self.assertEqual((p.grblX, p.grblY), (5.0, -1.0))

    def test_a_line_that_sets_the_distance_mode_is_estimated_in_that_mode(self):
        p = after(0, "G91 G0 X5")
        self.assertEqual((p.positioning, p.grblX), (1, 7.0))


if __name__ == "__main__":
    unittest.main()
