#!/usr/bin/env python3
"""Tests for fluidnc_virtual.py, the Python port of FluidNC (bdring/FluidNC @ 9b8236f).

Run:  cd tests/simulator && GRBL_SIM_CORPUS=<dir> PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_fluidnc_virtual -v

Expected bytes are quoted from the C++ format strings in the pinned tree (Report.cpp, Channel.cpp,
ProcessSettings.cpp, Protocol.cpp, FileCommands.cpp, Settings.cpp, Error.cpp ...), not produced by running the
port.  Test names carry the Better Grbl Support census ids: S.. = strings the plugin sends, P.. = patterns it
parses, F.. = firmware map rows.  Choices the source leaves open (version tag, default machine config) are the
ones declared in fluidnc_virtual.py's module docstring.
"""
import json
import os
import re
import string
import unittest

from fluidnc_virtual import FluidNCVirtual, collapse_gcode, generate_config_lines, parse_config_text

BANNER = ["\r\n", "Grbl 3.9 [FluidNC v3.9.9 (esp32-wifi) '$' for help]\r\n"]
OK = "ok\r\n"
CORPUS_DIR = os.environ.get("GRBL_SIM_CORPUS", "")
STATUS_RE = re.compile(r"<([^|>]+)((?:\|[^|>]+)*)>\r\n")

# Patterns copied from the plugin as listed in the census (section 2).
P02A = re.compile(r"<(-?[^,]+)[,\|][WM]Pos:(-?[\d\.]+),(-?[\d\.]+)(?:,(-?[\d\.]+))?(?:,(-?[\d\.]+))?(?:,(-?[\d\.]+))?")
P02D = re.compile(r"\|Pn:([XYZABPDHRS]+)")
P02E = re.compile(r"\|FS:(-?[\d\.]+),(-?[\d\.]+)")
P03 = re.compile(r"alarm:\ *(-?[\d.]+)")
P04 = re.compile(r"error:\ *(-?[\d.]+)")
P08 = re.compile(r"^[$](-?[\d\.]+)=(-?[\d\.]+)")

SETTINGS_XYZ = [
    "$Grbl/SoftLimitsEnable=0", "$Grbl/HardLimitsEnable=0", "$Grbl/HomingCycleEnable=0",
    "$Grbl/HomingDirections=0", "$Grbl/MaxSpindleSpeed=1000", "$Grbl/LaserMode=1",
    "$Grbl/Resolution/X=80.000", "$Grbl/Resolution/Y=80.000", "$Grbl/Resolution/Z=80.000",
    "$Grbl/MaxRate/X=1000.000", "$Grbl/MaxRate/Y=1000.000", "$Grbl/MaxRate/Z=1000.000",
    "$Grbl/Acceleration/X=25.000", "$Grbl/Acceleration/Y=25.000", "$Grbl/Acceleration/Z=25.000",
    "$Grbl/MaxTravel/X=1000.000", "$Grbl/MaxTravel/Y=1000.000", "$Grbl/MaxTravel/Z=1000.000",
    "$HTTP/BlockDuringMotion=ON", "$HTTP/Enable=OFF", "$HTTP/Port=80", "$Hostname=fluidnc",
    "$GCode/Echo=OFF", "$Start/Message=Grbl \\V [FluidNC \\B (\\X) \\H]", "$Firmware/Build=",
    "$SD/FallbackCS=-1", "$Report/Status=1", "$Config/Filename=config.yaml", "$Message/Level=Info",
]


def mk(*args, **kw):
    """Fresh controller with the banner (and any startup alarm lines) already drained."""
    g = FluidNCVirtual(*args, **kw)
    g.drain()
    return g


def out(g):
    return [x.decode("latin-1") for x in g.drain()]


def send(g, data):
    g.feed(data if isinstance(data, bytes) else data.encode("latin-1"))
    return out(g)


def status(line):
    """-> (state, {field: value}) for one status report line."""
    m = STATUS_RE.fullmatch(line)
    assert m, line
    fields = {}
    for part in m.group(2).split("|")[1:]:
        k, _, v = part.partition(":")
        fields[k] = v
    return m.group(1), fields


def mpos(line):
    return [float(v) for v in status(line)[1]["MPos"].split(",")]


def run_until_idle(g, step=0.05, limit=120.0):
    t = 0.0
    while t < limit:
        g.tick(step)
        t += step
        if status(send(g, b"?")[0])[0] == "Idle":
            return t
    raise AssertionError("never went idle")


def err(code, text, bad=None):
    lines = ["error:%d\r\n" % code, "[MSG:ERR: %s]\r\n" % text]
    return (["[MSG:ERR: Bad GCode: %s]\r\n" % bad] if bad else []) + lines


class Banner(unittest.TestCase):
    def test_F01_P01_banner_bytes(self):
        # Report.cpp:report_init_message: empty spacer line, then template "Grbl \V [FluidNC \B (\X) \H]"
        g = FluidNCVirtual()
        self.assertEqual(out(g), BANNER)
        self.assertRegex(BANNER[1], r"^Grbl")                           # plugin P01 gate (__init__:1201)

    def test_F01_banner_after_soft_reset(self):
        g = mk()
        self.assertEqual(send(g, b"\x18"), BANNER)                      # Protocol.cpp:protocol_do_soft_restart

    def test_F01_dollar_gs_prints_the_banner(self):
        self.assertEqual(send(mk(), b"$GS\n"), BANNER + [OK])           # ProcessSettings.cpp:report_init_message_cmd

    def test_F01_start_message_template_is_a_setting(self):
        g = mk()
        self.assertEqual(send(g, b"$Start/Message=Hello \\V\n"), [OK])
        self.assertEqual(send(g, b"$GS\n"), ["\r\n", "Hello 3.9\r\n", OK])

    def test_S04_S05_reset_forms(self):
        g = mk()
        self.assertEqual(send(g, b"\n\n\x18"), [OK, OK] + BANNER)
        self.assertEqual(send(g, b"\x18"), BANNER)

    def test_F22_default_machine_boots_idle_without_alarm(self):
        # Protocol.cpp:protocol_do_start: ALARM:14 only when an axis has homing configured
        g = FluidNCVirtual()
        self.assertEqual(out(g), BANNER)
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")

    def test_F22_homing_configured_boots_into_unhomed_alarm(self):
        g = FluidNCVirtual(homing="XYZ")
        self.assertEqual(out(g), BANNER + ["[MSG:INFO: ALARM: Unhomed]\r\n", "ALARM:14\r\n"])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")

    def test_F22_no_alarm_lock_hint_at_9b8236f(self):
        # Report.cpp:101 defines the text but nothing calls it: no "[MSG:... to unlock]" anywhere
        g = FluidNCVirtual(homing="XYZ")
        self.assertFalse([ln for ln in out(g) if "to unlock" in ln])


class StatusReports(unittest.TestCase):
    def test_F02_idle_wco_then_ov_cadence(self):
        g = mk()
        self.assertEqual(send(g, b"?"), ["<Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000>\r\n"])
        self.assertEqual(send(g, b"?"), ["<Idle|MPos:0.000,0.000,0.000|FS:0,0|Ov:100,100,100>\r\n"])
        self.assertEqual(send(g, b"?"), ["<Idle|MPos:0.000,0.000,0.000|FS:0,0>\r\n"])

    def test_F02_cadence_wco_1_11_21_ov_2_12_22(self):
        # Config.h:94-97 idle counts 10 / 10, stored minus one
        g = mk()
        wco, ov = [], []
        for i in range(1, 23):
            f = status(send(g, b"?")[0])[1]
            wco += ["WCO" in f and i]
            ov += ["Ov" in f and i]
        self.assertEqual(([i for i in wco if i], [i for i in ov if i]), ([1, 11, 21], [2, 12, 22]))

    def test_F02_cadence_while_busy_wco_1_31_61_ov_2_22_42(self):
        # Report.cpp:551 / :572 reload the counters for Homing, Cycle, Hold, Jog and SafetyDoor; Config.h:94-97
        # busy counts 30 (WCO) and 20 (Ov), stored minus one
        def run(g):
            wco, ov = [], []
            for i in range(1, 63):
                f = status(send(g, b"?")[0])[1]
                wco += ["WCO" in f and i]
                ov += ["Ov" in f and i]
            return [i for i in wco if i], [i for i in ov if i]
        busy = ([1, 31, 61], [2, 22, 42, 62])
        g = mk()
        send(g, b"G1X1000F600\n")
        g.tick(0.5)
        self.assertEqual(run(g), busy)                                  # Cycle
        g = mk()
        send(g, b"G1X1000F600\n")
        g.tick(0.5)
        send(g, b"!")
        g.tick(5.0)
        self.assertEqual(run(g), busy)                                  # Hold
        g = mk()
        send(g, b"$J=G91G21X1000F600\n")
        g.tick(0.1)
        self.assertEqual(run(g), busy)                                  # Jog
        g = mk()
        send(g, b"\x84")
        self.assertEqual(run(g), busy)                                  # SafetyDoor

    def test_S01_S02_S03_bare_question_mark_gets_status_and_ok(self):
        g = mk()
        r = send(g, b"?\n")
        self.assertEqual((status(r[0])[0], r[1]), ("Idle", OK))

    def test_S09_census_two_status_reports_for_poke_with_jog_cancel_byte(self):
        # RealtimeCmd.cpp:15-17 calls report_realtime_status per '?'; the leftover blank line is acked once
        g = mk()
        r = send(g, b"? \x85 ?\n")
        self.assertEqual([status(r[0])[0], status(r[1])[0], r[2]], ["Idle", "Idle", OK])
        self.assertEqual(len(r), 3)

    def test_F02_question_mark_answers_while_the_main_loop_is_blocked(self):
        # Channel::pollLine calls report_realtime_status directly; the gnea flag would wait for the blocked loop
        g = mk()
        send(g, b"G4P5\n")                                  # a dwell blocks the loop
        self.assertEqual(len(send(g, b"?")), 1)

    def test_F02_state_names(self):
        g = mk()
        send(g, b"G1X100F600\n")
        g.tick(0.5)
        self.assertEqual(status(send(g, b"?")[0])[0], "Run")
        send(g, b"!")
        self.assertEqual(status(send(g, b"?")[0])[0], "Hold:1")
        g.tick(5.0)
        self.assertEqual(status(send(g, b"?")[0])[0], "Hold:0")
        g2 = mk()
        send(g2, b"$C\n")
        self.assertEqual(status(send(g2, b"?")[0])[0], "Check")
        g3 = mk()
        send(g3, b"$J=G91G21X10F600\n")
        g3.tick(0.1)
        self.assertEqual(status(send(g3, b"?")[0])[0], "Jog")
        g4 = mk()
        r = send(g4, b"?\x84?\n")
        self.assertEqual([status(r[0])[0], r[1], status(r[2])[0]], ["Idle", "[MSG:INFO: Check door]\r\n", "Door:0"])
        g5 = mk()
        send(g5, b"$SLP\n")
        g5.tick(0.1)
        self.assertEqual(status(send(g5, b"?")[0])[0], "Sleep")

    def test_P02_plugin_status_regex_reads_mpos_state_pins_and_fs(self):
        g = mk(instant=True)
        send(g, b"G1X1.5Y-2F100\nM3S100\n")        # laser mode only fires the beam during G1/G2/G3
        g.limit_pins = 1 | 2
        line = send(g, b"?")[0]
        m = P02A.search(line)
        self.assertEqual((m.group(1), m.group(2), m.group(3), m.group(4)), ("Idle", "1.500", "-2.000", "0.000"))
        self.assertEqual(P02D.search(line).group(1), "XY")
        self.assertEqual(P02E.search(line).groups(), ("0", "100"))

    def test_F02_status_mask_wpos_and_buffer_state(self):
        g = mk()
        self.assertEqual(send(g, b"$Report/Status=3\n"), [OK])
        self.assertEqual(send(g, b"?"), ["<Idle|MPos:0.000,0.000,0.000|Bf:15,128|FS:0,0|WCO:0.000,0.000,0.000>\r\n"])
        self.assertEqual(send(g, b"$10=2\n"), [OK])
        self.assertEqual(send(g, b"?")[0].split("|")[1], "WPos:0.000,0.000,0.000")

    def test_F02_buffer_field_counts_the_planner_blocks_in_use(self):
        # Report.cpp:518-520: plan_get_block_buffer_available (Planner.cpp:443-449, 16 blocks) before the rx space
        g = mk()
        send(g, b"$Report/Status=3\n")
        self.assertEqual(status(send(g, b"?")[0])[1]["Bf"], "15,128")
        for x in (10, 20, 30):
            send(g, ("G1X%dF100\n" % x).encode())
        self.assertEqual(status(send(g, b"?")[0])[1]["Bf"], "12,128")
        run_until_idle(g)
        self.assertEqual(status(send(g, b"?")[0])[1]["Bf"], "15,128")

    def test_F02_accessory_field_shows_c_for_a_reversed_spindle(self):
        # Report.cpp:590-592: SpindleState::Ccw prints C
        g = mk(instant=True)
        send(g, b"G1F100\nM4S100\n")
        send(g, b"?")
        self.assertTrue(send(g, b"?")[0].endswith("|A:C>\r\n"))

    def test_F02_pn_order_probe_then_axes(self):
        g = mk(probe_pin=True)
        g.limit_pins = 1 | 4
        self.assertIn("|Pn:XZ|", send(g, b"?")[0])
        g.probe_tripped = True
        self.assertIn("|Pn:PXZ|", send(g, b"?")[0])

    def test_F02_no_probe_pin_never_reports_p(self):
        g = mk()
        g.probe_tripped = True
        self.assertNotIn("Pn", send(g, b"?")[0])

    def test_F02_accessory_field_flood_needs_a_flood_pin(self):
        g = mk(instant=True)
        send(g, b"G1F100\nM3S100M8\n")
        send(g, b"?")
        self.assertEqual(send(g, b"?"), ["<Idle|MPos:0.000,0.000,0.000|FS:0,100|Ov:100,100,100|A:S>\r\n"])
        g = mk(instant=True, flood_pin=True)
        send(g, b"G1F100\nM3S100M8\n")
        send(g, b"?")
        self.assertEqual(send(g, b"?"), ["<Idle|MPos:0.000,0.000,0.000|FS:0,100|Ov:100,100,100|A:SF>\r\n"])

    def test_F02_inch_reporting_linear_only(self):
        g = mk(axes="XYZA", instant=True)
        send(g, b"G0X25.4A90\n")
        send(g, b"$13=1\n")
        self.assertEqual(send(g, b"?")[0].split("|")[1], "MPos:1.0000,0.0000,0.0000,90.000")

    def test_F02_wco_includes_the_tool_length_offset_on_z(self):
        # System.cpp:get_wco: coord_system + coord_offset + tool_length_offset
        g = mk(instant=True)
        send(g, b"G43.1Z2\n")
        self.assertEqual(send(g, b"?")[0].split("|")[-1], "WCO:0.000,0.000,2.000>\r\n")

    def test_F02_rate_in_inch_reporting_is_rounded_not_truncated(self):
        # Report.cpp:report_realtime_status prints print(float, 0): 500 mm/min = 19.685 in/min -> 20
        g = mk()
        send(g, b"$13=1\n")
        send(g, b"G1X100F500\n")
        g.tick(1.0)
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"], "20,0")

    def test_F02_tiny_negative_prints_minus_zero(self):
        g = mk(instant=True)
        send(g, b"G92X-0.0004\n")
        send(g, b"$10=0\n")
        self.assertEqual(send(g, b"?")[0].split("|")[1], "WPos:-0.000,0.000,0.000")

    def test_F21_utf8_lead_byte_boundaries(self):
        # UTF8.cpp:13-72 (PASS_THROUGH_80_BF): below 0xBF a byte passes raw, 0xC0-0xDF lead a 2-byte sequence,
        # 0xE0-0xEF a 3-byte, 0xF0-0xF7 a 4-byte, 0xF8 and up are invalid.  A feed-override byte (0x91) that is
        # swallowed as a continuation byte leaves Ov at 100; one that arrives raw makes it 110.
        def ov(data):
            g = mk()
            send(g, b"?")
            send(g, b"?")
            r = send(g, b"?" + data + b"?\n")
            return status(r[1])[1].get("Ov")
        self.assertEqual(ov(b"\xbe\x91"), "110,100,100")             # 0xBE passes through, 0x91 is raw
        self.assertEqual(ov(b"\xbf\x91"), "110,100,100")             # 0xBF is a stray continuation byte: dropped (no command is mapped to it, so
                                                                       # dropping and passing it raw look the same here)
        self.assertEqual(ov(b"\xc0\x91"), None)                      # 0xC0: 2-byte lead, 0x91 completes it
        self.assertEqual(ov(b"\xdf\x91"), None)
        self.assertEqual(ov(b"\xe0\x80\x91"), None)                  # 0xE0: 3-byte lead
        self.assertEqual(ov(b"\xef\x80\x91"), None)
        self.assertEqual(ov(b"\xf0\x80\x80\x91"), None)              # 0xF0: 4-byte lead
        self.assertEqual(ov(b"\xf7\x80\x80\x91"), None)
        self.assertEqual(ov(b"\xf8\x91"), "110,100,100")             # 0xF8: invalid start byte, 0x91 is raw
        self.assertEqual(ov(b"\xff\x91"), "110,100,100")

    def test_F21_realtime_bytes_above_7f_pass_raw_utf8_lead_bytes_swallow_followers(self):
        g = mk()
        send(g, b"\x91")                                           # raw 0x91: feed override +10
        send(g, b"?")
        self.assertEqual(send(g, b"?")[0].split("|")[-1], "Ov:110,100,100>\r\n")
        g2 = mk()
        send(g2, b"\xc2\x91")                                       # UTF-8 encoded U+0091 decodes to the same command
        send(g2, b"?")
        self.assertEqual(send(g2, b"?")[0].split("|")[-1], "Ov:110,100,100>\r\n")
        g3 = mk()
        self.assertEqual(send(g3, b"\xc2?\n"), [OK])               # a lead byte swallows the next byte, even a '?'


