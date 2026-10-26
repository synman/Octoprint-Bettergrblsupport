"""One line per scenario from the evidence files that ``scenarios.py`` printed: ``python3 digest.py <evidence dir>``.

Files are named ``<scenario>_<label>.txt`` (the output of one ``scenarios.py`` run).  A line gives the pause strategy
the plugin logged, how long OctoPrint took to reach Paused and to resume, the machine's Z, state and spindle just
before the pause and once paused (the drain strategy is read after its drain), and the scenario's RESULT lines.
"""
import glob
import os
import re
import sys

ROW = re.compile(r"^\s+([+-]\d+\.\d)s state=(\S+)\s+Z=\s*(-?\d+\.\d+) X=\s*(-?\d+\.\d+) spindle=(\d)", re.M)


def trace(text):
    block = text.split("--- machine trace", 1)[-1]
    return [(float(m.group(1)), m.group(2), float(m.group(3)), int(m.group(5))) for m in ROW.finditer(block)]


def at(rows, t):
    found = None
    for row in rows:
        if row[0] <= t:
            found = row
    return found


def line_for(path):
    name, label = os.path.basename(path)[:-4].rsplit("_", 1)
    text = open(path).read()
    strategy = re.search(r"pause: strategy \[(\w+)\]", text)
    pause = re.search(r"PAUSE: job state \w+ after ([\d.]+)s", text)
    resume = re.search(r"RESUME: job state \w+ after ([\d.]+)s", text)
    results = [r[7:] for r in re.findall(r"^RESULT .*$", text, re.M)]
    out = "%-24s %-6s" % (name, label)
    if strategy:
        out += " strategy=%-5s" % strategy.group(1)
    if pause:
        out += " pause=%ss" % pause.group(1)
    if resume:
        out += " resume=%ss" % resume.group(1)
    rows = trace(text)
    if rows and pause and not name.startswith("C_"):
        paused_at = float(pause.group(1)) + 3.0 if float(pause.group(1)) > 3 else 7.0
        before, paused = at(rows, -0.5), at(rows, paused_at)
        if before and paused:
            out += " | before Z=%.1f %s spindle=%d | paused(+%.0fs) Z=%.1f %s spindle=%d | end Z=%.1f %s" % (
                before[2], before[1], before[3], paused_at, paused[2], paused[1], paused[3], rows[-1][2], rows[-1][1])
    return out + " | " + ("; ".join(results) if results else "NO RESULT LINE")


if __name__ == "__main__":
    for path in sorted(glob.glob(os.path.join(sys.argv[1], "*_*.txt"))):
        if not os.path.basename(path).startswith("_"):
            print(line_for(path))
