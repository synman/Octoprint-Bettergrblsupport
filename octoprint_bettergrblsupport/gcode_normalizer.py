# coding=utf-8
"""Normalise a Grbl G-code file into plain absolute G0/G1 moves for OctoPrint's built-in G-code viewer.

The viewer assumes absolute moves, treats ``G91.1`` as ``G91`` and ignores arcs in every plane but XY.
This module runs a miniature Grbl 1.1h ``gc_execute_line`` over the file and writes the places Grbl would
move to as absolute ``G0``/``G1`` lines.  Cutting moves carry a relative ``E`` value, because that is what
the viewer reads as "extrusion".  The stored file is never changed.

Pure standard library, Python 3.7+, no OctoPrint import.  See ``tests/viewer/test_gcode_normalizer.py``.

Frame: machine coordinates, assuming every value stored in Grbl's EEPROM is zero (G54-G59 data, G28/G30
positions) and a G92 offset of zero at the start of the file.  Power-on modal state is assumed:
G0 G54 G17 G21 G90 G94 M5 M9, F0 S0, position 0,0,0.

Deliberate deviations from the firmware (the oracle is ``tests/simulator/grbl_virtual.py``):
  * error 22 (undefined feed rate, including the G93 rule) is not raised;
  * an R-arc whose chord has no length in the arc plane is rejected with error 33 (the firmware divides by 0).

Arithmetic: every value that decides where Grbl goes (word values, targets, offsets, arc radius checks) is
rounded to IEEE binary32 at the same steps as the firmware, so a long G91 file accumulates the same rounding
drift Grbl itself accumulates (a 300 000-move relative file ends 0.15 mm off its ideal geometry on the real
machine, and here).  Arc point generation and the output text use doubles.
"""
import codecs
import math
import re
import struct
import types

VERSION = 5

_TWO_PI = 2.0 * math.pi
_MM_PER_INCH = 25.4

# --- line assembly (protocol.c:protocol_main_loop, as ported in grbl_virtual._assemble) ------------------
_COMMENT = re.compile(r"\([^)]*(?:\)|$)|;.*")
_ASSEMBLE = {c: None for c in range(0x21)}
_ASSEMBLE[0x2F] = None
_ASSEMBLE.update({c: c - 32 for c in range(97, 123)})
# realtime bytes are taken off the serial stream before line assembly, and Grbl's RX path drops bytes >= 0x80
_ASSEMBLE.update({ord(c): None for c in "?!~\x18"})
_NON_ASCII = re.compile(r"[^\x00-\x7f]+")
_MAX_BLOCK = 79

_WORD = re.compile(r"([A-Z])([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))?", re.ASCII)
_SPINDLE_WORD = re.compile(r"M\+?0*[34](?:\.0*)?(?![0-9.])", re.ASCII)

# --- modal groups / word bits (gcode.h) ------------------------------------------------------------------
(MG_G0, MG_G1, MG_G2, MG_G3, MG_G4, MG_G5, MG_G6, MG_G7, MG_G8, MG_G12, MG_G13, MG_M4, MG_M7, MG_M8) = range(14)
_VBIT = {"F": 1, "I": 2, "J": 4, "K": 8, "L": 16, "N": 32, "P": 64, "R": 128, "S": 256, "T": 512,
         "X": 1024, "Y": 2048, "Z": 4096}
_W_AXES = 1024 | 2048 | 4096
_AXIS = {"X": 0, "Y": 1, "Z": 2}
_IJK = {"I": 0, "J": 1, "K": 2}

SEEK, LINEAR, CW_ARC, CCW_ARC, NONE = 0, 1, 2, 3, 80

NM_DWELL, NM_SET_COORD_DATA, NM_GO_HOME_0, NM_SET_HOME_0, NM_GO_HOME_1, NM_SET_HOME_1 = 4, 10, 28, 38, 30, 40
NM_ABS_OVERRIDE, NM_SET_COORD_OFFSET, NM_RESET_COORD_OFFSET = 53, 92, 102
PAUSED = 3
G28_SLOT, G30_SLOT = 6, 7


_PACK = struct.Struct("<f")


