"""Start the GeoBox web preview and open the browser (used by start_geobox.bat).

    python tools/start_geobox.py [--port 8765] [--project sample_synthetic] [--no-browser]
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def pick_project(requested=None):
    from geobox import project
    names = [p["name"] for p in project.list_projects() if p.get("has_scene")]
    if requested and requested in names:
        return requested
    if "sample_synthetic" in names:
        return "sample_synthetic"
    return names[0] if names else None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("GEOBOX_PORT", 8765)))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--project")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    os.chdir(ROOT)
    from geobox.server import serve
    name = pick_project(a.project)
    path = "/?project=%s" % name if name else "/"
    return serve(a.host, a.port, open_url=None if a.no_browser else path) or 0


if __name__ == "__main__":
    sys.exit(main())
