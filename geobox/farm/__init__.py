"""GeoBox farm: run ingest / indexing / box builds on many machines.

    from geobox.farm import Farm
    farm = Farm("//nas/geobox_farm")                       # or $GEOBOX_FARM_ROOT, default <repo>/farm
    farm.submit("ingest_folder", {"folder": "//nas/assets/trees", "category": "tree"})
    # on each farm node:  python -m geobox farm worker --root //nas/geobox_farm --name cf-20

Modules: queue (shared-folder queue), jobs (job types), worker, cli, api (web routes).
Houdini PDG/TOPs version: houdini/python/geobox_houdini/pdg.py
"""
from .queue import Farm, default_root  # noqa: F401
