# coding=utf-8
"""Oracle tests for ``octoprint_bettergrblsupport/gcode_normalizer.py``.

Every source line goes through two interpreters in lockstep: the Grbl firmware port
(``tests/simulator/grbl_virtual.py``, ``GrblVirtual(instant=True)``, the authority) and the normaliser.
Expected values come from the oracle or from maths worked out by hand in the comments, never from running
the normaliser.

    cd tests/viewer
    BGS_VIEWER_CORPUS=<dir of .gcode files> PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_gcode_normalizer -v

Without ``BGS_VIEWER_CORPUS`` the corpus tests are skipped; the fixture tests always run.
``BGS_NORMALIZER_DIR`` points the import at another copy of ``gcode_normalizer.py`` (used to prove the
oracle tests go red when a transformation is broken).
"""
import io
import math
import os
import re
import sys
import time
import tracemalloc
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tests", "simulator"))
sys.path.insert(0, os.environ.get("BGS_NORMALIZER_DIR") or os.path.join(ROOT, "octoprint_bettergrblsupport"))

import gcode_normalizer as gn  # noqa: E402
from grbl_virtual import GrblVirtual, PL_COND_NO_FEED_OVR, PL_COND_RAPID, PL_COND_SPINDLE_MASK  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
CORPUS = os.environ.get("BGS_VIEWER_CORPUS")
TOL = 0.002          # mm, spec 4.1
ARC_TOL = 0.002      # mm, Grbl $12 default
MAX_ARC = 2000
UTF8_BOM = b"\xef\xbb\xbf"

TOTALS = {"files": 0, "lines": 0, "accepted": 0, "rejected": {}, "moves": 0, "cuts": 0, "arcs": 0,
          "arc_segments": 0, "suppressed": 0, "src_bytes": 0, "out_bytes": 0, "lockstep_s": 0.0,
          "dev22": 0, "ratios": {}}


# ---------------------------------------------------------------------------------------------------------
# independent reference helpers (written from the firmware source / OctoPrint's loop, not from the module)
def ref_lines(path):
    """OctoPrint's own filepos loop: (text, byte offset after the line)."""
    with open(path, "rb") as fh:
        off = len(UTF8_BOM) if fh.read(3) == UTF8_BOM else 0
    out = []
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        for line in fh:
            off += len(line.encode("utf-8"))
            out.append((line.rstrip("\r\n"), off))
    return out


def ref_assemble(text):
    """protocol.c line assembly written as the byte loop it is.  None = overflow (error 11)."""
    out = []
    paren = semi = False
    for c in text.encode("utf-8"):
        if c >= 0x80 or c in b"?!~\x18":   # removed by the RX ISR before line assembly sees them
            continue
        if semi:
            break
        if paren:
            if c == 41:
                paren = False
            continue
        if c <= 32 or c == 47:
            continue
        if c == 40:
            paren = True
        elif c == 59:
            semi = True
        elif len(out) >= 79:
            return None
        else:
            out.append(c - 32 if 97 <= c <= 122 else c)
    return bytes(out).decode("latin-1")


def ref_policy(texts):
    """all_feed when no (assembled) line has an M3 or M4 word."""
    for t in texts:
        b = ref_assemble(t)
        if b and any(float(v) in (3.0, 4.0) for v in re.findall(r"M([+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+))", b)):
            return "spindle"
    return "all_feed"


def translate(m, pos):
    """The JS ``translate`` of the design spec section 3, on byte positions instead of percentages."""
    a, b = m["orig"], m["norm"]
    hi = len(a) - 1
    if hi < 0:
        return pos
    if pos <= a[0]:
        return b[0] * (pos / a[0] if a[0] else 0)
    if pos >= a[hi]:
        return b[hi]
    lo = 0
    while hi - lo > 1:
        mid = (lo + hi) >> 1
        if a[mid] <= pos:
            lo = mid
        else:
            hi = mid
    f = (pos - a[lo]) / (a[hi] - a[lo])
    return b[lo] + f * (b[hi] - b[lo])


_MOVE = re.compile(r"^G([01]) X(-?\d+\.\d{3}) Y(-?\d+\.\d{3}) Z(-?\d+\.\d{3})(?: E(\d+\.\d{4}))?(?: F(\d+\.\d))?$")


def parse_move(s):
    m = _MOVE.match(s)
    if m is None:
        return None
    mv = {"g": int(m.group(1)), "p": [float(m.group(2)), float(m.group(3)), float(m.group(4))],
          "e": float(m.group(5)) if m.group(5) else None, "f": float(m.group(6)) if m.group(6) else None}
    if (mv["g"] == 1) != (mv["e"] is not None) or (mv["g"] == 0 and mv["f"] is not None):
        return None
    return mv


# ---------------------------------------------------------------------------------------------------------
class Oracle(object):
    """GrblVirtual(instant=True) driven one line at a time, recording every planned motion."""

    def __init__(self):
        g = self.g = GrblVirtual(instant=True)
        g.drain()
        self.events = []
        self._in_arc = False
        orig_line, orig_arc = g._mc_line, g._mc_arc

        def mc_line(target, pl, trip=None, mm=None):
            if not self._in_arc:
                self.events.append({"kind": "line", "target": list(target), "cond": pl.condition,
                                    "spd": pl.spindle_speed})
            yield from orig_line(target, pl, trip, mm)

        def mc_arc(target, pl, position, offset, radius, axis_0, axis_1, axis_linear, clockwise):
            self.events.append({"kind": "arc", "target": list(target), "cond": pl.condition,
                                "spd": pl.spindle_speed, "pos": list(position), "off": list(offset),
                                "r": radius, "a0": axis_0, "a1": axis_1, "al": axis_linear, "cw": clockwise})
            self._in_arc = True
            try:
                yield from orig_arc(target, pl, position, offset, radius, axis_0, axis_1, axis_linear, clockwise)
            finally:
                self._in_arc = False

        g._mc_line, g._mc_arc = mc_line, mc_arc
        # the spec's priming line: neutralises error 22, which the normaliser deliberately does not raise
        assert self.run_line("G94 F1000")[0] == b"ok"

    def run_line(self, text):
        g = self.g
        self.events = []
        # realtime bytes are picked off the stream by the RX ISR, so they never reach line assembly
        raw = bytes(c for c in text.encode("utf-8") if c < 0x80 and c not in b"?!~\x18")
        g.feed(raw + b"\n")
        replies = []
        for _ in range(50):
            g.tick(0)
            replies += [r for r in g.drain() if not r.startswith(b"[MSG:")]
            if replies:
                break
            if g.sys.state & 16:        # M0 pause: cycle start
                g.feed(b"~")
        if len(replies) != 1:
            raise AssertionError("expected one reply for %r, got %r" % (text[:60], replies))
        if replies[0].startswith(b"ALARM"):
            raise AssertionError("oracle alarm on %r: %r" % (text[:60], replies[0]))
        return replies[0].strip(), self.events


