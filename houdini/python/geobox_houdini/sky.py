"""GeoBox Sky -> Houdini.

Reads ``sky_params.json`` (written by viewer/sky.html) and builds, under /obj:

    <name>/                       subnet
        env_sky                   Environment Light using the baked HDR (sky/sky.$F4.hdr) — exact web look
        sun                       distant light aimed from sun azimuth / elevation, coloured like the web sun
        clouds                    (optional) volume over the city with a Volume Wrangle running the VEX
                                  look-alike cloud model (houdini/vex/include/geobox_sky.h), animated by $T

Usage (Houdini Python shell / shelf):

    import geobox_houdini.sky as sky
    sky.build_sky("/path/projects/taipei/sky_params.json")

``hou`` is imported lazily so the maths can be unit-tested outside Houdini.
Conventions: metres, +Y up, +X east, -Z north; azimuth = degrees clockwise from north.
"""
import glob
import json
import math
import os

# Subset of DEFAULT_SKY_PARAMS (viewer/js/sky/SkySystem.js) that Houdini uses.
DEFAULTS = {
    "version": 1,
    "type": "geobox.sky",
    "sun": {"azimuth": 150.0, "elevation": 38.0, "color": "#fff3e2", "intensity": 3.0},
    "atmosphere": {"rayleigh": 1.0, "mie": 1.0, "sunReddening": 1.0, "zenithColor": "#3b78d8",
                   "horizonColor": "#b9d0ea", "groundColor": "#6b665e", "skyIntensity": 0.6},
    "clouds": {"enabled": True, "coverage": 0.42, "density": 1.0, "type": 0.75, "bottom": 1500.0, "top": 4200.0,
               "scale": 1.0, "detailScale": 1.0, "detailStrength": 0.35, "coverageVariation": 0.3, "seed": 1,
               "extinction": 0.04, "litColor": "#ffffff", "shadowColor": "#76859e"},
    "animation": {"timeScale": 8.0, "paused": False, "windDirection": 70.0, "windSpeed": 12.0, "evolveSpeed": 1.0},
    "post": {"exposure": 1.0},
}

# Rotation (deg about +Y) applied to the environment light so the web bake orientation (u=0.5 -> +X,
# u=0.75 -> +Z, three.js convention) lines up with Houdini. NOT verified inside Houdini — check once with
# a chrome ball: the HDR sun highlight must coincide with the 'sun' light; otherwise change env_rotate_y.
ENV_ROTATE_Y_DEFAULT = -90.0


def _log(msg):
    print("GeoBox Sky: " + msg)


def _hou():
    import hou  # noqa: WPS433 - only available inside Houdini
    return hou


