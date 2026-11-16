"""Run the GeoBox Houdini self-test with hython (used by run_houdini_selftest.bat).

    python tools/run_selftest.py [--hython PATH] [--project projects/sample_synthetic]

Writes selftest/selftest_report.txt (+ .json and a .hip from the self-test
script). If hython itself fails (licence, crash), the captured output is put
in the report so it can be sent back as-is.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import find_houdini  # noqa: E402

SCRIPT = os.path.join(ROOT, "houdini", "scripts", "geobox_selftest.py")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--hython", default=os.environ.get("GEOBOX_HYTHON") or None)
    ap.add_argument("--project", default=os.path.join(ROOT, "projects", "sample_synthetic"))
    ap.add_argument("--out", default=os.path.join(ROOT, "selftest"))
    ap.add_argument("--timeout", type=int, default=1800)
    a = ap.parse_args(argv)

    os.makedirs(a.out, exist_ok=True)
    report = os.path.join(a.out, "selftest_report.txt")
    hython = a.hython if a.hython and os.path.isfile(a.hython) else None
    if not hython:
        found = [d for d in find_houdini.installs() if d.get("hython")]
        hython = found[0]["hython"] if found else None

    def write_report(text):
        with open(report, "w", encoding="utf-8") as fh:
            fh.write(text)

    head = "GeoBox Houdini self-test runner  %s\nhython: %s\nscript: %s\nproject: %s\n" % (
        _dt.datetime.now().isoformat(timespec="seconds"), hython, SCRIPT, a.project)
    if not hython:
        write_report(head + "\nFAIL: hython not found. Install Houdini or set HFS / GEOBOX_HYTHON.\n")
        print("hython not found (install Houdini, or set HFS)")
        return 2
    if not os.path.isfile(SCRIPT):
        write_report(head + "\nFAIL: %s is missing - update the repo.\n" % SCRIPT)
        print("missing " + SCRIPT)
        return 2

    env = dict(os.environ)
    env["GEOBOX"] = ROOT
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [ROOT, os.path.join(ROOT, "houdini", "python"), env.get("PYTHONPATH")]))
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [hython, SCRIPT, "--project", a.project, "--report", report,
           "--hip", os.path.join(a.out, "geobox_selftest.hip")]
    print("running: " + " ".join('"%s"' % c if " " in c else c for c in cmd), flush=True)
    before = os.path.getmtime(report) if os.path.exists(report) else None
    out_lines = []
    try:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        for line in proc.stdout:
            out_lines.append(line.rstrip("\n"))
            print(line, end="", flush=True)
        rc = proc.wait(timeout=a.timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -1
        out_lines.append("TIMEOUT after %d s" % a.timeout)
    except OSError as exc:
        rc = -2
        out_lines.append("cannot start hython: %s" % exc)

    produced = os.path.exists(report) and os.path.getmtime(report) != before
    log_path = os.path.join(a.out, "hython_output.log")
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out_lines) + "\n")
    if not produced:
        tail = "\n".join(out_lines[-120:])
        write_report(head + "\nFAIL: the self-test did not write a report (exit code %s).\n"
                     "Common causes: no Houdini licence available for hython, or a Python error below.\n\n"
                     "---- hython output (tail) ----\n%s\n" % (rc, tail))
    print("\nreport: %s  (exit %s)" % (report, rc))
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