class Result(object):
    pass


def _close(a, b, tol):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def run_lockstep(path, keep=False, max_mismatch=25):
    """Run ``path`` through the oracle and the normaliser side by side; returns a Result."""
    ref = ref_lines(path)
    texts = [t for t, _ in ref]
    policy = ref_policy(texts)
    oracle = Oracle()
    res = Result()
    res.path, res.policy, res.n_lines = path, policy, len(ref)
    res.ref = ref
    res.mismatches, res.dev22, res.by_line = [], [], {}
    res.accepted = res.moves = res.cut_moves = res.arcs = res.arc_segments = 0
    res.rejected = {}
    res.expected_suppressed = res.suppressed = 0
    res.bbox = None
    res.z_levels = set()
    res.arc_lines = 0
    st = {"prev_pos": [0.0, 0.0, 0.0], "drawn": [0.0, 0.0, 0.0], "last_f": None, "f_set": False, "cum": 0}
    cum_lines = []

    def bad(line_no, msg):
        if len(res.mismatches) < max_mismatch:
            res.mismatches.append("line %d %r: %s" % (line_no, texts[line_no - 1][:60], msg))

    def on_line(line_no, end, state, emitted):
        text = texts[line_no - 1]
        prev_pos = st["prev_pos"]
        pos_before = list(oracle.g.gc.position)
        reply, events = oracle.run_line(text)
        o_err = 0 if reply == b"ok" else int(reply.split(b":")[1])
        block = ref_assemble(text)
        dollar = block is not None and block.startswith("$") and not block.startswith("$J=")
        n_err = state.error
        st["cum"] += len(emitted)
        cum_lines.append(st["cum"])
        if keep:
            res.by_line[line_no] = list(emitted)
        # 1. accepted / rejected, and the code
        skip_rest = False
        if not dollar:
            if o_err == 22 and n_err == 0:
                res.dev22.append(line_no)
                skip_rest = True
            elif o_err != n_err:
                bad(line_no, "oracle error %d, normaliser error %d" % (o_err, n_err))
                skip_rest = True
        if o_err:
            res.rejected[o_err] = res.rejected.get(o_err, 0) + 1
        else:
            res.accepted += 1
        st["prev_pos"] = list(state.position)
        if skip_rest:
            return
        # 2. position after the line (gc.position, the f32 target, not the step-rounded mpos)
        if not _close(oracle.g.gc.position, state.position, TOL):
            bad(line_no, "position oracle %r normaliser %r" % (oracle.g.gc.position, state.position))
        # 3. what was written
        moves = []
        for e in emitted:
            if e.startswith("; bgs: "):
                continue
            mv = parse_move(e)
            if mv is None:
                bad(line_no, "unexpected output line form %r" % e)
                return
            moves.append(mv)
        res.moves += len(moves)
        if o_err or n_err:
            if moves:
                bad(line_no, "rejected line drew moves")
            if not any(e.startswith("; bgs: error:%d " % n_err) for e in emitted) and n_err:
                bad(line_no, "no error comment")
            return
        gtoks = [float(x) for x in re.findall(r"G([0-9]*\.?[0-9]+)", block or "")]
        is_home = any(t in (28.0, 30.0) for t in gtoks)
        jog = bool(block) and block.startswith("$J=")
        is_g53 = (53.0 in gtoks) and bool(events) and not jog
        if dollar:
            return
        if is_home or is_g53:
            res.expected_suppressed += 1
            if moves or not state.suppressed:
                bad(line_no, "expected a suppressed move, got moves=%d suppressed=%r" % (len(moves), state.suppressed))
            return
        if state.suppressed:
            bad(line_no, "unexpected suppressed flag")
        # after an undrawn move (G53/G28/G30) the normaliser first travels to the real position
        if moves and not _close(st["drawn"], pos_before, TOL):
            br = moves.pop(0)
            if br["g"] != 0 or not _close(br["p"], pos_before, TOL):
                bad(line_no, "expected a G0 bridge to %r, got %r" % (pos_before, br))
                return
            res.moves_bridge = getattr(res, "moves_bridge", 0) + 1
            st["drawn"] = list(br["p"])
        feed = oracle.g.gc.feed_rate
        if o_err == 0 and block and re.search(r"F[0-9.+-]", block):
            st["f_set"] = True
        expect = []
        for ev in events:
            cut = (not ev["cond"] & (PL_COND_RAPID | PL_COND_NO_FEED_OVR)) and \
                  (policy == "all_feed" or (bool(ev["cond"] & PL_COND_SPINDLE_MASK) and ev["spd"] > 0))
            expect.append((ev, cut))
        if len(events) > 1:
            bad(line_no, "more than one oracle motion on an unsuppressed line")
            return
        if not events:
            if moves:
                bad(line_no, "moves with no oracle motion")
            return
        ev, cut = expect[0]
        if ev["kind"] == "line":
            if not any(abs(a - b) > 1e-9 for a, b in zip(ev["target"], pos_before)):
                if moves:
                    bad(line_no, "zero-length move was drawn")
                return
            if len(moves) != 1:
                bad(line_no, "expected 1 move, got %d" % len(moves))
                return
            if not _close(moves[0]["p"], ev["target"], TOL):
                bad(line_no, "emitted end %r oracle target %r" % (moves[0]["p"], ev["target"]))
            check_arc = None
        else:
            check_arc = ev
            res.arcs += 1
            res.arc_segments += len(moves)
            res.arc_lines += 1
            arc_checks(bad, line_no, ev, moves, prev_pos)
        # cut flag, E, F, bbox
        drawn = st["drawn"]
        for mv in moves:
            if (mv["g"] == 1) != cut:
                bad(line_no, "cut flag: expected %r, emitted G%d" % (cut, mv["g"]))
            if mv["g"] == 1:
                res.cut_moves += 1
                length = math.sqrt(sum((a - b) ** 2 for a, b in zip(mv["p"], drawn)))
                if mv["e"] <= 0 or abs(mv["e"] - length) > 0.0016:
                    bad(line_no, "E %r vs drawn length %r" % (mv["e"], length))
                if mv["f"] is not None:
                    if st["last_f"] is not None and abs(mv["f"] - st["last_f"]) < 0.05:
                        bad(line_no, "F repeated")
                    st["last_f"] = mv["f"]
                    if abs(mv["f"] - feed) > 0.06:
                        bad(line_no, "F %r oracle feed %r" % (mv["f"], feed))
                elif st["f_set"] and (st["last_f"] is None or abs(feed - st["last_f"]) > 0.06):
                    bad(line_no, "F missing: oracle feed %r last written %r" % (feed, st["last_f"]))
                for pt in (drawn, mv["p"]):
                    if res.bbox is None:
                        res.bbox = [pt[0], pt[1], pt[0], pt[1]]
                    else:
                        res.bbox = [min(res.bbox[0], pt[0]), min(res.bbox[1], pt[1]),
                                    max(res.bbox[2], pt[0]), max(res.bbox[3], pt[1])]
                res.z_levels.add(round(mv["p"][2], 3))
            elif mv["f"] is not None:
                bad(line_no, "F on a G0 line")
            drawn = mv["p"]
        st["drawn"] = drawn

    out = io.BytesIO()
    t0 = time.perf_counter()
    result = gn.normalize_lines(iter(ref), out, cut_policy=policy, line_count=len(ref), trace=on_line)
    res.lockstep_s = time.perf_counter() - t0
    res.out = out.getvalue()
    res.map, res.stats = result["map"], result["stats"]
    res.suppressed = res.stats["suppressed"]
    map_checks(res, ref, cum_lines)
    return res