# ------------------------------------------------------------------------------------------------ params
def _merge(base, over):
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_params(path):
    """Read sky_params.json and fill missing keys with defaults."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("type") not in (None, "geobox.sky"):
        _log("warning: %s has type %r" % (path, data.get("type")))
    return _merge(DEFAULTS, data)


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def hex_to_linear(hex_str):
    """'#rrggbb' (sRGB) -> (r, g, b) linear floats, like the web viewer."""
    h = str(hex_str or "#ffffff").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    try:
        n = int(h[:6], 16)
    except ValueError:
        return (1.0, 1.0, 1.0)
    return tuple(srgb_to_linear(((n >> s) & 255) / 255.0) for s in (16, 8, 0))


def sun_direction(azimuth_deg, elevation_deg):
    """Unit vector toward the sun (docs/conventions.md)."""
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    return (math.sin(az) * math.cos(el), math.sin(el), -math.cos(az) * math.cos(el))


def heading_xz(deg):
    a = math.radians(deg)
    return (math.sin(a), -math.cos(a))


def sun_rotation(azimuth_deg, elevation_deg):
    """(rx, ry, rz) in degrees, rotation order xyz, so a light shining down its local -Z points AWAY from
    the sun (local +Z -> sun direction): Rx then Ry applied to +Z gives (cos rx·sin ry, -sin rx, cos rx·cos ry)."""
    d = sun_direction(azimuth_deg, elevation_deg)
    rx = -math.degrees(math.asin(max(-1.0, min(1.0, d[1]))))
    ry = math.degrees(math.atan2(d[0], d[2]))
    return (rx, ry, 0.0)


def rotate_xyz(v, rx, ry, rz=0.0):
    """Apply Rx, then Ry, then Rz (degrees) to vector v — Houdini 'xyz' rotation order (for tests)."""
    x, y, z = v
    a = math.radians(rx)
    y, z = y * math.cos(a) - z * math.sin(a), y * math.sin(a) + z * math.cos(a)
    b = math.radians(ry)
    x, z = x * math.cos(b) + z * math.sin(b), -x * math.sin(b) + z * math.cos(b)
    c = math.radians(rz)
    x, y = x * math.cos(c) - y * math.sin(c), x * math.sin(c) + y * math.cos(c)
    return (x, y, z)


def sun_transmittance(elevation_deg, rayleigh=1.0, mie=1.0):
    """Relative (1 at zenith) sun colour reddening — mirrors sunTransmittance() in SkySystem.js."""
    z = min(90.0 - max(elevation_deg, -2.0), 91.5)
    zr = math.radians(z)
    airmass = 1.0 / max(math.cos(zr) + 0.50572 * (96.07995 - z) ** -1.6364, 1e-3)
    tau_r = [b * 8000.0 * rayleigh for b in (5.802e-6, 13.558e-6, 33.1e-6)]
    tau_m = 21e-6 * 1.1 * 1200.0 * mie
    return tuple(math.exp(-(t + tau_m) * (airmass - 1.0)) for t in tau_r)


def _smoothstep(e0, e1, x):
    t = max(0.0, min(1.0, (x - e0) / (e1 - e0)))
    return t * t * (3.0 - 2.0 * t)


def sun_light(params):
    """Linear RGB sun radiance (colour x intensity x reddening x horizon fade), like SkySystem.syncUniforms.
    Returns (color normalised to max 1, intensity)."""
    s, a = params["sun"], params["atmosphere"]
    col = hex_to_linear(s.get("color", "#ffffff"))
    tr = sun_transmittance(s["elevation"], a.get("rayleigh", 1.0), a.get("mie", 1.0))
    fade = _smoothstep(-4.0, 1.0, s["elevation"])
    red = a.get("sunReddening", 1.0)
    light = [c * s.get("intensity", 3.0) * (1.0 + (t - 1.0) * red) * fade for c, t in zip(col, tr)]
    peak = max(max(light), 1e-6)
    return tuple(v / peak for v in light), peak


def seed_vector(seed):
    """Same noise-space seed offset as seedVector() in SkySystem.js."""
    def f(k):
        x = math.sin((seed + 1) * k) * 43758.5453
        return (x - math.floor(x)) * 97.0
    return (f(12.9898), f(78.233), f(37.719))


def hdr_pattern_for(params_path):
    """Best HDR path for the env light: '<dir>/sky/sky.$F4.hdr' for a sequence, the single file, or None."""
    sky_dir = os.path.join(os.path.dirname(os.path.abspath(params_path)), "sky")
    frames = sorted(glob.glob(os.path.join(sky_dir, "sky.[0-9][0-9][0-9][0-9].hdr")))
    if len(frames) > 1:
        return os.path.join(sky_dir, "sky.$F4.hdr").replace("\\", "/")
    if len(frames) == 1:
        return frames[0].replace("\\", "/")
    return None


# ------------------------------------------------------------------------------------------------ VEX
WRANGLE_SNIPPET = r'''// GeoBox Sky clouds — look-alike of the web shader (see geobox_sky.h). Parameters: spare parms below,
// written from sky_params.json by geobox_houdini.sky.apply_params_to_node().
#include "geobox_sky.h"

float t = @Time * chf("time_scale") + chf("time_offset");
float d = gbx_cloud_layer_density(v@P, t,
    chf("bottom"), chf("top"), chf("coverage"), chf("density"), chf("cloud_type"), chf("cloud_scale"),
    chf("detail_scale"), chf("detail_strength"), chf("coverage_variation"),
    chf("wind_direction"), chf("wind_speed"), chf("evolve_speed"),
    chv("seed_offset"), chi("lod"));
// extinction coefficient per metre (scene units are metres)
f@density = d * chf("extinction") * chf("density_mult");
'''

# (name, label, kind, getter) — kind: 'f' float, 'v' vector3, 'i' int
def _spare_values(params):
    c, an = params["clouds"], params["animation"]
    time_scale = 0.0 if an.get("paused") else an.get("timeScale", 1.0)
    return [
        ("time_scale", "Time Scale (sky s per s)", "f", time_scale),
        ("time_offset", "Time Offset (s)", "f", 0.0),
        ("bottom", "Cloud Bottom (m)", "f", c["bottom"]),
        ("top", "Cloud Top (m)", "f", max(c["top"], c["bottom"] + 10.0)),
        ("coverage", "Coverage", "f", c["coverage"] if c.get("enabled", True) else 0.0),
        ("density", "Density", "f", c["density"]),
        ("cloud_type", "Type (0 stratus .. 1 cumulus)", "f", c["type"]),
        ("cloud_scale", "Scale", "f", c["scale"]),
        ("detail_scale", "Detail Scale", "f", c["detailScale"]),
        ("detail_strength", "Detail Strength", "f", c["detailStrength"]),
        ("coverage_variation", "Coverage Variation", "f", c["coverageVariation"]),
        ("extinction", "Extinction (1/m)", "f", c.get("extinction", 0.04)),
        ("density_mult", "Density Multiplier", "f", 1.0),
        ("wind_direction", "Wind To (deg)", "f", an["windDirection"]),
        ("wind_speed", "Wind Speed (m/s)", "f", an["windSpeed"]),
        ("evolve_speed", "Evolve Speed", "f", an["evolveSpeed"]),
        ("seed_offset", "Seed Offset", "v", seed_vector(c.get("seed", 1))),
        ("lod", "LOD (0 full, 2 fast)", "i", 0),
    ]


# ------------------------------------------------------------------------------------------------ hou helpers
def _set(node, name, value):
    """Set a parm or parm tuple; ignore (but log) parms missing in this Houdini version."""
    try:
        if isinstance(value, (tuple, list)):
            pt = node.parmTuple(name)
            if pt is not None:
                pt.set(tuple(value))
                return True
        p = node.parm(name)
        if p is None:
            pt = node.parmTuple(name)
            if pt is None:
                _log("%s has no parm '%s' — skipped" % (node.path(), name))
                return False
            pt.set(value if isinstance(value, (tuple, list)) else (value,))
            return True
        p.set(value)
        return True
    except Exception as exc:  # noqa: BLE001
        _log("could not set %s/%s = %r (%s)" % (node.path(), name, value, exc))
        return False


def _set_menu(node, name, token):
    """Menu parms take a token on string menus and an index on int menus."""
    p = node.parm(name)
    if p is None:
        _log("%s has no parm '%s' — skipped" % (node.path(), name))
        return False
    try:
        p.set(token)
        return True
    except Exception:  # noqa: BLE001
        try:
            items = list(p.menuItems())
            p.set(items.index(token))
            return True
        except Exception as exc:  # noqa: BLE001
            _log("could not set menu %s/%s to %r (%s)" % (node.path(), name, token, exc))
            return False


def _create(parent, types, name):
    """Create the first node type that exists (type names differ between Houdini versions)."""
    last = None
    for t in types:
        try:
            return parent.createNode(t, name)
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError("could not create any of %s under %s: %s" % (types, parent.path(), last))


def apply_params_to_node(node, params, folder_name="geobox_sky", folder_label="GeoBox Sky"):
    """Create/refresh spare parms on ``node`` (e.g. the Volume Wrangle) from sky params."""
    hou = _hou()
    values = _spare_values(params)
    ptg = node.parmTemplateGroup()
    if ptg.find(folder_name) is not None:
        ptg.remove(folder_name)
    folder = hou.FolderParmTemplate(folder_name, folder_label, folder_type=hou.folderType.Tabs)
    for name, label, kind, val in values:
        if kind == "v":
            folder.addParmTemplate(hou.FloatParmTemplate(name, label, 3, default_value=tuple(val)))
        elif kind == "i":
            folder.addParmTemplate(hou.IntParmTemplate(name, label, 1, default_value=(int(val),)))
        else:
            folder.addParmTemplate(hou.FloatParmTemplate(name, label, 1, default_value=(float(val),)))
    ptg.append(folder)
    node.setParmTemplateGroup(ptg)
    for name, _label, kind, val in values:  # also set values (templates only set defaults)
        _set(node, name, tuple(val) if kind == "v" else val)
    return values


def build_sky(params_path, hdr_pattern=None, parent="/obj", with_volume=True, name="geobox_sky",
              cloud_extent=20000.0, voxel_size=60.0, env_rotate_y=ENV_ROTATE_Y_DEFAULT):
    """Build environment light + sun + (optional) VEX cloud volume from sky_params.json.

    hdr_pattern: e.g. '$HIP/sky/sky.$F4.hdr'. Default: the project's sky/ folder next to params_path.
    cloud_extent: horizontal size (m) of the cloud volume, centred on the origin (the city box centre).
    """
    hou = _hou()
    params = load_params(params_path)
    root = hou.node(parent)
    if root is None:
        raise RuntimeError("parent %s does not exist" % parent)
    old = root.node(name)
    if old is not None:
        old.destroy()
    net = root.createNode("subnet", name)
    nodes = {"subnet": net}

    # --- environment light (baked HDR = exact web look)
    pattern = hdr_pattern or hdr_pattern_for(params_path)
    env = _create(net, ["envlight"], "env_sky")
    if pattern:
        _set(env, "env_map", pattern)
    else:
        _log("no baked HDR found next to %s — bake one in sky.html (HDR 烘焙) and set env_sky/env_map" % params_path)
    _set(env, "ry", env_rotate_y)
    _set(env, "light_intensity", 1.0)
    nodes["env"] = env

    # --- sun
    sun = _create(net, ["hlight::2.0", "hlight"], "sun")
    _set_menu(sun, "light_type", "distant")
    col, inten = sun_light(params)
    _set(sun, "light_color", col)
    _set(sun, "light_intensity", inten)
    rx, ry, rz = sun_rotation(params["sun"]["azimuth"], params["sun"]["elevation"])
    _set(sun, "r", (rx, ry, rz))
    try:
        sun.setComment("az %.1f° el %.1f°" % (params["sun"]["azimuth"], params["sun"]["elevation"]))
    except Exception:  # noqa: BLE001
        pass
    nodes["sun"] = sun

    # --- clouds (VEX look-alike volume)
    if with_volume and params["clouds"].get("enabled", True):
        c = params["clouds"]
        bottom, top = float(c["bottom"]), max(float(c["top"]), float(c["bottom"]) + 10.0)
        geo = _create(net, ["geo"], "clouds")
        for child in list(geo.children()):  # older versions create a default file SOP
            child.destroy()
        vol = _create(geo, ["volume"], "cloud_bounds")
        _set(vol, "name", "density")
        _set(vol, "size", (cloud_extent, top - bottom, cloud_extent))
        _set(vol, "t", (0.0, 0.5 * (bottom + top), 0.0))
        _set_menu(vol, "uniformsamples", "size")  # Uniform Sampling: By Size
        _set(vol, "divsize", voxel_size)
        wr = _create(geo, ["volumewrangle"], "cloud_density")
        wr.setInput(0, vol)
        _set(wr, "snippet", WRANGLE_SNIPPET)
        apply_params_to_node(wr, params)
        out = _create(geo, ["null"], "OUT_CLOUDS")
        out.setInput(0, wr)
        try:
            out.setDisplayFlag(True)
            out.setRenderFlag(True)
            geo.layoutChildren()
        except Exception:  # noqa: BLE001
            pass
        nodes.update({"clouds": geo, "volume": vol, "wrangle": wr, "out": out})

    try:
        net.layoutChildren()
        net.setComment("GeoBox sky from %s" % os.path.basename(params_path))
    except Exception:  # noqa: BLE001
        pass
    _log("built %s (sun az %.1f el %.1f, HDR %s)" % (net.path(), params["sun"]["azimuth"],
                                                      params["sun"]["elevation"], pattern or "none"))
    return nodes


def update_sky(params_path, subnet_path="/obj/geobox_sky"):
    """Re-apply a changed sky_params.json to an existing build (keeps your manual node edits)."""
    hou = _hou()
    params = load_params(params_path)
    net = hou.node(subnet_path)
    if net is None:
        raise RuntimeError("%s not found — run build_sky first" % subnet_path)
    sun = net.node("sun")
    if sun is not None:
        col, inten = sun_light(params)
        _set(sun, "light_color", col)
        _set(sun, "light_intensity", inten)
        _set(sun, "r", sun_rotation(params["sun"]["azimuth"], params["sun"]["elevation"]))
    wr = net.node("clouds/cloud_density") if net.node("clouds") is not None else None
    if wr is not None:
        apply_params_to_node(wr, params)
    return params


__all__ = [
    "DEFAULTS", "load_params", "hex_to_linear", "sun_direction", "sun_rotation", "rotate_xyz", "sun_light",
    "sun_transmittance", "seed_vector", "heading_xz", "hdr_pattern_for", "apply_params_to_node", "build_sky",
    "update_sky", "WRANGLE_SNIPPET",
]
