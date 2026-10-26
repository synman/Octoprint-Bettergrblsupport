#!/usr/bin/env python3
"""Tests for grbl_virtual.py, the Python port of gnea/grbl 1.1h (bfb67f0).

Run:  cd tests/simulator && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_grbl_virtual -v

Expected bytes are quoted from the C format strings (report.c / protocol.c / system.c / settings.c in
the pinned tree), not produced by running the port.  Census row ids (S01..S62, T01..T17, P01..) refer to
the Better Grbl Support firmware-behaviour census.  Motion-timing expectations come from the documented
linear motion model in grbl_virtual.py (constant nominal speed, one constant-deceleration ramp).
"""
import os
import re
import string
import unittest

from grbl_virtual import GrblVirtual

BANNER = ["\r\n", "Grbl 1.1h ['$' for help]\r\n"]
OK = "ok\r\n"
IDLE0 = "<Idle|MPos:0.000,0.000,0.000|FS:0,0>\r\n"
# Directory of real laser/CNC G-code to stream (not in the repo). Set GRBL_SIM_CORPUS to enable test 10.
CORPUS_DIR = os.environ.get("GRBL_SIM_CORPUS", "")
STATUS_RE = re.compile(r"<([^|>]+)((?:\|[^|>]+)*)>\r\n")

# Patterns copied from the plugin as listed in the census (section 2).
P02A = re.compile(r"<(-?[^,]+)[,\|][WM]Pos:(-?[\d\.]+),(-?[\d\.]+)(?:,(-?[\d\.]+))?(?:,(-?[\d\.]+))?(?:,(-?[\d\.]+))?")
P02D = re.compile(r"\|Pn:([XYZABPDHRS]+)")
P02E = re.compile(r"\|FS:(-?[\d\.]+),(-?[\d\.]+)")
P03 = re.compile(r"alarm:\ *(-?[\d.]+)")
P04 = re.compile(r"error:\ *(-?[\d.]+)")
P08 = re.compile(r"^[$](-?[\d\.]+)=(-?[\d\.]+)")


def mk(**kw):
    """Fresh controller with the banner already drained."""
    g = GrblVirtual(**kw)
    assert [x.decode() for x in g.drain()] == BANNER
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


def near(tc, got, want, places=3):
    tc.assertEqual(len(got), len(want), (got, want))
    for a, b in zip(got, want):
        tc.assertAlmostEqual(a, b, places=places, msg=(got, want))


def tick_until(g, token, step=0.1, limit=60.0):
    """tick() until a reply line equal to ``token`` shows up; returns every line seen."""
    seen, t = [], 0.0
    while t <= limit:
        g.tick(step)
        t += step
        seen += out(g)
        if token in seen:
            return seen
    raise AssertionError("no %r within %.0fs: %r" % (token, limit, seen))


class Basics(unittest.TestCase):
    def test_01_banner_on_construct(self):
        # report.c:report_init_message prints "\r\nGrbl " GRBL_VERSION " ['$' for help]\r\n"
        g = GrblVirtual()
        self.assertEqual(out(g), BANNER)

    def test_01_banner_after_soft_reset(self):
        g = mk()
        self.assertEqual(send(g, b"\x18"), BANNER)

    def test_02_status_alone_is_one_report_without_ok(self):
        g = mk()
        # first report after boot carries WCO (report.c: sys.report_wco_counter == 0)
        self.assertEqual(send(g, b"?"), ["<Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000>\r\n"])

    def test_02_status_with_newline_gets_ok_for_the_empty_line(self):
        # protocol.c:93-95 "Empty or comment line. For syncing purposes." -> report_status_message(STATUS_OK)
        g = mk()
        r = send(g, b"?\n")
        self.assertEqual(len(r), 2)
        self.assertEqual(status(r[0])[0], "Idle")
        self.assertEqual(r[1], OK)

    def test_02_other_realtime_bytes_with_newline_ack_too(self):
        g = mk()
        self.assertEqual(send(g, b"~\n"), [OK])
        # '!' from Idle completes the hold at once (protocol.c: sys.suspend = SUSPEND_HOLD_COMPLETE) and the main
        # loop then sits in protocol_exec_rt_suspend, so the empty line's ok is only sent after the resume.
        self.assertEqual(send(g, b"!\n"), [])
        self.assertEqual(send(g, b"~"), [OK])

    def test_02_crlf_is_two_eol_characters_and_two_oks(self):
        # protocol.c treats '\n' and '\r' each as end-of-line, so CRLF acks the empty second line
        g = mk()
        self.assertEqual(send(g, b"G0X1\r\n"), [OK, OK])

    def test_03_settings_dump_defaults_in_report_order(self):
        # report.c:report_grbl_settings order; defaults.h DEFAULTS_GENERIC; floats N_DECIMAL_SETTINGVALUE=3
        expect = ["$0=10", "$1=25", "$2=0", "$3=0", "$4=0", "$5=0", "$6=0", "$10=1", "$11=0.010", "$12=0.002",
                  "$13=0", "$20=0", "$21=0", "$22=0", "$23=0", "$24=25.000", "$25=500.000", "$26=250",
                  "$27=1.000", "$30=1000", "$31=0", "$32=0", "$100=250.000", "$101=250.000", "$102=250.000",
                  "$110=500.000", "$111=500.000", "$112=500.000", "$120=10.000", "$121=10.000", "$122=10.000",
                  "$130=200.000", "$131=200.000", "$132=200.000"]
        r = send(mk(), b"$$\n")
        self.assertEqual(r, [e + "\r\n" for e in expect] + [OK])

    def test_04_build_info(self):
        # report.c:report_build_info: "[VER:" GRBL_VERSION "." GRBL_VERSION_BUILD ":" line "]" ; grbl.h:26 BUILD "20190830"
        # "[OPT:" V (VARIABLE_SPINDLE) "," BLOCK_BUFFER_SIZE-1 "," RX_BUFFER_SIZE "]" = V,15,128
        self.assertEqual(send(mk(), b"$I\n"), ["[VER:1.1h.20190830:]\r\n", "[OPT:V,15,128]\r\n", OK])

    def test_04_parser_state_after_defaults(self):
        # report.c:report_gcode_modes: "[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]"
        self.assertEqual(send(mk(), b"$G\n"), ["[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n", OK])

    def test_04_ngc_parameters_after_defaults(self):
        # report.c:report_ngc_parameters: G54-G59, G28, G30, G92, TLO, PRB (3 decimals, ":0" probe flag)
        z = "0.000,0.000,0.000"
        exp = ["[G%s:%s]\r\n" % (n, z) for n in ("54", "55", "56", "57", "58", "59", "28", "30", "92")]
        exp += ["[TLO:0.000]\r\n", "[PRB:0.000,0.000,0.000:0]\r\n", OK]
        self.assertEqual(send(mk(), b"$#\n"), exp)

    def test_04_help(self):
        self.assertEqual(send(mk(), b"$\n"),
                         ["[HLP:$$ $# $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H ~ ! ? ctrl-x]\r\n", OK])

    def test_line_assembly_comments_case_and_whitespace(self):
        g = mk(instant=True)
        self.assertEqual(send(g, b"g1 x1 (a comment) y2 f100 ; trailing\n"), [OK])
        self.assertEqual(g.mpos(), [1.0, 2.0, 0.0])
        self.assertEqual(send(g, b"/G0X5\n"), [OK])      # block delete char is ignored, not an error
        self.assertEqual(g.mpos()[0], 5.0)
        self.assertEqual(send(g, b"(only a comment)\n"), [OK])
        self.assertEqual(send(g, b"G0 X1 ) \n"), ["error:1\r\n"])   # stray ')' stays in the line

    def test_line_assembly_split_across_feeds(self):
        g = mk(instant=True)
        self.assertEqual(send(g, b"G1 X"), [])
        self.assertEqual(send(g, b"2 F50"), [])
        self.assertEqual(send(g, b"0\n"), [OK])
        self.assertEqual(g.mpos()[0], 2.0)

    def test_realtime_byte_inside_a_line_is_stripped(self):
        g = mk(instant=True)
        r = send(g, b"G1 X1 ? F100\n")
        self.assertEqual(status(r[0])[0], "Idle")
        self.assertEqual(r[1:], [OK])
        self.assertEqual(g.mpos()[0], 1.0)

    def test_extended_ascii_junk_is_discarded(self):
        g = mk()
        self.assertEqual(send(g, b"\xa1\xff\n"), [OK])   # 0xA1 needs ENABLE_M7; 0xFF is unassigned

    def test_clock_default_and_injection(self):
        t = [0.0]
        g = GrblVirtual(clock=lambda: t[0])
        out(g)
        send(g, b"G1X100F300\n")
        t[0] = 1.0
        g.tick()                       # dt from the injected clock: 1 s at 5 mm/s
        self.assertEqual(mpos(send(g, b"?")[0])[0], 5.0)

    def test_axes_other_than_xyz_are_refused(self):
        with self.assertRaises(ValueError):
            GrblVirtual(axes="XYZAB")

    def test_axis_count_is_per_instance_and_defaults_to_the_stock_three(self):
        g = GrblVirtual()
        self.assertEqual((g.n_axis, g.axis_letters), (3, "XYZ"))
        self.assertEqual((len(g.sys_position), len(g.settings.steps_per_mm), len(g.gc.position)), (3, 3, 3))


