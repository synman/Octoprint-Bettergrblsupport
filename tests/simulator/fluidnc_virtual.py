#!/usr/bin/env python3
"""A pure-Python virtual FluidNC (bdring/FluidNC @ 9b8236f) for testing Better Grbl Support without hardware.

``FluidNCVirtual`` subclasses ``GrblVirtual`` (the gnea/grbl 1.1h port): the G-code parser, planner, motion
model, probe, override and suspend machinery are inherited unchanged; everything the plugin can SEE of
FluidNC that differs from gnea is ported from the FluidNC source and carries a ``Source: <file>:<function> @
9b8236f (lines a-b)`` line in its docstring (sources under ``FluidNC/src/``).  Byte formats are the ones the
plugin parses (census section 3, rows F01-F23).

Deliberate decisions (the source leaves them to the build or the machine config)
-----------------------------------------------------------------------------------
* Version: the git tag is not resolvable from the tree, so the build is declared as tag ``v3.9.9`` (a release
  build: ``git_info`` is the bare tag, ``grbl_version`` is ``3.9`` per ``git-version.py:60-61``), MCU
  ``esp32``, variant ``wifi``  ->  ``Grbl 3.9 [FluidNC v3.9.9 (esp32-wifi) '$' for help]``.
* Machine config: ``FluidNC/data/config.yaml`` ("Default (Test Drive no I/O)": 80 steps/mm, 1000 mm/min,
  25 mm/s^2, 1000 mm travel, no homing, no soft/hard limits) plus ``Axis::group`` items it predates
  (``idle_disable``).  The default machine has no homing (so no startup ALARM:14), no probe pin and no flood
  pin; the constructor can add them (``homing=``, ``probe_pin=``, ``flood_pin=``).  The default spindle is a
  ``laser`` section (speed map 0..1000), because the plugin drives lasers and a ``NoSpindle`` machine rejects
  M4 with error:20 (``GCode.cpp:711-716``); ``spindle="none"`` gives the data/config.yaml behaviour;
  ``spindle="pwm"`` is a ``PWM`` section (a CNC spindle: speed map 0..10000, no direction pin, ``spinup_ms`` and
  ``spindown_ms`` as ``PWM::group`` prints them; the laser section carries them too here, which
  ``Laser::group`` does not at the pinned SHA).
* Parking (``Parking.cpp``): the ``parking:`` section is runtime config (``$/parking/enable=true``).  A safety-door
  event (0x84) parks when ``can_park()`` holds (enabled, not a laser, the parking axis has a homing cycle in the
  config; ``enable_parking_override_control`` / M56 is not modelled, it behaves as M56 P1) and the axis is below
  ``target_mpos_mm``; the resume restores the spindle with the spindle's own ``spinup_ms`` wait (only the
  ``PWM`` spindle has one; the door path is the only place the wait is modelled, not M3/M5 in a program) and has
  no coolant delay.
* ``$CD`` replays the config tree through an emulation of ``Configuration::Generator`` for the sections this
  port models; the full ``Configurable`` tree of the real firmware (uart, i2c, ethernet ...) is not reproduced.
* ``$S`` lists the settings this port models (the ones the plugin reads plus their neighbours), in FluidNC's
  reverse-creation order.  The relative order of the WebUI module settings (Hostname, HTTP/*) against the
  ``make_settings`` ones depends on module init order and is an assumption (modules after make_settings).
  ``HTTP/Enable`` is stored ``OFF`` (the firmware default is ON) so the plugin takes the config-over-serial
  path (census section 6).
* The serial channel is a ``UartChannel`` named ``uart_channel0`` with ``addCR`` (lines end ``\\r\\n``) and
  ``_report_when_idle`` (auto reports also fire while idle).  Its line assembly is ``Lineedit::step`` in the
  non-editing mode (``UartChannel::lineComplete``), so CR and LF each complete a line and ``CRLF`` is a line
  plus an empty, acknowledged line.

Not modelled (documented gaps): the real boot log (``$Bye`` prints ``[MSG:INFO: Restarting]``, ``ok`` and the
banner only), WiFi/module ``build_info`` lines in ``$I``, ``#`` parameters / expressions / O-word flow control
/ M6 / G59.1-.3 / G38.6-9 in G-code (the inherited parser answers as gnea does), the ``[G54:...]`` auto-report
after a coordinate write (``notifyNgc``), override-change auto-report nudges, SD card and file jobs, the
500 ms ``delay_ms`` after ``ALARM:`` and the ALARM:6 reset-during-homing path (homing completes instantly).
Also not modelled: the console's line-editing mode (``Lineedit::step``: backspace, DEL or any other control byte
switches it on and the line is then echoed back; here a backspace erases silently), and the channel's
queue-while-waiting-for-ack buffer (``Channel::pollLine`` / ``queue_push``: while a line awaits its ack the
polling task moves further bytes into a 1020-byte whole-line-drop queue, so a host can send far more than the
128-byte ring here holds while the loop is blocked; byte order and realtime handling are the same, only the
capacity differs).
"""
import re

from grbl_virtual import (
    BIT_LASER_MODE, BIT_REPORT_INCHES, BIT_SOFT_LIMIT, EXEC_ALARM_SOFT_LIMIT, EXEC_RESET, EXEC_SLEEP, GC_UPDATE_POS_NONE,
    MESSAGE_ALARM_LOCK, MESSAGE_ALARM_UNLOCK, MESSAGE_CHECK_LIMITS, MESSAGE_CRITICAL_EVENT, MESSAGE_DISABLED,
    MESSAGE_ENABLED, MESSAGE_PROGRAM_END, MESSAGE_RESTORE_DEFAULTS, MESSAGE_SAFETY_DOOR_AJAR,
    MESSAGE_SLEEP_MODE, MESSAGE_SPINDLE_RESTORE, MM_PER_INCH, PL_COND_COOLANT_FLOOD, PL_COND_SPINDLE_CCW,
    PL_COND_SPINDLE_CW, RX_BUFFER_SIZE, BLOCK_BUFFER_SIZE, STATE_ALARM, STATE_CHECK_MODE, STATE_CYCLE,
    STATE_HOLD, STATE_HOMING, STATE_IDLE, STATE_JOG, STATE_SAFETY_DOOR, STATE_SLEEP, STATUS_BAD_NUMBER_FORMAT,
    STATUS_GCODE_UNSUPPORTED_COMMAND, STATUS_IDLE_ERROR, STATUS_INVALID_STATEMENT, STATUS_OK,
    STATUS_SETTING_DISABLED, STATUS_SYSTEM_GC_LOCK, GrblVirtual, _Settings, _word_parts, c_fix_u32, c_round, f32,
    read_float,
)

SRC = "9b8236f"
GRBL_VERSION = "3.9"          # git-version.py:60-61: tag 'v3.9.9' minus 'v' minus last component
GIT_INFO = "v3.9.9"           # release build: exact tag, no branch/sha suffix (git-version.py:36-46)
MCU = "esp32"
VARIANT = "wifi"
DEFAULT_MACHINE_NAME = "Default (Test Drive no I/O)"      # FluidNC/data/config.yaml: name
CHANNEL_NAME = "uart_channel0"
START_MESSAGE = "Grbl \\V [FluidNC \\B (\\X) \\H]"        # SettingsDefinitions.cpp:107-108
LOCALFS_PREFIX = "/littlefs"                                # esp32/localfs.cpp:19 (littlefsPrefix)

# Error.h numbers not in grbl_virtual
STATUS_LINE_LENGTH_EXCEEDED = 14
STATUS_SINGLE_AXIS_HOMING = 19
STATUS_NUMBER_RANGE = 80
STATUS_INVALID_VALUE = 81
STATUS_FS_FAILED_DEL_FILE = 69
STATUS_ANOTHER_INTERFACE_BUSY = 120
STATUS_READ_ONLY_SETTING = 162

# Error.cpp:ErrorNames (lines 8-93): text printed as [MSG:ERR: <text>] after error:N when verbose_errors.
ERROR_TEXT = {
    1: "Expected GCodecommand letter", 2: "Bad GCode number format", 3: "Invalid $ statement",
    4: "Negative value", 5: "Setting disabled", 6: "Step pulse too short", 7: "Failed to read settings",
    8: "Command requires idle state", 9: "GCode cannot be executed in lock or alarm state",
    10: "Soft limit error", 11: "Line too long", 12: "Max step rate exceeded", 13: "Check door",
    14: "Line too long", 15: "Max travel exceeded during jog", 16: "Invalid jog command",
    17: "Laser mode requires PWM output", 18: "No Homing/Cycle defined in settings",
    19: "Single axis homing not allowed", 20: "Unsupported GCode command", 21: "Gcode modal group violation",
    22: "Gcode undefined feed rate", 23: "Gcode command value not integer", 24: "Gcode axis command conflict",
    25: "Gcode word repeated", 26: "Gcode no axis words", 27: "Gcode invalid line number",
    28: "Gcode value word missing", 29: "Gcode unsupported coordinate system",
    30: "Gcode G53 invalid motion mode", 31: "Gcode extra axis words", 32: "Gcode no axis words in plane",
    33: "Gcode invalid target", 34: "Gcode arc radius error", 35: "Gcode no offsets in plane",
    36: "Gcode unused words", 37: "Gcode G43 dynamic axis error", 38: "Gcode max value exceeded",
    39: "P param max exceeded", 40: "Check startup pins", 60: "Failed to mount device", 61: "Read failed",
    62: "Failed to open directory", 63: "Directory not found", 64: "File empty", 65: "File not found",
    66: "Failed to open file", 67: "Device is busy", 68: "Failed to delete directory",
    69: "Failed to delete file", 70: "Failed to rename file", 80: "Number out of range for setting",
    81: "Invalid value for setting", 82: "Failed to create file", 83: "Failed to format filesystem",
    90: "Failed to send message", 100: "Failed to store setting", 101: "Failed to get setting status",
    110: "Authentication failed!", 111: "End of line", 112: "End of file", 113: "System Reset",
    114: "No Data", 115: "Deferred", 120: "Another interface is busy", 130: "Jog Cancelled",
    150: "Bad Pin Specification", 151: "Bad Runtime Config Setting",
    152: "Configuration is invalid. Check boot messages for ERR's.", 160: "File Upload Failed",
    161: "File Download Failed", 162: "Read-only setting", 170: "Expression Divide By Zero",
    171: "Expression Invalid Argument", 173: "Expression Unknown Operator",
    174: "Expression Argument Out of Range", 175: "Expression Syntax Error", 176: "Flow Control Syntax Error",
    177: "Flow Control Not Executing Macro", 178: "Flow Control Out of Memory",
    179: "Flow Control Stack Overflow", 180: "Parameter Assignment Failed", 181: "Gcode invalid word value",
}