def arc_checks(bad, line_no, ev, moves, prev_pos):
    """Spec 4.1 item 5, against the oracle's own arguments to ``_mc_arc``."""
    a0, a1, al = ev["a0"], ev["a1"], ev["al"]
    pos, off, tgt, r = ev["pos"], ev["off"], ev["target"], ev["r"]
    cx, cy = prev_pos[a0] + off[a0], prev_pos[a1] + off[a1]    # centre in the normaliser's own frame
    r0, r1 = -off[a0], -off[a1]
    rt0, rt1 = tgt[a0] - (pos[a0] + off[a0]), tgt[a1] - (pos[a1] + off[a1])
    ang = math.atan2(r0 * rt1 - r1 * rt0, r0 * rt0 + r1 * rt1)           # ORA 1390-1398
    if ev["cw"]:
        if ang >= -5e-7:
            ang -= 2 * math.pi
    elif ang <= 5e-7:
        ang += 2 * math.pi
    n = len(moves)
    if r > ARC_TOL:
        ratio = abs(ang) / (2 * math.acos(1 - ARC_TOL / r))
        want = max(1, math.ceil(ratio))
        ok = n == min(want, MAX_ARC) or (abs(ratio - round(ratio)) < 1e-4 and n in (round(ratio), round(ratio) + 1))
    else:
        want, ok = 1, n == 1
    if not ok:
        bad(line_no, "arc segments: got %d, want %d (ratio %.6f)" % (n, want, ratio if r > ARC_TOL else 0))
        return
    if n == MAX_ARC:
        return
    pts = [list(prev_pos)] + [mv["p"] for mv in moves]
    # radius on every point but the last (the last is the exact target, as in mc_arc)
    for k, p in enumerate(pts[1:-1], 1):
        d = math.hypot(p[a0] - cx, p[a1] - cy)
        if abs(d - r) > ARC_TOL:
            bad(line_no, "arc point %d radius %.4f want %.4f" % (k, d, r))
            return
    if not _close(moves[-1]["p"], tgt, TOL):
        bad(line_no, "arc ends at %r, oracle target %r" % (moves[-1]["p"], tgt))
    if r > ARC_TOL:
        dth = abs(ang) / n
        if r * (1 - math.cos(dth / 2)) > ARC_TOL * (1 + 1e-9):
            bad(line_no, "chord sagitta too large")
    # linear axis moves linearly
    lin0 = prev_pos[al]
    for k, p in enumerate(pts[1:], 1):
        if abs(p[al] - (lin0 + (tgt[al] - pos[al]) * k / n)) > 0.0006 + TOL * (k == n):
            bad(line_no, "linear axis not linear at point %d" % k)
            return
    if n > 1:
        total = 0.0
        for p, q in zip(pts[:-1], pts[1:]):
            d = math.atan2(q[a1] - cy, q[a0] - cx) - math.atan2(p[a1] - cy, p[a0] - cx)
            total += (d + math.pi) % (2 * math.pi) - math.pi
        if abs(total - ang) > 1e-6 + 2.5e-3 / r:
            bad(line_no, "arc sweep %.6f oracle %.6f" % (total, ang))