class StatusReports(unittest.TestCase):
    def test_05_idle_wco_and_ov_cadence(self):
        # report.c:602-650 with REPORT_WCO_REFRESH_IDLE_COUNT 10 / REPORT_OVR_REFRESH_IDLE_COUNT 10:
        # WCO on reports 1, 11, 21; Ov on reports 2, 12, 22.
        g = mk()
        wco, ov = [], []
        for i in range(1, 23):
            f = status(send(g, b"?")[0])[1]
            if "WCO" in f:
                wco.append(i)
            if "Ov" in f:
                ov.append(i)
        self.assertEqual((wco, ov), ([1, 11, 21], [2, 12, 22]))

    def test_05_busy_wco_and_ov_cadence(self):
        # report.c:602-622 reload the counters from REPORT_WCO_REFRESH_BUSY_COUNT 30 / REPORT_OVR_REFRESH_BUSY_COUNT 20
        # (config.h:285-288) while the state is Homing, Cycle, Hold, Jog or Safety door, stored minus one: WCO on
        # reports 1, 31, 61; Ov on 2, 22, 42, 62 (the WCO refresh at 61 re-arms Ov for the next report).
        g = mk()
        self.assertEqual(send(g, b"G1X1000F600\n"), [OK])
        g.tick(0.5)
        wco, ov = [], []
        for i in range(1, 63):
            state, f = status(send(g, b"?")[0])
            self.assertEqual(state, "Run")
            if "WCO" in f:
                wco.append(i)
            if "Ov" in f:
                ov.append(i)
        self.assertEqual((wco, ov), ([1, 31, 61], [2, 22, 42, 62]))

    def test_05_ov_field_values(self):
        g = mk()
        send(g, b"?")
        self.assertEqual(send(g, b"?"), ["<Idle|MPos:0.000,0.000,0.000|FS:0,0|Ov:100,100,100>\r\n"])

    def test_05_run_during_a_move(self):
        g = mk()
        self.assertEqual(send(g, b"G1X100F500\n"), [OK])
        g.tick(1.0)                    # 500 mm/min = 8.333 mm/s -> 2083 steps at 250 steps/mm
        st = send(g, b"?")[0]
        self.assertEqual(st, "<Run|MPos:8.332,0.000,0.000|FS:500,0|WCO:0.000,0.000,0.000>\r\n")

    def test_05_run_finishes_idle_at_target(self):
        g = mk()
        send(g, b"G1X10F600\n")
        g.tick(0.5)
        self.assertEqual(status(send(g, b"?")[0])[0], "Run")
        g.tick(5.0)
        self.assertEqual(mpos(send(g, b"?")[0]), [10.0, 0.0, 0.0])
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")

    def test_05_hold_after_feed_hold(self):
        g = mk()
        send(g, b"G1X100F500\n")
        g.tick(1.0)
        self.assertEqual(send(g, b"!"), [])
        st, f = status(send(g, b"?")[0])
        self.assertEqual(st, "Hold:1")             # decelerating
        self.assertEqual(f["FS"], "500,0")
        g.tick(2.0)
        st, f = status(send(g, b"?")[0])
        self.assertEqual((st, f["FS"]), ("Hold:0", "0,0"))
        x = mpos(send(g, b"?")[0])[0]
        self.assertTrue(11.7 < x < 11.9, x)         # 8.33 mm + v*t/2 of the ramp
        g.tick(5.0)                                  # stays held until ~
        self.assertEqual(mpos(send(g, b"?")[0])[0], x)
        self.assertEqual(status(send(g, b"?")[0])[0], "Hold:0")

    def test_05_hold_then_cycle_start_resumes_and_finishes(self):
        g = mk()
        send(g, b"G1X20F600\n")
        g.tick(0.5)
        send(g, b"!")
        g.tick(1.0)
        self.assertEqual(status(send(g, b"?")[0])[0], "Hold:0")
        send(g, b"~")
        self.assertEqual(status(send(g, b"?")[0])[0], "Run")
        g.tick(10.0)
        self.assertEqual(mpos(send(g, b"?")[0])[0], 20.0)

    def test_05_hold_from_idle_is_hold_0_and_blocks_gcode_until_resume(self):
        g = mk(instant=True)
        send(g, b"!")
        self.assertEqual(status(send(g, b"?")[0])[0], "Hold:0")
        self.assertEqual(send(g, b"G0X1\n"), [])    # main loop is inside the suspend loop
        self.assertEqual(send(g, b"~"), [OK])
        self.assertEqual(g.mpos()[0], 1.0)

    def test_05_jog_state_during_jog(self):
        g = mk()
        self.assertEqual(send(g, b"$J=G91G21X10Y5F500\n"), [OK])
        g.tick(0.1)
        self.assertEqual(status(send(g, b"?")[0])[0], "Jog")
        g.tick(5.0)
        st = send(g, b"?")[0]
        self.assertEqual((status(st)[0], mpos(st)), ("Idle", [10.0, 5.0, 0.0]))

    def test_05_alarm_state(self):
        g = mk()
        g.inject_alarm(5)
        self.assertEqual(out(g), ["ALARM:5\r\n"])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")

    def test_05_check_mode_state(self):
        g = mk()
        send(g, b"$C\n")
        self.assertEqual(status(send(g, b"?")[0])[0], "Check")

    def test_06_status_mask_wpos_and_wco(self):
        # settings.c case 10; report.c:510-527: WPos = MPos - WCO when $10 bit0 is clear
        g = mk(instant=True)
        send(g, b"$10=0\n")
        self.assertEqual(send(g, b"?"), ["<Idle|WPos:0.000,0.000,0.000|FS:0,0|WCO:0.000,0.000,0.000>\r\n"])
        send(g, b"G0X10\n")
        send(g, b"G92X2\n")            # G92 = MPos - WCS - target = 10 - 0 - 2 = 8
        self.assertEqual(send(g, b"?")[0].split("|")[1], "WPos:2.000,0.000,0.000")
        self.assertEqual(send(g, b"$10=1\n"), [OK])
        self.assertEqual(send(g, b"?")[0].split("|")[1], "MPos:10.000,0.000,0.000")

    def test_06_status_mask_buffer_state(self):
        # report.c REPORT_FIELD_BUFFER_STATE: "|Bf:" planner-blocks-available "," rx-bytes-available
        g = mk()
        send(g, b"$10=3\n")
        self.assertEqual(send(g, b"?"), ["<Idle|MPos:0.000,0.000,0.000|Bf:15,128|FS:0,0|WCO:0.000,0.000,0.000>\r\n"])
        send(g, b"$10=2\n")
        self.assertIn("|WPos:", send(g, b"?")[0])

    def test_06_buffer_state_tracks_planner_and_rx(self):
        g = mk()
        send(g, b"$10=3\n")
        send(g, b"G1X10F100\nG1X20\nG1X30\n")
        self.assertIn("|Bf:12,128|", send(g, b"?")[0])      # 3 blocks queued, rx drained
        g2 = mk()
        send(g2, b"$10=3\n")
        send(g2, b"G4P5\n")                                   # main loop blocked in the dwell
        send(g2, b"G0X1\n")                                   # 5 bytes parked in the 128-byte RX ring
        self.assertIn("|Bf:15,123|", send(g2, b"?")[0])

    def test_06_pins_in_status(self):
        g = mk()
        g.limit_pins = 1 | 4
        self.assertIn("|Pn:XZ|", send(g, b"?")[0])
        g.probe_tripped = True
        self.assertIn("|Pn:PXZ|", send(g, b"?")[0])

    def test_06_probe_pin_inverted_setting_reports_pn_p(self):
        # probe.c: with $6=1 the normally-high pin reads as triggered when open
        g = mk()
        send(g, b"$6=1\n")
        self.assertIn("|Pn:P|", send(g, b"?")[0])

    def test_06_accessory_field_and_spindle_speed(self):
        g = mk(instant=True)
        send(g, b"M3S100M8\n")
        send(g, b"?")
        self.assertEqual(send(g, b"?"), ["<Idle|MPos:0.000,0.000,0.000|FS:0,100|Ov:100,100,100|A:SF>\r\n"])

    def test_06_inch_reporting(self):
        # print.c:printFloat_CoordValue: n*INCH_PER_MM with 4 decimals when $13=1
        g = mk(instant=True)
        send(g, b"G20G0X1\n")
        send(g, b"$13=1\n")
        self.assertEqual(send(g, b"?")[0].split("|")[1], "MPos:1.0000,0.0000,0.0000")

    def test_06_negative_tiny_prints_minus_zero(self):
        # print.c:printFloat prints '-' for n<0 before rounding
        g = mk(instant=True)
        send(g, b"G92X-0.0004\n")     # G92 = MPos - WCS - target = +0.0004, so WPos = -0.0004
        send(g, b"$10=0\n")
        self.assertEqual(send(g, b"?")[0].split("|")[1], "WPos:-0.000,0.000,0.000")