# Protocol.cpp:AlarmNames (lines 37-57), Alarm.h numbering.
ALARM_NAME = {
    0: "None", 1: "Hard Limit", 2: "Soft Limit", 3: "Abort Cycle", 4: "Probe Fail Initial",
    5: "Probe Fail Contact", 6: "Homing Fail Reset", 7: "Homing Fail Door", 8: "Homing Fail Pulloff",
    9: "Homing Fail Approach", 10: "Spindle Control", 11: "Input Pin Initially On", 12: "Ambiguous Switch",
    13: "Hard Stop", 14: "Unhomed", 15: "Init", 16: "Expander Reset", 17: "GCode Error", 18: "Probe Hard Limit",
}
ALARM_UNHOMED = 14
RAPID_OVERRIDE_EXTRA_LOW = 5     # Config.h:65-70 RapidOverride::ExtraLow (Default 100, Medium 50, Low 25)
CRITICAL_ALARMS = (1, 2)        # HardLimit / SoftLimit enter State::Critical; HardStop (13) / ExpanderReset (16) are not modelled

# Report.cpp:MessageText (lines 99-116): gnea message code -> FluidNC text (None: no call site / error path).
MESSAGE_TEXT = {
    MESSAGE_ALARM_UNLOCK: "Caution: Unlocked", MESSAGE_ENABLED: "Enabled", MESSAGE_DISABLED: "Disabled",
    MESSAGE_SAFETY_DOOR_AJAR: "Check door", MESSAGE_CHECK_LIMITS: "Check limits",
    MESSAGE_PROGRAM_END: "Program End", MESSAGE_RESTORE_DEFAULTS: "Restoring defaults",
    MESSAGE_SPINDLE_RESTORE: "Restoring spindle", MESSAGE_SLEEP_MODE: "Sleeping",
}

LEVELS = {"None": 0, "Error": 1, "Warning": 2, "Info": 3, "Debug": 4, "Verbose": 5}
ON_OFF = {"OFF": 0, "ON": 1}
NO_PIN = "NO_PIN"
PROBE_PIN = "gpio.32:low:pu"
FLOOD_PIN = "gpio.25"
LASER_OUTPUT_PIN = "gpio.17"


def fmt(value, decimals=3):
    """``formatFloat``: snprintf ``%.*f`` of a float.  Source: NutsBolts.cpp:formatFloat @ 9b8236f (lines 139-143)."""
    return "%.*f" % (decimals, f32(value))


def read_number(text):
    """A plain decimal literal -> float, else None.  Source: Parameters.cpp:read_number @ 9b8236f (lines 697-745)."""
    t = text.strip()
    if re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)", t):
        return float(t)
    return None


def uri_encode_grbl(text):
    """Source: ProcessSettings.cpp:uriEncodeGrblCharacters @ 9b8236f (lines 78-101)."""
    return text.replace("%", "%25").replace("!", "%21").replace("?", "%3F").replace("~", "%7E")


def collapse_gcode(line, on_comment=None):
    """Remove whitespace and comments, upper-case; a lone ``%`` collapses to the empty line.

    Source: GCode.cpp:collapseGCode @ 9b8236f (lines 183-246).  ``on_comment`` receives each complete or
    trailing ``( ... )`` comment text (GCode.cpp:gcode_comment_msg, lines 108-151)."""
    out = []
    paren = None            # index (into line) of the character after '('
    has_comment = False
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c.isspace():
            i += 1
            continue
        if c == ")":
            if paren is not None:
                if on_comment:
                    on_comment(line[paren:i])
                paren = None
        elif c == "(":
            has_comment = True
            paren = i + 1
        elif c == ";":
            break
        elif c == "\r":
            pass
        elif paren is None:
            out.append(c.upper())
        i += 1
    else:
        if paren is not None and on_comment:
            on_comment(line[paren:])
    text = "".join(out)
    if not has_comment and text == "%":
        return ""
    return text


def canon_path(name):
    """Source: FluidPath.cpp:canonPath @ 9b8236f (lines 106-155), default volume LocalFS."""
    if not name:
        return LOCALFS_PREFIX
    while len(name) > 1 and name.endswith("/"):
        name = name[:-1]
    if name[0] == "/":
        pos = name.find("/", 1)
        fsname, tail = (name[1:pos], name[pos:]) if pos >= 0 else (name[1:], "")
        if fsname.lower() in ("localfs", "spiffs", "littlefs"):
            return LOCALFS_PREFIX + tail
        if fsname.lower() == "sd":
            return "/sd" + tail
        return LOCALFS_PREFIX + name
    return LOCALFS_PREFIX + "/" + name


class _Utf8:
    """Byte-at-a-time decoder; bytes 0x80-0xBE pass through (GRBL realtime range).

    Source: UTF8.cpp:UTF8::decode @ 9b8236f (lines 13-72)."""

    def __init__(self):
        self.state = 0
        self.num = 0

    def decode(self, ch):
        """-> value (sequence complete) or None (pending or error)."""
        if self.state:
            if ch & 0xC0 != 0x80:
                self.state = 0
                return None
            self.state -= 1
            self.num = (self.num << 6) | (ch & 0x3F)
            return self.num if not self.state else None
        if ch < 0x80:
            return ch
        if ch < 0xBF:
            return ch
        if ch >= 0xF8:
            return None
        if ch >= 0xF0:
            self.state, self.num = 3, ch & 0x07
        elif ch >= 0xE0:
            self.state, self.num = 2, ch & 0x0F
        elif ch >= 0xC0:
            self.state, self.num = 1, ch & 0x1F
        return None


# ---------------------------------------------------------------------------------------------------
# Machine config tree (an ordered dict of dicts / typed scalars) and its YAML text form.

def _default_config(letters, homing, probe, flood, spindle, name):
    """The tree ``$CD`` dumps.  Source: FluidNC/data/config.yaml (whole file) + Axis.cpp:Axis::group
    @ 9b8236f (lines 12-60) + Machine/Homing.h:Homing::group (lines 80-130) + MachineConfig.cpp:group (lines 44-163)."""
    pins9 = ("safety_door_pin", "reset_pin", "feed_hold_pin", "cycle_start_pin", "macro0_pin", "macro1_pin",
             "macro2_pin", "macro3_pin", "fault_pin", "estop_pin")
    axes = {"shared_stepper_disable_pin": NO_PIN, "shared_stepper_reset_pin": NO_PIN, "homing_runs": 2}
    for letter in letters:
        ax = {"steps_per_mm": 80.0, "max_rate_mm_per_min": 1000.0, "acceleration_mm_per_sec2": 25.0,
              "max_travel_mm": 1000.0, "soft_limits": False, "idle_disable": True}
        if letter in homing:
            ax["homing"] = {"cycle": homing[letter], "allow_single_axis": True, "positive_direction": True,
                            "mpos_mm": 0.0, "feed_mm_per_min": 50.0, "seek_mm_per_min": 200.0,
                            "settle_ms": 250, "seek_scaler": 1.1, "feed_scaler": 1.1}
        ax["motor0"] = {"limit_neg_pin": NO_PIN, "limit_pos_pin": NO_PIN, "limit_all_pin": NO_PIN,
                        "hard_limits": False, "pulloff_mm": 1.0, "null_motor": {}}
        axes[letter.lower()] = ax
    cfg = {
        "board": "None", "name": name, "meta": "",
        "stepping": {"engine": "RMT", "idle_ms": 255, "pulse_us": 4, "dir_delay_us": 0,
                     "disable_delay_us": 0, "segments": 12},
        "spi": {"miso_pin": NO_PIN, "mosi_pin": NO_PIN, "sck_pin": NO_PIN},
        "sdcard": {"cs_pin": NO_PIN, "card_detect_pin": NO_PIN, "frequency_hz": 8000000},
        "kinematics": {"Cartesian": {}},
        "axes": axes,
        "control": {p: NO_PIN for p in pins9},
        "coolant": {"flood_pin": FLOOD_PIN if flood else NO_PIN, "mist_pin": NO_PIN, "delay_ms": 0},
        "probe": {"pin": PROBE_PIN if probe else NO_PIN, "toolsetter_pin": NO_PIN, "check_mode_start": True,
                  "hard_stop": False},
        "macros": {k: "" for k in ("startup_line0", "startup_line1", "Macro0", "Macro1", "Macro2", "Macro3",
                                   "after_homing", "after_reset", "after_unlock")},
        "start": {"must_home": False, "deactivate_parking": False, "check_limits": True},
        "parking": {"enable": False, "axis": "Z", "target_mpos_mm": -5.0, "rate_mm_per_min": 800.0,
                    "pullout_distance_mm": 5.0, "pullout_rate_mm_per_min": 250.0},
        "user_outputs": dict(
            [("analog%d_pin" % i, NO_PIN) for i in range(4)] + [("analog%d_hz" % i, 5000) for i in range(4)]
            + [("digital%d_pin" % i, NO_PIN) for i in range(8)]),
        "user_inputs": dict([("analog%d_pin" % i, NO_PIN) for i in range(4)]
                            + [("digital%d_pin" % i, NO_PIN) for i in range(8)]),
    }
    if spindle == "laser":
        cfg["laser"] = {"pwm_hz": 5000, "output_pin": LASER_OUTPUT_PIN, "enable_pin": NO_PIN, "tool_num": 0,
                        "speed_map": "0=0.00% 1000=100.00%", "off_on_alarm": False, "atc": "", "m6_macro": "",
                        "s0_with_disable": True, "disable_with_s0": False, "spinup_ms": 0, "spindown_ms": 0}
    if spindle == "pwm":
        cfg["PWM"] = {"pwm_hz": 5000, "direction_pin": NO_PIN, "output_pin": LASER_OUTPUT_PIN, "enable_pin": NO_PIN,
                      "tool_num": 0, "speed_map": "0=0.00% 10000=100.00%", "off_on_alarm": False, "atc": "",
                      "m6_macro": "", "s0_with_disable": False, "disable_with_s0": False, "spinup_ms": 0,
                      "spindown_ms": 0}
    cfg.update({"arc_tolerance_mm": 0.002, "junction_deviation_mm": 0.01, "verbose_errors": True,
                "report_inches": False, "enable_parking_override_control": False, "use_line_numbers": False,
                "planner_blocks": 16})
    return cfg