def map_checks(res, ref, cum_lines):
    m, data = res.map, res.out
    n = len(ref)
    k = max(1, -(-n // 65536))
    kept = [i for i in range(n) if (i + 1) % k == 0 or i == n - 1]
    if m["orig"] != [ref[i][1] for i in kept]:
        res.mismatches.append("map orig differs from OctoPrint's offsets")
    if m["orig_size"] != (ref[-1][1] if ref else 0) or m["norm_size"] != len(data):
        res.mismatches.append("map sizes: %r vs file %r/%r" % ((m["orig_size"], m["norm_size"]),
                                                              ref[-1][1] if ref else 0, len(data)))
    lines = data.split(b"\n")
    if lines[-1] != b"":
        res.mismatches.append("output does not end with a newline")
    ends, acc = [], 0
    for ln in lines[:-1]:
        acc += len(ln) + 1
        ends.append(acc)
    want = [ends[3 + cum_lines[i]] for i in kept]
    if m["norm"] != want:
        bad = [(i, a, b) for i, (a, b) in enumerate(zip(m["norm"], want)) if a != b][:3]
        res.mismatches.append("map norm differs from the real output offsets: %r" % (bad,))
    if any(b <= a for a, b in zip(m["orig"], m["orig"][1:])) or any(b < a for a, b in zip(m["norm"], m["norm"][1:])):
        res.mismatches.append("map not monotonic")
    step = max(1, len(kept) // 2000)
    for i in range(0, len(kept), step):
        if abs(translate(m, m["orig"][i]) - m["norm"][i]) > 1e-6:
            res.mismatches.append("map does not round-trip at entry %d" % i)
            break
    prev = -1.0
    for j in range(0, 1001):
        v = translate(m, m["orig_size"] * j / 1000.0)
        if v < prev - 1e-9:
            res.mismatches.append("translate not monotonic")
            break
        prev = v
    head = lines[:4]
    if head != [("; bgs-normalized v%d policy=%s" % (gn.VERSION, res.policy)).encode(), b"G21", b"G90", b"M83"]:
        res.mismatches.append("bad header %r" % head)
    for ln in lines[4:-1]:
        if not (ln.startswith(b"G0 ") or ln.startswith(b"G1 ") or ln.startswith(b"; bgs: ")):
            res.mismatches.append("output line form %r" % ln[:40])
            break


# ---------------------------------------------------------------------------------------------------------
_CACHE = {}


def get_result(path, keep=False):
    key = (path, keep)
    if key not in _CACHE:
        res = run_lockstep(path, keep=keep)
        _CACHE[key] = res
        if not keep:
            T = TOTALS
            T["files"] += 1
            T["lines"] += res.n_lines
            T["accepted"] += res.accepted
            for c, v in res.rejected.items():
                T["rejected"][c] = T["rejected"].get(c, 0) + v
            T["moves"] += res.moves
            T["cuts"] += res.cut_moves
            T["arcs"] += res.arcs
            T["arc_segments"] += res.arc_segments
            T["suppressed"] += res.suppressed
            T["src_bytes"] += res.map["orig_size"]
            T["out_bytes"] += res.map["norm_size"]
            T["lockstep_s"] += res.lockstep_s
            T["dev22"] += len(res.dev22)
            T["ratios"][os.path.relpath(path, CORPUS) if CORPUS and path.startswith(CORPUS) else path] = \
                (res.map["norm_size"] / float(res.map["orig_size"] or 1), res.arc_lines)
    return _CACHE[key]


def fixture(name, keep=True):
    return get_result(os.path.join(FIXTURES, name + ".gcode"), keep=keep)


class FixtureBase(unittest.TestCase):
    def moves(self, res, line_no):
        return [parse_move(e) for e in res.by_line[line_no] if not e.startswith("; bgs:")]

    def comments(self, res, line_no):
        return [e for e in res.by_line[line_no] if e.startswith("; bgs:")]

    def assertClean(self, res):
        self.assertEqual(res.mismatches, [])
        self.assertEqual(res.dev22, [])


class TestAssembleReference(unittest.TestCase):
    def test_assemble_matches_byte_loop(self):
        import random
        rnd = random.Random(7)
        alphabet = "GgMmXxYy0123456789.-+ ();/%$\t:!?~\xa0\u200b\xe9"
        cases = ["", "G1 X1 (c) Y2", "(unclosed X1", "X1 ; (c", "X1 (a;b) Y2", "x1)y2", "/G1", "G" * 79, "G" * 80,
                 "(" + "G" * 90 + ")G1", "G1 " + "X" * 76 + " ; tail", "G1 " + "X" * 77]
        cases += ["".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 120))) for _ in range(4000)]
        for t in cases:
            self.assertEqual(gn.assemble(t), ref_assemble(t), repr(t))