class Acks(unittest.TestCase):
    def test_F03_ok_for_blank_lines_and_comments(self):
        g = mk()
        self.assertEqual(send(g, b"\n"), [OK])
        self.assertEqual(send(g, b"   \n"), [OK])
        self.assertEqual(send(g, b"(a comment)\n"), [OK])
        self.assertEqual(send(g, b"; comment\n"), [OK])

    def test_F03_cr_and_lf_each_end_a_line_so_crlf_is_two_acks(self):
        # The console is a UartChannel: UartChannel.cpp:210-219 lineComplete -> lineedit.cpp:397 (non-editing
        # Lineedit::step returns true for '\\r' and for '\\n', with no CR-then-LF state); an empty line is acked
        # (ProcessSettings.cpp:1340-1344).  Channel::lineComplete's CRLF swallowing is not on this path.
        g = mk()
        self.assertEqual(send(g, b"G4P0\r\n"), [OK, OK])
        self.assertEqual(send(g, b"G4P0\r"), [OK])
        self.assertEqual(send(g, b"G4P0\n"), [OK])
        self.assertEqual(send(g, b"\r\n\r\n"), [OK] * 4)
        self.assertEqual(send(g, b"$G\r\n")[1:], [OK, OK])
        self.assertEqual(send(g, b"G4P0\r"), [OK])
        self.assertEqual(send(g, b"\n"), [OK])                      # the LF arrives alone: it is its own line

    def test_F03_backspace_erases_and_the_line_is_cut_at_254_characters(self):
        # lineedit.cpp:addchar / erase_char; Channel::maxLine = 255 (Channel.h:60) and the Lineedit is built with
        # maxLine - 1 (UartChannel.cpp:11).  $Start/Message has no length check (Settings.cpp:252 needs a min
        # length), so its stored value shows how much of the line was kept.  The echo the editing mode would send
        # for a backspace is not modelled (module docstring).
        g = mk(instant=True)
        self.assertEqual(send(g, b"G0X1\b2\n"), [OK])
        self.assertEqual(g.mpos(), [2.0, 0.0, 0.0])
        key = "$Start/Message="
        for length, kept in ((200, 200), (239, 239), (240, 239), (300, 239)):
            self.assertEqual(send(g, (key + "m" * length + "\n").encode()), [OK])
            self.assertEqual(send(g, b"$Start/Message\n"), [("$Start/Message=" + "m" * kept + "\r\n"), OK], length)
        # a backspace in a full line frees one slot, which the next character takes
        self.assertEqual(send(g, (key + "m" * 300 + "\bz\n").encode()), [OK])
        self.assertEqual(send(g, b"$Start/Message\n"), ["$Start/Message=" + "m" * 238 + "z\r\n", OK])

    def test_F03_percent_line_is_cleared_not_an_error(self):
        # GCode.cpp:collapseGCode clears a lone '%' (gnea answers error:1)
        self.assertEqual(send(mk(), b"%\n"), [OK])

    def test_F04_gcode_error_prints_bad_gcode_line_then_error_then_text(self):
        g = mk()
        self.assertEqual(send(g, b"G99\n"), err(20, "Unsupported GCode command", "G99"))
        self.assertEqual(send(g, b"  g1 x1\n"), err(22, "Gcode undefined feed rate", "g1 x1"))
        self.assertEqual(send(g, b"G1 Foo\n"), err(2, "Bad GCode number format", "G1 Foo"))

    def test_F04_system_gc_lock_has_no_bad_gcode_line(self):
        g = mk()
        g.inject_alarm(5)
        out(g)
        self.assertEqual(send(g, b"G0X1\n"), err(9, "GCode cannot be executed in lock or alarm state"))

    def test_F04_dollar_errors(self):
        g = mk()
        self.assertEqual(send(g, b"$99=1\n"), err(3, "Invalid $ statement"))
        self.assertEqual(send(g, b"$Nope\n"), err(3, "Invalid $ statement"))

    def test_F04_line_longer_than_127_is_error_14(self):
        g = mk()
        line = "G1 X1 F100 (" + "x" * 120 + ")"
        self.assertEqual(send(g, line + "\n"), err(14, "Line too long", line))

    def test_F04_the_127_character_limit_is_exclusive(self):
        # GCode.cpp:301: strlen(input_line) > 127
        g = mk(instant=True)
        head = "G1 X1 F100 ("
        for length, expected in ((126, [OK]), (127, [OK]), (128, None), (129, None)):
            line = head + "x" * (length - len(head) - 1) + ")"
            self.assertEqual(len(line), length)
            self.assertEqual(send(g, (line + "\n").encode()), expected or err(14, "Line too long", line), length)

    def test_F04_verbose_errors_can_be_turned_off_in_the_config(self):
        g = mk()
        self.assertEqual(send(g, b"$/verbose_errors=false\n"), [OK])
        self.assertEqual(send(g, b"G99\n"), ["[MSG:ERR: Bad GCode: G99]\r\n", "error:20\r\n"])

    def test_P03_P04_plugin_regexes_match_alarm_and_error_lines(self):
        g = mk()
        g.inject_alarm(5)
        alarm = [ln for ln in out(g) if ln.lower().startswith("alarm:")][0]
        self.assertEqual(P03.search(alarm.lower()).group(1), "5")
        error = [ln for ln in send(mk(), b"G99\n") if ln.startswith("error:")]  # G99: GCode.cpp unsupported command, 20
        self.assertEqual(len(error), 1)
        self.assertEqual(P04.search(error[0]).group(1), "20")

    def test_F04_m7_and_m8_take_a_dot_one_mantissa_and_nothing_else(self):
        # GCode.cpp:681-684 allows a mantissa only on M7/M8; 731-745 accept .1 (mantissa 10) and answer
        # GcodeUnsupportedCommand (20) for any other; every other M word with a mantissa stays error:23
        g = mk(instant=True, flood_pin=True)
        self.assertEqual(send(g, b"M8.1\n"), [OK])
        self.assertIn(" M8 ", send(g, b"$G\n")[0])
        self.assertEqual(send(g, b"M9\n"), [OK])
        self.assertIn(" M9 ", send(g, b"$G\n")[0])
        self.assertEqual(send(g, b"M7.1\n"), [OK])
        self.assertEqual(send(g, b"M8.10\n"), [OK])
        for word in ("M7.5", "M8.5", "M8.2", "M7.11"):
            self.assertEqual(send(g, (word + "\n").encode()), err(20, "Unsupported GCode command", word), word)
        for word in ("M3.5", "M9.1", "M5.1", "M0.1"):
            self.assertEqual(send(g, (word + "\n").encode()), err(23, "Gcode command value not integer", word), word)

    def test_F04_a_mantissa_error_keeps_its_place_in_the_line(self):
        g = mk(instant=True, flood_pin=True)
        self.assertEqual(send(g, b"M8.1 M8\n"), err(21, "Gcode modal group violation", "M8.1 M8"))
        self.assertEqual(send(g, b"G99 M8.5\n"), err(20, "Unsupported GCode command", "G99 M8.5"))
        self.assertEqual(send(g, b"M8.5 G99\n"), err(20, "Unsupported GCode command", "M8.5 G99"))
        self.assertEqual(send(g, b"G1.5 M8.1\n"), err(23, "Gcode command value not integer", "G1.5 M8.1"))
        self.assertEqual(send(g, b"M8.1 G1.5\n"), err(23, "Gcode command value not integer", "M8.1 G1.5"))
        self.assertEqual(send(g, b"G1X1F100M8.1\n"), [OK])

    def test_F04_m4_needs_a_laser_or_reversible_spindle(self):
        # GCode.cpp:711-716: NoSpindle is neither reversible nor rate adjusted
        g = mk(spindle="none")
        self.assertEqual(send(g, b"M4\n"), err(20, "Unsupported GCode command", "M4"))
        self.assertEqual(send(g, b"M3S100\n"), [OK])
        self.assertEqual(send(mk(), b"M4S100\n"), [OK])

    def test_F07_m7_m8_are_accepted_but_inert_without_pins(self):
        # GCode.cpp:728-760: coolant words only act when the pin exists
        g = mk(instant=True)
        self.assertEqual(send(g, b"M7\nM8\n"), [OK, OK])
        self.assertEqual(send(g, b"$G\n")[0], "[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n")
        g = mk(instant=True, flood_pin=True)
        send(g, b"M8\n")
        self.assertEqual(send(g, b"$G\n")[0], "[GC:G0 G54 G17 G21 G90 G94 M5 M8 T0 F0 S0]\r\n")


