"""GeoBox <-> Houdini bridge.

Modules:
    core        pure-Python data / math (no hou) - scene.json <-> point records
    sop         Python SOP entry points (instances, library, ground, areas, roads, footprints, massing)
    build       network builders: build_city(), build_stage(), reload(), export_edits()
    asset_prep  normalise models to the library standard and register them
    sky         sky / cloud setup from sky_params.json (env light, sun, volumetric clouds)
"""
__all__ = ["core", "sop", "build", "asset_prep", "sky"]
