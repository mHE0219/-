"""GeoBox <-> Houdini bridge (pipeline v2: Houdini is the main tool, see docs/houdini.md).

Modules:
    core        pure-Python data / math (no hou): scene.json <-> point records, greybox pieces,
                in-process dress() / save_v2() / seed / clear (calls the stdlib `geobox` package)
    sop         Python SOP entry points (greybox_seed, dress, library, ground, areas, roads,
                footprints, massing, instances)
    build       network builder build_city() -> /obj/geobox, buttons (seed/clear/reload/save/
                export_usd/web_preview/run_selftest), build_stage()
    asset_prep  normalise models to the library standard and register them
    pdg         TOPs networks for farm ingest / tiled district builds
    sky         sky / cloud setup from sky_params.json (env light, sun, volumetric clouds)
Self-test: houdini/scripts/geobox_selftest.py (hython) or build.run_selftest().
"""
__all__ = ["core", "sop", "build", "asset_prep", "pdg", "sky"]