class TestFixtures(FixtureBase):
    def test_every_fixture_matches_the_oracle(self):
        names = sorted(f[:-6] for f in os.listdir(FIXTURES) if f.endswith(".gcode"))
        self.assertEqual(len(names), 19)
        for name in names:
            res = fixture(name)
            self.assertEqual(res.mismatches, [], name)
            self.assertEqual(res.dev22, [], name)

    def test_arc_ijk_xy(self):
        # lines: 1 header, 2 M3, 3 G0, 4 G2 X10 Y0 I5 J0, 5 G3 X0 Y0 I-5 J0.  Both arcs are half circles of
        # r = 5 about (5,0); n = ceil(pi / (2 acos(1 - 0.002/5))) = ceil(55.54) = 56.
        # G2 from (0,0) to (10,0) runs clockwise over the top: max Y = 5.000.  G3 from (10,0) to (0,0) about
        # (5,0) runs counter-clockwise (angle 0 -> pi) and also passes over the top.  The design spec's table
        # says "CCW lower half, min Y = -5", which does not follow from the maths; the oracle agrees with
        # the maths (checked line by line by the lockstep).
        res = fixture("arc_ijk_xy")
        self.assertClean(res)
        for ln in (4, 5):
            mv = self.moves(res, ln)
            self.assertEqual(len(mv), 56)
            self.assertTrue(all(m["g"] == 1 and m["e"] > 0 for m in mv))
            self.assertAlmostEqual(max(m["p"][1] for m in mv), 5.0, places=3)
            self.assertGreaterEqual(min(m["p"][1] for m in mv), -0.001)
        self.assertEqual(self.moves(res, 4)[-1]["p"], [10.0, 0.0, 0.0])
        self.assertEqual(self.moves(res, 5)[-1]["p"], [0.0, 0.0, 0.0])

    def test_arc_full_circle(self):
        # G2 X0 Y0 I5 J0 from (0,0): centre (5,0), sweep -2 pi, n = ceil(2 pi / 0.056568) = ceil(111.07) = 112.
        res = fixture("arc_full_circle")
        self.assertClean(res)
        mv = self.moves(res, 4)
        self.assertEqual(len(mv), 112)
        self.assertEqual(mv[-1]["p"], [0.0, 0.0, 0.0])
        self.assertGreater(mv[0]["p"][1], 0)       # clockwise from the left-hand point goes up first
        self.assertAlmostEqual(max(m["p"][0] for m in mv), 10.0, places=3)

    def test_arc_r(self):
        res = fixture("arc_r")
        self.assertClean(res)
        # G2 X10 Y0 R5: chord 10 = 2r, h = 0, centre (5,0): the same half circle as above
        mv = self.moves(res, 4)
        self.assertEqual(len(mv), 56)
        self.assertAlmostEqual(max(m["p"][1] for m in mv), 5.0, places=3)
        # G2 X10 Y10 R-10 from (10,0): chord 10, h = -sqrt(400-100)/10 = -1.7321, negative R flips it, so
        # I = 0.5 (0 - 10*1.7321) = -8.660, J = 5: centre (1.340, 5).  Start is at -30 deg, end at +30 deg
        # about the centre; clockwise is the long way round: 300 deg = 5.2360 rad.
        mv = self.moves(res, 5)
        cx, cy = 10 - 8.660254, 5.0
        pts = [[10.0, 0.0, 0.0]] + [m["p"] for m in mv]
        total = 0.0
        for p, q in zip(pts[:-1], pts[1:]):
            d = math.atan2(q[1] - cy, q[0] - cx) - math.atan2(p[1] - cy, p[0] - cx)
            total += (d + math.pi) % (2 * math.pi) - math.pi
        self.assertAlmostEqual(total, -math.radians(300), delta=0.01)
        # the last line is error 34 (2r = 10 < chord 30): a comment, no move, position unchanged
        self.assertEqual(self.moves(res, 7), [])
        self.assertEqual(self.comments(res, 7), ["; bgs: error:34 G2 X30 Y0 R5"])

    def test_arc_planes(self):
        res = fixture("arc_planes")
        self.assertClean(res)
        # line 4: G18 XZ semicircle about (5,0,0): y stays 0, (x-5)^2 + z^2 = 25
        mv = self.moves(res, 4)
        for m in mv[:-1]:
            x, y, z = m["p"]
            self.assertAlmostEqual(y, 0.0, places=3)
            self.assertAlmostEqual(math.hypot(x - 5, z), 5.0, delta=0.002)
        self.assertEqual(mv[-1]["p"], [10.0, 0.0, 0.0])
        self.assertAlmostEqual(max(abs(m["p"][2]) for m in mv), 5.0, delta=0.002)
        # line 5: G19 YZ semicircle about (10,5,0): x stays 10
        mv = self.moves(res, 5)
        for m in mv[:-1]:
            x, y, z = m["p"]
            self.assertAlmostEqual(x, 10.0, places=3)
            self.assertAlmostEqual(math.hypot(y - 5, z), 5.0, delta=0.002)
        self.assertEqual(mv[-1]["p"], [10.0, 10.0, 0.0])
        # line 7: G17 full circle helix from (0,0,0) ending at Z-2, Z linear in the point index
        mv = self.moves(res, 7)
        n = len(mv)
        self.assertEqual(n, 112)
        self.assertEqual(mv[-1]["p"], [0.0, 0.0, -2.0])
        for k, m in enumerate(mv, 1):
            self.assertAlmostEqual(m["p"][2], -2.0 * k / n, delta=0.0006)

    def test_arc_radius_check(self):
        # IJK end 0.004 mm off the radius is inside Grbl's 0.005 window; 0.6 mm off is error 33
        res = fixture("arc_radius_check")
        self.assertClean(res)
        self.assertGreater(len(self.moves(res, 4)), 50)
        self.assertEqual(self.moves(res, 4)[-1]["p"], [10.004, 0.0, 0.0])
        self.assertEqual(self.moves(res, 6), [])
        self.assertTrue(self.comments(res, 6)[0].startswith("; bgs: error:33 "))

    def test_g91_1_is_a_no_op_and_g90_1_is_rejected(self):
        res = fixture("g91_1")
        self.assertClean(res)
        self.assertEqual(self.moves(res, 5)[-1]["p"], [10.0, 0.0, 0.0])
        self.assertEqual(len(self.moves(res, 5)), 56)
        self.assertEqual(self.comments(res, 6), ["; bgs: error:20 G90.1 G1 X5"])
        self.assertEqual(self.moves(res, 6), [])
        self.assertEqual(res.moves, 56)        # X5 was NOT executed

    def test_inches(self):
        # G20 G1 X1 Y1 F10 -> 25.400 25.400 at F 254.0; G91 X0.5 -> X = 25.4 + 12.7 = 38.100;
        # G90 G2 X1.5 Y0.5 R0.5 from (38.1, 25.4) to (38.1, 12.7), r = 12.7 mm
        res = fixture("g20")
        self.assertClean(res)
        mv = self.moves(res, 3)
        self.assertEqual(mv[0]["p"], [25.4, 25.4, 0.0])
        self.assertEqual(mv[0]["f"], 254.0)
        mv = self.moves(res, 4)
        self.assertEqual(mv[0]["p"], [38.1, 25.4, 0.0])
        self.assertIsNone(mv[0]["f"])
        mv = self.moves(res, 6)
        self.assertEqual(mv[-1]["p"], [38.1, 12.7, 0.0])
        self.assertGreater(len(mv), 10)

    def test_g92_and_g10(self):
        # X: G0 X10 -> 10; G92 X0 sets offset 10; G0 X5 -> 15; G92.1 clears; G0 X5 -> 5;
        # G10 L20 P1 X0 makes X=5 the zero of G54 (data 5): G0 X1 -> 6; G10 L2 P1 X3: G0 X1 -> 4;
        # G55 has data 0: G0 X1 -> 1; G92 X5 at X=1 sets offset 1 - 0 - 5 = -4: G0 X7 -> 7 - 4 = 3
        res = fixture("g92_g10")
        self.assertClean(res)
        xs = [parse_move(e)["p"][0] for ln in sorted(res.by_line) for e in res.by_line[ln] if not e.startswith("; bgs")]
        self.assertEqual(xs, [10.0, 15.0, 5.0, 6.0, 4.0, 1.0, 3.0])

    def test_g53_park_is_not_drawn(self):
        res = fixture("g53_park")
        self.assertClean(res)
        self.assertEqual(len(self.moves(res, 3)), 1)
        for ln in (4, 5):
            self.assertEqual(self.moves(res, ln), [])
            self.assertEqual(self.comments(res, ln), ["; bgs: G53 move not drawn"])
        # Nothing is drawn at the park point.  The G1 after the park carries no Z word, so Grbl really does
        # cut it at Z-2 (the parked Z), and the oracle position agrees; the design spec's "no output line has
        # Z-2" cannot hold for a G1 without Z, so it is asserted only for the park moves themselves.
        # The next drawn move first travels to where Grbl really is (the park point), then cuts from there.
        mv = self.moves(res, 6)
        self.assertEqual(len(mv), 2)
        self.assertEqual(mv[0]["p"], [0.0, 0.0, -2.0])
        self.assertEqual(mv[0]["g"], 0)
        self.assertEqual(mv[1]["p"], [20.0, 10.0, -2.0])
        # cut measured from the park point (0,0,-2): sqrt(20^2 + 10^2) = 22.361
        self.assertAlmostEqual(mv[1]["e"], math.sqrt(500.0), delta=0.0016)

    def test_dropped_bytes(self):
        # NBSP, zero-width space and realtime '?' never reach Grbl's line buffer, so these lines are valid moves
        res = fixture("dropped_bytes")
        self.assertClean(res)
        self.assertEqual([m["p"] for m in self.moves(res, 3)], [[1.0, 1.0, 0.0]])
        self.assertEqual([m["p"] for m in self.moves(res, 4)], [[3.0, 2.0, 0.0]])
        self.assertEqual([m["p"] for m in self.moves(res, 5)], [[4.0, 2.0, 0.0]])

    def test_g28_is_not_drawn(self):
        res = fixture("g28")
        self.assertClean(res)
        self.assertEqual(len(self.moves(res, 2)), 1)
        for ln in (3, 4):
            self.assertEqual(self.moves(res, ln), [])
            self.assertEqual(self.comments(res, ln), ["; bgs: G28/G30 not drawn"])

    def test_modal_mix(self):
        res = fixture("modal_mix")
        self.assertClean(res)
        self.assertEqual(self.moves(res, 2), [])                       # G1 G90 Z0 F100 at Z0: zero length
        # G0 G1 in one block: Grbl raises 24 (two axis commands) before it reaches the modal-group check (21);
        # the design spec's table says 21, the oracle and the firmware order say 24
        self.assertTrue(self.comments(res, 3)[0].startswith("; bgs: error:24 "))
        self.assertTrue(self.comments(res, 4)[0].startswith("; bgs: error:25 "))   # X given twice
        self.assertEqual(self.comments(res, 5), [])                    # S500M3 accepted
        self.assertEqual([m["p"] for m in self.moves(res, 6)], [[3.0, 0.0, 0.0]])
        # G91 then M30 puts the distance mode back to G90, so G0 X1 from X3 goes to X1 (G91 would give X4)
        mv = self.moves(res, 9)
        self.assertEqual([m["p"] for m in mv], [[1.0, 0.0, 0.0]])
        self.assertEqual(mv[0]["g"], 0)
        self.assertTrue(self.comments(res, 10)[0].startswith("; bgs: error:21 "))  # G90 G91: one modal group

    def test_tool_length_offset(self):
        res = fixture("tlo")
        self.assertClean(res)
        zs = [m["p"][2] for ln in sorted(res.by_line) for m in self.moves(res, ln)]
        self.assertEqual(zs, [4.0, 5.0])

    def test_jog_is_drawn_and_leaves_modal_state(self):
        res = fixture("jog")
        self.assertClean(res)
        mv = self.moves(res, 3)
        self.assertEqual([m["p"] for m in mv], [[5.0, 0.0, 0.0]])
        self.assertEqual(mv[0]["g"], 0)
        self.assertEqual([m["p"] for m in self.moves(res, 4)], [[1.0, 0.0, 0.0]])    # still G90

    def test_assembly(self):
        res = fixture("assembly")
        self.assertClean(res)
        mv = self.moves(res, 2)
        self.assertEqual(mv[0]["p"], [1.5, -0.5, 0.0])
        self.assertEqual(mv[0]["g"], 1)
        self.assertEqual(mv[0]["f"], 300.0)
        self.assertEqual(self.moves(res, 3)[0]["p"], [2.0, -0.5, 0.0])        # "/" dropped
        self.assertEqual(self.moves(res, 4), [])
        self.assertTrue(self.comments(res, 4)[0].startswith("; bgs: error:11 G1 X1111"))
        self.assertEqual(self.moves(res, 5)[0]["p"], [3.0, 3.0, 0.0])          # comments not counted
        self.assertEqual(len(res.stats["rejected_examples"]), 1)

    def test_line_ends_and_map(self):
        # BOM 3 + "G21 G90 G17 F600\r\n" 18 -> 21; "M3 S1000\r" 9 -> 30; "G0 X0 Y0\n" 9 -> 39;
        # "G1 X10 Y0\r\n" 11 -> 50; "G1 X10 Y10\r" 11 -> 61; "G1 X0 Y10" (no terminator) 9 -> 70
        res = fixture("line_ends")
        self.assertClean(res)
        self.assertEqual(res.map["orig"], [21, 30, 39, 50, 61, 70])
        self.assertEqual(res.map["orig_size"], 70)

    def test_spindle_policy(self):
        # M3 occurs later in the file, so the policy is "spindle": G1 before M3 travels, M4 S0 travels,
        # M3 S100 cuts, M5 travels
        res = fixture("spindle")
        self.assertClean(res)
        self.assertEqual(res.policy, "spindle")
        got = [(parse_move(e)["g"]) for ln in sorted(res.by_line) for e in res.by_line[ln] if not e.startswith("; bgs")]
        self.assertEqual(got, [0, 0, 1, 0])

    def test_all_feed_policy(self):
        res = fixture("no_spindle")
        self.assertClean(res)
        self.assertEqual(res.policy, "all_feed")
        mv = [m for ln in sorted(res.by_line) for m in self.moves(res, ln)]
        self.assertEqual([m["g"] for m in mv], [1, 1, 1, 1])
        self.assertEqual([m["p"] for m in mv], [[10, 0, 0], [10, 10, 0], [0, 10, 0], [0, 0, 0]])
        for m in mv:
            self.assertAlmostEqual(m["e"], 10.0, delta=0.0001)

    def test_tiny_cut_keeps_a_positive_e(self):
        # the viewer counts a move as extrusion only when E > 0: a 0.00001 mm cut is written with E0.0001
        res = fixture("tiny_move")
        self.assertClean(res)
        mv = self.moves(res, 4)
        self.assertEqual(len(mv), 1)
        self.assertEqual(mv[0]["g"], 1)
        self.assertEqual(mv[0]["e"], 0.0001)

    def test_output_is_ascii_with_lf(self):
        for name in ("assembly", "arc_r", "g53_park"):
            data = fixture(name).out
            data.decode("ascii")
            self.assertNotIn(b"\r", data)
            self.assertTrue(data.endswith(b"\n"))
            for ln in data.split(b"\n")[:-1]:
                self.assertFalse(ln[:1].isspace())

    def test_normalize_file_equals_normalize_lines(self):
        for name in ("arc_planes", "spindle", "no_spindle", "line_ends", "assembly"):
            path = os.path.join(FIXTURES, name + ".gcode")
            buf = io.BytesIO()
            r = gn.normalize_file(path, buf)
            ref = fixture(name)
            self.assertEqual(buf.getvalue(), ref.out, name)
            self.assertEqual(r["map"], ref.map, name)

    def test_arc_cap_is_counted(self):
        out = io.BytesIO()
        r = gn.normalize_lines(iter([("M3 S1000 F100", 14), ("G0 X0 Y0", 23), ("G2 X0 Y0 I5000 J0", 41)]), out,
                               max_arc_segments=50)
        self.assertEqual(r["stats"]["arc_cap_hits"], 1)
        self.assertEqual(out.getvalue().count(b"G1 "), 50)

    def test_a_word_past_binary32_range_is_not_drawn_and_never_reaches_the_map(self):
        # 45 nines is inf in the firmware's float too; "Xinf" in the stream and `Infinity` in the map's JSON are
        # not numbers, and the second is not valid JSON at all
        import json
        out = io.BytesIO()
        r = gn.normalize_lines(iter([("G21", 4), ("M3 S1000", 13), ("G0 X1 Y1", 22), ("G1 X" + "9" * 45 + " Y1 F100", 60),
                                     ("G1 X5 Y5", 70)]), out)
        self.assertNotIn(b"inf", out.getvalue().lower())
        self.assertNotIn(b"nan", out.getvalue().lower())
        self.assertGreaterEqual(r["stats"]["nonfinite_skipped"], 1)
        json.dumps(r["map"], allow_nan=False)
        self.assertEqual(r["map"]["bounds"], [1.0, 1.0, 5.0, 5.0])

    def test_output_cap_stops_a_runaway_expansion_early(self):
        out = io.BytesIO()
        lines = [("M3 S1000", 9)] + [("G2 X0 Y0 I50 J0 F500", 30 + i) for i in range(2000)]
        with self.assertRaises(gn.OutputTooLarge):
            gn.normalize_lines(iter(lines), out, max_out_bytes=1000000)
        self.assertLessEqual(len(out.getvalue()), 1000000)       # stopped at the cap, not after the whole file
        out = io.BytesIO()
        gn.normalize_lines(iter(lines[:3]), out, max_out_bytes=1000000)       # under the cap: unchanged behaviour
        self.assertGreater(len(out.getvalue()), 1000)

    def test_map_decimation_keeps_last_and_stays_monotonic(self):
        lines = [("G0 X%d" % i, 10 * (i + 1)) for i in range(1000)]
        r = gn.normalize_lines(iter(lines), io.BytesIO(), map_cap=100, line_count=len(lines))
        m = r["map"]
        self.assertEqual(m["orig"], [10 * (i + 1) for i in range(9, 1000, 10)])
        self.assertEqual(m["orig"][-1], 10000)
        self.assertEqual(len(m["norm"]), 100)
        lines = lines[:995]
        m = gn.normalize_lines(iter(lines), io.BytesIO(), map_cap=100, line_count=len(lines))["map"]
        self.assertEqual(m["orig"][-1], 9950)          # the last line is kept although 995 % 10 != 0