class CensusRealtime(unittest.TestCase):
    def test_S01_S02_S03_status_pokes(self):
        g = mk()
        r = send(g, b"?\n?\n?\n")
        self.assertEqual([status(r[0])[0], r[1], status(r[2])[0], r[3], status(r[4])[0], r[5]],
                         ["Idle", OK, "Idle", OK, "Idle", OK])

    def test_S04_reset_banner_then_dollar_g(self):
        g = mk()
        self.assertEqual(send(g, b"\x18"), BANNER)
        self.assertEqual(send(g, b"$G\n"), ["[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n", OK])

    def test_S04_reset_restores_parser_defaults_but_keeps_eeprom(self):
        g = mk(instant=True)
        send(g, b"G91M3S10\n")
        send(g, b"G10L2P1X5\n")
        send(g, b"\x18")
        out(g)
        self.assertEqual(send(g, b"$G\n")[0], "[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n")
        self.assertEqual(send(g, b"$#\n")[0], "[G54:5.000,0.000,0.000]\r\n")

    def test_S05_double_newline_reset(self):
        # '\n' '\n' are two empty lines (ok each) before 0x18; reset flushes the RX ring
        g = mk()
        self.assertEqual(send(g, b"\n\n\x18"), [OK, OK] + BANNER)

    def test_S06_feed_hold_newline(self):
        g = mk()
        send(g, b"G1X100F100\n")
        g.tick(1.0)
        self.assertEqual(send(g, b"!\n"), [OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Hold:1")
        g.tick(2.0)
        self.assertEqual(status(send(g, b"?")[0])[0], "Hold:0")

    def test_S07_cycle_start_newline(self):
        g = mk()
        send(g, b"G1X100F100\n")
        g.tick(1.0)
        send(g, b"!")
        g.tick(2.0)
        self.assertEqual(send(g, b"~\n"), [OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Run")

    def test_S08_safety_door(self):
        # serial.c 0x84 -> EXEC_SAFETY_DOOR; protocol.c prints [MSG:Check Door]; no door pin (config.h) so
        # the AJAR flag clears in the suspend loop: Door:0 once retract/de-energise is complete
        g = mk()
        r = send(g, b"?\x84?\n")
        self.assertEqual([status(r[0])[0], r[1], status(r[2])[0]], ["Idle", "[MSG:Check Door]\r\n", "Door:0"])
        self.assertEqual(len(r), 3)                 # the '\n' waits in the RX ring while the suspend loop runs
        self.assertEqual(send(g, b"~"), [OK])       # resume: restore completes, the empty line is finally acked
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")

    def test_S08_door_while_running_holds_first(self):
        g = mk()
        send(g, b"G1X100F100\n")
        g.tick(1.0)
        send(g, b"\x84")
        out(g)
        self.assertEqual(status(send(g, b"?")[0])[0], "Door:2")      # retracting: hold not complete yet
        g.tick(2.0)
        self.assertEqual(status(send(g, b"?")[0])[0], "Door:0")

    def test_S09_jog_cancel(self):
        g = mk()
        send(g, b"$J=G91G21X100F100\n")
        g.tick(1.0)
        r = send(g, b"?\x85?\n")
        self.assertEqual([status(r[0])[0], status(r[1])[0]], ["Jog", "Jog"])   # decelerating
        self.assertEqual(len(r), 2)                 # SUSPEND_JOG_CANCEL keeps the main loop in the suspend loop
        g.tick(2.0)
        self.assertEqual(out(g), [OK])              # ...until the cancel completes; then the empty line is acked
        st = send(g, b"?")[0]
        x = mpos(st)[0]
        self.assertEqual(status(st)[0], "Idle")
        self.assertTrue(1.0 < x < 3.0, x)                 # 1.67 mm travelled + the ramp, nowhere near 100
        self.assertEqual(send(g, b"G91\nG0X1\n"), [OK, OK])   # buffers flushed, parser usable again

    def test_S09_jog_cancel_is_ignored_when_not_jogging(self):
        g = mk()
        r = send(g, b"?\x85?\n")
        self.assertEqual([status(r[0])[0], status(r[1])[0], r[2]], ["Idle", "Idle", OK])

    def _ov(self, seq, expect_ov):
        g = mk()
        send(g, b"?")
        send(g, b"?")                                     # reports 1 and 2 carry WCO / Ov by cadence; skip them
        r = send(g, b"?" + seq + b"?\n")
        self.assertEqual(r[-1], OK)
        f = status(r[1])[1]
        self.assertEqual(f.get("Ov"), expect_ov, r)

    def test_S10_S11_feed_override_normal_and_plus10(self):
        self._ov(b"\x91", "110,100,100")                  # FEEDPLUS10 (coarse +10)
        g = mk()
        send(g, b"\x91?")
        out(g)
        r = send(g, b"\x90?\n")
        self.assertEqual(status(r[0])[1].get("Ov"), "100,100,100")   # FEEDNORMAL back to 100

    def test_S12_feed_minus10(self):
        self._ov(b"\x92", "90,100,100")

    def test_S13_feed_plus1(self):
        self._ov(b"\x93", "101,100,100")

    def test_S14_feed_minus1(self):
        self._ov(b"\x94", "99,100,100")

    def test_S15_spindle_normal_is_noop_at_100(self):
        g = mk()
        send(g, b"?")
        send(g, b"?")
        r = send(g, b"?\x99?\n")
        self.assertNotIn("Ov", status(r[1])[1])           # unchanged value: no immediate Ov refresh

    def test_S16_spindle_plus10(self):
        self._ov(b"\x9a", "100,100,110")

    def test_S17_spindle_minus10(self):
        self._ov(b"\x9b", "100,100,90")

    def test_S18_spindle_plus1(self):
        self._ov(b"\x9c", "100,100,101")

    def test_S19_spindle_minus1(self):
        self._ov(b"\x9d", "100,100,99")

    def test_S20_toggle_spindle_only_acts_in_hold(self):
        g = mk(instant=True)
        send(g, b"M3S500\n")
        r = send(g, b"?\x9e?\n")
        self.assertEqual(r[-1], OK)
        self.assertEqual(status(r[0])[1]["FS"], "0,500")
        self.assertEqual(status(r[1])[1]["FS"], "0,500")   # Idle: stop override ignored (protocol.c STATE_HOLD only)
        send(g, b"!")
        send(g, b"\x9e")
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"], "0,0")   # in Hold the spindle is stopped
        self.assertEqual(send(g, b"\x9e"), ["[MSG:Restoring spindle]\r\n"])    # second toggle restores
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"], "0,500")   # protocol.c suspend loop: spindle_set_state(restore)

    def test_S20_program_end_restores_overrides(self):
        g = mk(instant=True)
        send(g, b"\x91")
        send(g, b"M2\n")                                  # RESTORE_OVERRIDES_AFTER_PROGRAM_END
        send(g, b"?")
        r = send(g, b"?")
        self.assertIn("Ov:100,100,100", r[0])


class CensusDollar(unittest.TestCase):
    def test_S21_dollar_dollar_tracked_with_ok(self):
        r = send(mk(), b"$$\n")
        self.assertEqual(r[0], "$0=10\r\n")
        self.assertEqual(r[-1], OK)
        self.assertEqual(len(r), 35)

    def test_S21_dollar_dollar_blocked_in_run(self):
        g = mk()
        send(g, b"G1X100F100\n")
        g.tick(0.1)
        self.assertEqual(send(g, b"$$\n"), ["error:8\r\n"])         # system.c:140 STATE_CYCLE|STATE_HOLD

    def test_S22_dollar_g(self):
        self.assertEqual(send(mk(), b"$G\n")[-1], OK)

    def test_S22_dollar_g_after_modes(self):
        g = mk(instant=True)
        send(g, b"G91G21G55G18M3M8T2F500S750\n")
        self.assertEqual(send(g, b"$G\n")[0], "[GC:G0 G55 G18 G21 G91 G94 M3 M8 T2 F500 S750]\r\n")

    def test_S22_dollar_g_probe_mode(self):
        g = mk(instant=True)
        g.probe_trigger_z = -1.0
        send(g, b"G38.2Z-5F100\n")
        self.assertEqual(send(g, b"$G\n")[0], "[GC:G38.2 G54 G17 G21 G90 G94 M5 M9 T0 F100 S0]\r\n")

    def test_S23_dollar_i(self):
        self.assertEqual(send(mk(), b"$I\n"), ["[VER:1.1h.20190830:]\r\n", "[OPT:V,15,128]\r\n", OK])

    def test_S23_build_info_write(self):
        g = mk()
        self.assertEqual(send(g, b"$I=Hello World\n"), [OK])        # stored upper-cased, spaces removed
        self.assertEqual(send(g, b"$I\n")[0], "[VER:1.1h.20190830:HELLOWORLD]\r\n")
        self.assertEqual(send(g, b"$IX\n"), ["error:3\r\n"])

    def test_S24_dollar_hash_and_error8_when_running(self):
        g = mk()
        self.assertEqual(len(send(g, b"$#\n")), 12)
        send(g, b"G1X100F100\n")
        g.tick(0.1)
        self.assertEqual(send(g, b"$#\n"), ["error:8\r\n"])           # system.c:173 not Idle/Alarm
        self.assertEqual(send(g, b"$I\n"), ["error:8\r\n"])

    def test_S25_unlock(self):
        g = mk()
        g.inject_alarm(5)
        out(g)
        self.assertEqual(send(g, b"$X\n"), ["[MSG:Caution: Unlocked]\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")
        self.assertEqual(send(g, b"$X\n"), [OK])                      # no-op unless Alarm

    def test_S26_homing_disabled_is_error5(self):
        self.assertEqual(send(mk(), b"$H\n"), ["error:5\r\n"])        # system.c:180 $22=0

    def test_S26_homing_enabled_goes_to_pulloff_and_idle(self):
        # limits.c:371-399: dir mask 0 -> sys_position = lround(-pulloff*steps_per_mm) = -250 steps
        g = mk()
        self.assertEqual(send(g, b"$22=1\n"), [OK])
        self.assertEqual(send(g, b"$H\n"), [OK])
        st = send(g, b"?")[0]
        self.assertEqual((status(st)[0], mpos(st)), ("Idle", [-1.0, -1.0, -1.0]))

    def test_S26_homing_dir_mask_puts_axis_at_far_end(self):
        g = mk()
        send(g, b"$22=1\n$23=1\n$H\n")
        out(g)
        self.assertEqual(mpos(send(g, b"?")[0])[0], -199.0)          # max_travel + pulloff

    def test_S26_power_up_alarm_lock_only_at_power_up(self):
        g = mk()
        send(g, b"$22=1\n")
        self.assertEqual(send(g, b"\x18"), BANNER)                    # main.c HOMING_INIT_LOCK is power-up only

    def test_S27_sleep(self):
        g = mk()
        self.assertEqual(send(g, b"$SLP\n"), [OK, "[MSG:Sleeping]\r\n"])
        self.assertEqual(status(send(g, b"?")[0])[0], "Sleep")
        self.assertEqual(send(g, b"G0X1\n"), [])                      # nothing is processed until reset
        self.assertEqual(send(g, b"\x18"), BANNER + ["[MSG:'$H'|'$X' to unlock]\r\n"])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")

    def test_S27_sleep_rejects_trailing_junk(self):
        self.assertEqual(send(mk(), b"$SLX\n"), ["error:3\r\n"])

    def test_S28_jog_basic_and_variants(self):
        g = mk(instant=True)
        for cmd, pos in ((b"$J=G91 G21 X10.000000 Y5.000000 F500.000000\n", [10.0, 5.0, 0.0]),
                         (b"$J=G91 G21 Y-5.000000 F500.0\n", [10.0, 0.0, 0.0]),
                         (b"$J=G91 G21 Z-2.5 F100.0\n", [10.0, 0.0, -2.5])):
            self.assertEqual(send(g, cmd), [OK])
            self.assertEqual(g.mpos(), pos)

    def test_S28_jog_errors(self):
        g = mk(instant=True)
        self.assertEqual(send(g, b"$J=\n"), ["error:22\r\n"])               # no F -> undefined feed (gcode.c:2-step)
        self.assertEqual(send(g, b"$J=G91X10\n"), ["error:22\r\n"])
        self.assertEqual(send(g, b"$J=G1X10F100\n"), ["error:16\r\n"])      # gcode.c:856-857 invalid jog command
        self.assertEqual(send(g, b"$J=G91X10F100M3\n"), ["error:16\r\n"])
        self.assertEqual(send(g, b"$JG91X1F1\n"), ["error:3\r\n"])          # system.c: line[2] != '='
        self.assertEqual(send(g, b"$J=G91X10F100S5\n"), ["error:36\r\n"])   # S is unused for jogs

    def test_S28_jog_without_axis_words_goes_to_machine_origin(self):
        # gcode.c: with no axis words xyz stays zero (memset), jog_execute plans a move to machine 0,0,0
        g = mk(instant=True)
        send(g, b"G0X5Y5\n")
        self.assertEqual(send(g, b"$J=F100\n"), [OK])
        self.assertEqual(g.mpos(), [0.0, 0.0, 0.0])

    def test_S28_jog_blocks_gcode_with_error9_and_not_in_alarm(self):
        g = mk()
        send(g, b"$J=G91G21X100F100\n")
        g.tick(0.1)
        self.assertEqual(send(g, b"G0X1\n"), ["error:9\r\n"])                # protocol.c:99-101
        self.assertEqual(send(g, b"$J=G91G21X1F100\n"), [OK])               # jogs may queue while jogging
        a = mk()
        a.inject_alarm(5)
        out(a)
        self.assertEqual(send(a, b"$J=G91X1F100\n"), ["error:8\r\n"])       # system.c: only IDLE/JOG

    def test_S28_jog_soft_limit_error15(self):
        g = mk(instant=True)
        send(g, b"$22=1\n$20=1\n")
        self.assertEqual(send(g, b"$J=G53X-300F100\n"), ["error:15\r\n"])   # jog.c:35-37
        self.assertEqual(send(g, b"$J=G53X-100F100\n"), [OK])

    def test_S28_jog_inch_units(self):
        g = mk(instant=True)
        send(g, b"$J=G91G20X1F10\n")
        near(self, g.mpos(), [25.4, 0, 0])

    def test_S28g_gamepad_unformatted_numbers(self):
        g = mk(instant=True)
        self.assertEqual(send(g, b"$J=G91 G21 X-12.5 Y3.2 F2500\n"), [OK])
        near(self, g.mpos(), [-12.5, 3.2, 0.0])

    def test_S29_setting_writes(self):
        g = mk()
        self.assertEqual(send(g, b"$100=300\n"), [OK])
        self.assertIn("$100=300.000\r\n", send(g, b"$$\n"))
        self.assertEqual(send(g, b"$0=2\n"), ["error:6\r\n"])            # settings.c: pulse < 3
        self.assertEqual(send(g, b"$100=-1\n"), ["error:4\r\n"])          # negative
        self.assertEqual(send(g, b"$14=1\n"), ["error:3\r\n"])            # unknown id
        self.assertEqual(send(g, b"$103=1\n"), ["error:3\r\n"])
        self.assertEqual(send(g, b"$256=1\n"), ["error:3\r\n"])           # parameter > 255
        self.assertEqual(send(g, b"$20=1\n"), ["error:10\r\n"])           # soft limits need homing
        self.assertEqual(send(g, b"$100\n"), ["error:3\r\n"])             # no '='
        self.assertEqual(send(g, b"$y=1\n"), ["error:2\r\n"])
        self.assertEqual(send(g, b"$100=1X\n"), ["error:3\r\n"])          # trailing chars after value
        self.assertEqual(send(g, b"$11=0.02\n$12=0.01\n"), [OK, OK])

    def test_S29_setting_write_blocked_while_running(self):
        g = mk()
        send(g, b"G1X100F100\n")
        g.tick(0.1)
        self.assertEqual(send(g, b"$100=300\n"), ["error:8\r\n"])

    def test_S29_restore_backup_replays_every_dump_line(self):
        # plugin __init__:1422 writes every stored id back in id order; all must be accepted
        g = mk()
        for cmd, _ in self.NON_DEFAULT_WRITES:
            self.assertEqual(send(g, cmd), [OK], cmd)
        dump = [l for l in send(g, b"$$\n") if l.startswith("$")]
        send(g, b"$RST=$\n")                                # back to defaults, so the replay has real work to do
        g = mk()
        self.assertNotEqual([l for l in send(g, b"$$\n") if l.startswith("$")], dump)
        for line in dump:
            self.assertEqual(send(g, line.strip().encode() + b"\n"), [OK], line)
        self.assertEqual([l for l in send(g, b"$$\n") if l.startswith("$")], dump)

    # Every non-axis write path of settings.c:settings_store_global_setting plus the axis ones, with the value
    # report.c:report_grbl_settings prints back ($0-$3 $10 $13 $26 integers, $30/$31 rpm with no decimals, the
    # rest 3 decimals).
    NON_DEFAULT_WRITES = [
        (b"$0=7\n", "$0=7"), (b"$1=100\n", "$1=100"), (b"$2=5\n", "$2=5"), (b"$3=3\n", "$3=3"),
        (b"$4=1\n", "$4=1"), (b"$5=1\n", "$5=1"), (b"$6=1\n", "$6=1"), (b"$10=3\n", "$10=3"),
        (b"$11=0.02\n", "$11=0.020"), (b"$12=0.01\n", "$12=0.010"), (b"$13=1\n", "$13=1"),
        (b"$22=1\n", "$22=1"), (b"$23=3\n", "$23=3"), (b"$24=50\n", "$24=50.000"),
        (b"$25=1000\n", "$25=1000.000"), (b"$26=100\n", "$26=100"), (b"$27=2\n", "$27=2.000"),
        (b"$30=5000\n", "$30=5000"), (b"$31=100\n", "$31=100"), (b"$32=1\n", "$32=1"),
        (b"$110=123\n", "$110=123.000"), (b"$111=124\n", "$111=124.000"), (b"$112=125\n", "$112=125.000"),
    ]

    def test_S29_every_setting_write_takes_effect_and_flags_clear(self):
        g = mk()
        for cmd, _ in self.NON_DEFAULT_WRITES:
            self.assertEqual(send(g, cmd), [OK], cmd)
        dump = send(g, b"$$\n")
        for cmd, line in self.NON_DEFAULT_WRITES:
            self.assertIn(line + "\r\n", dump, cmd)
        for cmd in (b"$4=0\n", b"$5=0\n", b"$6=0\n", b"$13=0\n", b"$22=0\n", b"$32=0\n"):   # flag clearing
            self.assertEqual(send(g, cmd), [OK], cmd)
            self.assertIn(cmd[:-3].decode() + "=0\r\n", send(g, b"$$\n"), cmd)

    def test_S29_rpm_and_max_rate_writes_change_behaviour(self):
        g = mk()
        send(g, b"$30=500\n")                               # spindle_init(): rpm_max recalibrates the PWM scale
        send(g, b"M3S1000\n")
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"], "0,500")
        g = mk()
        send(g, b"$110=300\n")                              # x max rate caps the feed
        send(g, b"G1X100F600\n")
        g.tick(0.5)
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"], "300,0")

    def test_S29_setting_side_effects(self):
        g = mk()
        send(g, b"$22=1\n$20=1\n")
        self.assertIn("$20=1\r\n", send(g, b"$$\n"))
        send(g, b"$22=0\n")                      # disabling homing force-disables soft limits (settings.c case 22)
        r = send(g, b"$$\n")
        self.assertIn("$20=0\r\n", r)
        send(g, b"$130=100\n$120=20\n")
        r = send(g, b"$$\n")
        self.assertIn("$130=100.000\r\n", r)
        self.assertIn("$120=20.000\r\n", r)      # stored x3600, printed /3600

    def test_S30_status_mask_toggle(self):
        g = mk()
        self.assertEqual(send(g, b"$10=1\n"), [OK])
        r = send(g, b"?")[0]
        self.assertEqual([k for k in status(r)[1] if k in ("MPos", "WPos", "Bf")], ["MPos"])   # report.c: mask bit 0 = MPos
        self.assertEqual(send(g, b"$10=0\n"), [OK])
        r = send(g, b"?")[0]
        self.assertEqual([k for k in status(r)[1] if k in ("MPos", "WPos", "Bf")], ["WPos"])
        self.assertEqual(send(g, b"$10=3\n"), [OK])
        r = send(g, b"?")[0]
        self.assertEqual([k for k in status(r)[1] if k in ("MPos", "WPos", "Bf")], ["MPos", "Bf"])  # bit 1 = buffer state
        self.assertRegex(status(r)[1]["Bf"], r"^15,\d+$")        # BLOCK_BUFFER_SIZE-1 free planner blocks

    def test_S31_dollar_s(self):
        self.assertEqual(send(mk(), b"$S\n"), ["error:3\r\n"])           # system.c case 'S': not $SLP

    def test_S32_dollar_cd(self):
        self.assertEqual(send(mk(), b"$CD\n"), ["error:3\r\n"])          # case 'C' with line[2] != 0

    def test_S33_dollar_cd_file(self):
        self.assertEqual(send(mk(), b"$CD=Config/Filename\n"), ["error:3\r\n"])

    def test_S34_dollar_bye(self):
        self.assertEqual(send(mk(), b"$Bye\n"), ["error:2\r\n"])         # read_float finds no digits

    def test_S35_localfs_delete(self):
        self.assertEqual(send(mk(), b"$LocalFS/Delete=Config/Filename\n"), ["error:2\r\n"])   # '/' is block-delete

    def test_S36_config_path_set(self):
        self.assertEqual(send(mk(), b"$/axes/x/max_travel_mm=300\n"), ["error:2\r\n"])

    def test_S37_fluid_setting_write(self):
        # "$HOSTNAME=FOO" is parsed as the homing command: disabled -> error:5 (system.c:180)
        self.assertEqual(send(mk(), b"$Hostname=foo\n"), ["error:5\r\n"])
        self.assertEqual(send(mk(), b"$Report/Status=1\n"), ["error:3\r\n"])      # case 'R'

    def test_S38_report_interval(self):
        self.assertEqual(send(mk(), b"$Report/Interval=1000\n"), ["error:3\r\n"])
        self.assertEqual(send(mk(), b"$Report/Interval=0\n"), ["error:3\r\n"])

    def test_S39_smoothie_cat(self):
        self.assertEqual(send(mk(), b"Cat /sd/config\n"), ["error:2\r\n"])        # letter C then no number

    def test_S40_dollar_plus(self):
        self.assertEqual(send(mk(), b"$+\n"), ["error:2\r\n"])

    def test_dollar_n_startup_lines_store_print_and_run_at_reset(self):
        g = mk(instant=True)
        self.assertEqual(send(g, b"$N0=G20G91\n"), [OK])
        self.assertEqual(send(g, b"$N\n"), ["$N0=G20G91\r\n", "$N1=\r\n", OK])
        self.assertEqual(send(g, b"$N1=G1\n"), ["error:22\r\n"])                  # validated by running it
        self.assertEqual(send(g, b"\x18"), BANNER + [">G20G91:ok\r\n"])
        self.assertEqual(send(g, b"$G\n")[0], "[GC:G0 G54 G17 G20 G91 G94 M5 M9 T0 F0 S0]\r\n")

    def test_dollar_n_not_stored_unless_idle(self):
        g = mk()
        g.inject_alarm(5)
        out(g)
        self.assertEqual(send(g, b"$N0=G54\n"), ["error:8\r\n"])

    def test_restore_defaults(self):
        g = mk(instant=True)
        send(g, b"$100=300\nG10L2P1X5\n$N0=G54\n")
        msg = ["[MSG:Restoring defaults]\r\n", OK]
        # settings.c:settings_restore: '$' keeps parameters and startup lines, so $N0 still runs at the reset
        self.assertEqual(send(g, b"$RST=$\n"), msg + BANNER + [">G54:ok\r\n"])
        self.assertIn("$100=250.000\r\n", send(g, b"$$\n"))
        self.assertEqual(send(g, b"$#\n")[0], "[G54:5.000,0.000,0.000]\r\n")
        self.assertEqual(send(g, b"$RST=#\n"), msg + BANNER + [">G54:ok\r\n"])
        self.assertEqual(send(g, b"$#\n")[0], "[G54:0.000,0.000,0.000]\r\n")
        self.assertEqual(send(g, b"$RST=*\n"), msg + BANNER)               # '*' also wipes the startup lines
        self.assertEqual(send(g, b"$N\n"), ["$N0=\r\n", "$N1=\r\n", OK])
        self.assertEqual(send(g, b"$RST=X\n"), ["error:3\r\n"])

    def test_check_mode(self):
        g = mk()
        self.assertEqual(send(g, b"$C\n"), ["[MSG:Enabled]\r\n", OK])
        self.assertEqual(send(g, b"G1X100F100\n"), [OK])
        self.assertEqual(g.mpos(), [0.0, 0.0, 0.0])                       # mc_line: no motion in check mode
        self.assertEqual(send(g, b"G1X1X2F1\n"), ["error:25\r\n"])         # still error-checked
        self.assertEqual(send(g, b"$C\n"), ["[MSG:Disabled]\r\n", OK] + BANNER)
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")

    def test_check_mode_needs_idle(self):
        g = mk()
        g.inject_alarm(5)
        out(g)
        self.assertEqual(send(g, b"$C\n"), ["error:8\r\n"])

    def test_dollar_x_g_c_trailing_chars(self):
        g = mk()
        self.assertEqual(send(g, b"$XX\n"), ["error:3\r\n"])
        self.assertEqual(send(g, b"$GX\n"), ["error:3\r\n"])
        self.assertEqual(send(g, b"$$$\n"), ["error:3\r\n"])


class CensusGcode(unittest.TestCase):
    def setUp(self):
        self.g = mk(instant=True)

    def sg(self, text):
        return send(self.g, text + "\n")

    def modes(self):
        return self.sg("$G")[0]

    def test_S41_m5(self):
        self.assertEqual(self.sg("M5"), [OK])

    def test_S42_pause_retract_and_plunge(self):
        self.assertEqual(self.sg("G91 G0 Z5"), [OK])
        self.assertEqual(self.g.mpos()[2], 5.0)
        self.assertEqual(self.sg("G91 G0 Z-5"), [OK])
        self.assertEqual(self.g.mpos()[2], 0.0)

    def test_S42_dwell_p10_blocks_ten_seconds(self):
        g = mk()
        self.assertEqual(send(g, b"G4 P10\n"), [])
        g.tick(9.9)
        self.assertEqual(out(g), [])
        g.tick(0.1)
        self.assertEqual(out(g), [OK])      # delay_sec: ceil(1000/50*10) = 200 slices of 50 ms

    def test_S43_restore_modals_on_resume_one_word_per_line(self):
        for line in ("M3", "S500", "F500", "G91", "G90", "M4", "M5"):
            self.assertEqual(self.sg(line), [OK], line)
        self.assertEqual(self.sg("G91"), [OK])
        self.assertEqual(self.sg("M3"), [OK])
        self.assertEqual(self.sg("S250"), [OK])
        self.assertEqual(self.sg("F125"), [OK])
        self.assertEqual(self.modes(), "[GC:G0 G54 G17 G21 G91 G94 M3 M9 T0 F125 S250]\r\n")

    def test_S44_goto_origin_forms(self):
        self.assertEqual(self.sg("G0 G90 X10.000000 Y20.000000 Z-3.000000"), [OK])
        self.assertEqual(self.g.mpos(), [10.0, 20.0, -3.0])
        self.assertEqual(self.sg("G0 G90 X1.5"), [OK])
        self.assertEqual(self.sg("G0 G90 Z0"), [OK])
        self.assertEqual(self.g.mpos(), [1.5, 20.0, 0.0])

    def test_S45_set_work_origin_at_current_position(self):
        self.sg("G0 G90 X10 Y20 Z-3")
        self.assertEqual(self.sg("G91 G10 P1 L20 X0 Y0 Z0"), [OK])       # WCS = MPos - G92 - TLO - WPos
        self.assertEqual(self.sg("$#")[0], "[G54:10.000,20.000,-3.000]\r\n")
        self.assertIn("G91", self.modes())                                # distance mode left at G91
        self.assertEqual(self.sg("G10 P2 L20 X5"), [OK])               # P2 = G55; only X is rewritten
        self.assertEqual(self.sg("$#")[1], "[G55:5.000,0.000,0.000]\r\n")

    def test_S45_g10_l20_uses_current_position(self):
        self.sg("G0X10")
        self.sg("G10L20P1X2")                 # WCS_x = 10 - 0 - 2 = 8
        self.assertEqual(self.sg("$#")[0], "[G54:8.000,0.000,0.000]\r\n")
        self.assertEqual(self.sg("G10L20P0X0")[0], OK)       # P0 = active coordinate system
        self.assertEqual(self.sg("$#")[0], "[G54:10.000,0.000,0.000]\r\n")

    def test_S46_g1_with_only_f(self):
        self.assertEqual(self.sg("G1 F500"), [OK])
        self.assertEqual(self.modes(), "[GC:G1 G54 G17 G21 G90 G94 M5 M9 T0 F500 S0]\r\n")

    def test_S47_weak_laser_m3_s(self):
        self.assertEqual(self.sg("$32=1"), [OK])
        self.assertEqual(self.sg("M3 S10"), [OK])
        self.assertEqual(self.modes(), "[GC:G0 G54 G17 G21 G90 G94 M3 M9 T0 F0 S10]\r\n")

    def test_S48_framing_end_one_block(self):
        self.sg("M3 S100")
        self.assertEqual(self.sg("M5 S0 G0"), [OK])
        self.assertEqual(self.modes(), "[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]\r\n")

    def test_S49_weak_laser_toggle(self):
        self.assertEqual(self.sg("G1 F500 M3 S12.50"), [OK])
        self.assertEqual(self.modes(), "[GC:G1 G54 G17 G21 G90 G94 M3 M9 T0 F500 S13]\r\n")   # S: 0 decimals
        for line in ("M3 S0", "M5", "G0"):
            self.assertEqual(self.sg(line), [OK], line)

    def test_S50_feed_and_power_alone(self):
        self.assertEqual(self.sg("F750.000"), [OK])
        self.assertEqual(self.sg("S12.500"), [OK])
        self.assertIn(" F750 S13]", self.modes())

    def test_S51_coordinate_system_select_then_status(self):
        for n in range(54, 60):
            r = send(self.g, ("G%d\n?" % n).encode())
            self.assertEqual(r[0], OK)
            self.assertEqual(status(r[1])[0], "Idle")
            self.assertIn("G%d" % n, self.modes())

    def test_S52_distance_mode(self):
        self.sg("G91")
        self.assertIn(" G91 ", self.modes())
        self.sg("G90")
        self.assertIn(" G90 ", self.modes())

    def test_S53_coolant(self):
        self.assertEqual(self.sg("M8"), [OK])
        self.assertIn(" M8 ", self.modes())
        self.assertEqual(self.sg("M9"), [OK])
        self.assertIn(" M9 ", self.modes())
        self.assertEqual(self.sg("M7"), ["error:20\r\n"])        # gcode.c:263 ENABLE_M7 undefined

    def test_S54_xy_edge_probe_success(self):
        self.g.probe_trigger = {"X": -10.0}
        self.assertEqual(self.sg("G21"), [OK])
        self.assertEqual(self.sg("G0 G91 X-2.0 F500.0"), [OK])
        self.assertEqual(self.sg("G38.2 X-20.0 F200"), ["[PRB:-10.000,0.000,0.000:1]\r\n", OK])

    def test_S55_z_probe_forms(self):
        self.g.probe_trigger_z = -5.0
        self.assertEqual(self.sg("G91 G21 G38.2 Z-200.0 F100"), ["[PRB:0.000,0.000,-5.000:1]\r\n", OK])

    def test_S56_store_probed_origin_and_retreat(self):
        for line in ("G10 P1 L2 X-10.000000", "G0 X-10 Z15 F500", "G0 G90 X10 F500", "G91"):
            self.assertEqual(self.sg(line), [OK], line)
        self.assertEqual(self.sg("$#")[0], "[G54:-10.000,0.000,0.000]\r\n")

    def test_S57_after_xy_probe(self):
        self.assertEqual(self.sg("G0 G90 X0 Y0 F500"), [OK])
        self.assertEqual(self.sg("G91"), [OK])
        self.assertEqual(self.sg("G0 G90 X0Y0 F500"), [OK])

    def test_S58_after_simple_z_probe(self):
        for line in ("G91", "G21", "G10 P1 L2 Z-4.250000", "G0 Z5.0"):
            self.assertEqual(self.sg(line), [OK], line)
        self.assertEqual(self.sg("$#")[0], "[G54:0.000,0.000,-4.250]\r\n")

    def test_S59_multipoint_z_origin(self):
        self.assertEqual(self.sg("G10 P1 L2 Z-12.345000"), [OK])
        self.assertEqual(self.sg("$#")[0], "[G54:0.000,0.000,-12.345]\r\n")

    def test_S60_retract_after_each_multipoint_probe(self):
        self.assertEqual(self.sg("G0 G91 G21 Z5.0 F200.0"), [OK])
        self.assertEqual(self.sg("$J=G91 G21 Z5.0 F200.0"), [OK])
        self.assertEqual(self.g.mpos()[2], 10.0)

    def test_S61_baby_step_offset(self):
        self.assertEqual(self.sg("G10 L2 P1 X1.0"), [OK])
        self.assertEqual(self.sg("G10 L2 P1 Z-0.1"), [OK])
        self.assertEqual(self.sg("$#")[0], "[G54:1.000,0.000,-0.100]\r\n")

    def test_S62_wait_for_motion_dwell(self):
        g = mk()
        send(g, b"G1X1F300\n")                  # 1 mm at 5 mm/s = 0.2 s
        self.assertEqual(send(g, b"G4 P0.001\n"), [])
        g.tick(0.1)
        self.assertEqual(out(g), [])            # motion still running
        g.tick(0.1)
        self.assertEqual(out(g), [])            # motion done at t=0.2, one 50 ms dwell slice just begun
        g.tick(0.049)
        self.assertEqual(out(g), [])
        g.tick(0.002)
        self.assertEqual(out(g), [OK])


class Translations(unittest.TestCase):
    """T-rows: strings OctoPrint-side hooks rewrite into; the firmware reply to the rewritten form."""

    def test_T01_T08_m105_m114_become_status(self):
        self.assertEqual(status(send(mk(), b"?\n")[0])[0], "Idle")

    def test_T02_m110_becomes_double_newline_reset(self):
        self.assertEqual(send(mk(), b"\n\n\x18"), [OK, OK] + BANNER)

    def test_T05_percent_lines_are_error1_which_is_why_the_plugin_suppresses_them(self):
        # protocol.c: '%' is NOT stripped ("TODO: Install '%' feature"); gcode.c:285 letter < 'A' -> error:1
        g = mk()
        self.assertEqual(send(g, b"%\n"), ["error:1\r\n"])
        self.assertEqual(send(g, b"% Side To Side\n"), ["error:1\r\n"])

    def test_T06_m115_becomes_settings_dump(self):
        self.assertEqual(send(mk(), b"$$\n")[-1], OK)

    def test_T07_m400_becomes_g4_p0001(self):
        g = mk(instant=True)
        self.assertEqual(send(g, b"G4 P0.001\n"), [OK])

    def test_T09_m999_becomes_reset_then_dollar_g(self):
        g = mk()
        self.assertEqual(send(g, b"\x18"), BANNER)
        self.assertEqual(send(g, b"$G\n")[-1], OK)

    def test_T10_m290_becomes_g10_l2(self):
        g = mk(instant=True)
        self.assertEqual(send(g, b"G10 L2 P1 Z-0.05\n"), [OK])

    def test_T12_override_bytes_are_swallowed_with_status(self):
        g = mk()
        for b in (b"\x90", b"\x91", b"\x92", b"\x93", b"\x94", b"\x99", b"\x9a", b"\x9b", b"\x9c", b"\x9d", b"\x9e"):
            r = send(g, b"?" + b + b"?\n")
            self.assertEqual(len(r), 3, b)
            self.assertEqual(r[2], OK)

    def test_T13_dollar_h_and_g38_2_passthrough(self):
        g = mk(instant=True)
        self.assertEqual(send(g, b"$H\n"), ["error:5\r\n"])
        g.probe_trigger_z = -1
        self.assertEqual(send(g, b"G38.2 Z-5 F100\n"), ["[PRB:0.000,0.000,-1.000:1]\r\n", OK])

    def test_T14_bare_axis_words_use_the_modal_motion(self):
        g = mk(instant=True)
        send(g, b"G1 F100\n")
        self.assertEqual(send(g, b"X10 Y5\n"), [OK])
        self.assertEqual(g.mpos(), [10.0, 5.0, 0.0])
        send(g, b"G0\n")
        self.assertEqual(send(g, b"X1\n"), [OK])

    def test_T15_scaled_feed_and_power_numbers(self):
        g = mk(instant=True)
        self.assertEqual(send(g, b"G1 X1 F625.000 S312.500\n"), [OK])
        self.assertIn(" F625 S313]", send(g, b"$G\n")[0])

    def test_T16_T17_tracked_commands_reply_shape(self):
        g = mk()
        for cmd in (b"$$\n", b"$I\n", b"$G\n", b"$#\n"):
            r = send(g, cmd)
            self.assertEqual(r[-1], OK)
            self.assertTrue(all(x.startswith(("$", "[")) for x in r[:-1]))


class Errors(unittest.TestCase):
    def setUp(self):
        self.g = mk(instant=True)

    def e(self, text, code):
        self.assertEqual(send(self.g, text + "\n"), ["error:%d\r\n" % code], text)

    def test_unsupported_commands_error20(self):
        for t in ("G5", "G12", "G41", "G42", "G64", "G98", "G92.2", "G92.3", "G28.2", "G38.1", "G38.6", "M6", "M7",
                  "M56", "G43", "G10L2P1X1R5", "G10 P1 X1", "G61.1", "G90.1", "X1A5", "G0B1", "C5", "M48"):
            self.e(t, 20)

    def test_modal_group_violations_error21(self):
        for t in ("G1 G80", "M3 M5", "G17 G18", "G90 G91", "G20 G21", "G54 G55", "M0 M2", "G4 G53",
                  "G93 G94", "M8 M9", "G40 G40", "G61 G61"):
            self.e(t, 21)

    def test_axis_command_conflict_error24(self):
        self.e("G10 L2 P1 X1 G1 X2", 24)
        self.e("G0 G92 X1", 24)
        self.e("G43.1 Z1 G0", 24)

    def test_bad_number_and_letter_errors(self):
        self.e("G1 X", 2)
        self.e("G1 X- F10", 2)
        self.e("G", 2)
        self.e("G1 X1.2.3 F10", 1)
        self.e("G1 X1 =", 1)
        self.e("1", 1)
        self.e("G1 X1_ F10", 1)

    def test_value_errors(self):
        self.e("G1 X1 X2 F10", 25)       # word repeated
        self.e("G1 X1 F-5", 4)           # negative F
        self.e("S-5", 4)
        self.e("G4 P-1", 4)
        self.e("T256", 38)
        self.e("N20000000 G0", 27)
        self.e("G4", 28)                 # P missing
        self.e("G1 X5", 22)              # no feed rate ever set
        self.e("G38.2 X5", 22)
        self.e("G0 P5", 36)              # unused P
        self.e("G1 X1 F10 I5", 36)       # unused I
        self.e("G1 F10 L5", 36)
        self.e("G80 X1", 31)
        self.e("G10 L2 P1", 26)          # no axis words
        self.e("G92", 26)
        self.e("G10 L2 P7 X1", 29)
        self.e("G53 G2 X1 F10 I1", 30)
        self.e("G43.1 X1", 37)
        self.e("G1.5 X1", 23)
        self.e("M3.5", 23)
        self.e("G10.1 L2 P1 X1", 23)

    def test_g38_and_arc_errors(self):
        send(self.g, b"G1F100\n")
        self.e("G38.2", 26)
        self.e("G38.2 X0", 33)                      # target == current position
        self.e("G2 F100", 26)
        self.e("G2 Z5 I1", 32)                      # no axis word in the XY plane
        self.e("G2 X10 Y0", 35)                     # neither R nor IJ offsets
        self.e("G2 X10 Y0 R3", 34)                  # 4r^2 - d^2 < 0
        self.e("G2 X0 Y0 R5", 33)                   # R mode target == current
        self.e("G2 X10 Y0 I4 J0", 33)               # radius mismatch 1 mm > 0.5
        self.e("G2 X10 Y0 I5 J0 R5", 36)            # R given: IJ words unused
        self.e("G17 G3 X10.01 Y0 I5 J0", 33)        # 0.01 mm mismatch > 0.005 mm and > 0.1% of r

    def test_arcs_accepted(self):
        self.assertEqual(send(self.g, b"G1F100\nG3 X0.004 Y0 I0.002 J0\n"), [OK, OK])   # 0.002 mm mismatch is tolerated
        send(self.g, b"G0X0\n")
        self.assertEqual(send(self.g, b"G2 X10 Y0 I5 J0\n"), [OK])
        self.assertEqual(self.g.mpos()[:2], [10.0, 0.0])
        self.assertEqual(send(self.g, b"G3 X0 Y0 R5\n"), [OK])                 # half circle via R
        self.assertEqual(send(self.g, b"G2 X0 Y0 I-5\n"), [OK])                # full circle
        self.assertEqual(send(self.g, b"G18 G2 X5 Z5 I2.5 K2.5\n"), [OK])      # other planes
        self.assertEqual(send(self.g, b"G19 G3 Y5 Z0 J2.5 K-2.5\n"), [OK])

    def test_overflow_line_error11(self):
        self.assertEqual(send(self.g, ("X" + "0" * 78 + "\n").encode()), [OK])   # 79 chars still fit
        self.assertEqual(send(self.g, ("X" + "0" * 79 + "\n").encode()), ["error:11\r\n"])   # protocol.c:LINE_BUFFER_SIZE 80
        self.assertEqual(send(self.g, b"G0X1\n"), [OK])                          # flag cleared for the next line
        self.assertEqual(send(self.g, (" " * 200 + "G0X1\n").encode()), [OK])     # whitespace does not count
        self.assertEqual(send(self.g, ("X0 (" + "c" * 200 + ")\n").encode()), [OK])

    def test_gcode_in_alarm_error9(self):
        self.g.inject_alarm(5)
        out(self.g)
        for t in ("G0X1", "M5", "G4P1"):
            self.e(t, 9)
        self.assertEqual(send(self.g, b"$G\n")[-1], OK)         # '$' commands still run

    def test_g_code_blocked_in_alarm_after_probe_fail(self):
        g = mk(instant=True)
        send(g, b"G38.2Z-5F100\n")
        self.assertEqual(send(g, b"G0X1\n"), ["error:9\r\n"])

    def test_setting_write_out_of_range(self):
        self.e("$0=1", 6)
        self.e("$100=-5", 4)
        self.e("$20=1", 10)

    def test_homing_disabled(self):
        self.e("$H", 5)

    def test_error13_is_unreachable_without_a_door_pin(self):
        import grbl_virtual
        self.assertEqual(grbl_virtual.STATUS_CHECK_DOOR, 13)                 # report.h:37
        self.g.inject_alarm(5)                                               # 1 and 2 are critical: no `$X` until reset
        out(self.g)
        # system.c:181 blocks `$X` with error:13 only when system_check_safety_door_ajar(); with no door pin
        # it never is, so the unlock goes through and the alarm clears.
        self.assertEqual(send(self.g, b"$X\n"), ["[MSG:Caution: Unlocked]\r\n", OK])
        self.assertEqual(status(send(self.g, b"?")[0])[0], "Idle")

    def test_unit_conversion_and_modes(self):
        self.assertEqual(send(self.g, b"G20 G1 X1 F10\n"), [OK])
        near(self, self.g.mpos(), [25.4, 0, 0])
        self.assertEqual(send(self.g, b"G21 G91 X-0.4\n"), [OK])
        near(self, self.g.mpos(), [25.0, 0, 0])

    def test_inverse_time_feed(self):
        send(self.g, b"G93\n")
        self.e("G1 X1", 22)                       # F missing in G93
        self.assertEqual(send(self.g, b"G1 X1 F2\n"), [OK])
        self.assertIn(" G93 ", send(self.g, b"$G\n")[0])

    def test_g53_g28_g30_g92_g43(self):
        g = self.g
        self.assertEqual(send(g, b"G0X5Y5\nG28.1\nG0X0Y0\nG28\n"), [OK] * 4)
        self.assertEqual(g.mpos()[:2], [5.0, 5.0])
        self.assertEqual(send(g, b"G30.1\n$#\n")[0], OK)
        self.assertEqual(send(g, b"G53 G0 X-1\n"), [OK])
        self.assertEqual(g.mpos()[0], -1.0)
        send(g, b"G92 X0\n")
        self.assertEqual(send(g, b"$#\n")[8], "[G92:-1.000,0.000,0.000]\r\n")
        send(g, b"G92.1\n")
        self.assertEqual(send(g, b"$#\n")[8], "[G92:0.000,0.000,0.000]\r\n")
        send(g, b"G43.1 Z2.5\n")
        self.assertEqual(send(g, b"$#\n")[9], "[TLO:2.500]\r\n")
        send(g, b"G49\n")
        self.assertEqual(send(g, b"$#\n")[9], "[TLO:0.000]\r\n")

    def test_program_end_and_pause(self):
        g = self.g
        send(g, b"G91 G20 M3 M8 S100\n")
        self.assertEqual(send(g, b"M2\n"), ["[MSG:Pgm End]\r\n", OK])
        self.assertEqual(send(g, b"$G\n")[0], "[GC:G1 G54 G17 G20 G90 G94 M5 M9 T0 F0 S100]\r\n")
        self.assertEqual(send(g, b"M30\n"), ["[MSG:Pgm End]\r\n", OK])
        self.assertEqual(send(g, b"M0\n"), [])
        self.assertEqual(status(send(g, b"?")[0])[0], "Hold:0")
        self.assertEqual(send(g, b"~"), [OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")

    def test_status_while_blocked_inside_gcode_line(self):
        g = mk()
        send(g, b"G4P1\n")
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")
        g.tick(1.0)
        self.assertEqual(out(g), [OK])


class Probing(unittest.TestCase):
    def test_success_with_trigger_set_instant(self):
        g = mk(instant=True)
        g.probe_trigger_z = -5.0
        self.assertEqual(send(g, b"G38.2 Z-20 F100\n"), ["[PRB:0.000,0.000,-5.000:1]\r\n", OK])
        self.assertEqual(send(g, b"$#\n")[10], "[PRB:0.000,0.000,-5.000:1]\r\n")

    def test_success_timed(self):
        g = mk()
        g.probe_trigger_z = -5.0
        self.assertEqual(send(g, b"G38.2 Z-20 F100\n"), [])
        g.tick(2.9)
        self.assertEqual(out(g), [])                       # contact needs 3.0 s at 100 mm/min
        self.assertEqual(status(send(g, b"?")[0])[0], "Run")
        r = tick_until(g, OK)
        self.assertEqual(r, ["[PRB:0.000,0.000,-5.000:1]\r\n", OK])
        z = mpos(send(g, b"?")[0])[2]
        self.assertTrue(-5.2 < z < -5.0, z)               # decelerates past the contact point
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")

    def test_failure_sequence_alarm5_prb0_ok(self):
        # motion_control.c:296-314: ALARM:5, then report_probe_parameters with probe_succeeded=0, then ok
        g = mk(instant=True)
        self.assertEqual(send(g, b"G38.2 Z-20 F100\n"),
                         ["ALARM:5\r\n", "[PRB:0.000,0.000,0.000:0]\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")
        self.assertEqual(g.mpos()[2], -20.0)

    def test_failure_sequence_timed(self):
        g = mk()
        send(g, b"G38.2 Z-3 F100\n")
        r = tick_until(g, OK)
        self.assertEqual(r, ["ALARM:5\r\n", "[PRB:0.000,0.000,0.000:0]\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")

    def test_prb_failure_reports_the_last_successful_probe_position(self):
        g = mk(instant=True)
        g.probe_trigger_z = -2.0
        send(g, b"G38.2 Z-10 F100\n")
        g.probe_trigger = None
        send(g, b"$X\nG38.2 Z-20 F100\n")
        self.assertEqual(send(g, b"$X\n$#\n")[12], "[PRB:0.000,0.000,-2.000:0]\r\n")

    def test_already_tripped_is_alarm4(self):
        g = mk(instant=True)
        g.probe_tripped = True
        self.assertEqual(send(g, b"G38.2 Z-5 F100\n"), ["ALARM:4\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")

    def test_g38_3_no_error_variant(self):
        g = mk(instant=True)
        self.assertEqual(send(g, b"G38.3 Z-5 F100\n"), ["[PRB:0.000,0.000,-5.000:0]\r\n", OK])
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")
        g.probe_trigger_z = -7.0
        self.assertEqual(send(g, b"G38.3 Z-10 F100\n"), ["[PRB:0.000,0.000,-7.000:1]\r\n", OK])

    def test_trigger_dict_x_axis_and_generic(self):
        g = mk(instant=True)
        g.probe_trigger = {"X": 4.0, "Z": -3.0}
        self.assertEqual(send(g, b"G38.2 X10 F100\n"), ["[PRB:4.000,0.000,0.000:1]\r\n", OK])
        self.assertEqual(g.probe_trigger_z, -3.0)
        g.probe_trigger_z = None
        self.assertEqual(g.probe_trigger, {"X": 4.0})

    def test_probe_after_probe_needs_clearing_the_plane(self):
        g = mk(instant=True)
        g.probe_trigger_z = -5.0
        send(g, b"G38.2 Z-20 F100\n")
        out(g)
        # still sitting on the plane: pin reads triggered -> ALARM:4
        self.assertEqual(send(g, b"G38.2 Z-20 F100\n"), ["ALARM:4\r\n", OK])

    def test_probe_in_check_mode(self):
        g = mk(instant=True)
        send(g, b"$C\n")
        self.assertEqual(send(g, b"G38.2 Z-5 F100\n"), [OK])

    def test_probe_blocks_follow_up_lines_until_done(self):
        g = mk()
        g.probe_trigger_z = -2.0
        send(g, b"G38.2 Z-5 F300\nG4P0.01\n")
        r = tick_until(g, OK)
        self.assertEqual(r[:2], ["[PRB:0.000,0.000,-2.000:1]\r\n", OK])


class Alarms(unittest.TestCase):
    def test_reset_in_motion_is_alarm3(self):
        # motion_control.c:mc_reset: abrupt stop -> ALARM:3 then re-init in Alarm (protocol.c:52-54 lock message)
        g = mk()
        send(g, b"G1X100F100\n")
        g.tick(1.0)
        self.assertEqual(send(g, b"\x18"), ["ALARM:3\r\n"] + BANNER + ["[MSG:'$H'|'$X' to unlock]\r\n"])
        self.assertEqual(status(send(g, b"?")[0])[0], "Alarm")
        self.assertEqual(send(g, b"G0X1\n"), ["error:9\r\n"])
        self.assertEqual(send(g, b"$X\n"), ["[MSG:Caution: Unlocked]\r\n", OK])

    def test_reset_during_hold_complete_is_clean(self):
        g = mk()
        send(g, b"G1X100F100\n")
        g.tick(1.0)
        send(g, b"!")
        g.tick(2.0)
        self.assertEqual(send(g, b"\x18"), BANNER)

    def test_reset_while_decelerating_is_alarm3(self):
        g = mk()
        send(g, b"G1X100F500\n")
        g.tick(1.0)
        send(g, b"!")
        self.assertEqual(send(g, b"\x18")[0], "ALARM:3\r\n")

    def test_inject_alarm_message(self):
        g = mk()
        for code in (3, 4, 5, 9, 15):
            g2 = mk()
            g2.inject_alarm(code)
            self.assertEqual(out(g2), ["ALARM:%d\r\n" % code])

    def test_hard_limit_is_a_critical_event_until_reset(self):
        # protocol.c:218-238: ALARM:1, [MSG:Reset to continue], then nothing but reset is serviced
        g = mk()
        g.inject_alarm(1)
        self.assertEqual(out(g), ["ALARM:1\r\n", "[MSG:Reset to continue]\r\n"])
        self.assertEqual(send(g, b"?\n$G\n"), [])                 # not even status reports
        self.assertEqual(send(g, b"\x18"), BANNER + ["[MSG:'$H'|'$X' to unlock]\r\n"])

    def test_soft_limit_alarm_via_travel_check(self):
        g = mk(instant=True)
        send(g, b"$22=1\n$20=1\n$X\n")
        out(g)
        r = send(g, b"G0X-300\n")
        self.assertEqual(r, ["ALARM:2\r\n", "[MSG:Reset to continue]\r\n"])
        # protocol.c: gc_execute_line returns after limits_soft_check, so the line's "ok" is still printed
        # before the main loop notices sys.abort and re-initialises
        self.assertEqual(send(g, b"\x18"), [OK] + BANNER + ["[MSG:'$H'|'$X' to unlock]\r\n"])

    def test_check_limits_at_init(self):
        # protocol.c:41-47 CHECK_LIMITS_AT_INIT with hard limits enabled and a limit pin active
        g = mk()
        send(g, b"$21=1\n")
        g.limit_pins = 1
        self.assertEqual(send(g, b"\x18"), BANNER + ["[MSG:Check Limits]\r\n", "[MSG:'$H'|'$X' to unlock]\r\n"])


class PluginParseCompat(unittest.TestCase):
    """Plugin regexes (census section 2) must accept what the firmware prints."""

    def test_P01_banner(self):
        g = GrblVirtual()
        lines = out(g)                                   # what the port itself prints at power-up
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(re.match(r"^Grbl", lines[1]), lines)

    def test_P02_status_regex_and_fields(self):
        g = mk(instant=True)
        send(g, b"G0X1.5Y-2Z3\n")
        line = send(g, b"?")[0]
        m = P02A.search(line)
        self.assertEqual((m.group(1), m.group(2), m.group(3), m.group(4)), ("Idle", "1.500", "-2.000", "3.000"))
        self.assertEqual(P02E.search(line).groups(), ("0", "0"))
        g.limit_pins = 7
        g.probe_tripped = True
        self.assertEqual(P02D.search(send(g, b"?")[0]).group(1), "PXYZ")
        send(g, b"$10=0\n")
        self.assertIsNotNone(re.search(r"\|WPos:", send(g, b"?")[0]))
        self.assertIsNotNone(P02A.search(send(g, b"?")[0]))

    def test_P02_state_strings_used_by_the_plugin(self):
        g = mk()
        seen = set()
        send(g, b"G1X100F500\n")
        g.tick(0.5)
        seen.add(status(send(g, b"?")[0])[0])
        send(g, b"!")
        seen.add(status(send(g, b"?")[0])[0])
        g.tick(2)
        seen.add(status(send(g, b"?")[0])[0])
        self.assertEqual(seen, {"Run", "Hold:1", "Hold:0"})

    def test_P03_P04(self):
        g = mk(instant=True)
        g.inject_alarm(5)
        self.assertEqual(P03.search(out(g)[0].lower()).group(1), "5")
        send(g, b"$X\n")
        out(g)
        self.assertEqual(P04.search(send(g, b"G5\n")[0]).group(1), "20")

    def test_P06_prb_split(self):
        g = mk(instant=True)
        g.probe_trigger_z = -1.5
        line = send(g, b"G38.2Z-5F100\n")[0]
        parts = line.strip().strip("[]").split(":")           # plugin: split on ':' then ','
        self.assertEqual((parts[0], parts[1].split(","), int(float(parts[2]))), ("PRB", ["0.000", "0.000", "-1.500"], 1))

    def test_P07_gc_tokens(self):
        g = mk(instant=True)
        send(g, b"G91G55M8F300S20\n")
        toks = send(g, b"$G\n")[0].strip().strip("[]").replace("GC:", "").split(" ")
        for want in ("G91", "G55", "M8", "F300", "S20"):
            self.assertIn(want, toks)

    def test_P08_dollar_dollar_lines(self):
        lines = [l for l in send(mk(), b"$$\n") if l.startswith("$")]
        self.assertEqual(len(lines), 34)
        for l in lines:
            self.assertIsNotNone(P08.match(l), l)

    def test_P09c_ngc_has_three_axis_values_per_coordinate_line(self):
        for l in send(mk(), b"$#\n"):
            if re.match(r"\[G5[4-9]:", l):
                self.assertEqual(len(l.strip().strip("[]").split(":")[1].split(",")), 3)


class PluginForms(unittest.TestCase):
    """bgs-gcode-words.txt: every G/M word and $ command the plugin emits."""

    G = {  # word -> sample block (instant machine, F preset) and expected reply
        "G0": "G0 X1", "G1": "G1 X1", "G2": "G2 X1 Y1 I0.5 J0.5", "G3": "G3 X0 Y0 I-0.5 J-0.5",
        "G4": "G4 P0.001", "G10": "G10 L2 P1 X1", "G17": "G17", "G18": "G18", "G19": "G19", "G20": "G20",
        "G21": "G21", "G30": "G30", "G53": "G53 G0 X-1", "G54": "G54", "G55": "G55", "G56": "G56",
        "G57": "G57", "G58": "G58", "G59": "G59", "G80": "G80", "G90": "G90", "G91": "G91", "G92": "G92 X0",
        "G93": "G93", "G94": "G94", "M3": "M3", "M4": "M4", "M5": "M5", "M8": "M8", "M9": "M9",
        "M30": "M30",
    }

    def test_supported_g_and_m_words_are_accepted(self):
        for word, block in self.G.items():
            g = mk(instant=True)
            send(g, b"G1F500\nG0X1Y1\n")
            r = send(g, (block + "\n").encode())
            self.assertEqual(r[-1], OK, (word, r))
            self.assertFalse(any(x.startswith(("error", "ALARM")) for x in r), (word, r))

    def test_g38_family(self):
        g = mk(instant=True)
        g.probe_trigger_z = -2.0
        for w in ("G38.2", "G38.3"):
            send(g, b"G0Z0\n")
            self.assertEqual(send(g, (w + " Z-5 F100\n").encode()), ["[PRB:0.000,0.000,-2.000:1]\r\n", OK])
        for w in ("G38.4", "G38.5"):         # away: start in contact (below the plane), stop on leaving it
            send(g, b"G0Z-3\n")
            self.assertEqual(send(g, (w + " Z1 F100\n").encode()), ["[PRB:0.000,0.000,-2.000:1]\r\n", OK])

    def test_g38_1_is_unsupported_on_gnea(self):
        self.assertEqual(send(mk(), b"G38.1 Z-5 F100\n"), ["error:20\r\n"])

    def test_words_the_plugin_rewrites_are_error20_if_sent_raw(self):
        for w in ("M7", "M21", "M84", "M104", "M105", "M106", "M108", "M110", "M114", "M115", "M140"):
            self.assertEqual(send(mk(), (w + "\n").encode()), ["error:20\r\n"], w)
        # int_value is a uint8_t (gcode.c:98): 290/400/999 wrap to 34/144/231 and leave a huge mantissa -> error:23
        for w in ("M290", "M400", "M999"):
            self.assertEqual(send(mk(), (w + "\n").encode()), ["error:23\r\n"], w)

    def test_dollar_commands(self):
        for cmd, n in (("$#", 12), ("$$", 35), ("$G", 2), ("$I", 3)):
            r = send(mk(), (cmd + "\n").encode())
            self.assertEqual((len(r), r[-1]), (n, OK), cmd)
        for cmd in ("$10=0", "$10=1", "$X"):
            self.assertEqual(send(mk(), (cmd + "\n").encode()), [OK], cmd)
        self.assertEqual(send(mk(), b"$SLP\n"), [OK, "[MSG:Sleeping]\r\n"])
        for cmd, err in (("$H", 5), ("$BUILD/INFO", 2), ("$CD", 3), ("$CONFIG/DUMP", 3), ("$GCODE/MODES", 3),
                         ("$SETTINGS/LIST", 3), ("$S", 3), ("$Bye", 2)):
            self.assertEqual(send(mk(), (cmd + "\n").encode()), ["error:%d\r\n" % err], cmd)


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
            g = GrblVirtual(instant=True)
            g.drain()
            n = errs = 0
            with open(path, "rb") as fh:
                for raw in fh:
                    n += 1
                    line = raw.rstrip(b"\r\n")
                    g.feed(line + b"\n")
                    replies = g.drain()
                    if line.lstrip().startswith(b"%"):
                        # Program-delimiter lines: gnea does not strip '%' (protocol.c "TODO: Install '%' feature"),
                        # so the firmware answers error:1; the plugin never forwards them (census T05).
                        pct += 1
                        pct_replies.append((os.path.basename(path), n, replies))
                        continue
                    for r in replies:
                        if r.startswith((b"error:", b"ALARM:")):
                            errs += 1
                            if bad + errs <= 5:
                                print("UNEXPECTED %s:%d %r -> %r" % (os.path.basename(path), n, line, r))
            total_lines += n
            bad += errs
            summary.append((os.path.relpath(path, CORPUS_DIR), n, errs))
        print("\ncorpus summary: %d files, %d lines, %d error/ALARM replies, %d '%%' lines held back (plugin T05)"
              % (len(files), total_lines, bad, pct))
        for name, n, e in summary:
            print("  %8d lines %3d errors  %s" % (n, e, name))
        for name, n, replies in pct_replies:
            self.assertEqual(replies, [b"error:1\r\n"], (name, n))
        self.assertEqual(bad, 0)
        self.assertEqual(total_lines, sum(n for _, n, _ in summary))


class ReviewFixes(unittest.TestCase):
    """Behavioural coverage for review findings 1, 2, 10, 12, 14 (grbl_virtual.py fixes and test-vacuity gaps)."""

    # -- finding 1: $100-$102=0 must not take the generator down (nuts_bolts/system.c float division, no trap)
    def test_zero_steps_per_mm_keeps_the_port_running(self):
        g = mk()
        self.assertEqual(send(g, b"$100=0\n"), [OK])
        r = send(g, b"?\n")
        self.assertEqual(len(r), 2, r)
        self.assertEqual(r[1], OK)
        r = send(g, b"?\n")                                   # a second report: x/0 is inf or NaN in C, no fault
        self.assertEqual(status(r[0])[0], "Idle")
        self.assertEqual(send(g, b"$G\n")[-1], OK)
        self.assertEqual(send(g, b"$100=250\n"), [OK])

    def test_zero_steps_per_mm_survives_motion_planning(self):
        g = mk()
        send(g, b"$100=0\n")
        self.assertEqual(send(g, b"G1Y1F6000\n"), [OK])      # planner.c: delta_mm = 0/0 is NaN in C, no trap
        r = send(g, b"?")
        self.assertEqual(status(r[0])[0], "Run")

    # -- finding 2: a word value that overflows binary32 to infinity must not raise
    def test_infinite_word_value_keeps_the_port_running(self):
        g = mk()
        for line in (b"X" + b"9" * 40, b"G" + b"9" * 40, b"M" + b"9" * 40, b"G1 F" + b"9" * 40):
            r = send(g, line + b"\n")
            self.assertEqual(len(r), 1, (line, r))
            self.assertTrue(r[0] == OK or r[0].startswith("error:"), r)
            self.assertEqual(send(g, b"$G\n")[-1], OK, line)       # generator still alive

    def test_infinite_value_never_raises_for_any_word_or_setting(self):
        # finding 2: every parsed float that overflows binary32 reaches a C cast that saturates, never a trap.
        # Every word letter bare and after a motion, both signs, then the setting classes ($0..$139 covers the
        # integer, flag, float and per-axis settings), $N startup lines and the G4 / G10 / N special uses.
        big = b"9" * 40
        lines = []
        for letter in string.ascii_uppercase.encode():
            for sign in (b"", b"-"):
                word = bytes([letter]) + sign + big
                lines += [word, b"G1X1F100 " + word, b"G2X10Y10 " + word + b"F100"]
        lines += [b"G4P" + big, b"G10L2P" + big + b"X1", b"G10L20P" + big + b"X1", b"G10L" + big + b"P1X1",
                  b"G2X1R" + big + b"F100", b"M3S" + big, b"G91G1X" + big + b"Y" + big + b"F" + big]
        lines += [b"$%d=%s%s" % (n, sign, big) for n in range(140) for sign in (b"", b"-")]
        lines += [b"$N" + big + b"=G0", b"$N-" + big + b"=G0", b"$" + big + b"=1", b"$-" + big + b"=1"]
        for line in lines:
            g = mk()
            g.feed(line + b"\n")
            g.tick(0.2)
            r = out(g)
            self.assertTrue(len(r) <= 1 and all(x == OK or x.startswith("error:") for x in r), (line, r))
            self.assertTrue(STATUS_RE.fullmatch(send(g, b"?")[0]), line)            # still serving realtime commands

    def test_overflow_conversions_follow_the_c_casts(self):
        big = b"9" * 40
        # gcode.c:309 gc_block.values.n = trunc(value), an int32_t saturating at 2^31-1: over MAX_LINE_NUMBER
        # (gcode.c:27, :376) -> error:27; a negative word is error:4 (:330)
        self.assertEqual(send(mk(), b"N" + big + b"\n"), ["error:27\r\n"])
        self.assertEqual(send(mk(), b"N-" + big + b"\n"), ["error:4\r\n"])
        # settings.c:229 uint8_t int_value = trunc(value): +inf is all bits set; $0 and $1 store 255, $4 is a flag
        g = mk()
        for n in (0, 1, 4):
            self.assertEqual(send(g, b"$%d=%s\n" % (n, big)), [OK])
        listing = send(g, b"$$\n")
        self.assertIn("$0=255\r\n", listing)
        self.assertIn("$1=255\r\n", listing)
        self.assertIn("$4=1\r\n", listing)
        # gcode.c:529-530 uint8_t coord_select = trunc(p): inf -> 255 > N_COORDINATE_SYSTEM; 256 wraps to 0 = active system
        self.assertEqual(send(mk(), b"G10L2P" + big + b"X1\n"), ["error:29\r\n"])
        self.assertEqual(send(mk(), b"G10L2P256X1\n"), [OK])
        self.assertEqual(send(mk(), b"G10L2P257X1\n"), [OK])                    # 257 wraps to 1 = G54
        # system.c:265 helper_var = trunc(parameter), a uint8_t: $N256 stores startup line 0
        g = mk()
        self.assertEqual(send(g, b"$N256=G0\n"), [OK])
        self.assertEqual(send(g, b"$N\n"), ["$N0=G0\r\n", "$N1=\r\n", OK])

    def test_dwell_ticks_are_a_uint16_of_ceil_of_20_p(self):
        # nuts_bolts.c:delay_sec: uint16_t i = ceil(1000/DWELL_TIME_STEP*seconds), 50 ms per step
        g = mk()
        send(g, b"G4P4000\n")                 # 80000 steps wrap to 14464: 723.2 s, not 4000 s
        g.tick(722.0)
        self.assertEqual(out(g), [])
        g.tick(2.0)
        self.assertEqual(out(g), [OK])
        g = mk()
        send(g, b"G4P" + b"9" * 40 + b"\n")   # +inf saturates to all bits set, narrowed to 65535 steps: 3276.75 s
        g.tick(3276.0)
        self.assertEqual(out(g), [])
        g.tick(1.0)
        self.assertEqual(out(g), [OK])

    # -- finding 10: laser mode ($32=1) S reporting and beam control (gcode.c:868-934, stepper.c:845/958-970)
    def _laser_status(self, cmds, tick=0.5, **kw):
        g = mk(**kw)
        for c in cmds:
            self.assertEqual(send(g, c), [OK], c)
        g.tick(tick)
        return g, status(send(g, b"?")[0])

    def test_laser_m3_s_is_reported_during_g1(self):
        g, (state, f) = self._laser_status([b"$32=1\n", b"M3S100\n", b"G1X100F600\n"])
        self.assertEqual((state, f["FS"]), ("Run", "500,100"))   # block load forces the PWM update (stepper.c:845)

    def test_laser_idle_modal_beam_is_off_after_m3(self):
        g, (state, f) = self._laser_status([b"$32=1\n", b"M3S100\n"])
        self.assertEqual((state, f["FS"]), ("Idle", "0,0"))      # modal G0: LASER_DISABLE, spindle value 0

    def test_laser_beam_is_off_during_g0(self):
        g, (state, f) = self._laser_status([b"$32=1\n", b"M3S100\n", b"G0X100\n"])
        self.assertEqual((state, f["FS"]), ("Run", "500,0"))     # non-G1/2/3 motion disables the beam
        g, (state, f) = self._laser_status([b"$32=0\n", b"M3S100\n", b"G0X100\n"])
        self.assertEqual((state, f["FS"]), ("Run", "500,100"))   # ...which a normal spindle does not do

    def test_laser_m4_dynamic_power_scales_with_speed(self):
        # $110=300 caps the feed at 300 of the programmed 600: rpm = S * current_speed * inv_rate = 100 * 300/600
        g, (state, f) = self._laser_status([b"$32=1\n", b"$110=300\n", b"M4S100\n", b"G1X100F600\n"])
        self.assertEqual((state, f["FS"]), ("Run", "300,50"))
        g, (state, f) = self._laser_status([b"$32=1\n", b"$110=300\n", b"M3S100\n", b"G1X100F600\n"])
        self.assertEqual((state, f["FS"]), ("Run", "300,100"))   # M3 is constant power

    def _accessory(self, g):
        """The A: field of the next report that carries Ov (None when no spindle/coolant output is on)."""
        for _ in range(25):
            f = status(send(g, b"?")[0])[1]
            if "Ov" in f:
                return f.get("A")
        raise AssertionError("no Ov report")

    def test_laser_m4_beam_off_when_the_move_completes(self):
        g, _ = self._laser_status([b"$32=1\n", b"M4S100\n", b"G1X100F600\n"])
        self.assertEqual(self._accessory(g), "C")                # beam on (CCW) while the block runs
        g, (state, f) = self._laser_status([b"$32=1\n", b"M4S100\n", b"G1X1F6000\n"], tick=2.0)
        self.assertEqual(state, "Idle")                          # stepper.c:393-399: PWM off at the end of an M4 block
        self.assertIsNone(self._accessory(g))

    # -- finding 12: 0x99 resets a raised spindle override to 100 (serial.c:175, protocol.c:439)
    def test_S15_spindle_normal_resets_a_raised_override(self):
        g = mk()
        send(g, b"?")
        r = send(g, b"\x9a?")
        self.assertEqual(status(r[0])[1]["Ov"], "100,100,110")
        r = send(g, b"\x99?")
        self.assertEqual(status(r[0])[1]["Ov"], "100,100,100")

    def test_T12_override_bytes_take_effect(self):
        g = mk()
        send(g, b"?")
        steps = [(b"\x91", "110,100,100"), (b"\x90", "100,100,100"), (b"\x92", "90,100,100"),
                 (b"\x93", "91,100,100"), (b"\x94", "90,100,100"), (b"\x90", "100,100,100"),
                 (b"\x9a", "100,100,110"), (b"\x99", "100,100,100"), (b"\x9b", "100,100,90"),
                 (b"\x9c", "100,100,91"), (b"\x9d", "100,100,90"), (b"\x99", "100,100,100")]
        for byte, want in steps:
            r = send(g, byte + b"?")
            if "Ov" not in status(r[0])[1]:                      # a WCO report defers Ov to the next one (report.c)
                r = send(g, b"?")
            self.assertEqual(status(r[0])[1].get("Ov"), want, byte)

    # -- finding 14: safety-door resume restores spindle then coolant with their delays; flood toggle
    def test_door_resume_restores_spindle_then_coolant_after_delays(self):
        g = mk()
        for c in (b"M3S500\n", b"M8\n"):
            self.assertEqual(send(g, c), [OK], c)
            g.tick(0.1)
        r = send(g, b"\x84")
        g.tick(0.5)
        f = status(send(g, b"?")[0])[1]
        self.assertEqual(f["FS"], "0,0")                         # retract: spindle and coolant de-energised
        send(g, b"~")
        g.tick(3.9)
        state, f = status(send(g, b"?")[0])
        self.assertEqual((state, f["FS"], f.get("A")), ("Door:3", "0,500", "S"))      # spindle back, coolant still off
        g.tick(0.2)
        state, f = status(send(g, b"?")[0])
        self.assertEqual((state, f.get("A")), ("Door:3", "SF"))   # SAFETY_DOOR_SPINDLE_DELAY 4.0 s, then coolant on
        g.tick(0.7)
        self.assertEqual(status(send(g, b"?")[0])[0], "Door:3")   # SAFETY_DOOR_COOLANT_DELAY 1.0 s still running
        g.tick(0.4)
        self.assertEqual(status(send(g, b"?")[0])[0], "Idle")

    def test_flood_coolant_toggle_0xA0(self):
        g = mk()
        self.assertEqual(send(g, b"M8\n"), [OK])
        self.assertIn(" M8 ", send(g, b"$G\n")[0])
        send(g, b"\xa0")                                         # protocol.c:467-475
        self.assertIn(" M9 ", send(g, b"$G\n")[0])
        send(g, b"\xa0")
        self.assertIn(" M8 ", send(g, b"$G\n")[0])


def advance(g, seconds, step=0.1):
    """tick() in small steps: a delay or a system motion that ends inside one big tick() starts its successor on the next."""
    for _ in range(int(round(seconds / step))):
        g.tick(step)


class Parking(unittest.TestCase):
    """PARKING_ENABLE build (``GrblVirtual(parking=True)``): protocol.c:protocol_exec_rt_suspend parking branches.

    Constants (config.h:567-571): axis Z, target -5.0 mm, fast rate 500, pull-out 5 mm at 100 mm/min.  Machine
    coordinates are negative space; the job below cuts at Z -20, so the pull-out waypoint is -15 and the park
    target -5.  Timing comes from the linear model (constant speed, no ramp for system motions)."""

    @staticmethod
    def cutting(parking=True, z=-20.0, extra=b""):
        """Homing enabled (``$22=1``, the parking precondition) and unlocked, spindle on, one long X cut in progress."""
        g = mk(parking=parking)
        for line in (b"$22=1\n", b"$X\n", b"G21G90\n", b"G0Z%f\n" % z, b"M3S500\n") + ((extra,) if extra else ()):
            send(g, line)
        advance(g, 5.0)
        send(g, b"G1X100F600\n")
        advance(g, 1.0)
        return g

    @staticmethod
    def z(g):
        return mpos(send(g, b"?")[0])[2]

    @staticmethod
    def state(g):
        return status(send(g, b"?")[0])

    def test_opt_p_only_in_a_parking_build(self):
        # report.c:report_build_info: 'P' under #ifdef PARKING_ENABLE, letters before the first comma
        self.assertEqual(send(mk(), b"$I\n")[1], "[OPT:V,15,128]\r\n")
        self.assertEqual(send(mk(parking=True), b"$I\n")[1], "[OPT:VP,15,128]\r\n")

    def test_retract_pullout_with_spindle_on_then_spindle_off_then_fast_park(self):
        g = self.cutting()
        send(g, b"\x84")
        advance(g, 2.5)                                   # 1.0 s to stop the 600 mm/min cut, then the pull-out (3 s) runs
        state, f = self.state(g)
        self.assertEqual(state, "Door:2")
        self.assertEqual(f["FS"][-3:], "500")         # spindle still on during the pull-out
        self.assertTrue(-20.0 < self.z(g) < -15.0)
        advance(g, 2.1)                                   # t=4.6: pull-out done at -15, spindle off, fast leg under way
        z = self.z(g)
        self.assertTrue(-15.0 < z < -5.0, z)
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"], "0,0")
        advance(g, 0.7)                                   # t=5.3: 10 mm at 500 mm/min = 1.2 s is done
        state, f = self.state(g)
        self.assertEqual(state, "Door:0")
        self.assertAlmostEqual(self.z(g), -5.0, places=3)

    def test_restore_plunge_runs_at_the_pullout_rate_not_the_fast_rate(self):
        # protocol.c:703-708: the last leg back to the stop point is at PARKING_PULLOUT_RATE (100 mm/min), after the
        # fast leg to the waypoint (500 mm/min) and the 4 s spindle delay. The older resume test only asserts the
        # END of the plunge, which a 500 mm/min plunge (0.6 s) also reaches.
        g = self.cutting()
        send(g, b"\x84")
        advance(g, 6.0)
        send(g, b"~")
        advance(g, 1.5)
        self.assertAlmostEqual(self.z(g), -15.0, places=2)        # fast leg done: 10 mm at 500 mm/min = 1.2 s
        advance(g, 3.0 + 1.2)                                      # 4 s spindle delay, then about half a second of plunge
        z = self.z(g)
        self.assertTrue(-17.5 < z < -15.3, z)                      # 100 mm/min: under 1 mm in; at 500 mm/min it is at -20
        advance(g, 3.0)
        self.assertAlmostEqual(self.z(g), -20.0, places=3)

    def test_door_reopened_during_the_restore_retracts_again_and_still_returns_to_the_stop_point(self):
        # protocol.c SUSPEND_RESTART_RETRACT (lines 573-576, 649-715): a second door event while Door:3 re-runs the
        # retract WITHOUT re-capturing the restore point (it is still the original cut depth). What this pins is that
        # restore point (mutating the guard on `park.restore` turns it red). The two guards that skip the spindle and
        # coolant restore on a reopen are NOT pinned: the retract that follows switches both off again within the
        # same simulator slice, so removing either guard is an equivalent mutant here (measured, not assumed).
        g = self.cutting(extra=b"M8\n")
        send(g, b"\x84")
        advance(g, 6.0)
        self.assertEqual(self.state(g)[0], "Door:0")
        send(g, b"~")
        advance(g, 2.0)                                            # in the spindle delay: Z at the waypoint, spindle on
        state, f = self.state(g)
        self.assertEqual((state, f["FS"], f["A"]), ("Door:3", "0,500", "S"))
        send(g, b"\x84")
        advance(g, 0.3)
        state, f = self.state(g)
        self.assertEqual((state, f["FS"]), ("Door:2", "0,0"))      # retracting again, spindle off
        advance(g, 4.0)
        state, f = self.state(g)
        self.assertEqual((state, f["FS"]), ("Door:0", "0,0"))
        self.assertAlmostEqual(self.z(g), -5.0, places=3)
        send(g, b"~")
        advance(g, 14.0)
        state, f = self.state(g)
        self.assertEqual(state, "Run")
        self.assertAlmostEqual(self.z(g), -20.0, places=3)         # the ORIGINAL depth, not the waypoint it was reopened at
        self.assertEqual(f["A"], "SF")

    def test_cycle_start_is_dropped_until_door_0(self):
        g = self.cutting()
        send(g, b"\x84")
        advance(g, 3.0)
        self.assertEqual(self.state(g)[0], "Door:2")
        send(g, b"~")                                 # protocol.c:334-347: only acts at Door:0 (retract complete)
        advance(g, 0.2)
        self.assertEqual(self.state(g)[0], "Door:2")

    def test_resume_returns_to_the_exact_stop_point_and_the_cut_continues(self):
        g = self.cutting()
        send(g, b"\x84")
        advance(g, 6.0)
        self.assertEqual(self.state(g)[0], "Door:0")
        stopped = mpos(send(g, b"?")[0])
        send(g, b"~")
        advance(g, 0.5)
        self.assertEqual(self.state(g)[0], "Door:3")   # restoring: fast back to the pull-out waypoint (1.2 s)
        advance(g, 1.0)
        self.assertAlmostEqual(self.z(g), -15.0, places=2)
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"], "0,500")      # spindle on again
        advance(g, 3.0)                                    # SAFETY_DOOR_SPINDLE_DELAY 4.0 s: no motion
        self.assertEqual(self.state(g)[0], "Door:3")
        self.assertAlmostEqual(self.z(g), -15.0, places=2)
        near(self, mpos(send(g, b"?")[0])[:2], stopped[:2])      # X and Y never moved while parked
        advance(g, 4.5)                                    # slow plunge 5 mm at 100 mm/min = 3 s
        self.assertEqual(self.state(g)[0], "Run")
        self.assertAlmostEqual(self.z(g), -20.0, places=3)
        advance(g, 60.0)
        self.assertAlmostEqual(mpos(send(g, b"?")[0])[0], 100.0, places=2)   # the interrupted block finished

    def test_a_block_advances_by_step_deltas_so_an_excursion_is_not_undone(self):
        # stepper.c steps from wherever the machine is: after a hold a block carries on from the current position, so
        # a parking move that left an axis elsewhere is only undone by the restore motion, never by the next block
        g = self.cutting()
        send(g, b"!")
        advance(g, 3.0)
        self.assertEqual(self.state(g)[0], "Hold:0")
        g.sys_position[2] += 1000            # 4 mm up at 250 steps/mm, as a parking move would leave it
        send(g, b"~")
        advance(g, 2.0)
        self.assertAlmostEqual(self.z(g), -16.0, places=3)

    def test_parking_motion_never_enters_the_planner(self):
        # motion_control.c:mc_parking_motion: independent of the main planner buffer
        g = self.cutting()
        send(g, b"\x84")
        advance(g, 1.5)
        self.assertEqual(len(g.blocks), 1)             # only the interrupted X cut
        self.assertIsNotNone(g.sys_block)
        self.assertTrue(g.sys_block.condition & 2)     # PL_COND_SYSTEM
        advance(g, 10.0)
        self.assertIsNone(g.sys_block)
        self.assertEqual(len(g.blocks), 1)

    def test_z_already_above_the_park_target_only_stops_the_spindle(self):
        # protocol.c:581-590: parking_target[Z] < PARKING_TARGET is false -> "just disable spindle and coolant".
        # The firmware repeats that comparison before the fast move (line 608), so deleting only the outer one is an
        # equivalent mutant; deleting both turns this test red (checked).
        g = self.cutting(z=-3.0)
        send(g, b"\x84")
        advance(g, 3.0)
        state, f = self.state(g)
        self.assertEqual((state, f["FS"]), ("Door:0", "0,0"))
        self.assertAlmostEqual(self.z(g), -3.0, places=3)
        send(g, b"~")
        advance(g, 1.0)
        self.assertAlmostEqual(self.z(g), -3.0, places=3)      # no restore motion either
        advance(g, 4.0)                                             # spindle delay, then back to work
        self.assertEqual(self.state(g)[0], "Run")
        self.assertEqual(status(send(g, b"?")[0])[1]["FS"][-3:], "500")

    def test_pullout_stops_at_the_park_target_when_less_than_5_mm_below_it(self):
        # protocol.c:577-578: retract_waypoint = min(Z + 5, PARKING_TARGET): Z -7 -> waypoint -5, one move only
        g = self.cutting(z=-7.0)
        send(g, b"\x84")
        advance(g, 2.5)                                    # 2 mm at 100 mm/min = 1.2 s; no fast leg after it
        state, f = self.state(g)
        self.assertEqual((state, f["FS"]), ("Door:0", "0,0"))
        self.assertAlmostEqual(self.z(g), -5.0, places=3)

    def test_no_parking_without_homing_enabled(self):
        g = mk(parking=True)
        for line in (b"G21G90\n", b"G0Z-20F500\n", b"M3S500\n"):
            send(g, line)
        advance(g, 5.0)
        send(g, b"\x84")
        advance(g, 5.0)
        state, f = self.state(g)
        self.assertEqual((state, f["FS"]), ("Door:0", "0,0"))
        self.assertAlmostEqual(self.z(g), -20.0, places=3)     # $22=0: spin-down only

    def test_no_parking_in_laser_mode(self):
        g = mk(parking=True)
        for line in (b"$22=1\n", b"$32=1\n", b"$X\n", b"G21G90\n", b"G0Z-20F500\n", b"M3S500\n"):
            send(g, line)
        advance(g, 5.0)
        send(g, b"\x84")
        advance(g, 5.0)
        self.assertEqual(self.state(g)[0], "Door:0")
        self.assertAlmostEqual(self.z(g), -20.0, places=3)

    def test_stock_build_stops_without_lifting(self):
        g = self.cutting(parking=False)
        send(g, b"\x84")
        advance(g, 5.0)
        state, f = self.state(g)
        self.assertEqual((state, f["FS"]), ("Door:0", "0,0"))
        self.assertAlmostEqual(self.z(g), -20.0, places=3)

    def test_reset_during_the_retract_raises_alarm_3(self):
        # motion_control.c:mc_reset: a reset while a system motion runs is an aborted cycle (ALARM:3)
        g = self.cutting()
        send(g, b"\x84")
        advance(g, 2.0)
        r = send(g, b"\x18")
        self.assertIn("ALARM:3\r\n", r)

    def test_reset_at_door_0_is_clean(self):
        g = self.cutting()
        send(g, b"\x84")
        advance(g, 8.0)
        self.assertEqual(self.state(g)[0], "Door:0")
        r = send(g, b"\x18")
        self.assertNotIn("ALARM:3\r\n", r)
        self.assertEqual(self.state(g)[0], "Idle")

    def test_idle_machine_parks_too(self):
        g = mk(parking=True)
        for line in (b"$22=1\n", b"$X\n", b"G21G90\n", b"G0Z-20F500\n", b"M3S500\n"):
            send(g, line)
        advance(g, 5.0)
        send(g, b"\x84")
        advance(g, 5.0)
        self.assertEqual(self.state(g)[0], "Door:0")
        self.assertAlmostEqual(self.z(g), -5.0, places=3)

    def test_parking_instant_mode_completes_without_ticks(self):
        g = mk(parking=True, instant=True)
        for line in (b"$22=1\n", b"$X\n", b"G21G90\n", b"G0Z-20F500\n", b"M3S500\n"):
            send(g, line)
        send(g, b"\x84")
        self.assertEqual(self.state(g)[0], "Door:0")
        self.assertAlmostEqual(self.z(g), -5.0, places=3)


if __name__ == "__main__":
    unittest.main()