class OutputTooLarge(Exception):
    """The normalised copy passed the byte cap the caller set: an arc-heavy file expands about 570x."""


def _f32(x):
    """Round to IEEE binary32 (avr-gcc float)."""
    try:
        return _PACK.unpack(_PACK.pack(x))[0]
    except OverflowError:
        return math.copysign(math.inf, x)


def _c_round(x):
    """C round(): half away from zero."""
    return int(math.copysign(math.floor(abs(x) + 0.5), x))


def _word_parts(value):
    """gcode.c ``int_value = trunc(value); mantissa = round(100*(value - int_value))`` (as in the oracle)."""
    if value > 1e9:
        return 0xFF, 0xFFFF
    iv = (int(value) & 0xFF) if value >= 0 else 0
    return iv, _c_round(100.0 * (value - iv)) & 0xFFFF


def assemble(text):
    """Grbl line assembly: drop ``(...)`` and ``;`` comments, characters <= 0x20 and ``/``, upper-case
    ASCII letters.  Returns None when the block is longer than 79 characters (error 11)."""
    if "(" in text or ";" in text:
        text = _COMMENT.sub("", text)
    if not text.isascii():
        text = _NON_ASCII.sub("", text)
    block = text.translate(_ASSEMBLE)
    if len(block) > _MAX_BLOCK:
        return None
    return block


