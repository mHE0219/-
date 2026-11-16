"""Workstation landing helpers (tools/*.py): Houdini discovery, setup, self-test runner, server start."""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import find_houdini  # noqa: E402


def _exe(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class LandingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="geobox_landing_")
        self.env_backup = {k: os.environ.get(k) for k in ("HFS", "HOUDINI_USER_PREF_DIR", "GEOBOX_HYTHON")}

    def tearDown(self):
        for k, v in self.env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_houdini(self, version, report_ok=True):
        """Windows-like layout: <pf>/Side Effects Software/Houdini X.Y.Z/{bin/hython, python311/python}."""
        hfs = os.path.join(self.tmp, "pf", "Side Effects Software", "Houdini " + version)
        if report_ok:
            body = ("#!%s\nimport sys\nargs=sys.argv[1:]\nrep=args[args.index('--report')+1]\n"
                    "open(rep,'w',encoding='utf-8').write('PASS fake selftest\\n')\nprint('fake hython ok')\n" % sys.executable)
        else:
            body = "#!%s\nimport sys\nprint('No licenses could be found to run this application.')\nsys.exit(3)\n" % sys.executable
        _exe(os.path.join(hfs, "bin", "hython"), body)
        _exe(os.path.join(hfs, "python311", "python"), "#!/bin/sh\nexit 0\n")
        return hfs

    def test_find_houdini_newest_first_and_prefs(self):
        self._fake_houdini("20.0.653")
        self._fake_houdini("20.5.410")
        os.environ.pop("HFS", None)
        found = find_houdini.installs(program_files=[os.path.join(self.tmp, "pf")])
        self.assertEqual([d["version"] for d in found], ["20.5.410", "20.0.653"])
        self.assertTrue(found[0]["hython"].endswith(os.path.join("bin", "hython")))
        self.assertTrue(found[0]["python"].endswith(os.path.join("python311", "python")))
        self.assertEqual(found[0]["major_minor"], "20.5")
        os.environ.pop("HOUDINI_USER_PREF_DIR", None)
        self.assertEqual(find_houdini.pref_dir_for("20.5", documents="D:\\Docs", platform="win").replace("\\", "/"),
                         "D:/Docs/houdini20.5")
        self.assertTrue(find_houdini.pref_dir_for("21.0", home="/h", platform="mac").endswith("Preferences/houdini/21.0"))
        os.environ["HOUDINI_USER_PREF_DIR"] = "/x/houdini__HVER__"
        self.assertEqual(find_houdini.pref_dir_for("20.5"), "/x/houdini20.5")

    def test_mac_version_from_parent_folder(self):
        d = find_houdini.describe("/Applications/Houdini/Houdini20.5.410/Frameworks/Houdini.framework/Versions/Current/Resources")
        self.assertEqual(d["version"], "20.5.410")

    def test_setup_writes_package_env_and_report(self):
        hfs = self._fake_houdini("20.5.410")
        os.environ["HFS"] = hfs
        os.environ["HOUDINI_USER_PREF_DIR"] = os.path.join(self.tmp, "prefs", "houdini__HVER__")
        report = os.path.join(self.tmp, "setup_report.txt")
        env_bat = os.path.join(ROOT, "tools", "geobox_env.bat")
        had_env = os.path.exists(env_bat)
        old_env = open(env_bat, "rb").read() if had_env else None
        try:
            r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "setup_cf20.py"), "--no-tests", "--report", report],
                               capture_output=True, text=True, encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            pkg = os.path.join(self.tmp, "prefs", "houdini20.5", "packages", "geobox.json")
            self.assertTrue(os.path.exists(pkg), r.stdout)
            with open(pkg, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn(ROOT.replace("\\", "/"), json.dumps(data))
            with open(env_bat, "rb") as fh:
                bat = fh.read()
            self.assertIn(b"\r\n", bat)
            self.assertIn(b"GEOBOX_HYTHON=", bat)
            with open(report, encoding="utf-8") as fh:
                self.assertIn("Houdini 20.5.410", fh.read())
        finally:
            if had_env:
                with open(env_bat, "wb") as fh:
                    fh.write(old_env)
            elif os.path.exists(env_bat):
                os.remove(env_bat)

    def test_selftest_runner_pass_and_licence_failure(self):
        out = os.path.join(self.tmp, "out")
        hy = os.path.join(self._fake_houdini("20.5.410"), "bin", "hython")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "run_selftest.py"), "--hython", hy, "--out", out],
                           capture_output=True, text=True, encoding="utf-8")
        script_exists = os.path.exists(os.path.join(ROOT, "houdini", "scripts", "geobox_selftest.py"))
        with open(os.path.join(out, "selftest_report.txt"), encoding="utf-8") as fh:
            rep = fh.read()
        if script_exists:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("PASS fake selftest", rep)
        else:
            self.assertIn("missing", rep)
        shutil.rmtree(self.tmp)
        os.makedirs(self.tmp)
        hy = os.path.join(self._fake_houdini("20.5.410", report_ok=False), "bin", "hython")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "run_selftest.py"), "--hython", hy, "--out", out],
                           capture_output=True, text=True, encoding="utf-8")
        with open(os.path.join(out, "selftest_report.txt"), encoding="utf-8") as fh:
            rep = fh.read()
        self.assertNotEqual(r.returncode, 0)
        if script_exists:
            self.assertIn("No licenses", rep)

    def test_start_server_and_port_in_use(self):
        port = _free_port()
        cmd = [sys.executable, os.path.join(ROOT, "tools", "start_geobox.py"), "--port", str(port), "--no-browser"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        try:
            body = None
            for _ in range(50):
                try:
                    body = urllib.request.urlopen("http://127.0.0.1:%d/" % port, timeout=1).read().decode("utf-8")
                    break
                except OSError:
                    time.sleep(0.1)
            self.assertIsNotNone(body)
            self.assertIn("./vendor/three/build/three.module.js", body)
            self.assertNotIn("node_modules", body)
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(r.returncode, 1)
            self.assertIn("already be running", r.stdout)
        finally:
            p.terminate()
            p.wait(timeout=10)

    def test_vendor_complete(self):
        """Every import-map target and every vendored file listed in the manifest exists."""
        man = os.path.join(ROOT, "viewer", "vendor", "manifest.json")
        with open(man, encoding="utf-8") as fh:
            m = json.load(fh)
        for rel in m["files"]:
            self.assertTrue(os.path.exists(os.path.join(ROOT, "viewer", "vendor", rel)), rel)
        for html in ("index.html", "sky.html"):
            with open(os.path.join(ROOT, "viewer", html), encoding="utf-8") as fh:
                txt = fh.read()
            self.assertNotIn("node_modules", txt, html)
            import re
            for target in re.findall(r'"\./vendor/([^"]+)"', txt):
                p = os.path.join(ROOT, "viewer", "vendor", target)
                self.assertTrue(os.path.exists(p) or target.endswith("/"), target)

    def test_bat_files_crlf_ascii(self):
        for name in ("setup_cf20.bat", "start_geobox.bat", "run_houdini_selftest.bat", "run_farm_worker.bat",
                     os.path.join("tools", "find_python.bat")):
            with open(os.path.join(ROOT, name), "rb") as fh:
                data = fh.read()
            self.assertTrue(all(b < 128 for b in data), name)
            self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), name)


if __name__ == "__main__":
    unittest.main()