def _scalar_text(value):
    """Source: Configuration/Generator.h:Generator::item @ 9b8236f (lines 45-137): bool true/false, floats
    ``std::to_string`` (``%f``), a value containing ':' is single-quoted."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return "%f" % f32(value)
    text = "%d" % value if isinstance(value, int) else str(value)
    return "'%s'" % text if ":" in text else text


def generate_config_lines(cfg, indent=0):
    """Emulate ``Configuration::Generator``: every item ``<2*indent spaces><name>: <value>``, sections
    ``<name>: `` (trailing space), a blank line after a section unless one was just emitted.

    Source: Configuration/Generator.cpp:Generator::enter/leave @ 9b8236f (lines 12-38) and
    Generator.h:send_item (lines 45-62)."""
    lines = []
    state = {"nl": False}

    def walk(node, level):
        for key, val in node.items():
            lines.append("  " * level + key + ": " + ("" if isinstance(val, dict) else _scalar_text(val)))
            state["nl"] = False
            if isinstance(val, dict):
                walk(val, level + 1)
                if not state["nl"]:
                    lines.append("")
                    state["nl"] = True

    walk(cfg, indent)
    return lines


def parse_config_text(text, template):
    """Inverse of ``generate_config_lines`` for the tree this port writes (types come from ``template``)."""
    rows = []
    for raw in text.split("\n"):
        if not raw.strip():
            continue
        body = raw.lstrip(" ")
        key, _, val = body.partition(":")
        rows.append(((len(raw) - len(body)) // 2, key.strip(), val.strip()))
    root = {}
    stack = [(-1, root)]
    for idx, (level, key, val) in enumerate(rows):
        while stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1]
        nxt = rows[idx + 1][0] if idx + 1 < len(rows) else -1
        if nxt > level and val == "":
            node = {}
            parent[key] = node
            stack.append((level, node))
        else:
            parent[key] = val

    def typed(node, tmpl):
        out = {}
        for key, val in node.items():
            t = tmpl.get(key) if isinstance(tmpl, dict) else None
            if isinstance(val, dict):
                out[key] = typed(val, t if isinstance(t, dict) else {})
            elif isinstance(t, dict):
                out[key] = {}
            else:
                if len(val) >= 2 and val[0] == val[-1] == "'":
                    val = val[1:-1]
                if isinstance(t, bool) or (t is None and val in ("true", "false")):
                    out[key] = val.lower() == "true"
                elif isinstance(t, float) or (t is None and re.fullmatch(r"-?\d+\.\d+", val)):
                    out[key] = float(val)
                elif isinstance(t, int) or (t is None and re.fullmatch(r"-?\d+", val)):
                    out[key] = int(val)
                else:
                    out[key] = val
        return out

    return typed(root, template)


# ---------------------------------------------------------------------------------------------------
# Settings registry (Settings.cpp / SettingsDefinitions.cpp)

class _Setting:
    """One entry of ``Setting::List``.  kinds: int, str, enum, pf (float proxy), pi (int proxy)."""

    def __init__(self, name, grbl=None, kind="int", default=None, lo=None, hi=None, options=None, getter=None):
        self.name, self.grbl, self.kind = name, grbl, kind
        self.default, self.lo, self.hi, self.options, self.getter = default, lo, hi, options, getter
        self.value = default

    def string(self):
        """Source: Settings.cpp:*Setting::getStringValue @ 9b8236f (lines 182-198, 274-276, 379-381);
        Settings.h:FloatProxySetting::getStringValue (lines 220-223); Settings.cpp:IntProxySetting (lines 538-542)."""
        if self.kind == "pf":
            return "%.3f" % f32(self.getter())
        if self.kind == "pi":
            return "%d" % self.getter()
        if self.kind == "int":
            return "%d" % self.value
        return self.value


class FluidNCVirtual(GrblVirtual):
    """A virtual FluidNC 3.9 controller (see the module docstring for the decisions and the gaps)."""

    _coolant_m_words = (7, 8, 9)

    # ------------------------------------------------------------------ construction
    def __init__(self, axes="XYZ", clock=None, instant=False, *, homing=None, spindle="laser",
                 probe_pin=False, flood_pin=False, machine_name=DEFAULT_MACHINE_NAME, http_enable=False):
        """Boot: ``Main.cpp:setup`` then ``Protocol.cpp:protocol_do_start`` (banner, then ALARM:14 only when an
        axis has homing configured).  ``homing`` is ``"XYZ"`` (all in cycle 1) or ``{"X": 1, "Z": 2}``."""
        if spindle not in ("laser", "none", "pwm"):
            raise ValueError("spindle must be 'laser', 'none' or 'pwm'")
        if isinstance(homing, str):
            homing = {a: 1 for a in homing}
        self._homing_arg = dict(homing or {})
        self._spindle_arg, self._probe_arg, self._flood_arg = spindle, bool(probe_pin), bool(flood_pin)
        self._machine_name = machine_name
        self._http_enable = bool(http_enable)
        self._utf8 = _Utf8()
        self._buf = bytearray()
        self._booted = False
        self._critical = False
        self._rapid_extra_low = False
        self._restart_pending = False
        self._fn_spindle = 0
        self._fn_rpm = 0.0
        self._rep_interval = 0           # ms; 0 = off
        self._rep_next = 0.0
        self._rep_ovr = self._rep_wco = False
        self._rep_state = True
        self._rep_last_state = None
        self._rep_last_pin = ""
        self._rep_last_gc = None
        self._level_cache = None
        self._localfs = {}
        super().__init__(axes=axes, clock=clock, instant=instant)

    def _init_axes(self, axes):
        """Axis set: the first N of X Y Z A B C, 2..6 (FluidNC ``Axes::_numberAxis``).

        Source: Types.h:axis_t @ 9b8236f (lines 15-29); Machine/Axes.cpp:Axes::group (axis sections x..c)."""
        if not isinstance(axes, str) or not 2 <= len(axes) <= 6 or axes != "XYZABC"[:len(axes)]:
            raise ValueError("axes must be a prefix of 'XYZABC' with 2..6 letters, got %r" % (axes,))
        self.axis_letters = axes
        self.n_axis = len(axes)
        bad = [a for a in self._homing_arg if a not in axes]
        if bad:
            raise ValueError("homing names axes this build does not have: %r" % bad)
        self._cfg = _default_config(axes, self._homing_arg, self._probe_arg, self._flood_arg,
                                    self._spindle_arg, self._machine_name)
        self._template = _default_config(axes, {a: 1 for a in axes}, True, True, "laser", "")
        self._extra_coord_zero = [0.0] * self.n_axis
        self._saved_name = "config.yaml"
        self._localfs[canon_path(self._saved_name)] = "\n".join(generate_config_lines(self._cfg)) + "\n"
        self._build_settings()
        self._apply_config()

    def _make_settings(self):
        """Source: Machine/MachineConfig.cpp:group @ 9b8236f (lines 38-164) -> Grbl-model fields."""
        st = _Settings(self.n_axis)
        self._config_to_settings(st)
        return st

    # ------------------------------------------------------------------ config <-> grbl model
    def _axis_cfg(self, i):
        return self._cfg["axes"].get(self.axis_letters[i].lower(), {})

    def _config_to_settings(self, st):
        for i in range(self.n_axis):
            ax = self._axis_cfg(i)
            st.steps_per_mm[i] = f32(ax.get("steps_per_mm", 80.0))
            st.max_rate[i] = f32(ax.get("max_rate_mm_per_min", 1000.0))
            st.acceleration[i] = f32(ax.get("acceleration_mm_per_sec2", 25.0) * 60 * 60)
            st.max_travel[i] = -f32(ax.get("max_travel_mm", 1000.0))
        st.junction_deviation = f32(self._cfg.get("junction_deviation_mm", 0.01))
        st.arc_tolerance = f32(self._cfg.get("arc_tolerance_mm", 0.002))
        st.rpm_max, st.rpm_min = 1000.0, 0.0
        flags = 0
        if "laser" in self._cfg:
            flags |= BIT_LASER_MODE
        if self._axis_cfg(0).get("soft_limits"):
            flags |= BIT_SOFT_LIMIT
        if self._cfg.get("report_inches"):
            flags |= BIT_REPORT_INCHES
        st.flags = flags

    def _apply_config(self):
        """Re-derive every model field from the config tree (``validate`` + ``afterParse`` + ``Axes::init``)."""
        if hasattr(self, "settings"):
            self._config_to_settings(self.settings)
        self.verbose_errors = bool(self._cfg.get("verbose_errors", True))
        self.spindle_kind = "laser" if "laser" in self._cfg else "pwm" if "PWM" in self._cfg else "none"
        self._m4_supported = self.spindle_kind == "laser" or (
            self.spindle_kind == "pwm" and self._cfg["PWM"].get("direction_pin", NO_PIN) != NO_PIN)
        self.probe_configured = self._cfg.get("probe", {}).get("pin", NO_PIN) != NO_PIN
        self.flood_configured = self._cfg.get("coolant", {}).get("flood_pin", NO_PIN) != NO_PIN
        self.homing_cfg = {}
        for i in range(self.n_axis):
            h = self._axis_cfg(i).get("homing")
            if isinstance(h, dict):
                self.homing_cfg[i] = h
        if not hasattr(self, "unhomed"):
            self.unhomed = set(self.homing_cfg)
        self.machine_name = self._cfg.get("name", "")

    def config_text(self):
        """The current machine config as ``$CD`` prints it (one string)."""
        return "\n".join(generate_config_lines(self._cfg)) + "\n"

    # ------------------------------------------------------------------ settings registry
    def _build_settings(self):
        """Creation order of the modelled settings; ``Setting::List`` is the reverse (front insertion).

        Source: SettingsDefinitions.cpp:make_settings @ 9b8236f (lines 93-111) then module init (assumed) then
        SettingsDefinitions.cpp:make_proxies (lines 113-154); Settings.cpp:Setting::Setting (lines 73-78)."""
        S = _Setting
        lst = [
            S("Message/Level", None, "enum", "Info", options=LEVELS),
            S("Config/Filename", None, "str", "config.yaml", lo=1, hi=50),
            S("Report/Status", "10", "int", 1, lo=0, hi=3),
            S("SD/FallbackCS", None, "int", -1, lo=-1, hi=40),
            S("Firmware/Build", None, "str", "", lo=0, hi=20),
            S("Start/Message", None, "str", START_MESSAGE, lo=0, hi=40),
            S("GCode/Echo", None, "enum", "OFF", options=ON_OFF),
            S("Hostname", None, "str", "fluidnc", lo=1, hi=32),
            S("HTTP/Port", None, "int", 80, lo=1, hi=65001),
            S("HTTP/Enable", None, "enum", "ON" if self._http_enable else "OFF", options=ON_OFF),
            S("HTTP/BlockDuringMotion", None, "enum", "ON", options=ON_OFF),
        ]
        n = self.n_axis

        def axf(key, i):
            return lambda: self._axis_cfg(i).get(key, 0.0)

        for base, name, key in ((130, "Grbl/MaxTravel/", "max_travel_mm"), (120, "Grbl/Acceleration/", "acceleration_mm_per_sec2"),
                                (110, "Grbl/MaxRate/", "max_rate_mm_per_min"), (100, "Grbl/Resolution/", "steps_per_mm")):
            for i in reversed(range(n)):
                lst.append(S(name + self.axis_letters[i], str(base + i), "pf", getter=axf(key, i)))
        lst.append(S("Grbl/LaserMode", "32", "pi", getter=lambda: 1 if self.spindle_kind == "laser" else 0))
        lst.append(S("Grbl/MaxSpindleSpeed", "30", "pi", getter=lambda: {"laser": 1000, "pwm": 10000}.get(
            self.spindle_kind, 0)))
        lst.append(S("Grbl/HomingDirections", "23", "pi", getter=lambda: sum(
            1 << i for i, h in self.homing_cfg.items() if not h.get("positive_direction", True))))
        lst.append(S("Grbl/HomingCycleEnable", "22", "pi", getter=lambda: 1 if self.homing_cfg else 0))
        lst.append(S("Grbl/HardLimitsEnable", "21", "pi", getter=lambda: 1 if any(
            self._axis_cfg(i).get("motor0", {}).get("hard_limits") for i in range(n)) else 0))
        lst.append(S("Grbl/SoftLimitsEnable", "20", "pi", getter=lambda: 1 if self._axis_cfg(0).get("soft_limits") else 0))
        self._settings_creation = lst

    def _setting_list(self):
        return list(reversed(self._settings_creation))

    def _find_setting(self, key, by_grbl):
        for s in self._setting_list():
            name = s.grbl if by_grbl else s.name
            if name is not None and name.lower() == key.lower():
                return s
        return None

    def _set_setting(self, s, text):
        """Validate and store one setting value; returns the Error number.

        Source: Settings.cpp:IntSetting::setStringValue @ 9b8236f (lines 142-174)
        Also: StringSetting::setStringValue (lines 247-269), EnumSetting::setStringValue (lines 321-365),
        Settings.h Float/IntProxySetting (read-only, lines 225-234),
        WebUI/NetSettings.cpp:HostnameSetting::setStringValue (lines 62-70)."""
        if s.kind in ("pf", "pi"):
            return STATUS_READ_ONLY_SETTING
        if self._not_idle_or_alarm():
            return STATUS_IDLE_ERROR
        if s.kind == "int":
            f = read_number(text)
            if f is None:
                return STATUS_BAD_NUMBER_FORMAT
            v = int(f)
            if v < s.lo or v > s.hi:
                return STATUS_NUMBER_RANGE
            s.value = v
            if s.name == "Report/Status":
                self.settings.status_report_mask = v
            return STATUS_OK
        if s.kind == "enum":
            names = sorted(s.options, key=str.lower)
            hit = next((k for k in names if k.lower() == text.strip().lower()), None)
            if hit is None:
                if not text.strip():
                    self._info("Valid options: " + " ".join(names))
                    return STATUS_BAD_NUMBER_FORMAT
                f = read_number(text)
                if f is None:
                    self._info("Valid options: " + " ".join(names))
                    return STATUS_BAD_NUMBER_FORMAT
                hit = next((k for k in names if s.options[k] == int(f)), None)
                if hit is None:
                    return STATUS_BAD_NUMBER_FORMAT
            s.value = hit
            return STATUS_OK
        if s.name == "Hostname" and any(c == " " or not (c.isascii() and (c.isalnum() or c == "-")) for c in text):
            return STATUS_INVALID_VALUE
        if s.lo and s.hi and not s.lo <= len(text) <= s.hi:
            self._err("Setting length error")
            return STATUS_BAD_NUMBER_FORMAT
        s.value = text
        return STATUS_OK

    # ------------------------------------------------------------------ output primitives
    def _level(self):
        s = next(x for x in self._settings_creation if x.name == "Message/Level")
        return LEVELS[s.value]

    def _emit(self, text):
        """One channel line; ``]`` is appended when the line starts with ``[``.

        Source: Logging.cpp:LogStream::~LogStream @ 9b8236f (lines 34-39); Channel.cpp:print_msg (lines 467-472)
        with the UartChannel ``\\n`` -> ``\\r\\n`` rewrite (UartChannel.cpp:166-192)."""
        if text.startswith("["):
            text += "]"
        self._w(text + "\r\n")

    def _msg(self, text):
        """``log_msg`` (no level tag).  Source: Logging.h:log_msg @ 9b8236f (lines 77-77)."""
        self._emit("[MSG:" + text)

    def _info(self, text):
        """``log_info``.  Source: Logging.h:log_info @ 9b8236f (lines 80-80)."""
        if self._level() >= LEVELS["Info"]:
            self._emit("[MSG:INFO: " + text)

    def _err(self, text):
        """``log_error``.  Source: Logging.h:log_error @ 9b8236f (lines 82-82)."""
        if self._level() >= LEVELS["Error"]:
            self._emit("[MSG:ERR: " + text)

    # ------------------------------------------------------------------ report.cpp equivalents
    def report_init_message(self):
        """Banner and the boot/reset alarm lines.

        Source: Protocol.cpp:protocol_do_soft_restart @ 9b8236f (lines 655-706)
        Also: protocol_do_rt_reset (lines 1456-1479), protocol_do_start (lines 708-733); the banner text itself
        is ``_banner`` (Report.cpp:report_init_message, lines 137-184)."""
        self._buf.clear()
        prior = self.sys.state
        if self._critical:
            self._critical = False
            if self.unhomed:
                self._alarm(ALARM_UNHOMED)
                self.sys.state = STATE_ALARM
            else:
                self.sys.state = STATE_IDLE
        elif prior == STATE_SLEEP:
            self._alarm(3)
            self.sys.state = STATE_ALARM
        self._banner()
        if not self._booted:
            self._booted = True
            if self.unhomed:
                self._alarm(ALARM_UNHOMED)
                self.sys.state = STATE_ALARM

    def _banner(self):
        """Empty spacer line, then the ``Start/Message`` template with its escapes expanded.

        Source: Report.cpp:report_init_message @ 9b8236f (lines 137-184); template default
        SettingsDefinitions.cpp:make_settings (lines 107-108)."""
        self._emit("")
        tmpl = next(x for x in self._settings_creation if x.name == "Start/Message").value
        out, i = [], 0
        while i < len(tmpl):
            c = tmpl[i]
            i += 1
            if c != "\\":
                out.append(c)
                continue
            if i >= len(tmpl):
                break
            e = tmpl[i]
            i += 1
            out.append({"H": "'$' for help", "B": GIT_INFO, "V": GRBL_VERSION, "X": MCU + "-" + VARIANT,
                        "R": "wifi"}.get(e, e))
        self._emit("".join(out))

    def _alarm(self, code):
        """Source: Protocol.cpp:alarm_msg @ 9b8236f (lines 458-462) (the 500 ms delay is omitted); the state
        set_state(Critical) / set_state(Alarm) in protocol_do_alarm (lines 735-765)."""
        self._info("ALARM: " + ALARM_NAME.get(code, "Unknown"))
        self._emit("ALARM:%d" % code)
        if code == 1:
            self.unhomed = set(self.homing_cfg)
        self._critical = code in CRITICAL_ALARMS

    def _rt_system_once(self):
        """FluidNC has no critical-event loop: protocol_do_alarm only sets State::Critical, prints the alarm and
        returns, so the inherited gnea loop (``self.critical``) is never entered; ``_critical`` is the state, which
        reports as ``Alarm``, keeps answering commands (``$X`` is a no-op in it) and is left by a soft reset or
        by a homing cycle (``Homing::run_cycles`` sets State::Homing).  A pending extra-low rapid override (0x98)
        is applied after the inherited pass, which knows only the 100/50/25 steps.

        Source: Protocol.cpp:protocol_do_alarm @ 9b8236f (lines 735-765); Report.cpp:state_name (lines 425-449);
        Protocol.cpp:protocol_do_rapid_override (lines 1367-1374); Machine/Homing.cpp:Homing::run_cycles (line 387)."""
        super()._rt_system_once()
        if self.critical:
            self.critical = False
            self.rt_alarm = 0
        if self._rapid_extra_low:
            self._rapid_extra_low = False
            s = self.sys
            if s.r_override != RAPID_OVERRIDE_EXTRA_LOW:
                s.r_override = RAPID_OVERRIDE_EXTRA_LOW
                s.report_ovr_counter = 0

    def report_alarm_message(self, code):
        """Source: Protocol.cpp:alarm_msg @ 9b8236f (lines 458-462); Protocol.cpp:protocol_do_alarm (lines 735-765)."""
        self._alarm(code)

    def report_feedback_message(self, code):
        """``log_info`` of the FluidNC text; the critical event is an error-level message and the alarm-lock hint
        has no call site at 9b8236f.

        Source: Report.cpp:report_feedback_message @ 9b8236f (lines 123-128), report_error_message (lines 129-134);
        texts Report.cpp:MessageText (lines 99-116)."""
        if code == MESSAGE_CRITICAL_EVENT:
            self._err("Reset to continue")
        elif code == MESSAGE_ALARM_LOCK:
            return
        else:
            self._info(MESSAGE_TEXT[code])

    def report_status_message(self, code):
        """``ok`` / ``error:N`` followed by ``[MSG:ERR: <text>]`` when verbose_errors.

        Source: Channel.cpp:Channel::ack @ 9b8236f (lines 449-465); Error.cpp:ErrorNames (lines 8-93)."""
        if code == STATUS_OK:
            self._emit("ok")
            return
        self._emit("error:%d" % code)
        if self.verbose_errors and code in ERROR_TEXT:
            self._err(ERROR_TEXT[code])

    def _axis_values(self, vals):
        """Source: Report.cpp:report_util_axis_values @ 9b8236f (lines 71-97): linear axes honour report_inches
        (4 decimals), rotary A/B/C always 3."""
        inches = bool(self.settings.flags & BIT_REPORT_INCHES)
        parts = []
        for i, v in enumerate(vals):
            if inches and self.axis_letters[i] in "XYZ":
                parts.append(fmt(f32(v / f32(MM_PER_INCH)), 4))
            else:
                parts.append(fmt(v, 3))
        return ",".join(parts)

    def _pin_string(self):
        """Source: Report.cpp:report_recompute_pin_string @ 9b8236f (lines 465-489)."""
        s = ""
        if self.probe_configured and self._probe_get_state(self._probe_contact_now(), False):
            s += "P"
        for i, c in enumerate(self.axis_letters):
            if self.limit_pins & (1 << i):
                s += c
        return s

    def _wco(self):
        """Source: System.cpp:get_wco @ 9b8236f (lines 127-136)."""
        gcs = self.gc
        out = []
        for i in range(self.n_axis):
            v = f32(gcs.coord_system[i] + gcs.coord_offset[i])
            if i == 2:
                v = f32(v + gcs.tool_length_offset)
            out.append(v)
        return out

    def report_realtime_status(self):
        """Source: Report.cpp:report_realtime_status @ 9b8236f (lines 499-617); state names state_name (lines 425-463);
        wco counters Config.h:94-97."""
        s, st = self.sys, self.settings
        pos = [self._mpos_axis(self.sys_position, i) for i in range(self.n_axis)]
        out = "<" + self._state_text()
        wco = self._wco()
        if st.status_report_mask & 1:
            out += "|MPos:"
        else:
            out += "|WPos:"
            pos = [f32(pos[i] - wco[i]) for i in range(self.n_axis)]
        out += self._axis_values(pos)
        if st.status_report_mask & 2:
            out += "|Bf:%d,%d" % (BLOCK_BUFFER_SIZE - 1 - len(self.blocks), RX_BUFFER_SIZE - len(self._rx))
        busy = bool(s.state & (STATE_HOMING | STATE_CYCLE | STATE_HOLD | STATE_JOG | STATE_SAFETY_DOOR))
        rate = self.cur_speed if busy else 0.0
        if st.flags & BIT_REPORT_INCHES:
            rate = f32(rate / f32(MM_PER_INCH))
        out += "|FS:%d,%d" % (int(rate + 0.5), int(s.spindle_speed))
        pin = self._pin_string()
        if pin:
            out += "|Pn:" + pin
        if s.report_wco_counter > 0:
            s.report_wco_counter -= 1
        else:
            s.report_wco_counter = 29 if busy else 9
            if s.report_ovr_counter == 0:
                s.report_ovr_counter = 1
            out += "|WCO:" + self._axis_values(wco)
        if s.report_ovr_counter > 0:
            s.report_ovr_counter -= 1
        else:
            s.report_ovr_counter = 19 if busy else 9
            out += "|Ov:%d,%d,%d" % (s.f_override, s.r_override, s.spindle_speed_ovr)
            sp, cl = self._spindle_get_state(), self.coolant_on
            if sp or cl:
                out += "|A:" + (("S" if sp == 1 else "C") if sp else "") + ("F" if cl else "")
        self._emit(out + ">")

    def report_probe_parameters(self):
        """Source: Report.cpp:report_probe_parameters @ 9b8236f (lines 189-196)."""
        pos = [self._mpos_axis(self.sys_probe_position, i) for i in range(self.n_axis)]
        self._emit("[PRB:%s:%d" % (self._axis_values(pos), 1 if self.sys.probe_succeeded else 0))

    def report_ngc_parameters(self):
        """One ``[name:values]`` line per CoordIndex (G54-G59, G59.1-G59.3, G28, G30, G92, TLO), no PRB line.

        Source: Report.cpp:report_ngc_parameters @ 9b8236f (lines 212-216)
        Also: report_ngc_coord (lines 202-211); CoordIndex order GCode.h:240-257 (G59.1-.3 exist because
        MAX_N_AXIS > U/V/W, SettingsDefinitions.cpp:69-91)."""
        rows = [("G%d" % (54 + i), self.eeprom_coord[i]) for i in range(6)]
        rows += [("G59.%d" % k, self._extra_coord_zero) for k in (1, 2, 3)]
        rows += [("G28", self.eeprom_coord[6]), ("G30", self.eeprom_coord[7]), ("G92", self.gc.coord_offset)]
        tlo = [0.0] * self.n_axis
        if self.n_axis > 2:
            tlo[2] = self.gc.tool_length_offset
        rows.append(("TLO", tlo))
        for name, vals in rows:
            self._emit("[%s:%s" % (name, self._axis_values(vals)))

    def report_gcode_modes(self):
        """Source: Report.cpp:report_gcode_modes @ 9b8236f (lines 219-360) (F is not converted to inches there,
        only its decimals change)."""
        m, gcs = self.gc.modal, self.gc
        mo = m.motion
        word = {80: "G80", 0: "G0", 1: "G1", 2: "G2", 3: "G3", 140: "G38.2", 141: "G38.3", 142: "G38.4",
                143: "G38.5"}[mo]
        parts = [word, "G%d" % (54 + m.coord_select), "G%d" % (17 + m.plane_select), "G%d" % (21 - m.units),
                 "G%d" % (90 + m.distance), "G%d" % (94 - m.feed_rate)]
        flow = {0: "", 3: "M0", 2: "M2", 30: "M30"}.get(m.program_flow, "")
        if flow:
            parts.append(flow)
        parts.append("M3" if m.spindle == PL_COND_SPINDLE_CW else "M4" if m.spindle == PL_COND_SPINDLE_CCW else "M5")
        parts.append("M8" if m.coolant else "M9")
        parts.append("T%d" % gcs.tool)
        parts.append("F" + fmt(gcs.feed_rate, 1 if self.settings.flags & BIT_REPORT_INCHES else 0))
        parts.append("S%d" % c_fix_u32(gcs.spindle_speed))       # Report.cpp:358 std::to_string(uint32_t(speed))
        self._emit("[GC:" + " ".join(parts))

    def report_build_info(self, line):
        """Source: Report.cpp:report_build_info @ 9b8236f (lines 363-404): ``[OPT:PHS]`` is the option string for
        a config with no mist, no bluetooth, no parking override and Config.h:148-163 defaults."""
        self._emit("[VER:%s FluidNC %s (%s-%s) :%s" % (GRBL_VERSION, GIT_INFO, MCU, VARIANT, line))
        self._emit("[OPT:PHS")
        self._emit("[CLUSTER:16")
        self._msg("Machine: " + self.machine_name)

    def report_grbl_help(self):
        """Source: ProcessSettings.cpp:show_help @ 9b8236f (lines 185-188) (a bare line, no brackets)."""
        self._emit("HLP:$$ $+ $# $S $L $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H $F $E=err ~ ! ? ctrl-x")

    def report_grbl_settings(self):
        """``$13`` first, then every GRBL-type setting that has a Grbl number, in list order.

        Source: ProcessSettings.cpp:report_normal_settings @ 9b8236f (lines 205-208)
        Also: show_settings (lines 195-203), switchInchMM (lines 778-786), show_setting (lines 132-139)."""
        self._emit("$13=%d" % (1 if self.settings.flags & BIT_REPORT_INCHES else 0))
        for s in self._setting_list():
            if s.grbl is not None:
                self._emit("$%s=%s" % (s.grbl, uri_encode_grbl(s.string())))

    # ------------------------------------------------------------------ realtime bytes
    def _isr_realtime(self, c):
        """``?`` reports at once (not via a flag); bytes >= 0x80 go through the UTF-8 decoder first (0x80-0xBE
        pass raw); only a Jog state honours 0x85.

        Source: Channel.cpp:Channel::pollLine @ 9b8236f (lines 375-425), handleRealtimeCharacter (lines 321-365);
        RealtimeCmd.cpp:execute_realtime_command (lines 10-102); UTF8.cpp:UTF8::decode (lines 13-72)."""
        cmd = self._utf8.decode(c)
        if cmd is None or cmd >= 0x100:
            return
        if cmd == 0x3F:
            self.report_realtime_status()
        elif cmd == 0x98:
            self._rapid_extra_low = True        # RealtimeCmd.cpp:63-64: rapidOverrideEvent ExtraLow (5%)
        elif cmd in (0x86, 0x87, 0x88, 0x89, 0x8A, 0xA1):
            return          # debug report / macros (none configured) / mist toggle: no effect here
        elif cmd == 0xA0 and not self.flood_configured:
            return
        else:
            super()._isr_realtime(cmd)

    # ------------------------------------------------------------------ line handling
    def _protocol_main_loop(self):
        """Console line assembly (every CR and every LF completes a line, so CRLF is a line then an empty line that
        is acked; backspace edits; 254 chars kept) and the execute/ack cycle.

        Source: Protocol.cpp:protocol_main_loop @ 9b8236f (lines 464-602); UartChannel.cpp:UartChannel::lineComplete
        (lines 210-219) -> lineedit.cpp:Lineedit::step (lines 381-407, non-editing mode); Channel.cpp:Channel::pollLine
        (lines 375-421); ProcessSettings.cpp:execute_line (lines 1340-1389)."""
        s = self.sys
        s.state = STATE_ALARM if s.state & (STATE_ALARM | STATE_SLEEP) else STATE_IDLE
        rx = self._rx
        while True:
            while rx:
                m = re.search(rb"[\r\n]", rx)
                if m is None:
                    self._accumulate(bytes(rx))
                    rx.clear()
                    break
                seg = bytes(rx[:m.start()])
                del rx[:m.end()]
                self._accumulate(seg)
                raw = self._buf.decode("latin-1")
                self._buf.clear()
                self._rt_system()
                if self.critical or s.suspend:
                    yield from self._rt_block()
                if s.abort:
                    return
                status = yield from self._execute_line(raw)
                if not s.abort:         # an aborted line gets no reply (Protocol.cpp:566-571)
                    self.report_status_message(status)
                if self._restart_pending:
                    self._restart_pending = False
                    self._full_restart()
                    rx.clear()
                    self._rt_system()
                    if s.abort:
                        return
            self._auto_cycle_start()
            self._rt_system()
            if self.critical or s.suspend:
                yield from self._rt_block()
            if s.abort:
                return
            self._auto_report()
            yield

    def _accumulate(self, seg):
        """Source: lineedit.cpp:Lineedit::addchar / erase_char @ 9b8236f (lines 22-54, the line holds at most
        Channel::maxLine - 1 = 254 characters, the rest is dropped); a backspace is applied without the echo the
        console's editing mode would send."""
        if b"\b" in seg:
            for c in seg:
                if c == 8:
                    del self._buf[-1:]
                elif len(self._buf) < 254:
                    self._buf.append(c)
        else:
            room = 254 - len(self._buf)
            if room > 0:
                self._buf += seg[:room]

    def _execute_line(self, raw):
        """Source: ProcessSettings.cpp:execute_line @ 9b8236f (lines 1340-1389) (on-protocol-task branch)."""
        line = raw.lstrip(" \t\n\r\v\f")
        if not line:
            return STATUS_OK
        if line[0] in "$[":
            return (yield from self._settings_execute_line(line))
        if self.sys.state & (STATE_ALARM | STATE_JOG):
            return STATUS_SYSTEM_GC_LOCK
        status = yield from self._gcode_execute(line)
        if status != STATUS_OK:
            self._err("Bad GCode: " + line)
        return status

    def _gcode_execute(self, line):
        """Source: GCode.cpp:gc_execute_line @ 9b8236f (lines 274-2075): 127-character limit on the raw line,
        collapse, then the inherited gnea parser/executor."""
        if len(line) > 127:
            return STATUS_LINE_LENGTH_EXCEEDED
        cleaned = collapse_gcode(line, self._comment_msg)
        if not cleaned:
            return STATUS_OK
        status = self._gc_check(cleaned)
        if status is None:
            status = yield from self._gc_run()
        return status

    def _gc_check(self, line):
        """GCode.cpp allows a mantissa only on M7 and M8 (lines 681-684); M7.1 / M8.1 take the M7 / M8 branch
        unchanged (lines 731-745) and any other mantissa there is error:20, while the inherited gnea parser answers
        error:23 for a mantissa on any M word.  Those two words are normalised before it parses, in place, so
        every other error keeps its position in the line.

        Source: GCode.cpp:gc_execute_line @ 9b8236f (lines 681-684, 731-745)."""
        i, n, parts, last = (3 if line[:1] == "$" else 0), len(line), [], 0
        while i < n and "A" <= line[i] <= "Z":
            ok, value, j = read_float(line, i + 1)
            if not ok:
                break
            if line[i] == "M":
                iv, mantissa = _word_parts(value)
                if iv in (7, 8) and mantissa > 0:
                    parts += [line[last:i], "M%d" % iv if mantissa == 10 else "M99"]
                    last = j
            i = j
        return super()._gc_check("".join(parts) + line[last:])

    def _comment_msg(self, comment):
        """Source: GCode.cpp:gcode_comment_msg @ 9b8236f (lines 108-151) (the MSG form only)."""
        if "MSG" in comment:
            self._info("MSG," + comment[4:])

    # ------------------------------------------------------------------ $ commands
    def _not_idle_or_alarm(self):
        """Source: Settings.cpp:notIdleOrAlarm @ 9b8236f (lines 48-51)."""
        return self.sys.state not in (STATE_IDLE, STATE_ALARM, STATE_SAFETY_DOOR)

    def _not_idle_or_jog(self):
        """Source: Settings.cpp:notIdleOrJog @ 9b8236f (lines 45-47)."""
        return self.sys.state not in (STATE_IDLE, STATE_JOG)

    def _allow_config_states(self):
        """Source: Settings.cpp:allowConfigStates @ 9b8236f (lines 56-58)."""
        return self.sys.state not in (STATE_IDLE, STATE_ALARM)

    # (grbl name, full name, handler, state guard, kind): kind 'user' -> error:8 when guarded,
    # 'web' -> error:120; 'drain' commands wait for the planner first (UserCommand, Settings.h:418-433).
    def _command_table(self):
        none = None
        return [
            ("", "Help", self._c_help, none, "report"),
            ("$", "GrblSettings/List", self._c_grbl_settings, none, "report"),
            ("S", "Settings/List", self._c_settings_list, none, "report"),
            ("CD", "Config/Dump", self._c_config_dump, none, "report"),
            ("#", "GCode/Offsets", self._c_offsets, none, "report"),
            ("G", "GCode/Modes", self._c_modes, none, "report"),
            ("I", "Build/Info", self._c_build_info, none, "report"),
            ("RI", "Report/Interval", self._c_report_interval, none, "report"),
            ("13", "Report/Inches", self._c_inches, self._not_idle_or_alarm, "report"),
            ("GS", "GRBL/Show", self._c_show, none, "report"),
            ("X", "Alarm/Disable", self._c_unlock, none, "drain"),
            ("C", "GCode/Check", self._c_check, none, "drain"),
            ("SLP", "System/Sleep", self._c_sleep, self._not_idle_or_alarm, "drain"),
            ("H", "Home", self._c_home, self._allow_config_states, "drain"),
        ] + [("H" + a, "Home/" + a, (lambda v, a=a: self._c_home_axis(a)), self._allow_config_states, "drain")
             for a in "XYZABC"] + [
            ("J", "Jog", self._c_jog, self._not_idle_or_jog, "async"),
            (None, "LocalFS/Delete", self._c_localfs_delete, self._allow_config_states, "web"),
            (None, "Bye", self._c_bye, none, "web"),
        ]

    def _settings_execute_line(self, line):
        """Source: ProcessSettings.cpp:settings_execute_line @ 9b8236f (lines 1277-1290) and
        do_command_or_setting (lines 1154-1275): commands, then config paths, then settings by name, then by
        Grbl number, then a wildcard display, else error:3."""
        if line[0] == "[":
            return STATUS_INVALID_STATEMENT          # [ESPnnn] webui commands are not modelled
        key, _, value = line[1:].partition("=")
        key = key.strip()
        for grbl, full, handler, guard, kind in self._command_table():
            if (grbl is not None and grbl.lower() == key.lower()) or full.lower() == key.lower():
                if guard and guard():
                    return STATUS_ANOTHER_INTERFACE_BUSY if kind == "web" else STATUS_IDLE_ERROR
                if kind == "drain":
                    yield from self._buffer_sync()
                res = handler(value)
                if hasattr(res, "send"):
                    res = yield from res
                return res
        handled, status = self._runtime_setting(key, value)
        if handled:
            return status
        for by_grbl in (False, True):
            s = self._find_setting(key, by_grbl)
            if s is not None:
                if value == "":
                    self._emit("$%s=%s" % (s.grbl if by_grbl else s.name, uri_encode_grbl(s.string())))
                    return STATUS_OK
                return self._set_setting(s, urldecode(value, self._err))
        if value == "":
            hits = [s for s in self._setting_list() if regex_match(key, s.name, False)]
            for s in hits:
                self._emit("$%s=%s" % (s.name, uri_encode_grbl(s.string())))
            if hits:
                return STATUS_OK
        return STATUS_INVALID_STATEMENT

    # ---- report commands
    def _c_help(self, value):
        """``$``.  Source: ProcessSettings.cpp:show_help @ 9b8236f (lines 185-188)."""
        self.report_grbl_help()
        return STATUS_OK

    def _c_grbl_settings(self, value):
        """``$$``.  Source: ProcessSettings.cpp:report_normal_settings @ 9b8236f (lines 205-208)."""
        self.report_grbl_settings()
        return STATUS_OK

    def _c_settings_list(self, value):
        """Source: ProcessSettings.cpp:list_settings @ 9b8236f (lines 220-228)."""
        for s in self._setting_list():
            self._emit("$%s=%s" % (s.name, uri_encode_grbl(s.string())))
        return STATUS_OK

    def _c_config_dump(self, value):
        """``$CD`` prints the config; ``$CD=<file>`` writes it to LocalFS and prints nothing.

        Source: ProcessSettings.cpp:dump_config @ 9b8236f (lines 748-770)."""
        text_lines = generate_config_lines(self._cfg)
        if value:
            self._localfs[canon_path(value)] = "\n".join(text_lines) + "\n"
            return STATUS_OK
        for ln in text_lines:
            self._emit(ln)
        return STATUS_OK

    def _c_offsets(self, value):
        """``$#``.  Source: ProcessSettings.cpp:report_ngc @ 9b8236f (lines 335-338)."""
        self.report_ngc_parameters()
        return STATUS_OK

    def _c_modes(self, value):
        """``$G``.  Source: ProcessSettings.cpp:report_gcode @ 9b8236f (lines 190-193)."""
        self.report_gcode_modes()
        return STATUS_OK

    def _c_build_info(self, value):
        """Source: ProcessSettings.cpp:get_report_build_info @ 9b8236f (lines 580-586)."""
        if value:
            return STATUS_INVALID_STATEMENT
        build = next(x for x in self._settings_creation if x.name == "Firmware/Build").value
        self.report_build_info(build)
        return STATUS_OK

    def _c_show(self, value):
        """Source: ProcessSettings.cpp:report_init_message_cmd @ 9b8236f (lines 772-776)."""
        self._banner()
        return STATUS_OK

    def _c_inches(self, value):
        """Source: ProcessSettings.cpp:switchInchMM @ 9b8236f (lines 778-786)."""
        if value:
            self._cfg["report_inches"] = value[0] == "1"
            self._apply_config()
        else:
            self._emit("$13=%d" % (1 if self.settings.flags & BIT_REPORT_INCHES else 0))
        return STATUS_OK

    def _c_report_interval(self, value):
        """Source: ProcessSettings.cpp:setReportInterval @ 9b8236f (lines 1009-1038); Channel.cpp:setReportInterval
        (lines 242-251): at least 50 ms; the next auto report carries WCO and Ov and a fresh [GC:] line."""
        if value:
            if not re.fullmatch(r"\d+", value.strip()):
                return STATUS_BAD_NUMBER_FORMAT
            ms = int(value)
            actual = max(ms, 50) if ms else 0
            self._rep_interval = actual
            self._rep_next = self.time
            self._rep_last_gc = None
            if actual:
                self._info("%s auto report interval set to %d ms" % (CHANNEL_NAME, actual))
            else:
                self._info("%s auto reporting turned off" % CHANNEL_NAME)
            self._rep_wco = self._rep_ovr = True
            return STATUS_OK
        if self._rep_interval:
            self._info("%s auto report interval is %d ms" % (CHANNEL_NAME, self._rep_interval))
        else:
            self._info("%s auto reporting is off" % CHANNEL_NAME)
        return STATUS_OK

    # ---- action commands
    def _c_unlock(self, value):
        """Source: ProcessSettings.cpp:disable_alarm_lock @ 9b8236f (lines 309-334): a no-op unless in Alarm
        (State::Critical is not State::Alarm)."""
        if self.sys.state != STATE_ALARM or self._critical:
            return STATUS_OK
        self.unhomed = set()
        self.report_feedback_message(MESSAGE_ALARM_UNLOCK)
        self.sys.state = STATE_IDLE
        return STATUS_OK

    def _c_check(self, value):
        """Source: ProcessSettings.cpp:toggle_check_mode @ 9b8236f (lines 257-277)."""
        if self.sys.state == STATE_CHECK_MODE:
            self.report_feedback_message(MESSAGE_DISABLED)
            self._mc_reset()
            return STATUS_OK
        if self.sys.state != STATE_IDLE:
            return STATUS_IDLE_ERROR
        self.sys.state = STATE_CHECK_MODE
        self.report_feedback_message(MESSAGE_ENABLED)
        return STATUS_OK

    def _c_sleep(self, value):
        """Source: ProcessSettings.cpp:go_to_sleep @ 9b8236f (lines 576-579)."""
        self.rt_state |= EXEC_SLEEP
        return STATUS_OK

    def _c_home(self, value):
        """``$H`` / ``$H<cycles>`` / ``$H<axis names>``.

        Source: ProcessSettings.cpp:home_all @ 9b8236f (lines 473-514), home (lines 435-472)."""
        if value:
            if value.isdigit():
                for ch in value:
                    axes = [i for i, h in self.homing_cfg.items() if h.get("cycle") == int(ch)]
                    if not axes:
                        self._err("No axes for homing cycle " + ch)
                        return STATUS_INVALID_VALUE
                for ch in value:
                    status = self._home([i for i, h in self.homing_cfg.items() if h.get("cycle") == int(ch)], True)
                    if status != STATUS_OK:
                        return status
                return STATUS_OK
            idx = [self.axis_letters.find(c) for c in value.upper()]
            if not idx or any(i < 0 for i in idx):
                return STATUS_INVALID_VALUE
            return self._home(sorted(set(idx)), True)
        return self._home(None, False)

    def _c_home_axis(self, letter):
        """``$HX`` ... ``$HC``.  Source: ProcessSettings.cpp:home @ 9b8236f (lines 435-472) via home_x..home_c."""
        return self._home([self.axis_letters.find(letter)], True) if letter in self.axis_letters else \
            STATUS_INVALID_STATEMENT

    def _home(self, axes, single):
        """Source: ProcessSettings.cpp:home @ 9b8236f (lines 435-472); Machine/Homing.cpp:Homing::run_cycles
        (lines 333-389), set_mpos (lines 286-314), done (lines 205-227).  Homing completes instantly."""
        if single:
            for i in axes:
                h = self.homing_cfg.get(i)
                if not (h and h.get("allow_single_axis", True)):
                    return STATUS_SINGLE_AXIS_HOMING
        if not self.homing_cfg:
            return STATUS_SETTING_DISABLED
        if self.sys.abort:
            return STATUS_OK            # a reset arrived first: Homing::done returns without completing the cycle
        if axes is None:
            cycles = sorted({h["cycle"] for h in self.homing_cfg.values() if h.get("cycle", 0) > 0})
            groups = [[i for i, h in self.homing_cfg.items() if h.get("cycle") == c] for c in cycles]
        else:
            groups = [[i for i in axes if i in self.homing_cfg]]
        for grp in groups:
            names = ""
            for i in sorted(grp):
                h = self.homing_cfg[i]
                self.sys_position[i] = c_round(f32(f32(h.get("mpos_mm", 0.0)) * self.settings.steps_per_mm[i]))
                self.unhomed.discard(i)
                names += self.axis_letters[i]
            self._msg("Homed:" + names)
        self._gc_sync_position()
        self._plan_sync_position()
        self.sys.state = STATE_ALARM if self.unhomed else STATE_IDLE
        self._critical = False
        return STATUS_OK

    def _c_jog(self, value):
        """Source: ProcessSettings.cpp:doJog @ 9b8236f (lines 616-633) -> GCode.cpp:gc_execute_line (jog form)."""
        if not value:
            return STATUS_INVALID_STATEMENT
        if len("$J=" + value) > 127:
            return STATUS_LINE_LENGTH_EXCEEDED
        cleaned = collapse_gcode("$J=" + value, self._comment_msg)
        status = self._gc_check(cleaned)
        if status is None:
            status = yield from self._gc_run()
        return status

    def _mc_line(self, target, pl, trip=None, mm=None):
        """A line whose target violates a soft limit is rejected: ``mc_linear`` returns before the planner sees it,
        so it is never queued.  Jogs are constrained instead (``_jog_execute``).

        Source: MotionControl.cpp:mc_linear @ 9b8236f (lines 117-124); Cartesian.cpp:invalid_line (lines 262-272)."""
        if self.settings.flags & BIT_SOFT_LIMIT and self.sys.state != STATE_JOG and self._check_travel_limits(target):
            yield from self._limit_error()
            return
        yield from super()._mc_line(target, pl, trip, mm)

    def _limit_error(self):
        """Soft-limit violation: everything already queued runs out first (a hold is only for a cycle still
        active after that), then ``mc_critical`` raises ALARM:2.  The planner is empty and the steppers are idle
        when Critical is entered, and the rejected line was never queued, so Critical answers commands and a
        soft reset has no queued motion to finish.

        Source: Limit.cpp:limit_error (lines 114-134); MotionControl.cpp:mc_critical (lines 450-456)."""
        s = self.sys
        s.soft_limit = True
        yield from self._buffer_sync()
        if s.abort:
            return
        self.rt_alarm = EXEC_ALARM_SOFT_LIMIT
        self._rt_system()

    def _jog_execute(self, pl, blk):
        """Jog targets are constrained to the soft limits, never refused (error:15 is a gnea behaviour).

        Source: Jog.cpp:jog_execute @ 9b8236f (lines 14-30) via Kinematics::constrain_jog."""
        if self.settings.flags & BIT_SOFT_LIMIT:
            for i in range(self.n_axis):
                blk.xyz[i] = min(0.0, max(self.settings.max_travel[i], blk.xyz[i]))
        return (yield from super()._jog_execute(pl, blk))

    def _c_localfs_delete(self, value):
        """Source: FileCommands.cpp:deleteObject @ 9b8236f (lines 289-314), deleteLocalFile (lines 320-322).
        ``std::filesystem::remove`` of a missing path throws (stdfs/std-ops.cpp:fs::remove, lines 904-922:
        ``ec`` keeps ENOENT), so a missing file is error:69 (Failed to delete file), not 65."""
        if not value or value == "/":
            self._err("Will not delete everything")
            return STATUS_INVALID_VALUE
        path = canon_path(value)
        victims = [p for p in self._localfs if p == path or p.startswith(path + "/")]
        if not victims:
            self._err("filesystem error: cannot remove: No such file or directory [%s]" % path)
            return STATUS_FS_FAILED_DEL_FILE
        for p in victims:
            del self._localfs[p]
        return STATUS_OK

    def _c_bye(self, value):
        """Source: FileCommands.cpp:restart @ 9b8236f (lines 692-696): the restart follows the ``ok``."""
        self._info("Restarting")
        self._restart_pending = True
        return STATUS_OK

    def _full_restart(self):
        """MCU reboot: runtime state is lost, the config reloads from LocalFS, NVS settings and coordinate
        offsets persist.  Source: FileCommands.cpp:restart / Protocol.cpp:fullResetEvent (line 1517)."""
        name = next(x for x in self._settings_creation if x.name == "Config/Filename").value
        text = self._localfs.get(canon_path(name))
        if text is None:
            self._cfg = _default_config(self.axis_letters, self._homing_arg, self._probe_arg, self._flood_arg,
                                        self._spindle_arg, self._machine_name)
        else:
            self._cfg = parse_config_text(text, self._template)
        del self.unhomed
        self._apply_config()
        self.sys_position = [0] * self.n_axis
        self._booted = False
        self._critical = False
        self._rep_interval = 0
        self._rep_last_gc = None
        self.sys.state = STATE_IDLE
        self.rt_state |= EXEC_RESET

    # ---- runtime config edits ($/path=value)
    def _runtime_setting(self, key, value):
        """``$/path[=value]`` edits and displays of the config tree; returns (handled, status).

        Source: Configuration/RuntimeSetting.cpp:RuntimeSetting::item @ 9b8236f (lines 66-200)
        Also: ProcessSettings.cpp:do_command_or_setting (lines 1190-1226)."""
        path = key
        if path.startswith("/"):
            path = path[1:]
        if path.endswith("/"):
            path = path[:-1]
        if not path:
            return False, STATUS_OK
        node, parent_key = self._cfg, None
        parts = path.split("/")
        for n, part in enumerate(parts):
            hit = next((k for k in node if k.lower() == part.lower()), None) if isinstance(node, dict) else None
            if hit is None:
                return False, STATUS_OK
            parent, parent_key, node = node, hit, node[hit]
            if not isinstance(node, dict) and n < len(parts) - 1:
                return False, STATUS_OK
        if isinstance(node, dict):
            if value == "":
                self._emit("/" + path + ":")
                for ln in generate_config_lines(node, 1):
                    self._emit(ln)
                return True, STATUS_OK
            self._err("Can't set a value on a section")
            return False, STATUS_OK
        prefix = "$/%s=" % path
        if value == "":
            if isinstance(node, bool):
                shown = "true" if node else "false"
            elif isinstance(node, float):
                shown = fmt(node, 3)
            else:
                shown = str(node)
            self._emit(prefix + shown)
            return True, STATUS_OK
        if isinstance(node, bool):
            low = value.lower()
            if low in ("true", "yes", "on"):
                parent[parent_key] = True
            elif low in ("false", "no", "off"):
                parent[parent_key] = False
            else:
                f = read_number(value)
                if f is None:
                    self._err("Bad numeric value")
                else:
                    parent[parent_key] = bool(f)
        elif isinstance(node, (int, float)):
            f = read_number(value)
            if f is None:
                self._err("Bad numeric value")
            else:
                parent[parent_key] = f32(f) if isinstance(node, float) else int(f)
        else:
            parent[parent_key] = value
        self._apply_config()
        return True, STATUS_OK

    # ------------------------------------------------------------------ spindle / coolant / probe
    def _coolant_for_m(self, iv):
        """M7/M8 only take effect when the matching pin is configured (flood only here).

        Source: GCode.cpp:gc_execute_line @ 9b8236f (lines 274-2075); the M7/M8/M9 case is lines 728-760."""
        return PL_COND_COOLANT_FLOOD if iv == 8 and self.flood_configured else 0

    def _spindle_compute_pwm(self, rpm):
        """The status speed is the commanded S (x override % for a PWM/laser spindle); the NoSpindle shows the
        raw S.  Source: Spindles/Spindle.cpp:Spindle::mapSpeed @ 9b8236f (lines 164-167), Null::setState."""
        s = self.sys
        if self.spindle_kind in ("laser", "pwm"):
            s.spindle_speed = (c_fix_u32(rpm) * s.spindle_speed_ovr & 0xFFFFFFFF) // 100     # uint32_t arithmetic
        else:
            s.spindle_speed = c_fix_u32(rpm)
        return 1

    def _spindle_set_state(self, state, rpm):
        if self.sys.abort:
            return
        super()._spindle_set_state(state, rpm)
        self._fn_spindle = state
        self._fn_rpm = rpm if state else 0.0

    def _spindle_stop(self):
        super()._spindle_stop()
        self._fn_spindle = 0
        self._fn_rpm = 0.0

    # ------------------------------------------------------------------ parking (Parking.cpp)
    def _park_params(self):
        """``(axis, target_mpos, rate, pullout_distance, pullout_rate)`` from the ``parking:`` config section.

        Source: Parking.cpp:Parking::group @ 9b8236f (lines 176-217)."""
        p = self._cfg.get("parking", {})
        letter = str(p.get("axis", "Z")).upper()
        axis = self.axis_letters.index(letter) if letter in self.axis_letters else 2
        return (axis, float(p.get("target_mpos_mm", -5.0)), float(p.get("rate_mm_per_min", 800.0)),
                float(p.get("pullout_distance_mm", 5.0)), float(p.get("pullout_rate_mm_per_min", 250.0)))

    def _park_can_park(self):
        """Enabled, not a laser (``isRateAdjusted``), the parking axis has a homing cycle configured.

        Source: Parking.cpp:Parking::can_park @ 9b8236f (lines 35-56); M56 (override control) not modelled."""
        p = self._cfg.get("parking", {})
        axis = self._park_params()[0]
        return bool(p.get("enable")) and self.spindle_kind != "laser" and axis in self.homing_cfg

    def _spindle_delay_ms(self, key, rpm):
        """``spinup_ms`` / ``spindown_ms`` of the spindle, proportional to the speed change below the map maximum.

        Source: Spindles/Spindle.cpp:Spindle::spindleDelay @ 9b8236f (lines 214-286); only the PWM spindle has the
        settings (Spindle.h:181-195 groupDelaySettings, called from OnOff/PWM/HBridge/VFD group())."""
        sec = self._cfg.get("PWM")
        ms = int(sec.get(key, 0)) if sec else 0
        top = self._getter_max_speed()
        return int(ms * rpm / top) if ms and rpm < top else ms

    def _getter_max_speed(self):
        return {"laser": 1000, "pwm": 10000}.get(self.spindle_kind, 0)

    def _door_dwell_ms(self, ms):
        """Source: Spindles/Spindle.cpp:Spindle::spindleDelay @ 9b8236f (lines 272-282): ``dwell_ms(.., SysSuspend)``."""
        if ms:
            yield from self._delay_sec(ms / 1000.0, True)

    def _door_spin_down(self):
        """Source: Parking.cpp:Parking::park @ 9b8236f (line 91 ``spindle->spinDown()``)."""
        rpm = self._fn_rpm
        self._spindle_set_state(0, 0.0)
        yield from self._door_dwell_ms(self._spindle_delay_ms("spindown_ms", rpm) if rpm else 0)

    def _door_spindle_delay(self, rpm):
        """Source: Parking.cpp:Parking::restore_spindle @ 9b8236f (lines 144-146) -> ``Spindle::setState``."""
        yield from self._door_dwell_ms(self._spindle_delay_ms("spinup_ms", rpm))

    def _door_coolant_delay(self):
        """No coolant delay on the door path.  Source: Parking.cpp:Parking::restore_coolant @ 9b8236f (lines 148-150)."""
        return
        yield

    def _spindle_get_state(self):
        """Spindle state is what was commanded (``_current_state``), independent of the PWM duty.

        Source: Spindles/NullSpindle.cpp:Null::setState @ 9b8236f (lines 22-25)."""
        if not self._fn_spindle:
            return 0
        return 2 if self._fn_spindle == PL_COND_SPINDLE_CCW else 1

    def _mc_probe_cycle(self, target, pl, flags):
        """Source: MotionControl.cpp:mc_probe_cycle @ 9b8236f (lines 344-434): no probe pin -> an error message
        and ``ok``."""
        if not self.probe_configured:
            self._err("Probe pin is not configured")
            return GC_UPDATE_POS_NONE
        return (yield from super()._mc_probe_cycle(target, pl, flags))

    def _flag_wco_change(self):
        """Source: GCode.cpp:gc_wco_changed @ 9b8236f (lines 259-264) (``allChannels.notifyWco``)."""
        yield from super()._flag_wco_change()
        self._rep_wco = True

    # ------------------------------------------------------------------ auto reports
    def tick(self, dt=None):
        """Advance virtual time, run the motion model, then the channel auto-report poll."""
        super().tick(dt)
        self._auto_report()

    def _auto_report(self):
        """Unsolicited ``<...>`` reports every N ms (idle included on a UART channel), immediately on a state
        change, a pin change or a WCO/Ov notification, plus a ``[GC:...]`` line when the modal state changed.

        Source: Channel.cpp:Channel::autoReport @ 9b8236f (lines 276-303), autoReportGCodeState (lines 256-275)."""
        if not self._rep_interval:
            return
        s = self.sys
        name = self._state_text()
        if name != self._rep_last_state:
            self._rep_last_state = name
            self._rep_state = True
        pin = self._pin_string()
        due = self.time + 1e-9 >= self._rep_next
        if self._rep_ovr or self._rep_wco or self._rep_state or pin != self._rep_last_pin or due:
            self._rep_state = False
            if self._rep_ovr:
                s.report_ovr_counter = 0
                self._rep_ovr = False
            if self._rep_wco:
                s.report_wco_counter = 0
                self._rep_wco = False
            self._rep_last_pin = pin
            self._rep_next = self.time + self._rep_interval / 1000.0
            self.report_realtime_status()
        motion = bool(s.state & (STATE_CYCLE | STATE_HOMING | STATE_JOG))
        m = self.gc.modal
        modal = (m.motion, m.feed_rate, m.units, m.distance, m.plane_select, m.tool_length, m.coord_select,
                 m.program_flow, m.coolant, m.spindle, self.gc.tool)
        speeds = (self.gc.spindle_speed, self.gc.feed_rate)
        last = self._rep_last_gc
        if last is None:
            changed = True
        else:
            compare = (last[0][0],) + modal[1:] if motion else modal      # motion-mode-only changes are muted
            changed = compare != last[0] or (not motion and speeds != last[1])
        if changed:
            self.report_gcode_modes()
            self._rep_last_gc = (modal, speeds)


def regex_match(regexp, text, case_sensitive):
    """FluidNC's setting-name matcher: the pattern is searched anywhere in ``text``; ``^`` anchors a leading
    pattern to the start, a final ``$`` to the end, ``*`` matches any run of characters and every other character
    is literal.

    Source: Regexpr.cpp:regexMatch / matchHere / matchStar @ 9b8236f (lines 28-71)."""
    if not case_sensitive:
        regexp, text = regexp.lower(), text.lower()

    def match_here(rx, i):
        while True:
            if not rx:
                return True
            if rx[0] == "*":
                return any(match_here(rx[1:], j) for j in range(i, len(text) + 1))
            if rx == "$":
                return i == len(text)
            if i < len(text) and rx[0] == text[i]:
                rx, i = rx[1:], i + 1
                continue
            return False

    if regexp.startswith("^"):
        return match_here(regexp[1:], 0)
    return any(match_here(regexp, i) for i in range(len(text) + 1))


def urldecode(text, on_error=None):
    """``%HH`` escapes; a ``%`` with fewer than two characters after it, or with non-hex ones, ends the text
    decoded so far and is reported through ``on_error`` (the firmware's ``log_error``).

    Source: ProcessSettings.cpp:uriDecode @ 9b8236f (lines 105-128)."""
    out, i = [], 0
    while i < len(text):
        c = text[i]
        i += 1
        if c == "%":
            esc = text[i:i + 2]
            if len(esc) < 2:
                if on_error:
                    on_error("Bad % encoding - too short")
                break
            if not re.fullmatch(r"[0-9A-Fa-f]{2}", esc):
                if on_error:
                    on_error("Bad % encoding - not hex")
                break
            i += 2
            c = chr(int(esc, 16))
        out.append(c)
    return "".join(out)