class Alarms(unittest.TestCase):
    def test_F05_P03_alarm_prefix_line_then_number(self):
        g = mk()
        g.inject_alarm(5)
        self.assertEqual(out(g), ["[MSG:INFO: ALARM: Probe Fail Contact]\r\n", "ALARM:5\r\n"])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")

    def test_F05_alarm_names_cover_fluidnc_numbers(self):
        names = {1: "Hard Limit", 2: "Soft Limit", 3: "Abort Cycle", 4: "Probe Fail Initial", 5: "Probe Fail Contact",
                 6: "Homing Fail Reset", 7: "Homing Fail Door", 8: "Homing Fail Pulloff", 9: "Homing Fail Approach",
                 10: "Spindle Control", 11: "Input Pin Initially On", 12: "Ambiguous Switch", 13: "Hard Stop",
                 14: "Unhomed", 15: "Init", 16: "Expander Reset", 17: "GCode Error", 18: "Probe Hard Limit"}
        for code, name in names.items():
            g = mk()
            g.inject_alarm(code)
            # Protocol.cpp:744-760: HardLimit and SoftLimit are critical and print the CriticalEvent message
            critical = ["[MSG:ERR: Reset to continue]\r\n"] if code in (1, 2) else []
            self.assertEqual(out(g), ["[MSG:INFO: ALARM: %s]\r\n" % name, "ALARM:%d\r\n" % code] + critical, code)

    def test_F05_hard_limit_is_critical_and_needs_a_reset(self):
        g = mk()
        g.inject_alarm(1)
        self.assertEqual(out(g), ["[MSG:INFO: ALARM: Hard Limit]\r\n", "ALARM:1\r\n", "[MSG:ERR: Reset to continue]\r\n"])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")
        self.assertEqual(send(g, b"\x18"), BANNER)                     # no homing configured: back to Idle
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")

    def test_F05_soft_limit_is_critical_and_needs_a_reset(self):
        # Protocol.cpp:757-760: SoftLimit sets State::Critical and prints the CriticalEvent message
        g = mk()
        g.inject_alarm(2)
        self.assertEqual(out(g), ["[MSG:INFO: ALARM: Soft Limit]\r\n", "ALARM:2\r\n", "[MSG:ERR: Reset to continue]\r\n"])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")
        self.assertEqual(send(g, b"\x18"), BANNER)
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")
        self.assertEqual(send(g, b"G0X1\n"), [OK])

    def test_F05_only_a_hard_limit_marks_the_axes_unhomed(self):
        # Protocol.cpp:749-752 calls Homing::set_all_axes_unhomed for HardLimit/HardStop; SoftLimit (757-760) does not,
        # so its reset lands in Idle where a hard limit's lands in ALARM:14 (the test above)
        g = mk(homing="XYZ")
        send(g, b"$H\n")
        out(g)
        g.inject_alarm(2)
        out(g)
        self.assertEqual(send(g, b"\x18"), BANNER)
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")

    def test_F05_critical_state_keeps_answering_but_locks_gcode(self):
        # protocol_do_alarm only sets State::Critical and returns, so the main loop keeps running
        # (Protocol.cpp:735-765): '?' is a status report plus ok, '$' commands run, G-code is error:9
        # (ProcessSettings.cpp:1378), and '$X' is Ok without effect because the state is not Alarm (309-316).
        for code in (1, 2):
            g = mk()
            g.inject_alarm(code)
            out(g)
            r = send(g, b"?\n")
            self.assertEqual((status(r[0])[0], r[1:]), ("Alarm", [OK]), code)
            self.assertEqual(send(g, b"$G\n"), ["[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n", OK], code)
            self.assertEqual(send(g, b"$Report/Status\n"), ["$Report/Status=1\r\n", OK], code)
            self.assertEqual(send(g, b"G0X1\n"), err(9, "GCode cannot be executed in lock or alarm state"), code)
            self.assertEqual(send(g, b"$X\n"), [OK], code)
            self.assertEqual(status(send(g, b"?")[0])[0], "Alarm", code)
            self.assertEqual(send(g, b"G0X1\n"), err(9, "GCode cannot be executed in lock or alarm state"), code)
            self.assertEqual(send(g, b"\x18"), BANNER, code)
            self.assertEqual(send(g, b"G0X1\n"), [OK], code)

    def test_F05_homing_leaves_the_critical_state(self):
        # ProcessSettings.cpp:435-472 home() has no state check (Control.cpp:142-150 blocks on the fault/e-stop PINS
        # only) and Homing.cpp:done (lines 205-227) sets Idle or Alarm: the Critical state ends there, so a later
        # '$X' is a real unlock again
        g = mk(homing="XYZ")
        send(g, b"$H\n")
        g.inject_alarm(2)
        out(g)
        self.assertEqual(send(g, b"$H=X\n"), ["[MSG:Homed:X]\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")
        g.inject_alarm(1)
        out(g)
        self.assertEqual(send(g, b"$HX\n"), ["[MSG:Homed:X]\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")                 # Y and Z are still unhomed
        self.assertEqual(send(g, b"$X\n"), ["[MSG:INFO: Caution: Unlocked]\r\n", OK])

    def test_F05_alarm_state_unlocks_with_dollar_x_but_critical_does_not(self):
        g = mk()
        g.inject_alarm(5)
        out(g)
        self.assertEqual(send(g, b"$X\n"), ["[MSG:INFO: Caution: Unlocked]\r\n", OK])
        self.assertEqual(send(g, b"G0X1\n"), [OK])

    def test_F05_critical_reset_with_homing_lands_in_unhomed_alarm_before_the_banner(self):
        # Protocol.cpp:protocol_do_rt_reset: State::Critical + unhomed axes -> alarm 14 (printed first)
        g = mk(homing="XYZ")
        send(g, b"$H\n")
        out(g)
        g.inject_alarm(1)
        out(g)
        self.assertEqual(send(g, b"\x18"), ["[MSG:INFO: ALARM: Unhomed]\r\n", "ALARM:14\r\n"] + BANNER)
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")

    def test_F05_reset_while_moving_is_abort_cycle_then_banner(self):
        g = mk()
        send(g, b"G1X100F600\n")
        g.tick(0.5)
        self.assertEqual(send(g, b"\x18"), ["[MSG:INFO: ALARM: Abort Cycle]\r\n", "ALARM:3\r\n"] + BANNER)

    def test_F12_unlock_is_a_noop_unless_alarm(self):
        g = mk()
        self.assertEqual(send(g, b"$X\n"), [OK])

    def test_F12_unlock_message_and_state(self):
        g = mk()
        g.inject_alarm(5)
        out(g)
        self.assertEqual(send(g, b"$X\n"), ["[MSG:INFO: Caution: Unlocked]\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")

    def test_F06_feedback_messages_are_info_level_with_fluidnc_text(self):
        g = mk(instant=True)
        self.assertEqual(send(g, b"$C\n"), ["[MSG:INFO: Enabled]\r\n", OK])
        self.assertEqual(send(g, b"$C\n"), ["[MSG:INFO: Disabled]\r\n", OK] + BANNER)
        self.assertEqual(send(g, b"M2\n"), ["[MSG:INFO: Program End]\r\n", OK])

    def test_F14_sleep_replies_ok_then_message(self):
        g = mk()
        r = send(g, b"$SLP\n")
        g.tick(0.1)
        r += out(g)
        self.assertEqual(r, [OK, "[MSG:INFO: Sleeping]\r\n"])

    def test_F14_guard_blocks_sleep_while_running(self):
        g = mk()
        send(g, b"G1X100F100\n")
        g.tick(0.2)
        self.assertEqual(send(g, b"$SLP\n"), err(8, "Command requires idle state"))

    # -- soft limit raised from motion (Limit.cpp:limit_error, MotionControl.cpp:mc_linear / mc_critical)
    SOFT_LIMIT = ["[MSG:INFO: ALARM: Soft Limit]\r\n", "ALARM:2\r\n", "[MSG:ERR: Reset to continue]\r\n"]

    def _soft_limit_machine(self):
        g = mk(homing="XYZ")
        self.assertEqual(send(g, b"$H\n"), ["[MSG:Homed:XYZ]\r\n", OK])
        self.assertEqual(send(g, b"$/axes/x/soft_limits=true\n"), [OK])
        return g

    def test_F05_soft_limit_from_motion_rejects_the_line_and_critical_answers_commands(self):
        # MotionControl.cpp:119-121: mc_linear returns before plan_buffer_line, so the rejected line is never
        # queued; ProcessSettings.cpp:1161-1176 runs commands in State::Critical (only gcode is locked, :1378);
        # disable_alarm_lock (:309-318) is a no-op unless State::Alarm; $H sets State::Homing (Homing.cpp:387)
        g = self._soft_limit_machine()
        self.assertEqual(send(g, b"G1X500F6000\n"), self.SOFT_LIMIT + [OK])
        self.assertEqual(g.blocks, [])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")
        self.assertEqual(send(g, b"G0X1\n"), err(9, "GCode cannot be executed in lock or alarm state"))
        gc = send(g, b"$G\n")
        self.assertEqual((len(gc), gc[0][:4], gc[1]), (2, "[GC:", OK))
        self.assertEqual(send(g, b"$X\n"), [OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")                      # still Critical
        self.assertEqual(send(g, b"$H\n"), ["[MSG:Homed:XYZ]\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")

    def test_F05_soft_limit_lets_queued_motion_finish_before_the_alarm(self):
        # Limit.cpp:limit_error calls protocol_buffer_synchronize() first (Protocol.cpp:606-615 loops until the
        # planner is empty and the state is not Cycle): the earlier line runs to its end, then ALARM:2
        g = self._soft_limit_machine()
        self.assertEqual(send(g, b"G1X-10F600\n"), [OK])
        self.assertEqual(send(g, b"G1X500F600\n"), [])
        seen = []
        for _ in range(100):
            g.tick(0.1)
            seen += out(g)
        self.assertEqual(seen, self.SOFT_LIMIT + [OK])
        self.assertEqual(g.blocks, [])
        state, f = status(send(g, b"?")[0])
        self.assertEqual((state, f["MPos"]), ("Alarm", "-10.000,0.000,0.000"))
        self.assertEqual(send(g, b"$G\n")[-1], OK)

    def test_F05_reset_after_a_soft_limit_alarm_prints_only_the_banner(self):
        # Protocol.cpp:1466-1472 (reset in Critical -> Idle when nothing is unhomed) then protocol_do_soft_restart
        # clears the planner (plan_reset, :677): $H after the alarm completes at once (nothing queued behind the
        # rejected line), so the reset has no queued cycle to finish and prints only the banner
        g = self._soft_limit_machine()
        self.assertEqual(send(g, b"G1X500F6000\n"), self.SOFT_LIMIT + [OK])
        self.assertEqual(send(g, b"$H\n"), ["[MSG:Homed:XYZ]\r\n", OK])
        self.assertEqual(send(g, b"\x18"), BANNER)
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")


class SettingNames(unittest.TestCase):
    def test_F10_partial_name_is_an_unanchored_search(self):
        # ProcessSettings.cpp:1254-1266: regexMatch(key, name, false) searches the name for the text anywhere
        g = mk()
        self.assertEqual(send(g, b"$Grbl/Laser\n"), ["$Grbl/LaserMode=1\r\n", OK])
        self.assertEqual(send(g, b"$Report\n"), ["$Report/Status=1\r\n", OK])
        self.assertEqual(send(g, b"$grbl/LASER\n"), ["$Grbl/LaserMode=1\r\n", OK])
        self.assertEqual(send(g, b"$Grbl\n"), [x + "\r\n" for x in SETTINGS_XYZ if x.startswith("$Grbl/")] + [OK])
        self.assertEqual(send(g, b"$Nothing\n"), err(3, "Invalid $ statement"))

    def test_F10_partial_name_regexp_anchors_and_star(self):
        # Regexpr.cpp:57-71 and 36-55: '^' anchors the start, a final '$' the end, '*' is any run of characters
        g = mk()
        self.assertEqual(send(g, b"$^Report\n"), ["$Report/Status=1\r\n", OK])
        self.assertEqual(send(g, b"$^Status\n"), err(3, "Invalid $ statement"))
        self.assertEqual(send(g, b"$Status$\n"), ["$Report/Status=1\r\n", OK])
        self.assertEqual(send(g, b"$Laser$\n"), err(3, "Invalid $ statement"))
        self.assertEqual(send(g, b"$MaxR*/Y\n"), ["$Grbl/MaxRate/Y=1000.000\r\n", OK])
        self.assertEqual(send(g, b"$^Grbl/Max*/Z$\n"), ["$Grbl/MaxRate/Z=1000.000\r\n", "$Grbl/MaxTravel/Z=1000.000\r\n", OK])

    def test_F10_percent_escape_decodes_hex_pairs(self):
        g = mk()
        self.assertEqual(send(g, b"$Hostname=a%62c\n"), [OK])
        self.assertEqual(send(g, b"$Hostname\n"), ["$Hostname=abc\r\n", OK])

    def test_F10_bad_percent_escape_logs_an_error_and_ends_the_text(self):
        # ProcessSettings.cpp:105-128 uriDecode: too short / not hex -> log_error and the text decoded so far
        g = mk()
        self.assertEqual(send(g, b"$Hostname=a%\n"), ["[MSG:ERR: Bad % encoding - too short]\r\n", OK])
        self.assertEqual(send(g, b"$Hostname\n"), ["$Hostname=a\r\n", OK])
        self.assertEqual(send(g, b"$Hostname=ab%5\n"), ["[MSG:ERR: Bad % encoding - too short]\r\n", OK])
        self.assertEqual(send(g, b"$Hostname\n"), ["$Hostname=ab\r\n", OK])
        self.assertEqual(send(g, b"$Hostname=xyz%zz9\n"), ["[MSG:ERR: Bad % encoding - not hex]\r\n", OK])
        self.assertEqual(send(g, b"$Hostname\n"), ["$Hostname=xyz\r\n", OK])

    def test_F10_realtime_characters_are_escaped_when_a_setting_is_shown(self):
        # ProcessSettings.cpp:75-102 uriEncodeGrblCharacters: % -> %25, ! -> %21, ? -> %3F, ~ -> %7E
        g = mk()
        self.assertEqual(send(g, b"$Start/Message=a%25b%21c%3Fd%7Ee\n"), [OK])
        self.assertEqual(send(g, b"$Start/Message\n"), ["$Start/Message=a%25b%21c%3Fd%7Ee\r\n", OK])
        self.assertIn("$Start/Message=a%25b%21c%3Fd%7Ee\r\n", send(g, b"$S\n"))


class Reports(unittest.TestCase):
    def test_F07_P07_gcode_modes_initial(self):
        self.assertEqual(send(mk(), b"$G\n"), ["[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n", OK])

    def test_F07_after_modes(self):
        g = mk(instant=True, flood_pin=True)
        send(g, b"G91G21G55G18M3M8T2F500S750\n")
        self.assertEqual(send(g, b"$G\n")[0], "[GC:G0 G55 G18 G21 G91 G94 M3 M8 T2 F500 S750]\r\n")

    def test_F07_inch_and_inverse_time_words(self):
        # Report.cpp:303-309 prints G20 for Units::Inches, 317-327 prints G93 for FeedRate::InverseTime
        g = mk(instant=True)
        send(g, b"G20G93\n")
        self.assertEqual(send(g, b"$G\n")[0], "[GC:G0 G54 G17 G20 G90 G93 M5 M9 T0 F0 S0]\r\n")
        send(g, b"G21G94\n")
        self.assertEqual(send(g, b"$G\n")[0], "[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n")

    def test_F07_probe_motion_words_are_g38_2_to_g38_5(self):
        # Report.cpp:236-244
        for word in ("G38.2", "G38.3", "G38.4", "G38.5"):
            g = mk(instant=True, probe_pin=True)
            g.probe_trigger = {"Z": -1.0}
            send(g, ("%sZ-5F100\n" % word).encode())
            self.assertEqual(send(g, b"$G\n")[0], "[GC:%s G54 G17 G21 G90 G94 M5 M9 T0 F100 S0]\r\n" % word)

    def test_F07_motion_word_g80_after_none_and_probe_words(self):
        g = mk(instant=True, probe_pin=True)
        g.probe_trigger = {"Z": -1.0}
        send(g, b"G38.2Z-5F100\n")
        self.assertEqual(send(g, b"$G\n")[0], "[GC:G38.2 G54 G17 G21 G90 G94 M5 M9 T0 F100 S0]\r\n")

    def test_F07_motion_word_g80_only_after_motion_cancel(self):
        # Report.cpp:report_gcode_modes: Motion::None prints G80 (the power-up word is G0)
        g = mk(instant=True)
        send(g, b"G80\n")
        self.assertEqual(send(g, b"$G\n")[0], "[GC:G80 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n")

    def test_F11_build_info_lines(self):
        # Report.cpp:report_build_info: VER / OPT / CLUSTER bracketed with ']' by LogStream, then the machine name
        self.assertEqual(send(mk(), b"$I\n"), [
            "[VER:3.9 FluidNC v3.9.9 (esp32-wifi) :]\r\n", "[OPT:PHS]\r\n", "[CLUSTER:16]\r\n",
            "[MSG:Machine: Default (Test Drive no I/O)]\r\n", OK])

    def test_F11_build_string_setting_lands_after_the_colon(self):
        g = mk()
        send(g, b"$Firmware/Build=abc\n")
        self.assertEqual(send(g, b"$I\n")[0], "[VER:3.9 FluidNC v3.9.9 (esp32-wifi) :abc]\r\n")

    def test_P09d_plugin_detection_text(self):
        r = send(mk(), b"$I\n")
        grbl_version = " ".join(ln.rstrip("\r\n") for ln in r if "ok" not in ln.lower() and not ln.startswith("[MSG:"))
        self.assertIn(" FLUIDNC ", grbl_version.upper())                # _bgs:1645-1648
        self.assertNotIn("VER:1.", grbl_version)

    def test_F09_ngc_parameters_one_line_per_coord_index_no_prb(self):
        r = send(mk(), b"$#\n")
        names = [ln[1:ln.index(":")] for ln in r[:-1]]
        self.assertEqual(names, ["G54", "G55", "G56", "G57", "G58", "G59", "G59.1", "G59.2", "G59.3", "G28", "G30",
                                 "G92", "TLO"])
        self.assertEqual(r[0], "[G54:0.000,0.000,0.000]\r\n")
        self.assertEqual(r[-1], OK)

    def test_F09_offsets_reflect_g10_g92_and_tlo(self):
        g = mk(instant=True)
        send(g, b"G10L2P1X5Y6Z7\nG92X1\nG43.1Z2\n")
        r = send(g, b"$#\n")
        self.assertEqual(r[0], "[G54:5.000,6.000,7.000]\r\n")
        self.assertEqual(r[11], "[G92:-6.000,0.000,0.000]\r\n")      # G92 = MPos - WCS - target = 0 - 5 - 1
        self.assertEqual(r[12], "[TLO:0.000,0.000,2.000]\r\n")

    def test_F10_P08_dollar_dollar_order_and_formats(self):
        # $13 first, then Setting::List order (reverse creation): $20..$23, $30, $32, $100s, $110s, $120s, $130s, $10
        r = send(mk(), b"$$\n")
        self.assertEqual([ln.rstrip("\r\n") for ln in r[:-1]], [
            "$13=0", "$20=0", "$21=0", "$22=0", "$23=0", "$30=1000", "$32=1",
            "$100=80.000", "$101=80.000", "$102=80.000", "$110=1000.000", "$111=1000.000", "$112=1000.000",
            "$120=25.000", "$121=25.000", "$122=25.000", "$130=1000.000", "$131=1000.000", "$132=1000.000", "$10=1"])
        self.assertEqual(r[-1], OK)
        for ln in r[:-1]:
            self.assertTrue(P08.match(ln), ln)                          # the plugin stores every one

    def test_F10_dollar_dollar_is_allowed_while_running(self):
        g = mk()
        send(g, b"G1X100F100\n")
        g.tick(0.1)
        self.assertEqual(send(g, b"$$\n")[-1], OK)                      # anyState (gnea answers error:8)

    def test_F10_none_spindle_machine_reports_zero_laser_and_max_speed(self):
        r = send(mk(spindle="none"), b"$$\n")
        self.assertIn("$30=0\r\n", r)
        self.assertIn("$32=0\r\n", r)

    def test_F10_proxy_writes_are_read_only_error_162(self):
        g = mk()
        for cmd in ("$20=1", "$32=0", "$100=5", "$110=5", "$120=5", "$130=5", "$30=5", "$21=1", "$22=1", "$23=1"):
            self.assertEqual(send(g, (cmd + "\n").encode()), err(162, "Read-only setting"), cmd)

    def test_F16_S29_S30_status_report_setting_is_the_one_writable_number(self):
        g = mk()
        self.assertEqual(send(g, b"$10=3\n"), [OK])
        self.assertEqual(send(g, b"$10\n"), ["$10=3\r\n", OK])
        self.assertEqual(send(g, b"$10=4\n"), err(80, "Number out of range for setting"))
        self.assertEqual(send(g, b"$10=x\n"), err(2, "Bad GCode number format"))
        self.assertEqual(send(g, b"$Report/Status=1\n"), [OK])

    def test_F16_settings_writes_need_idle_or_alarm(self):
        g = mk()
        send(g, b"G1X100F100\n")
        g.tick(0.1)
        self.assertEqual(send(g, b"$10=1\n"), err(8, "Command requires idle state"))
        self.assertEqual(send(g, b"$Hostname=abc\n"), err(8, "Command requires idle state"))

    def test_F10_dollar_13_is_a_command_with_its_own_guard(self):
        g = mk()
        self.assertEqual(send(g, b"$13\n"), ["$13=0\r\n", OK])
        self.assertEqual(send(g, b"$13=1\n"), [OK])
        self.assertEqual(send(g, b"$13\n"), ["$13=1\r\n", OK])
        send(g, b"G1X100F100\n")
        g.tick(0.1)
        self.assertEqual(send(g, b"$13\n"), err(8, "Command requires idle state"))

    def test_F17_S31_settings_list_exact(self):
        r = send(mk(), b"$S\n")
        self.assertEqual([ln.rstrip("\r\n") for ln in r[:-1]], SETTINGS_XYZ)
        self.assertEqual(r[-1], OK)

    def test_F17_plugin_s_transform_parses_and_has_the_keys_it_reads(self):
        # __init__:1327-1334 json.loads over the collected reply lines
        r = send(mk(), b"$S\n")
        last_response = "".join(ln for ln in r if "ok" not in ln.lower()).rstrip("\r").rstrip("\n")
        text = "{" + last_response.replace("\r", "").replace("=", '": "').replace("\n", '", ').replace("$", '"').replace("\\", "\\\\") + '"}'
        settings = json.loads(text)
        for key in ("Grbl/LaserMode", "Grbl/MaxRate/X", "Grbl/MaxRate/Y", "Grbl/MaxRate/Z", "Grbl/MaxTravel/X",
                    "Grbl/MaxTravel/Y", "Grbl/MaxTravel/Z", "HTTP/Enable", "HTTP/Port", "Hostname", "Config/Filename"):
            self.assertIn(key, settings)
        self.assertEqual((settings["HTTP/Enable"], settings["Grbl/LaserMode"], settings["Config/Filename"]),
                         ("OFF", "1", "config.yaml"))

    def test_F17_named_settings_display_and_set(self):
        g = mk()
        self.assertEqual(send(g, b"$Hostname\n"), ["$Hostname=fluidnc\r\n", OK])
        self.assertEqual(send(g, b"$Hostname=my-rig\n"), [OK])
        self.assertEqual(send(g, b"$hostname\n"), ["$Hostname=my-rig\r\n", OK])           # names are case-insensitive
        self.assertEqual(send(g, b"$Hostname=bad name\n"), err(81, "Invalid value for setting"))
        self.assertEqual(send(g, b"$HTTP/Port=0\n"), err(80, "Number out of range for setting"))
        self.assertEqual(send(g, b"$HTTP/Enable=on\n"), [OK])
        self.assertEqual(send(g, b"$HTTP/Enable\n"), ["$HTTP/Enable=ON\r\n", OK])
        self.assertEqual(send(g, b"$HTTP/Enable=0\n"), [OK])                       # the numeric index also works
        self.assertEqual(send(g, b"$HTTP/Enable\n"), ["$HTTP/Enable=OFF\r\n", OK])
        r = send(g, b"$HTTP/Enable=maybe\n")
        self.assertEqual(r[0], "[MSG:INFO: Valid options: OFF ON]\r\n")
        self.assertEqual(r[1:], err(2, "Bad GCode number format"))
        self.assertEqual(send(g, b"$Config/Filename=\n"), ["$Config/Filename=config.yaml\r\n", OK])    # empty value = display
        self.assertEqual(send(g, b"$Config/Filename=" + b"x" * 51 + b"\n")[0], "[MSG:ERR: Setting length error]\r\n")

    def test_F17_wildcard_display(self):
        r = send(mk(), b"$Grbl/MaxRate/*\n")
        self.assertEqual(r, ["$Grbl/MaxRate/X=1000.000\r\n", "$Grbl/MaxRate/Y=1000.000\r\n", "$Grbl/MaxRate/Z=1000.000\r\n", OK])

    def test_F10_help_line_has_no_brackets(self):
        self.assertEqual(send(mk(), b"$\n"), [
            "HLP:$$ $+ $# $S $L $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H $F $E=err ~ ! ? ctrl-x\r\n", OK])


class Config(unittest.TestCase):
    def _cd(self, **kw):
        g = mk(**kw)
        r = send(g, b"$CD\n")
        self.assertEqual(r[-1], OK)
        return g, r[:-1]

    def test_F18_S32_config_dump_shape(self):
        _, r = self._cd()
        self.assertEqual(r[:5], ["board: None\r\n", "name: Default (Test Drive no I/O)\r\n", "meta: \r\n",
                                 "stepping: \r\n", "  engine: RMT\r\n"])
        text = "".join(r)
        self.assertNotIn("\r\n\r\n\r\n", text)                       # leave() never doubles a blank line
        self.assertIn("      null_motor: \r\n\r\n  y: \r\n", text)    # section end -> one blank -> next section
        self.assertIn("    steps_per_mm: 80.000000\r\n", text)       # std::to_string(float)
        self.assertIn("    max_travel_mm: 1000.000000\r\n", text)
        self.assertTrue(text.endswith("planner_blocks: 16\r\n"))

    def test_P09e_plugin_yaml_collection_loads_the_axes(self):
        try:
            import yaml
        except ImportError:                                           # pragma: no cover
            self.skipTest("PyYAML is not installed")
        for axes in ("XY", "XYZ", "XYZA", "XYZAB"):
            _, r = self._cd(axes=axes)
            collected = "".join(ln for ln in r if "ok" not in ln.lower()).rstrip("\r").rstrip("\n")
            cfg = yaml.safe_load(collected)
            self.assertEqual(sorted(cfg["axes"].keys() & set("xyzabc")), sorted(c.lower() for c in axes))
            for letter in axes.lower():
                self.assertEqual(cfg["axes"][letter]["max_travel_mm"], 1000.0)
            self.assertEqual(cfg["name"], "Default (Test Drive no I/O)")

    def test_F18_dump_round_trips_through_the_parser(self):
        g = mk(axes="XYZA", homing="XYZ", probe_pin=True, flood_pin=True)
        tree = parse_config_text(g.config_text(), g._template)
        self.assertEqual("\n".join(generate_config_lines(tree)) + "\n", g.config_text())

    def test_S33_dump_to_file_prints_nothing(self):
        g = mk()
        self.assertEqual(send(g, b"$CD=config.yaml\n"), [OK])

    def test_S36_runtime_config_edit_and_readback(self):
        g = mk()
        self.assertEqual(send(g, b"$/axes/x/max_travel_mm=500\n"), [OK])
        self.assertEqual(send(g, b"$/axes/x/max_travel_mm\n"), ["$/axes/x/max_travel_mm=500.000\r\n", OK])
        self.assertEqual(send(g, b"$130\n"), ["$130=500.000\r\n", OK])
        self.assertEqual(send(g, b"$Grbl/MaxTravel/X\n"), ["$Grbl/MaxTravel/X=500.000\r\n", OK])
        self.assertEqual(send(g, b"$/axes/x/soft_limits=true\n"), [OK])
        self.assertEqual(send(g, b"$20\n"), ["$20=1\r\n", OK])
        self.assertEqual(send(g, b"$/name=My Machine\n"), [OK])
        self.assertEqual(send(g, b"$I\n")[3], "[MSG:Machine: My Machine]\r\n")

    def test_S36_section_display_and_errors(self):
        g = mk()
        r = send(g, b"$/probe\n")
        self.assertEqual(r[0], "/probe:\r\n")
        self.assertEqual(r[1], "  pin: NO_PIN\r\n")
        self.assertEqual(r[-1], OK)
        self.assertEqual(send(g, b"$/nothing/here=1\n"), err(3, "Invalid $ statement"))
        self.assertEqual(send(g, b"$/axes/x/max_travel_mm=abc\n"), ["[MSG:ERR: Bad numeric value]\r\n", OK])
        self.assertEqual(send(g, b"$/probe=1\n")[0], "[MSG:ERR: Can't set a value on a section]\r\n")

    def test_S33_S34_S35_rewrite_flow_survives_a_restart(self):
        # the plugin's config push: delete the file, edit with $/path=, write with $CD=<file>, $Bye
        g = mk()
        self.assertEqual(send(g, b"$LocalFS/Delete=config.yaml\n"), [OK])
        self.assertEqual(send(g, b"$/axes/y/max_travel_mm=300\n"), [OK])
        self.assertEqual(send(g, b"$CD=config.yaml\n"), [OK])
        self.assertEqual(send(g, b"$Bye\n"), ["[MSG:INFO: Restarting]\r\n", OK] + BANNER)
        self.assertEqual(send(g, b"$131\n"), ["$131=300.000\r\n", OK])

    def test_S34_unsaved_edits_do_not_survive_a_restart(self):
        g = mk()
        send(g, b"$/axes/y/max_travel_mm=300\n")
        send(g, b"$Bye\n")
        out(g)
        self.assertEqual(send(g, b"$131\n"), ["$131=1000.000\r\n", OK])

    def test_S34_restart_loses_position_and_runtime_state(self):
        g = mk(instant=True)
        send(g, b"G0X5\nM3S10\n")
        send(g, b"$Bye\n")
        out(g)
        self.assertEqual(mpos(send(g, b"?")[0]), [0.0, 0.0, 0.0])
        self.assertEqual(send(g, b"$G\n")[0], "[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n")

    def test_S34_restart_replays_the_homing_alarm(self):
        g = mk(homing="XYZ")
        send(g, b"$H\n")
        out(g)
        self.assertEqual(send(g, b"$Bye\n"), ["[MSG:INFO: Restarting]\r\n", OK] + BANNER + [
            "[MSG:INFO: ALARM: Unhomed]\r\n", "ALARM:14\r\n"])

    def test_S35_delete_missing_file_is_error_69_not_65(self):
        # FileCommands.cpp:deleteObject: fs::remove throws on ENOENT (stdfs/std-ops.cpp:904-922) -> FsFailedDelFile
        g = mk()
        self.assertEqual(send(g, b"$LocalFS/Delete=nope.yaml\n"), [
            "[MSG:ERR: filesystem error: cannot remove: No such file or directory [/littlefs/nope.yaml]]\r\n",
            "error:69\r\n", "[MSG:ERR: Failed to delete file]\r\n"])
        self.assertEqual(send(g, b"$LocalFS/Delete=/localfs/config.yaml\n"), [OK])      # canonPath aliases
        self.assertEqual(send(g, b"$LocalFS/Delete=config.yaml\n")[1], "error:69\r\n")
        self.assertEqual(send(g, b"$LocalFS/Delete=\n"), ["[MSG:ERR: Will not delete everything]\r\n"] + err(81, "Invalid value for setting"))

    def test_S35_delete_while_running_is_another_interface_busy(self):
        g = mk()
        send(g, b"G1X100F100\n")
        g.tick(0.1)
        self.assertEqual(send(g, b"$LocalFS/Delete=config.yaml\n"), err(120, "Another interface is busy"))

    def test_S34_bye_is_not_state_guarded_and_ack_precedes_the_banner(self):
        r = send(mk(), b"$Bye\n")
        self.assertEqual(r[:2], ["[MSG:INFO: Restarting]\r\n", OK])


class AutoReport(unittest.TestCase):
    def test_S38_interval_message_then_ok_then_first_report_with_wco_and_gc(self):
        g = mk()
        r = send(g, b"$Report/Interval=1000\n")
        self.assertEqual(r[:2], ["[MSG:INFO: uart_channel0 auto report interval set to 1000 ms]\r\n", OK])
        self.assertEqual(r[2], "<Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000>\r\n")
        self.assertEqual(r[3], "[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n")
        self.assertEqual(len(r), 4)

    def test_S38_reports_follow_tick_time_idle_included(self):
        g = mk()
        send(g, b"$Report/Interval=1000\n")
        g.tick(0.5)
        self.assertEqual(out(g), [])
        g.tick(0.5)
        r = out(g)
        self.assertEqual(r, ["<Idle|MPos:0.000,0.000,0.000|FS:0,0|Ov:100,100,100>\r\n"])
        for _ in range(3):
            g.tick(1.0)
            self.assertEqual(out(g), ["<Idle|MPos:0.000,0.000,0.000|FS:0,0>\r\n"])
        self.assertEqual(P02A.search(r[0]).group(1), "Idle")                    # plugin P11: same parser

    def test_S38_interval_is_clamped_to_50_ms_and_zero_turns_it_off(self):
        g = mk()
        self.assertEqual(send(g, b"$Report/Interval=10\n")[0],
                         "[MSG:INFO: uart_channel0 auto report interval set to 50 ms]\r\n")
        self.assertEqual(send(g, b"$Report/Interval=0\n"), ["[MSG:INFO: uart_channel0 auto reporting turned off]\r\n", OK])
        for _ in range(5):
            g.tick(1.0)
        self.assertEqual(out(g), [])
        self.assertEqual(send(g, b"$Report/Interval\n"), ["[MSG:INFO: uart_channel0 auto reporting is off]\r\n", OK])
        send(g, b"$Report/Interval=250\n")
        self.assertEqual(send(g, b"$Report/Interval\n")[0], "[MSG:INFO: uart_channel0 auto report interval is 250 ms]\r\n")
        self.assertEqual(send(g, b"$Report/Interval=abc\n"), err(2, "Bad GCode number format"))

    def test_S38_state_change_reports_immediately(self):
        g = mk()
        send(g, b"$Report/Interval=1000\n")
        r = send(g, b"G1X10F600\n")
        self.assertEqual(r[0], OK)
        self.assertTrue(r[1].startswith("<Run|"), r)

    def test_S38_gc_line_follows_modal_changes_only(self):
        g = mk(instant=True)
        send(g, b"$Report/Interval=1000\n")
        g.tick(0.0)
        out(g)
        r = send(g, b"G91\n")
        self.assertEqual(r, [OK, "[GC:G0 G54 G17 G21 G91 G94 M5 M9 T0 F0 S0]\r\n"])
        g.tick(0.1)
        self.assertEqual([ln for ln in out(g) if ln.startswith("[GC:")], [])

    def test_S38_restart_turns_the_interval_off(self):
        g = mk()
        send(g, b"$Report/Interval=1000\n")
        send(g, b"$Bye\n")
        out(g)
        g.tick(5.0)
        self.assertEqual(out(g), [])


class Homing(unittest.TestCase):
    def test_F13_dollar_h_without_homing_config_is_setting_disabled(self):
        self.assertEqual(send(mk(), b"$H\n"), err(5, "Setting disabled"))

    def test_F13_single_axis_without_homing_is_error_19(self):
        self.assertEqual(send(mk(), b"$HX\n"), err(19, "Single axis homing not allowed"))

    def test_F13_homed_message_has_no_info_prefix(self):
        g = mk(homing="XYZ")
        self.assertEqual(send(g, b"$H\n"), ["[MSG:Homed:XYZ]\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")

    def test_F13_cycles_run_in_order_one_homed_line_each(self):
        g = mk(homing={"X": 1, "Y": 1, "Z": 2})
        self.assertEqual(send(g, b"$H\n"), ["[MSG:Homed:XY]\r\n", "[MSG:Homed:Z]\r\n", OK])

    def test_F13_partial_homing_leaves_the_alarm(self):
        g = mk(homing={"X": 1, "Y": 1, "Z": 2})
        self.assertEqual(send(g, b"$HZ\n"), ["[MSG:Homed:Z]\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")
        self.assertEqual(send(g, b"$H=21\n"), ["[MSG:Homed:Z]\r\n", "[MSG:Homed:XY]\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")
        self.assertEqual(send(g, b"$H=9\n"), ["[MSG:ERR: No axes for homing cycle 9]\r\n"] + err(81, "Invalid value for setting"))
        self.assertEqual(send(g, b"$H=XY\n"), ["[MSG:Homed:XY]\r\n", OK])
        self.assertEqual(send(g, b"$H=Q\n"), err(81, "Invalid value for setting"))

    def test_F13_cycle_list_honours_allow_single_axis(self):
        # ProcessSettings.cpp:435-447: any axis mask other than AllCycles (0) checks allow_single_axis, and a
        # cycle's mask is non-zero, so $H=<cycle> is refused too (Machine/Homing.h:39)
        g = mk(homing="XYZ")
        send(g, b"$X\n")
        self.assertEqual(send(g, b"$/axes/x/homing/allow_single_axis=false\n"), [OK])
        self.assertEqual(send(g, b"$H=1\n"), err(19, "Single axis homing not allowed"))
        self.assertEqual(send(g, b"$H=X\n"), err(19, "Single axis homing not allowed"))
        self.assertEqual(send(g, b"$H\n"), ["[MSG:Homed:XYZ]\r\n", OK])        # AllCycles still runs
        self.assertEqual(send(g, b"$/axes/x/homing/allow_single_axis=true\n"), [OK])
        self.assertEqual(send(g, b"$H=1\n"), ["[MSG:Homed:XYZ]\r\n", OK])

    def test_F13_homing_sets_mpos_from_the_config(self):
        g = mk(homing="X")
        send(g, b"$/axes/x/homing/mpos_mm=-5\n")
        send(g, b"$H\n")
        self.assertEqual(mpos(send(g, b"?")[0])[0], -5.0)

    def test_F13_unlock_clears_unhomed(self):
        g = mk(homing="XYZ")
        self.assertEqual(send(g, b"$X\n"), ["[MSG:INFO: Caution: Unlocked]\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")
        # Homing::set_all_axes_homed: a later single-axis home does not fall back into Alarm
        self.assertEqual(send(g, b"$HX\n"), ["[MSG:Homed:X]\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")

    def test_F13_homing_directions_and_enable_proxies(self):
        g = mk(homing="XYZ")
        self.assertIn("$22=1\r\n", send(g, b"$$\n"))
        send(g, b"$/axes/y/homing/positive_direction=false\n")
        self.assertIn("$23=2\r\n", send(g, b"$$\n"))

    def test_F13_homing_is_guarded_while_running(self):
        g = mk(homing="XYZ")
        send(g, b"$H\n")
        send(g, b"G1X10F100\n")
        g.tick(0.1)
        self.assertEqual(send(g, b"$H\n"), err(8, "Command requires idle state"))

    def test_F13_gcode_is_locked_until_homed_or_unlocked(self):
        g = mk(homing="XYZ")
        self.assertEqual(send(g, b"G0X1\n"), err(9, "GCode cannot be executed in lock or alarm state"))
        send(g, b"$H\n")
        self.assertEqual(send(g, b"G0X1\n"), [OK])

    def test_F13_reset_does_not_complete_a_queued_homing_cycle(self):
        # Homing.cpp:209-211 (Homing::done): on abort the cycle did not complete; Protocol.cpp:566-571 writes no
        # reply for a line that was aborted.  $H queued behind motion (it drains the planner first) must neither
        # print [MSG:Homed:...] nor ok when a soft reset arrives.
        g = mk(homing="XYZ")
        self.assertEqual(send(g, b"$H\n"), ["[MSG:Homed:XYZ]\r\n", OK])
        self.assertEqual(send(g, b"G1X-100F60\n$H\n"), [OK])
        g.tick(0.5)
        self.assertEqual(out(g), [])
        self.assertEqual(send(g, b"\x18"), ["[MSG:INFO: ALARM: Abort Cycle]\r\n", "ALARM:3\r\n"] + BANNER)
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")
        self.assertEqual(send(g, b"$G\n")[-1], OK)


class Jogging(unittest.TestCase):
    def test_F15_S28_jog_state_then_idle_at_target(self):
        g = mk()
        self.assertEqual(send(g, b"$J=G91 G21 X10 Y5 F600\n"), [OK])
        g.tick(0.1)
        self.assertEqual(status(send(g, b"?")[0])[0], "Jog")
        run_until_idle(g)
        self.assertEqual(mpos(send(g, b"?")[0]), [10.0, 5.0, 0.0])

    def test_S28g_gamepad_form_with_js_floats(self):
        g = mk()
        self.assertEqual(send(g, b"$J=G91 G21 X-12.5 Y3.25 F2500\n"), [OK])
        run_until_idle(g)
        self.assertEqual(mpos(send(g, b"?")[0]), [-12.5, 3.25, 0.0])
        self.assertEqual(send(g, b"$J=G91 G21 X100 F100\n"), [OK])
        g.tick(0.5)
        r = send(g, b"?\x85?\n")
        self.assertEqual([status(r[0])[0], status(r[1])[0]], ["Jog", "Jog"])

    def test_F15_jog_cancel_stops_the_jog_and_only_in_jog(self):
        g = mk()
        send(g, b"$J=G91G21X100F100\n")
        g.tick(1.0)
        send(g, b"\x85")
        g.tick(3.0)
        st = send(g, b"?")[0]
        self.assertEqual(status(st)[0], "Idle")
        self.assertTrue(1.0 < mpos(st)[0] < 4.0, mpos(st))
        r = send(mk(), b"?\x85?\n")                                  # not jogging: ignored
        self.assertEqual([status(r[0])[0], status(r[1])[0], r[2]], ["Idle", "Idle", OK])

    def test_F15_jog_errors(self):
        g = mk()
        self.assertEqual(send(g, b"$J=\n"), err(3, "Invalid $ statement"))
        self.assertEqual(send(g, b"$J=G91G21X5\n"), err(22, "Gcode undefined feed rate"))
        self.assertEqual(send(g, b"$J=G91G21G1X5F100\n"), err(16, "Invalid jog command"))
        g.inject_alarm(5)
        out(g)
        self.assertEqual(send(g, b"$J=G91G21X5F100\n"), err(8, "Command requires idle state"))

    def test_F15_gcode_in_jog_state_is_locked_error_9(self):
        g = mk()
        send(g, b"$J=G91G21X50F100\n")
        g.tick(0.1)
        self.assertEqual(send(g, b"G0X1\n"), err(9, "GCode cannot be executed in lock or alarm state"))

    def test_F15_jog_is_constrained_not_refused_at_a_soft_limit(self):
        g = mk()
        send(g, b"$/axes/x/soft_limits=true\n")
        self.assertEqual(send(g, b"$J=G90G21X-5000F6000\n"), [OK])
        run_until_idle(g)
        self.assertEqual(mpos(send(g, b"?")[0])[0], -1000.0)

    def test_F15_jog_past_the_positive_limit_is_constrained_to_zero(self):
        # Jog.cpp:15 -> Kinematics/Cartesian.cpp:Cartesian::constrain_jog (lines 203-258): a target past the
        # axis range is set to the limit; this machine's X range is -1000 to 0
        g = mk()
        send(g, b"$/axes/x/soft_limits=true\n")
        self.assertEqual(send(g, b"$J=G90G21X500F6000\n"), [OK])
        run_until_idle(g)
        self.assertEqual(mpos(send(g, b"?")[0])[0], 0.0)
        self.assertEqual(send(g, b"$J=G90G21X-300F6000\n"), [OK])
        run_until_idle(g)
        self.assertEqual(mpos(send(g, b"?")[0])[0], -300.0)


class Probing(unittest.TestCase):
    def test_F08_no_probe_pin_is_an_error_message_then_ok(self):
        self.assertEqual(send(mk(instant=True), b"G38.2Z-5F100\n"), ["[MSG:ERR: Probe pin is not configured]\r\n", OK])

    def test_F08_P06_successful_probe_prints_machine_position_and_ok(self):
        g = mk(instant=True, probe_pin=True)
        g.probe_trigger = {"Z": -5.0}
        self.assertEqual(send(g, b"G38.2Z-10F100\n"), ["[PRB:0.000,0.000,-5.000:1]\r\n", OK])

    def test_F08_probe_failure_is_alarm_prb_ok_in_that_order(self):
        g = mk(instant=True, probe_pin=True)
        g.probe_trigger = {"Z": -50.0}
        self.assertEqual(send(g, b"G38.2Z-10F100\n"), [
            "[MSG:INFO: ALARM: Probe Fail Contact]\r\n", "ALARM:5\r\n", "[PRB:0.000,0.000,0.000:0]\r\n", OK])

    def test_F08_probe_already_tripped_is_alarm_4_without_prb(self):
        g = mk(instant=True, probe_pin=True)
        g.probe_tripped = True
        self.assertEqual(send(g, b"G38.2Z-10F100\n"), ["[MSG:INFO: ALARM: Probe Fail Initial]\r\n", "ALARM:4\r\n", OK])

    def test_F08_g38_3_no_error_variant_just_reports_zero(self):
        g = mk(instant=True, probe_pin=True)
        g.probe_trigger = {"Z": -50.0}
        r = send(g, b"G38.3Z-10F100\n")
        self.assertEqual(r[-1], OK)
        self.assertTrue(r[0].endswith(":0]\r\n"))
        self.assertNotIn("ALARM:5\r\n", r)

    def test_F08_xy_probe_uses_the_axis_index(self):
        g = mk(instant=True, probe_pin=True)
        g.probe_trigger = {"X": -3.0}
        self.assertEqual(send(g, b"G91G38.2X-10F100\n"), ["[PRB:-3.000,0.000,0.000:1]\r\n", OK])


class GcodeRows(unittest.TestCase):
    """Census rows S41-S62: every string the plugin generates is accepted by the port."""

    def test_S41_to_S62_accepted(self):
        g = mk(instant=True)
        lines = [
            "M5", "G91 G0 Z5", "G4 P0", "G91 G0 Z-5", "M3", "S500", "F500", "G91", "G90", "M4",
            "G0 G90 X1 Y1 Z1", "G0 G90 X2", "G91 G10 P1 L20 X0 Y0 Z0", "G90", "G1 F500", "M3 S300",
            "M5 S0 G0", "G1 F125 M3 S10.50", "M3 S0", "M5", "G0", "G54", "G55", "G54", "M8", "M9",
            "G21", "G0 G91 X5 F1000", "G0 G91 Z-15.0 F100",
            "G10 P1 L2 X5.000000", "G0 Y10 Z15 F100", "G0 G90 Y-10 F1000", "G91", "G0 G90 X0 Y0 F1000",
            "G91", "G21", "G10 P1 L2 Z-10.000000", "G0 Z2", "G10 L2 P1 X1", "G4 P0.001",
            "G1 X1 F100", "G1 F100 M3 S10.00", "G90",
        ]
        for line in lines:
            self.assertEqual(send(g, (line + "\n").encode()), [OK], line)

    def test_T14_prefixed_axis_only_lines_use_the_last_motion_mode(self):
        g = mk(instant=True)
        send(g, b"G1 X1 F100\n")
        self.assertEqual(send(g, b"X2\n"), [OK])
        self.assertEqual(g.mpos()[0], 2.0)

    def test_collapse_gcode_matches_collapseGCode(self):
        self.assertEqual(collapse_gcode(" g1 x1.5 (a (nested) b) y2 ; trailing"), "G1X1.5BY2")   # text after the first ')' is code
        self.assertEqual(collapse_gcode("(only)"), "")
        self.assertEqual(collapse_gcode("% "), "")
        self.assertEqual(collapse_gcode("(x)%"), "%")
        self.assertEqual(collapse_gcode("g0 ) x1"), "G0X1")
        seen = []
        collapse_gcode("G0 (MSG,hi) X1 (tail", seen.append)
        self.assertEqual(seen, ["MSG,hi", "tail"])
        seen = []
        collapse_gcode("(a (nested) b)", seen.append)
        self.assertEqual(seen, ["nested"])                    # a second '(' restarts the comment text

    def test_gcode_comment_msg_is_a_log_info_line(self):
        g = mk()
        self.assertEqual(send(g, b"(MSG,hello there)\n"), ["[MSG:INFO: MSG,hello there]\r\n", OK])

    def test_F21_laser_spindle_speed_in_status(self):
        g = mk(instant=True)
        send(g, b"G1F100\nM3S100\n")
        self.assertEqual(P02E.search(send(g, b"?")[0]).groups(), ("0", "100"))
        send(g, b"M5\n")
        self.assertEqual(P02E.search(send(g, b"?")[0]).groups(), ("0", "0"))

    def test_F21_hold_and_resume(self):
        g = mk()
        send(g, b"G1X100F100\n")
        g.tick(1.0)
        self.assertEqual(send(g, b"!\n"), [OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Hold:1")
        g.tick(3.0)
        self.assertEqual(status(send(g, b"?")[0])[0], "Hold:0")
        self.assertEqual(send(g, b"~\n"), [OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Run")

    def test_F21_feed_override_bytes_change_the_ov_field(self):
        for byte, expect in ((b"\x91", "110,100,100"), (b"\x92", "90,100,100"), (b"\x93", "101,100,100"),
                             (b"\x94", "99,100,100"), (b"\x9a", "100,100,110"), (b"\x9b", "100,100,90"),
                             (b"\x9c", "100,100,101"), (b"\x9d", "100,100,99")):
            g = mk()
            send(g, b"?")
            send(g, b"?")
            r = send(g, b"?" + byte + b"?\n")
            self.assertEqual(status(r[1])[1].get("Ov"), expect, byte)
            self.assertEqual(r[-1], OK)

    def _ov_after(self, g, *bytes_):
        """Ov field of the first report after the given realtime bytes (the counters are run past WCO first)."""
        send(g, b"?")
        send(g, b"?")
        r = send(g, b"?" + b"".join(bytes_) + b"?\n")
        return status(r[1])[1].get("Ov")

    def test_F21_feed_and_spindle_reset_bytes_restore_100(self):
        # RealtimeCmd.cpp:39-40 FeedOvrReset (0x90), 66-67 SpindleOvrReset (0x99)
        self.assertEqual(self._ov_after(mk(), b"\x91\x91\x90"), "100,100,100")
        g = mk()
        self.assertEqual(self._ov_after(g, b"\x91"), "110,100,100")
        self.assertEqual(self._ov_after(g, b"\x90"), "100,100,100")
        g = mk()
        self.assertEqual(self._ov_after(g, b"\x9a"), "100,100,110")
        self.assertEqual(self._ov_after(g, b"\x99"), "100,100,100")

    def test_F21_rapid_override_bytes_set_the_second_ov_value(self):
        # RealtimeCmd.cpp:54-65: 0x95 Default 100, 0x96 Medium 50, 0x97 Low 25, 0x98 ExtraLow 5 (Config.h:65-70)
        for byte, expect in ((b"\x96", "100,50,100"), (b"\x97", "100,25,100"), (b"\x98", "100,5,100")):
            g = mk()
            self.assertEqual(self._ov_after(g, byte), expect, byte)
        g = mk()
        self.assertEqual(self._ov_after(g, b"\x98"), "100,5,100")
        self.assertEqual(self._ov_after(g, b"\x95"), "100,100,100")
        self.assertEqual(self._ov_after(g, b"\x98"), "100,5,100")
        self.assertEqual(self._ov_after(g, b"\x97"), "100,25,100")

    def test_F21_rapid_override_bytes_in_one_write_are_applied_in_order(self):
        self.assertEqual(self._ov_after(mk(), b"\x97\x98"), "100,5,100")
        self.assertEqual(self._ov_after(mk(), b"\x98\x95"), "100,100,100")
        self.assertEqual(self._ov_after(mk(), b"\x98\x96"), "100,50,100")

    def test_F21_extra_low_rapid_override_slows_a_rapid_move(self):
        # Protocol.cpp:1367-1374 applies the percentage; a G0 runs at 5% of the 1000 mm/min max rate
        g = mk()
        send(g, b"\x98")
        send(g, b"G0X100\n")
        g.tick(2.0)
        self.assertLess(mpos(send(g, b"?")[0])[0], 3.0)
        g2 = mk()
        send(g2, b"G0X100\n")
        g2.tick(2.0)
        self.assertGreater(mpos(send(g2, b"?")[0])[0], 10.0)

    def test_F21_spindle_stop_override_byte_acts_only_in_hold(self):
        # Protocol.cpp:1403-1414: SpindleStopOvr is honoured only in State::Hold; the second byte restores
        g = mk(instant=True, spindle="none")
        send(g, b"M3S500\n")
        r = send(g, b"?\x9e?\n")
        self.assertEqual((status(r[0])[1]["FS"], status(r[1])[1]["FS"], r[-1]), ("0,500", "0,500", OK))
        send(g, b"G1X100F100\n")
        send(g, b"!")
        g.tick(5.0)
        send(g, b"\x9e")
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"].split(",")[1], "0")
        send(g, b"\x9e")
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"].split(",")[1], "500")

    def test_F21_flood_toggle_byte_flips_the_coolant_state(self):
        # Protocol.cpp:1415-1422: FloodToggle needs a flood pin and State Idle/Cycle/Hold; Mist (0xA1) needs a mist pin
        g = mk(flood_pin=True)
        self.assertEqual(send(g, b"M8\n"), [OK])
        self.assertIn(" M8 ", send(g, b"$G\n")[0])
        send(g, b"\xa0")
        self.assertIn(" M9 ", send(g, b"$G\n")[0])
        send(g, b"\xa0")
        self.assertIn(" M8 ", send(g, b"$G\n")[0])
        g = mk()
        send(g, b"\xa0\xa1")
        self.assertIn(" M9 ", send(g, b"$G\n")[0])

    def test_F21_ignored_bytes_still_end_with_an_ok_for_the_line(self):
        g = mk(instant=True)
        for byte in (b"\x86", b"\x87", b"\xa1"):
            self.assertEqual(send(g, byte + b"\n"), [OK], byte)

    def test_F21_safety_door_byte(self):
        g = mk()
        r = send(g, b"\x84")
        self.assertEqual(r, ["[MSG:INFO: Check door]\r\n"])

    def test_F21_check_mode_swallows_motion(self):
        g = mk()
        self.assertEqual(send(g, b"$C\n"), ["[MSG:INFO: Enabled]\r\n", OK])
        self.assertEqual(send(g, b"G1X10F100\n"), [OK])
        self.assertEqual(g.mpos(), [0.0, 0.0, 0.0])

    def test_infinite_value_never_raises_for_any_word_or_setting(self):
        # finding 2: a parsed float that overflows binary32 reaches a C cast that saturates, never a trap.  Every
        # word letter bare and after a motion, both signs, the special uses, the numbered settings and the named
        # integer / config settings.
        big = b"9" * 40
        lines = []
        for letter in string.ascii_uppercase.encode():
            for sign in (b"", b"-"):
                word = bytes([letter]) + sign + big
                lines += [word, b"G1X1F100 " + word, b"G2X10Y10 " + word + b"F100"]
        lines += [b"G4P" + big, b"G10L2P" + big + b"X1", b"G10L20P" + big + b"X1", b"G10L" + big + b"P1X1",
                  b"G2X1R" + big + b"F100", b"M3S" + big, b"M4S" + big, b"G91G1X" + big + b"Y" + big + b"F" + big]
        names = [b"$%d=" % n for n in range(140)] + [
            b"$Report/Status=", b"$Message/Level=", b"$HTTP/Port=", b"$SD/FallbackCS=", b"$Hostname=",
            b"$/axes/x/steps_per_mm=", b"$/axes/x/max_rate_mm_per_min=", b"$/axes/x/acceleration_mm_per_sec2=",
            b"$/axes/x/max_travel_mm=", b"$/axes/x/homing/cycle=", b"$/axes/x/soft_limits="]
        lines += [name + sign + big for name in names for sign in (b"", b"-")]
        for line in lines:
            g = mk()
            g.feed(line + b"\n")
            g.tick(0.2)
            r = out(g)
            self.assertTrue(all(x == OK or x.startswith(("error:", "[MSG:", "ALARM:")) for x in r), (line, r))
            self.assertTrue(STATUS_RE.fullmatch(send(g, b"?")[0]), line)            # still serving realtime commands

    def test_spindle_speed_overflow_follows_the_uint32_casts(self):
        big = b"9" * 40
        # Report.cpp:358 std::to_string(uint32_t(gc_state.spindle_speed)): +inf saturates to 2^32-1
        g = mk()
        self.assertEqual(send(g, b"S" + big + b"\n"), [OK])
        self.assertEqual(send(g, b"$G\n"), ["[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S4294967295]\r\n", OK])
        # Spindle.cpp:164-166 mapSpeed: uint32_t speed * ovr / 100 wraps at 2^32 on a laser: (2^32-1)*100 mod 2^32
        # = 4294967196, / 100 = 42949671
        g = mk()
        for line in (b"M3S" + big, b"G1X100F600"):
            self.assertEqual(send(g, line + b"\n"), [OK])
        g.tick(0.5)
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"], "600,42949671")
        # a spindle without PWM reports the raw (saturated) speed
        g = mk(spindle="none")
        send(g, b"M3S" + big + b"\n")
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"], "0,4294967295")


class AxisVariants(unittest.TestCase):
    ZERO = {"XY": "0.000,0.000", "XYZ": "0.000,0.000,0.000", "XYZA": "0.000,0.000,0.000,0.000",
            "XYZAB": "0.000,0.000,0.000,0.000,0.000"}

    def test_axes_must_be_a_prefix_of_xyzabc_with_2_to_6_letters(self):
        for bad in ("X", "XZ", "YZ", "ZYX", "XYZABCD", "XYB", "", "xyz"):
            with self.assertRaises(ValueError, msg=bad):
                FluidNCVirtual(axes=bad)
        self.assertEqual(FluidNCVirtual(axes="XYZABC").n_axis, 6)
        with self.assertRaises(ValueError):
            FluidNCVirtual(homing="Q")

    def test_status_idle_and_wco_value_counts(self):
        for axes, zeros in self.ZERO.items():
            g = mk(axes=axes)
            self.assertEqual(send(g, b"?"), ["<Idle|MPos:%s|FS:0,0|WCO:%s>\r\n" % (zeros, zeros)], axes)

    def test_offsets_have_exactly_n_values_per_line(self):
        for axes, zeros in self.ZERO.items():
            r = send(mk(axes=axes), b"$#\n")
            n = len(axes)
            self.assertEqual(len(r), 14, axes)
            for ln in r[:-1]:
                self.assertEqual(len(ln[ln.index(":") + 1:ln.index("]")].split(",")), n, (axes, ln))

    def test_P09c_two_axis_dollar_hash_reply_has_two_values_per_line(self):
        r = send(mk(axes="XY"), b"$#\n")
        self.assertEqual(r[0], "[G54:0.000,0.000]\r\n")
        self.assertEqual(r[-2], "[TLO:0.000,0.000]\r\n")
        for ln in r[:-1]:
            body = ln.replace("[", "").replace("]", "").strip().split(":")[1]
            self.assertEqual(len(body.split(",")), 2, ln)

    def test_P09c_plugin_offsets_loop_breaks_on_two_axis_replies(self):
        # PLUGIN BUG (recorded, not fixed): __init__:1304-1315 reads offsetvalues[2] unconditionally, so a
        # 2-axis FluidNC (this exact reply) raises IndexError; a 3-axis reply parses.
        def plugin_loop(reply):
            offsets = {}
            for offset in "".join(reply[:-1]).rstrip("\r").rstrip("\n").split("\n"):
                offsetpair = offset.replace("[", "").replace("]", "").replace("\r", "").split(":")
                if offsetpair[0] in ("G54", "G55", "G56", "G57", "G58", "G59"):
                    values = offsetpair[1].split(",")
                    offsets[offsetpair[0]] = (float(values[0]), float(values[1]), float(values[2]))
            return offsets
        self.assertEqual(len(plugin_loop(send(mk(axes="XYZ"), b"$#\n"))), 6)
        with self.assertRaises(IndexError):
            plugin_loop(send(mk(axes="XY"), b"$#\n"))

    def test_proxy_settings_follow_the_axis_count(self):
        for axes in self.ZERO:
            r = [ln.rstrip("\r\n") for ln in send(mk(axes=axes), b"$$\n")[:-1]]
            n = len(axes)
            for base in (100, 110, 120, 130):
                self.assertEqual([x for x in r if re.match(r"\$%d\d=" % (base // 10), x)],
                                 ["$%d=%s" % (base + i, {100: "80.000", 110: "1000.000", 120: "25.000", 130: "1000.000"}[base])
                                  for i in range(n)], (axes, base))

    def test_settings_list_follows_the_axis_count(self):
        r = [ln.rstrip("\r\n") for ln in send(mk(axes="XY"), b"$S\n")[:-1]]
        self.assertIn("$Grbl/MaxRate/Y=1000.000", r)
        self.assertNotIn("$Grbl/MaxRate/Z=1000.000", r)
        self.assertEqual(r[6:8], ["$Grbl/Resolution/X=80.000", "$Grbl/Resolution/Y=80.000"])

    def test_dump_has_only_the_built_axes(self):
        for axes in self.ZERO:
            text = "".join(send(mk(axes=axes), b"$CD\n")[:-1])
            present = re.findall(r"^  ([xyzabc]): \r$", text, re.M)
            self.assertEqual(present, [c.lower() for c in axes], axes)

    def test_words_for_missing_axes_are_error_20(self):
        for axes in self.ZERO:
            g = mk(axes=axes, instant=True)
            for letter in "ABC":
                reply = send(g, ("G1 %s1 F100\n" % letter).encode())
                if letter in axes:
                    self.assertEqual(reply, [OK], (axes, letter))
                else:
                    self.assertEqual(reply[1:], err(20, "Unsupported GCode command")[0:2], (axes, letter))
            reply = send(g, b"G1 Z1 F100\n")
            self.assertEqual(reply, [OK] if "Z" in axes else
                             ["[MSG:ERR: Bad GCode: G1 Z1 F100]\r\n", "error:20\r\n", "[MSG:ERR: Unsupported GCode command]\r\n"])

    def test_motion_reaches_every_axis(self):
        g = mk(axes="XYZAB", instant=True)
        send(g, b"G0 X1 Y2 Z3 A4 B5\n")
        self.assertEqual(mpos(send(g, b"?")[0]), [1.0, 2.0, 3.0, 4.0, 5.0])
        g = mk(axes="XY", instant=True)
        send(g, b"G0 X1 Y2\n")
        self.assertEqual(mpos(send(g, b"?")[0]), [1.0, 2.0])
        g = mk(axes="XYZABC", instant=True)
        send(g, b"G0 X1 Y2 Z3 A4 B5 C6\n")
        self.assertEqual(mpos(send(g, b"?")[0]), [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        self.assertEqual(send(g, b"G0 D1\n")[1:], err(20, "Unsupported GCode command"))

    def test_xy_arcs_and_modes_work_without_a_z_axis(self):
        g = mk(axes="XY", instant=True)
        self.assertEqual(send(g, b"G17 G2 X10 Y0 I5 J0 F600\n"), [OK])
        self.assertEqual(mpos(send(g, b"?")[0]), [10.0, 0.0])
        self.assertEqual(send(g, b"G18 G2 X0 I5 K0 F600\n"), err(32, "Gcode no axis words in plane", "G18 G2 X0 I5 K0 F600"))

    def test_xy_tool_length_and_g49(self):
        g = mk(axes="XY", instant=True)
        self.assertEqual(send(g, b"G49\n"), [OK])
        self.assertEqual(send(g, b"G43.1 X1\n"), err(37, "Gcode G43 dynamic axis error", "G43.1 X1"))

    def test_xy_g92_wco_and_work_offsets_keep_two_values(self):
        g = mk(axes="XY", instant=True)
        send(g, b"G10 L2 P1 X5 Y6\nG0 X1 Y2\nG92 X0\n")
        r = send(g, b"$#\n")
        self.assertEqual(r[0], "[G54:5.000,6.000]\r\n")
        self.assertEqual(r[11], "[G92:1.000,0.000]\r\n")                   # G92x = MPos(6) - WCS(5) - target(0)

    def test_xy_probe_prb_has_two_values(self):
        g = mk(axes="XY", instant=True, probe_pin=True)
        g.probe_trigger = {"Y": -3.0}
        self.assertEqual(send(g, b"G91 G38.2 Y-5 F50\n"), ["[PRB:0.000,-3.000:1]\r\n", OK])

    def test_xyza_status_inch_mode_leaves_the_rotary_axis_in_degrees(self):
        g = mk(axes="XYZA", instant=True)
        send(g, b"G0 X25.4 A45\n$13=1\n")
        self.assertEqual(send(g, b"?")[0].split("|")[1], "MPos:1.0000,0.0000,0.0000,45.000")

    def test_axes_with_homing_and_cycle_names(self):
        g = mk(axes="XYZA", homing="XYZA")
        self.assertEqual(send(g, b"$H\n"), ["[MSG:Homed:XYZA]\r\n", OK])


def advance(g, seconds, step=0.1):
    """tick() in small steps: a delay or a system motion that ends inside one big tick() starts its successor on the next."""
    for _ in range(int(round(seconds / step))):
        g.tick(step)


class Parking(unittest.TestCase):
    """Parking.cpp: ``parking:`` is runtime config; 0x84 parks when ``can_park()`` and the axis is below target.

    Defaults (Parking.h): axis Z, target -5.0, fast 800 mm/min, pull-out 5 mm at 250 mm/min.  The cut below is at
    Z -20, so the waypoint is -15.  Timing is the linear model: the 600 mm/min cut stops in 0.4 s (25 mm/s^2), then
    the pull-out takes 1.2 s and the fast leg 0.75 s, so the checks use margins rather than exact instants."""

    @staticmethod
    def cutting(enable=True, spindle="pwm", homing="XYZ", z=-20.0, rpm=5000, pwm=None):
        g = mk(homing=homing, spindle=spindle)
        send(g, b"$X\n")
        if enable is not None:
            send(g, b"$/parking/enable=%s\n" % (b"true" if enable else b"false"))
        for key, val in (pwm or {}).items():
            send(g, b"$/PWM/%s=%d\n" % (key.encode(), val))
        for line in (b"G21G90\n", b"G0Z%f\n" % z, b"M3S%d\n" % rpm):
            send(g, line)
        advance(g, 5.0)
        send(g, b"G1X100F600\n")
        advance(g, 1.0)
        return g

    @staticmethod
    def state(g):
        return status(send(g, b"?")[0])

    @staticmethod
    def z(g):
        return mpos(send(g, b"?")[0])[2]

    def test_parking_enable_is_runtime_config_default_false(self):
        g = mk()
        self.assertEqual(send(g, b"$/parking/enable\n"), ["$/parking/enable=false\r\n", OK])
        send(g, b"$/parking/enable=true\n")
        self.assertEqual(send(g, b"$/parking/enable\n"), ["$/parking/enable=true\r\n", OK])

    def test_opt_always_carries_p_so_it_says_nothing_about_parking(self):
        # Report.cpp:376: msg += "PH" unconditionally
        for enable in (False, True):
            g = mk()
            send(g, b"$/parking/enable=%s\n" % (b"true" if enable else b"false"))
            r = send(g, b"$I\n")
            self.assertIn("[OPT:PHS]\r\n", r)

    def test_parking_defaults_are_the_firmware_ones(self):
        # Parking.h defaults: axis Z, target -5.0, fast 800 mm/min, pull-out 5 mm at 250 mm/min. Read back through
        # the config interface and through the parameters the motion uses, so a changed default cannot hide
        g = mk()
        for key, value in (("axis", "Z"), ("target_mpos_mm", "-5.000"), ("rate_mm_per_min", "800.000"),
                           ("pullout_distance_mm", "5.000"), ("pullout_rate_mm_per_min", "250.000")):
            self.assertEqual(send(g, b"$/parking/%s\n" % key.encode())[0], "$/parking/%s=%s\r\n" % (key, value))
        self.assertEqual(g._park_params(), (2, -5.0, 800.0, 5.0, 250.0))

    def test_a_parking_section_with_only_enable_falls_back_to_the_same_defaults(self):
        # the .get() fallbacks of _park_params are a second copy of the defaults: they must agree with the first
        g = mk()
        g._cfg["parking"] = {"enable": True}
        self.assertEqual(g._park_params(), (2, -5.0, 800.0, 5.0, 250.0))

    def test_pullout_at_250_then_spindown_then_fast_retract_at_800(self):
        g = self.cutting()
        send(g, b"\x84")
        advance(g, 1.0)                                # stop the cut, begin the pull-out (5 mm at 250 mm/min = 1.2 s)
        state, f = self.state(g)
        self.assertEqual(state, "Door:2")
        self.assertEqual(f["FS"][-4:], "5000")         # spindle still on during the pull-out
        self.assertTrue(-20.0 < self.z(g) < -15.0, self.z(g))
        advance(g, 0.8)                                # pull-out done, spindle off, fast leg (10 mm at 800 = 0.75 s)
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"], "0,0")
        advance(g, 0.8)
        state, f = self.state(g)
        self.assertEqual(state, "Door:0")
        self.assertAlmostEqual(self.z(g), -5.0, places=3)

    def test_resume_has_no_spindle_wait_by_default(self):
        # Spindle.h:96 _spinup_ms = 0 and no coolant delay: the plunge follows the spindle at once
        g = self.cutting()
        send(g, b"\x84")
        advance(g, 4.0)
        self.assertEqual(self.state(g)[0], "Door:0")
        stopped = mpos(send(g, b"?")[0])
        send(g, b"~")
        advance(g, 0.9)                                # fast back to the waypoint (10 mm at 800 mm/min = 0.75 s)
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"][-4:], "5000")      # spindle on again
        advance(g, 0.2)
        self.assertTrue(self.z(g) < -15.0, self.z(g))  # already plunging: no 4 s wait
        advance(g, 1.5)                                # plunge 5 mm at 250 mm/min = 1.2 s
        state, f = self.state(g)
        self.assertEqual(state, "Run")
        self.assertAlmostEqual(self.z(g), -20.0, places=3)

    def test_cycle_start_is_dropped_until_door_0(self):
        g = self.cutting()
        send(g, b"\x84")
        advance(g, 1.5)
        self.assertEqual(self.state(g)[0], "Door:2")
        send(g, b"~")
        advance(g, 0.2)
        self.assertEqual(self.state(g)[0], "Door:2")

    def test_spinup_ms_waits_in_proportion_to_the_speed_after_the_spindle_comes_back(self):
        # Spindle.cpp:272-282: dwell = spinup_ms * up / maxSpeed (PWM default map tops at 10000): S5000 -> half
        g = self.cutting(pwm={"spinup_ms": 2000, "spindown_ms": 1000})
        send(g, b"\x84")
        advance(g, 5.0)
        self.assertEqual(self.state(g)[0], "Door:0")
        send(g, b"~")
        advance(g, 0.9)
        z_on = self.z(g)
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"][-4:], "5000")
        advance(g, 0.8)                                # 1.0 s spin-up (half of 2000 ms) still running
        self.assertAlmostEqual(self.z(g), z_on, places=3)
        advance(g, 0.5)
        self.assertTrue(self.z(g) < z_on)              # plunge has begun

    def test_spindown_ms_delays_the_fast_retract(self):
        g = self.cutting(pwm={"spindown_ms": 2000})    # S5000 of 10000 -> 1.0 s
        send(g, b"\x84")
        advance(g, 2.5)                                # 0.4 s stop + 1.2 s pull-out + 0.9 s into the spin-down
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"], "0,0")
        z_hold = self.z(g)
        self.assertAlmostEqual(z_hold, -15.0, places=3)
        advance(g, 0.5)
        self.assertTrue(self.z(g) > -15.0)             # fast leg started after the delay

    def test_parking_disabled_only_stops_the_spindle(self):
        g = self.cutting(enable=False)
        send(g, b"\x84")
        advance(g, 4.0)
        state, f = self.state(g)
        self.assertEqual((state, f["FS"]), ("Door:0", "0,0"))
        self.assertAlmostEqual(self.z(g), -20.0, places=3)

    def test_no_parking_when_the_axis_has_no_homing_cycle(self):
        # Parking.cpp:44-47 "No parking without homing"
        g = self.cutting(homing=None)
        send(g, b"\x84")
        advance(g, 4.0)
        self.assertEqual(self.state(g)[0], "Door:0")
        self.assertAlmostEqual(self.z(g), -20.0, places=3)

    def test_no_parking_for_a_laser(self):
        # Parking.cpp:39-42 "No parking in Laser mode"
        g = self.cutting(spindle="laser")
        send(g, b"\x84")
        advance(g, 4.0)
        self.assertEqual(self.state(g)[0], "Door:0")
        self.assertAlmostEqual(self.z(g), -20.0, places=3)

    def test_z_above_the_target_does_not_move(self):
        g = self.cutting(z=-3.0)
        send(g, b"\x84")
        advance(g, 3.0)
        state, f = self.state(g)
        self.assertEqual((state, f["FS"]), ("Door:0", "0,0"))
        self.assertAlmostEqual(self.z(g), -3.0, places=3)
        send(g, b"~")
        advance(g, 1.0)
        self.assertEqual(self.state(g)[0], "Run")
        self.assertAlmostEqual(self.z(g), -3.0, places=3)

    def test_target_and_rates_come_from_the_config(self):
        g = self.cutting()
        send(g, b"$/parking/target_mpos_mm=-12\n")
        send(g, b"$/parking/pullout_distance_mm=2\n")
        send(g, b"\x84")
        advance(g, 5.0)
        self.assertEqual(self.state(g)[0], "Door:0")
        self.assertAlmostEqual(self.z(g), -12.0, places=3)

    def test_parking_motion_never_enters_the_planner(self):
        g = self.cutting()
        send(g, b"\x84")
        advance(g, 1.0)
        self.assertEqual(len(g.blocks), 1)
        self.assertIsNotNone(g.sys_block)


class CorpusStream(unittest.TestCase):
    def test_10_every_corpus_line_streams_without_error_or_alarm(self):
        if not os.path.isdir(CORPUS_DIR):
            self.skipTest("corpus directory %s is absent (it lives in scratch)" % CORPUS_DIR)
        files = sorted(os.path.join(d, f) for d, _, fs in os.walk(CORPUS_DIR) for f in fs)
        if not files:
            self.skipTest("corpus directory is empty")
        total_lines = bad = pct = 0
        pct_replies = []
        summary = []
        for path in files:
            g = FluidNCVirtual(instant=True)
            g.drain()
            n = errs = 0
            with open(path, "rb") as fh:
                for raw in fh:
                    n += 1
                    line = raw.rstrip(b"\r\n")
                    g.feed(line + b"\n")
                    replies = g.drain()
                    if line.lstrip().startswith(b"%"):
                        # Program-delimiter / banner lines the plugin never forwards (census T05).  FluidNC clears a
                        # lone '%' (collapseGCode) and answers error:1 to '% text', as the C++ does.
                        pct += 1
                        pct_replies.append((os.path.basename(path), n, line, replies))
                        continue
                    for r in replies:
                        if r.startswith((b"error:", b"ALARM:", b"[MSG:ERR")):
                            errs += 1
                            if bad + errs <= 5:
                                print("UNEXPECTED %s:%d %r -> %r" % (os.path.basename(path), n, line, r))
            total_lines += n
            bad += errs
            summary.append((os.path.relpath(path, CORPUS_DIR), n, errs))
        print("\nfluidnc corpus summary: %d files, %d lines, %d error/ALARM replies, %d '%%' lines held back (plugin T05)"
              % (len(files), total_lines, bad, pct))
        for name, n, e in summary:
            print("  %8d lines %3d errors  %s" % (n, e, name))
        for name, n, line, replies in pct_replies:
            if line.strip() == b"%":
                self.assertEqual(replies, [b"ok\r\n"], (name, n))
            else:
                self.assertIn(b"error:1\r\n", replies, (name, n))
        self.assertEqual(bad, 0)
        self.assertEqual(total_lines, sum(n for _, n, _ in summary))


if __name__ == "__main__":
    unittest.main()