# ---------------------------------------------------------------------------------------------------------
BOUNDS = re.compile(r";\s*Bounds:\s*X(\S+)\s+Y(\S+)\s+to\s+X(\S+)\s+Y(\S+)")
# width deltas the oracle measures against the LightBurn header (bounds_probe.py in the stage notes)
HEADER_OUTLIERS = {"aztec.gcode": 0.15, "line_interval-test.gcode": -2.11, "Love_Dad_2025.gcode": -1.25,
                   "Haven-Coaster.gcode": -2.50}


def corpus_files():
    out = []
    for dp, _, fs in os.walk(CORPUS):
        for f in fs:
            if f.endswith(".gcode"):
                out.append(os.path.join(dp, f))
    return sorted(out)


class TestCorpus(unittest.TestCase):
    pass


def _make_corpus_test(path):
    rel = os.path.relpath(path, CORPUS)

    def test(self):
        res = get_result(path)
        self.assertEqual(res.mismatches, [], rel)
        self.assertEqual(res.dev22, [], rel)
        # the stored header says where the cut box is, in the file's own frame (spec 4.3)
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            head = [next(fh, "") for _ in range(6)]
        m = next((BOUNDS.match(h) for h in head if BOUNDS.match(h)), None)
        if m and os.path.basename(path) != "HHS-From-Dad-S0.gcode":
            hw, hh = float(m.group(3)) - float(m.group(1)), float(m.group(4)) - float(m.group(2))
            self.assertIsNotNone(res.bbox, rel)
            w, h = res.bbox[2] - res.bbox[0], res.bbox[3] - res.bbox[1]
            if os.path.basename(path) in HEADER_OUTLIERS:
                self.assertAlmostEqual(w - hw, HEADER_OUTLIERS[os.path.basename(path)], delta=0.02, msg=rel)
                self.assertLessEqual(abs(h - hh), 0.05, rel)
            else:
                self.assertLessEqual(abs(w - hw), 0.05, "%s width %.3f header %.3f" % (rel, w, hw))
                self.assertLessEqual(abs(h - hh), 0.05, "%s height %.3f header %.3f" % (rel, h, hh))
        ratio = res.map["norm_size"] / float(res.map["orig_size"])
        self.assertLessEqual(ratio, 25.0 if res.arc_lines else 4.0, "%s output/source %.2f" % (rel, ratio))
        # normalize_file (two passes on disk) must produce the same bytes and the same policy
        buf = io.BytesIO()
        r = gn.normalize_file(path, buf)
        self.assertEqual(r["stats"]["policy"], res.policy, rel)
        self.assertEqual(buf.getvalue(), res.out, rel)
        self.assertEqual(r["map"], res.map, rel)

    return test