def iter_source_lines(src_path):
    """Yield ``(text, orig_end_offset)`` counting bytes exactly as OctoPrint does for ``filepos``:
    ``utf-8-sig``, ``errors="replace"``, ``newline=""``, ``len(line.encode("utf-8"))`` per line, BOM added."""
    with open(src_path, "rb") as fh:
        offset = len(codecs.BOM_UTF8) if fh.read(3) == codecs.BOM_UTF8 else 0
    with open(src_path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        while True:
            line = fh.readline()
            if not line:
                return
            offset += len(line.encode("utf-8"))
            yield line.rstrip("\r\n"), offset


class _Modal(object):
    __slots__ = ("motion", "feed_mode", "units", "distance", "plane", "tool_length", "coord_select",
                 "program_flow", "coolant", "spindle")

    def __init__(self):
        self.motion = SEEK
        self.feed_mode = 0      # 0 = G94, 1 = G93
        self.units = 0          # 0 = G21, 1 = G20
        self.distance = 0       # 0 = G90, 1 = G91
        self.plane = 0
        self.tool_length = 0
        self.coord_select = 0
        self.program_flow = 0
        self.coolant = 0
        self.spindle = 0        # 0, 3 (CW), 4 (CCW)

    def copy(self):
        m = _Modal.__new__(_Modal)
        for name in _Modal.__slots__:
            setattr(m, name, getattr(self, name))
        return m


class _Block(object):
    __slots__ = ("nmc", "modal", "f", "s", "p", "r", "l", "ijk", "xyz", "flags", "axis_command", "axis_words",
                 "command_words", "coord_select", "block_cs", "a0", "a1", "al", "cw", "jog")


class _Interpreter(object):
    def __init__(self, policy, arc_tol, max_arc_segments):
        self.policy = policy
        self.arc_tol = arc_tol
        self.max_arc = max_arc_segments
        self.modal = _Modal()
        self.shift = (0.0, 0.0)  # added to drawn X/Y only, to place the job on the viewer's bed
        self.bounds = None       # [min x, min y, max x, max y] of drawn cuts, shift applied
        self.cut_z = set()       # distinct Z heights of drawn cuts (capped), for the viewer's layer count
        self.pos = [0.0, 0.0, 0.0]
        self.drawn = [0.0, 0.0, 0.0]
        self.cs = [0.0, 0.0, 0.0]
        self.g92 = [0.0, 0.0, 0.0]
        self.tlo = 0.0
        self.eeprom = [[0.0, 0.0, 0.0] for _ in range(8)]
        self.feed = 0.0
        self.spindle_speed = 0.0
        self.last_f = None
        self.emitted = []
        self.suppressed = False
        self.error = 0
        self.stats = {"lines": 0, "rejected": {}, "rejected_examples": [], "suppressed": 0, "cuts": 0,
                      "arc_cap_hits": 0, "nonfinite_skipped": 0, "out_bytes": 0, "policy": policy}

    # ------------------------------------------------------------------ output
    def _draw(self, target, cut):
        """Emit one move to ``target``.  A segment is measured from the last point actually drawn, because that
        is where the viewer draws it from (moves that are not drawn leave it where it was)."""
        if not all(math.isfinite(v) for v in target):
            # a word past binary32 range became inf (the firmware reads it the same way and then alarms): there
            # is nothing to draw, and "Xinf" would put a non-number in the stream and in the map bounds
            self.stats["nonfinite_skipped"] += 1
            return
        start = self.drawn
        self.drawn = list(target)
        if cut:
            e = math.sqrt((target[0] - start[0]) ** 2 + (target[1] - start[1]) ** 2 + (target[2] - start[2]) ** 2)
            x, y = target[0] + self.shift[0], target[1] + self.shift[1]
            s = "G1 X%.3f Y%.3f Z%.3f E%.4f" % (x, y, target[2], max(round(e, 4), 0.0001))
            if len(self.cut_z) < 1000:
                self.cut_z.add(round(target[2], 3))
            b = self.bounds
            if b is None:
                self.bounds = [min(x, start[0] + self.shift[0]), min(y, start[1] + self.shift[1]),
                               max(x, start[0] + self.shift[0]), max(y, start[1] + self.shift[1])]
            else:
                b[0], b[1], b[2], b[3] = min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y)
            if self.feed > 0:
                f = "%.1f" % self.feed
                if f != self.last_f:
                    self.last_f = f
                    s += " F" + f
            self.stats["cuts"] += 1
        else:
            s = "G0 X%.3f Y%.3f Z%.3f" % (target[0] + self.shift[0], target[1] + self.shift[1], target[2])
        self.emitted.append(s.replace("-0.000", "0.000") if "-0.000" in s else s)

    def _bridge(self):
        """After a move that was not drawn (G53, G28, G30, G38), travel to where Grbl really is before the next
        drawn move, so that move does not start from the stale last-drawn point."""
        if self.drawn != self.pos:
            self._draw(self.pos, False)

    def _note(self, text):
        self.emitted.append("; bgs: " + text)

    def _cuts(self, motion):
        if motion not in (LINEAR, CW_ARC, CCW_ARC):
            return False
        if self.policy == "all_feed":
            return True
        return self.modal.spindle != 0 and self.spindle_speed > 0

    # ------------------------------------------------------------------ one source line
    def line(self, text, line_no):
        """Interpret one source line.  Returns the list of emitted output lines (no terminators)."""
        self.emitted = []
        self.suppressed = False
        self.error = 0
        self.stats["lines"] += 1
        block = assemble(text)
        if block is None:
            err = 11
        elif not block or block[0] == "%":
            err = 0 if not block else 1
        elif block[0] == "$":
            err = self._dollar(block)
        else:
            err = self._gcode(block, False)
        if err:
            self.error = err
            rej = self.stats["rejected"]
            rej[err] = rej.get(err, 0) + 1
            if len(self.stats["rejected_examples"]) < 20:
                self.stats["rejected_examples"].append((line_no, err))
            shown = "".join(c if " " <= c <= "~" else "?" for c in text[:60])
            self._note("error:%d %s" % (err, shown))
        if self.suppressed:
            self.stats["suppressed"] += 1
        return self.emitted

    def _dollar(self, block):
        if block[:2] == "$J":
            if block[2:3] != "=":
                return 3
            return self._gcode(block, True)
        if block == "$H":
            self.suppressed = True
            self._note("$H not drawn")
        return 0

    def _gcode(self, block, jog):
        err, blk = self._check(block, jog)
        if err:
            return err
        return self._run(blk)

    # ------------------------------------------------------------------ parse + check (gc_execute_line 1-3)
    def _check(self, line, jog):
        gcs = self.modal
        blk = _Block()
        blk.modal = m = gcs.copy()
        blk.nmc = 0
        blk.f = blk.s = blk.p = blk.r = 0.0
        blk.l = 0
        blk.jog = jog
        blk.xyz = xyz = [0.0, 0.0, 0.0]
        blk.ijk = ijk = [0.0, 0.0, 0.0]
        axis_command = axis_words = ijk_words = command_words = value_words = 0
        has_f = False
        n_value = 0
        if jog:
            m.motion = LINEAR
            m.feed_mode = 0
        i = 3 if jog else 0
        n = len(line)
        while i < n:
            mt = _WORD.match(line, i)
            if mt is None:
                return 1, None
            if mt.group(2) is None:
                return 2, None
            letter = mt.group(1)
            value = _f32(float(mt.group(2)))
            i = mt.end()
            iv, mantissa = _word_parts(value)
            if letter == "G":
                if iv in (10, 28, 30, 92):
                    if mantissa == 0:
                        if axis_command:
                            return 24, None
                        axis_command = 1
                    wb = MG_G0
                    blk.nmc = iv
                    if iv != 10:
                        if mantissa not in (0, 10):
                            return 20, None
                        blk.nmc += mantissa
                        mantissa = 0
                elif iv in (4, 53):
                    wb = MG_G0
                    blk.nmc = iv
                elif iv in (0, 1, 2, 3, 38, 80):
                    if iv != 80:
                        if axis_command:
                            return 24, None
                        axis_command = 2
                    wb = MG_G1
                    m.motion = iv
                    if iv == 38:
                        if mantissa not in (20, 30, 40, 50):
                            return 20, None
                        m.motion += mantissa // 10 + 100
                        mantissa = 0
                elif iv in (17, 18, 19):
                    wb = MG_G2
                    m.plane = iv - 17
                elif iv in (90, 91):
                    if mantissa == 0:
                        wb = MG_G3
                        m.distance = iv - 90
                    else:
                        wb = MG_G4
                        if mantissa != 10 or iv == 90:
                            return 20, None
                        mantissa = 0
                elif iv in (93, 94):
                    wb = MG_G5
                    m.feed_mode = 94 - iv
                elif iv in (20, 21):
                    wb = MG_G6
                    m.units = 21 - iv
                elif iv == 40:
                    wb = MG_G7
                elif iv in (43, 49):
                    wb = MG_G8
                    if axis_command:
                        return 24, None
                    axis_command = 3
                    if iv == 49:
                        m.tool_length = 0
                    elif mantissa == 10:
                        m.tool_length = 1
                    else:
                        return 20, None
                    mantissa = 0
                elif 54 <= iv <= 59:
                    wb = MG_G12
                    m.coord_select = iv - 54
                elif iv == 61:
                    wb = MG_G13
                    if mantissa != 0:
                        return 20, None
                else:
                    return 20, None
                if mantissa > 0:
                    return 23, None
                if command_words & (1 << wb):
                    return 21, None
                command_words |= 1 << wb
            elif letter == "M":
                if mantissa > 0:
                    return 23, None
                if iv in (0, 1, 2, 30):
                    wb = MG_M4
                    if iv == 0:
                        m.program_flow = PAUSED
                    elif iv != 1:
                        m.program_flow = iv
                elif iv in (3, 4, 5):
                    wb = MG_M7
                    m.spindle = iv if iv != 5 else 0
                elif iv in (8, 9):
                    wb = MG_M8
                    m.coolant = 1 if iv == 8 else 0
                else:
                    return 20, None
                if command_words & (1 << wb):
                    return 21, None
                command_words |= 1 << wb
            else:
                if letter == "F":
                    blk.f = value
                    has_f = True
                elif letter in _IJK:
                    ijk[_IJK[letter]] = value
                    ijk_words |= 1 << _IJK[letter]
                elif letter == "L":
                    blk.l = iv
                elif letter == "N":
                    n_value = int(value)
                elif letter == "P":
                    blk.p = value
                elif letter == "R":
                    blk.r = value
                elif letter == "S":
                    blk.s = value
                elif letter == "T":
                    if value > 255:
                        return 38, None
                elif letter in _AXIS:
                    xyz[_AXIS[letter]] = value
                    axis_words |= 1 << _AXIS[letter]
                else:
                    return 20, None
                bit = _VBIT[letter]
                if value_words & bit:
                    return 25, None
                if letter in "FNPTS" and value < 0.0:
                    return 4, None
                value_words |= bit
        # ---- step 3
        if axis_words and not axis_command:
            axis_command = 2
        if value_words & _VBIT["N"] and n_value > 10000000:
            return 27, None
        if jog:
            if has_f and m.units == 1:
                blk.f = _f32(blk.f * _MM_PER_INCH)
        elif m.feed_mode == 0 and has_f:
            blk.f = _f32(blk.f * _MM_PER_INCH) if m.units == 1 else blk.f
        if not value_words & _VBIT["S"]:
            blk.s = self.spindle_speed
        if blk.nmc == NM_DWELL:
            if not value_words & _VBIT["P"]:
                return 28, None
            value_words &= ~_VBIT["P"]
        if m.plane == 0:
            a0, a1, al = 0, 1, 2
        elif m.plane == 1:
            a0, a1, al = 2, 0, 1
        else:
            a0, a1, al = 1, 2, 0
        if m.units == 1:
            for idx in range(3):
                if axis_words & (1 << idx):
                    xyz[idx] = _f32(xyz[idx] * _MM_PER_INCH)
        if axis_command == 3 and m.tool_length == 1 and axis_words ^ 4:
            return 37, None
        block_cs = list(self.cs)
        if command_words & (1 << MG_G12) and gcs.coord_select != m.coord_select:
            block_cs = list(self.eeprom[m.coord_select])
        coord_select = 0
        pos = self.pos
        nmc = blk.nmc
        if nmc == NM_SET_COORD_DATA:
            if not axis_words:
                return 26, None
            if not value_words & (_VBIT["P"] | _VBIT["L"]):
                return 28, None
            coord_select = (int(blk.p) & 0xFF) if blk.p >= 0 else 0
            if coord_select > 6:
                return 29, None
            if blk.l != 20:
                if blk.l == 2:
                    if value_words & _VBIT["R"]:
                        return 20, None
                else:
                    return 20, None
            value_words &= ~(_VBIT["L"] | _VBIT["P"])
            if coord_select > 0:
                coord_select -= 1
            else:
                coord_select = m.coord_select
            blk.ijk = ijk = list(self.eeprom[coord_select])
            for idx in range(3):
                if axis_words & (1 << idx):
                    if blk.l == 20:
                        v = _f32(_f32(pos[idx] - self.g92[idx]) - xyz[idx])
                        if idx == 2:
                            v = _f32(v - self.tlo)
                        ijk[idx] = v
                    else:
                        ijk[idx] = xyz[idx]
        elif nmc == NM_SET_COORD_OFFSET:
            if not axis_words:
                return 26, None
            for idx in range(3):
                if axis_words & (1 << idx):
                    v = _f32(_f32(pos[idx] - block_cs[idx]) - xyz[idx])
                    if idx == 2:
                        v = _f32(v - self.tlo)
                    xyz[idx] = v
                else:
                    xyz[idx] = self.g92[idx]
        else:
            if axis_command != 3 and axis_words:
                for idx in range(3):
                    if not axis_words & (1 << idx):
                        xyz[idx] = pos[idx]
                    elif nmc != NM_ABS_OVERRIDE:
                        if m.distance == 0:
                            xyz[idx] = _f32(xyz[idx] + _f32(block_cs[idx] + self.g92[idx]))
                            if idx == 2:
                                xyz[idx] = _f32(xyz[idx] + self.tlo)
                        else:
                            xyz[idx] = _f32(xyz[idx] + pos[idx])
            if nmc in (NM_GO_HOME_0, NM_GO_HOME_1):
                blk.ijk = ijk = list(self.eeprom[G28_SLOT if nmc == NM_GO_HOME_0 else G30_SLOT])
                if axis_words:
                    for idx in range(3):
                        if not axis_words & (1 << idx):
                            ijk[idx] = pos[idx]
                else:
                    axis_command = 0
            elif nmc == NM_ABS_OVERRIDE:
                if not (m.motion == SEEK or m.motion == LINEAR):
                    return 30, None
        cw = False
        if m.motion == NONE:
            if axis_words:
                return 31, None
        elif axis_command == 2:
            if m.motion == SEEK:
                if not axis_words:
                    axis_command = 0
            elif m.motion == LINEAR:
                if not axis_words:
                    axis_command = 0
            elif m.motion in (CW_ARC, CCW_ARC):
                cw = m.motion == CW_ARC
                if not axis_words:
                    return 26, None
                if not axis_words & ((1 << a0) | (1 << a1)):
                    return 32, None
                dx = _f32(xyz[a0] - pos[a0])
                dy = _f32(xyz[a1] - pos[a1])
                if value_words & _VBIT["R"]:
                    value_words &= ~_VBIT["R"]
                    if pos == xyz:
                        return 33, None
                    if m.units == 1:
                        blk.r = _f32(blk.r * _MM_PER_INCH)
                    h = _f32(_f32(_f32(4.0 * _f32(blk.r * blk.r)) - _f32(dx * dx)) - _f32(dy * dy))
                    if h < 0:
                        return 34, None
                    chord = _f32(math.sqrt(_f32(_f32(dx * dx) + _f32(dy * dy))))
                    if chord == 0.0:
                        return 33, None
                    h = _f32(-math.sqrt(h) / chord)
                    if m.motion == CCW_ARC:
                        h = -h
                    if blk.r < 0:
                        h = -h
                        blk.r = -blk.r
                    ijk[a0] = _f32(0.5 * _f32(dx - _f32(dy * h)))
                    ijk[a1] = _f32(0.5 * _f32(dy + _f32(dx * h)))
                else:
                    if not ijk_words & ((1 << a0) | (1 << a1)):
                        return 35, None
                    value_words &= ~(_VBIT["I"] | _VBIT["J"] | _VBIT["K"])
                    if m.units == 1:
                        for idx in range(3):
                            if ijk_words & (1 << idx):
                                ijk[idx] = _f32(ijk[idx] * _MM_PER_INCH)
                    dx = _f32(dx - ijk[a0])
                    dy = _f32(dy - ijk[a1])
                    target_r = _f32(math.sqrt(_f32(_f32(dx * dx) + _f32(dy * dy))))
                    blk.r = _f32(math.sqrt(_f32(_f32(ijk[a0] * ijk[a0]) + _f32(ijk[a1] * ijk[a1]))))
                    delta_r = abs(_f32(target_r - blk.r))
                    if delta_r > 0.005:
                        if delta_r > 0.5:
                            return 33, None
                        if delta_r > _f32(0.001 * blk.r):
                            return 33, None
            else:
                if not axis_words:
                    return 26, None
                if pos == xyz:
                    return 33, None
        if jog:
            value_words &= ~(_VBIT["N"] | _VBIT["F"])
        else:
            value_words &= ~(_VBIT["N"] | _VBIT["F"] | _VBIT["S"] | _VBIT["T"])
        if axis_command:
            value_words &= ~_W_AXES
        if value_words:
            return 36, None
        blk.axis_command, blk.axis_words, blk.command_words = axis_command, axis_words, command_words
        blk.coord_select, blk.block_cs, blk.a0, blk.a1, blk.al, blk.cw = coord_select, block_cs, a0, a1, al, cw
        return 0, blk

    # ------------------------------------------------------------------ execute (gc_execute_line 4)
    def _run(self, blk):
        gcs = self.modal
        m = blk.modal
        axis_command = blk.axis_command
        if blk.jog:
            if blk.command_words & ~((1 << MG_G3) | (1 << MG_G6) | (1 << MG_G0)):
                return 16
            if not (blk.nmc == NM_ABS_OVERRIDE or blk.nmc == 0):
                return 16
            self._travel(blk.xyz)
            return 0
        if m.feed_mode == 0 and blk.f:
            self.feed = blk.f
        self.spindle_speed = blk.s
        gcs.spindle = m.spindle
        gcs.coolant = m.coolant
        gcs.plane = m.plane
        gcs.units = m.units
        gcs.feed_mode = m.feed_mode
        if axis_command == 3:
            gcs.tool_length = m.tool_length
            if gcs.tool_length == 0:
                blk.xyz[2] = 0.0
            self.tlo = blk.xyz[2]
        if gcs.coord_select != m.coord_select:
            gcs.coord_select = m.coord_select
            self.cs = list(blk.block_cs)
        gcs.distance = m.distance
        nmc = blk.nmc
        if nmc == NM_SET_COORD_DATA:
            self.eeprom[blk.coord_select] = list(blk.ijk)
            if gcs.coord_select == blk.coord_select:
                self.cs = list(blk.ijk)
        elif nmc in (NM_GO_HOME_0, NM_GO_HOME_1):
            self.suppressed = True
            self._note("G28/G30 not drawn")
            self.pos = list(blk.ijk)
        elif nmc == NM_SET_HOME_0:
            self.eeprom[G28_SLOT] = list(self.pos)
        elif nmc == NM_SET_HOME_1:
            self.eeprom[G30_SLOT] = list(self.pos)
        elif nmc == NM_SET_COORD_OFFSET:
            self.g92 = list(blk.xyz)
        elif nmc == NM_RESET_COORD_OFFSET:
            self.g92 = [0.0, 0.0, 0.0]
        gcs.motion = m.motion
        if gcs.motion != NONE and axis_command == 2:
            mo = gcs.motion
            if mo == SEEK:
                self._line_move(blk, False)
            elif mo == LINEAR:
                self._line_move(blk, self._cuts(LINEAR))
            elif mo in (CW_ARC, CCW_ARC):
                self._arc(blk, self._cuts(mo))
                self.pos = list(blk.xyz)
            else:
                self.suppressed = True
                self._note("G38 not drawn")
        gcs.program_flow = m.program_flow
        if gcs.program_flow:
            if gcs.program_flow != PAUSED:
                gcs.motion, gcs.plane, gcs.distance, gcs.feed_mode = LINEAR, 0, 0, 0
                gcs.coord_select, gcs.spindle, gcs.coolant = 0, 0, 0
                self.cs = list(self.eeprom[0])
            gcs.program_flow = 0
        return 0

    def _travel(self, target):
        """A jog: drawn as a travel move."""
        if target != self.pos:
            self._bridge()
            self._draw(target, False)
        self.pos = list(target)

    def _line_move(self, blk, cut):
        target = blk.xyz
        if blk.nmc == NM_ABS_OVERRIDE:
            self.suppressed = True
            self._note("G53 move not drawn")
        elif target != self.pos:
            self._bridge()
            self._draw(target, cut)
        self.pos = list(target)

    def _arc(self, blk, cut):
        self._bridge()
        pos, target, off = self.pos, blk.xyz, blk.ijk
        a0, a1, al = blk.a0, blk.a1, blk.al
        cx, cy = pos[a0] + off[a0], pos[a1] + off[a1]
        r0, r1 = -off[a0], -off[a1]
        rt0, rt1 = target[a0] - cx, target[a1] - cy
        ang = math.atan2(r0 * rt1 - r1 * rt0, r0 * rt0 + r1 * rt1)
        if blk.cw:
            if ang >= -5e-7:
                ang -= _TWO_PI
        elif ang <= 5e-7:
            ang += _TWO_PI
        radius = math.hypot(r0, r1)
        if radius > self.arc_tol:
            n = max(1, int(math.ceil(abs(ang) / (2.0 * math.acos(1.0 - self.arc_tol / radius)))))
        else:
            n = 1
        if n > self.max_arc:
            n = self.max_arc
            self.stats["arc_cap_hits"] += 1
        lin0, dlin = pos[al], target[al] - pos[al]
        for k in range(1, n):
            phi = ang * k / n
            c, s = math.cos(phi), math.sin(phi)
            p = [0.0, 0.0, 0.0]
            p[a0] = cx + r0 * c - r1 * s
            p[a1] = cy + r0 * s + r1 * c
            p[al] = lin0 + dlin * k / n
            self._draw(p, cut)
        self._draw(target, cut)