class TestCorpusNamed(unittest.TestCase):
    def res(self, rel):
        return get_result(os.path.join(CORPUS, rel))

    def test_death_star(self):
        r = self.res("octopi-laser/death-star.gcode")
        self.assertEqual(r.policy, "spindle")
        x0, y0, x1, y1 = r.bbox
        self.assertAlmostEqual(x0, 0.0, delta=0.006)
        self.assertAlmostEqual(y0, 0.0, delta=0.006)
        self.assertAlmostEqual(x1, 50.00, delta=0.006)
        self.assertAlmostEqual(y1, 50.01, delta=0.006)

    def test_square_g90_then_g91(self):
        x0, y0, x1, y1 = self.res("octopi-laser/50mm-Square-5mm-5p-80.gcode").bbox
        for v, want in ((x0, -0.2), (y0, -0.2), (x1, 50.2), (y1, 50.2)):
            self.assertAlmostEqual(v, want, delta=0.006)
        self.assertAlmostEqual(x1 - x0, 50.4, delta=0.01)

    def test_g53_park_is_not_drawn(self):
        r = self.res("octopi-alien/Tool-Tray-Pocket.gcode")
        self.assertGreater(r.cut_moves, 0)
        self.assertGreater(r.expected_suppressed, 0)
        self.assertEqual(r.suppressed, r.expected_suppressed)

    def test_box_z_layers_step_by_0_2(self):
        r = self.res("octopi-alien/Spoil Board/Box.gcode")
        plunges = sum(1 for t, _ in r.ref if t.strip() == "G1 Z-0.2 F100")
        self.assertGreater(plunges, 3)
        self.assertEqual(sorted(r.z_levels), sorted(round(-0.2 * k, 3) for k in range(1, plunges + 1)))

    def test_butterflies_has_no_nan(self):
        r = self.res("acro-octo/uploads/butterflies.gcode")
        self.assertEqual(r.n_lines, 43489)
        self.assertNotRegex(r.out.decode("ascii").lower(), r"nan|inf")

    def test_vectric_files_have_arcs(self):
        for rel in ("octopi-zero2/aluminum-ring.gcode", "qbp-octo/upload/Jack.gcode"):
            self.assertGreater(self.res(rel).arcs, 0, rel)

    def test_testmove_is_all_feed(self):
        r = self.res("octopi-zero2/testmove.gcode")
        self.assertEqual(r.policy, "all_feed")
        self.assertGreater(r.cut_moves, 0)

    def test_files_that_never_feed(self):
        for rel in ("acro-octo/uploads/600mm.gcode", "acro-octo/uploads/600mm-Half.gcode"):
            r = self.res(rel)
            self.assertEqual(r.cut_moves, 0, rel)
            self.assertGreater(r.moves, 0, rel)

    def test_explicit_s0_dry_run_has_no_cuts(self):
        hits = [p for p in corpus_files() if os.path.basename(p) == "HHS-From-Dad-S0.gcode"]
        self.assertEqual(len(hits), 1)
        r = get_result(hits[0])
        self.assertEqual(r.cut_moves, 0)
        self.assertNotIn(b" E", r.out)


class TestPerformance(unittest.TestCase):
    def test_largest_file_speed_and_memory(self):
        files = corpus_files()
        big = max(files, key=os.path.getsize)
        sink = open(os.devnull, "wb")
        t0 = time.perf_counter()
        r = gn.normalize_file(big, sink)
        elapsed = time.perf_counter() - t0
        tracemalloc.start()
        gn.normalize_file(big, sink)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        sink.close()
        TOTALS["perf"] = (os.path.relpath(big, CORPUS), os.path.getsize(big), r["stats"]["lines"], elapsed, peak,
                          r["stats"]["out_bytes"])
        self.assertLessEqual(elapsed, 20.0)
        self.assertLess(peak, 16 * 1024 * 1024)  # a whole-file read measured 59.6 MiB; streaming 5.3 MiB


if CORPUS:
    for _p in (corpus_files() if os.path.isdir(CORPUS) else []):
        _name = "test_corpus_" + re.sub(r"[^A-Za-z0-9]+", "_", os.path.relpath(_p, CORPUS))
        setattr(TestCorpus, _name, _make_corpus_test(_p))
    if not os.path.isdir(CORPUS):
        raise RuntimeError("BGS_VIEWER_CORPUS is not a directory: %s" % CORPUS)
else:
    TestCorpus = unittest.skip("BGS_VIEWER_CORPUS not set")(TestCorpus)
    TestCorpusNamed = unittest.skip("BGS_VIEWER_CORPUS not set")(TestCorpusNamed)
    TestPerformance = unittest.skip("BGS_VIEWER_CORPUS not set")(TestPerformance)


def tearDownModule():
    if TOTALS["files"]:
        T = TOTALS
        sys.stderr.write("\ncorpus totals: files=%d lines=%d accepted=%d rejected=%r dev22=%d\n"
                         % (T["files"], T["lines"], T["accepted"], T["rejected"], T["dev22"]))
        sys.stderr.write("  output moves=%d cut_moves=%d suppressed=%d arc_lines=%d arc_segments=%d\n"
                         % (T["moves"], T["cuts"], T["suppressed"], T["arcs"], T["arc_segments"]))
        sys.stderr.write("  source bytes=%d output bytes=%d (x%.2f) lockstep (oracle + normaliser + checks) %.1f s\n"
                         % (T["src_bytes"], T["out_bytes"], T["out_bytes"] / float(T["src_bytes"] or 1),
                            T["lockstep_s"]))
        worst = sorted(T["ratios"].items(), key=lambda kv: -kv[1][0])[:3]
        sys.stderr.write("  largest output/source ratios: %s\n" % ", ".join("%s %.2f%s" % (k, v[0], " (arcs)" if v[1] else "")
                                                                         for k, v in worst))
    if "perf" in TOTALS:
        n, size, lines, el, peak, ob = TOTALS["perf"]
        sys.stderr.write("  perf %s: %d bytes, %d lines -> %d bytes in %.2f s, tracemalloc peak %.1f MiB\n"
                         % (n, size, lines, ob, el, peak / 1048576.0))


if __name__ == "__main__":
    unittest.main()