# ---------------------------------------------------------------------------------------------------------
def _has_spindle_word(text):
    block = assemble(text)
    return block is not None and "M" in block and _SPINDLE_WORD.search(block) is not None


def normalize_lines(lines_iter, out_fp, cut_policy="spindle", map_cap=65536, arc_tol=0.002,
                    max_arc_segments=2000, line_count=None, trace=None, shift=(0.0, 0.0), max_out_bytes=None):
    """Normalise ``lines_iter`` (yielding ``(text, orig_end_offset)``) into ``out_fp`` (binary, ASCII only).

    ``line_count`` is the number of source lines when known; it sets the map decimation
    ``k = ceil(line_count / map_cap)`` (None: keep every line).  ``trace`` is called after each source line as
    ``trace(line_no, orig_end_offset, state, emitted_lines)`` where ``state`` has ``position`` (a copy),
    ``error`` (0 or the Grbl error code) and ``suppressed``.
    ``max_out_bytes`` (None: no cap) raises ``OutputTooLarge`` as soon as the output passes it, so a runaway
    expansion stops early instead of after the whole file is written.
    Returns ``{"map": {...}, "stats": {...}}``."""
    it = _Interpreter(cut_policy, arc_tol, max_arc_segments)
    it.shift = (float(shift[0]), float(shift[1]))
    header = "; bgs-normalized v%d policy=%s\nG21\nG90\nM83\n" % (VERSION, cut_policy)
    nbytes = len(header)
    out_fp.write(header.encode("ascii"))
    pending, pending_len = [], 0
    stride = 1 if not line_count else max(1, -(-line_count // map_cap))
    orig, norm = [], []
    last_pair = None
    last_end = 0
    line_no = 0
    for text, end in lines_iter:
        line_no += 1
        emitted = it.line(text, line_no)
        if emitted:
            chunk = "\n".join(emitted) + "\n"
            nbytes += len(chunk)
            if max_out_bytes is not None and nbytes > max_out_bytes:
                raise OutputTooLarge("normalised output passed %d bytes at source line %d" % (max_out_bytes, line_no))
            pending.append(chunk)
            pending_len += len(chunk)
            if pending_len > 65536:
                out_fp.write("".join(pending).encode("ascii"))
                pending, pending_len = [], 0
        last_end = end
        last_pair = (end, nbytes)
        if line_no % stride == 0:
            orig.append(end)
            norm.append(nbytes)
            last_pair = None
        if trace is not None:
            trace(line_no, end, types.SimpleNamespace(position=list(it.pos), error=it.error,
                                                      suppressed=it.suppressed), emitted)
    if pending:
        out_fp.write("".join(pending).encode("ascii"))
    if last_pair is not None:
        orig.append(last_pair[0])
        norm.append(last_pair[1])
    it.stats["out_bytes"] = nbytes
    bounds = [round(v, 3) for v in it.bounds] if it.bounds else None
    return {"map": {"version": 1, "orig_size": last_end, "norm_size": nbytes, "orig": orig, "norm": norm,
                    "bounds": bounds, "layers": len(it.cut_z)},
            "stats": it.stats}


def normalize_file(src_path, out_fp, map_cap=65536, arc_tol=0.002, max_arc_segments=2000, shift=(0.0, 0.0),
                   max_out_bytes=None):
    """Normalise the file at ``src_path`` into ``out_fp`` in two passes (constant memory apart from the map).

    Pass 1 counts lines and picks the cut policy: ``"all_feed"`` when no line carries an M3 or M4 word,
    else ``"spindle"``.  Pass 2 streams the output."""
    count = 0
    spindle = False
    for text, _ in iter_source_lines(src_path):
        count += 1
        if not spindle and _has_spindle_word(text):
            spindle = True
    return normalize_lines(iter_source_lines(src_path), out_fp, "spindle" if spindle else "all_feed",
                           map_cap=map_cap, arc_tol=arc_tol, max_arc_segments=max_arc_segments,
                           line_count=count, shift=shift, max_out_bytes=max_out_bytes)
