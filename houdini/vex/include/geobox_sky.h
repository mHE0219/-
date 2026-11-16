// GeoBox Sky — VEX port of viewer/js/sky/skyShader.js
//
//   #include "geobox_sky.h"      (found via $HOUDINI_PATH/vex/include once the GeoBox package is loaded)
//
// LOOK-ALIKE, NOT BIT-EXACT: the GLSL version uses hash-based gradient/Worley noise (pcg3d); here the
// same structure is built from VEX noise() / wnoise(). Parameter names, units and meaning are identical
// to sky_params.json (metres, +Y up, +X east, -Z north, compass degrees clockwise from north).
// For a pixel-exact match of the web preview use the baked HDR (sky/sky.$F4.hdr) as the environment.
//
// Main entry points
//   gbx_cloud_density(P, hf, lod, coverage, density, ctype, cscale, detail_scale, detail_strength,
//                     coverage_var, evolve, seed)            -> density 0..density  (multiply by extinction)
//   gbx_cloud_layer_density(P, time, ...)                     -> convenience wrapper incl. wind + layer
//   gbx_sky_gradient / gbx_physical_sky / gbx_sky_background -> sky radiance for a direction (lat-long bakes)
//   gbx_latlong_dir(u, v)                                    -> same panorama convention as the web bake

#ifndef GEOBOX_SKY_H
#define GEOBOX_SKY_H

#define GBX_EARTH_R 6360000.0
#define GBX_PI 3.14159265359

// ------------------------------------------------------------------ conventions
vector gbx_sun_dir(float azimuth_deg; float elevation_deg)
{
    float az = radians(azimuth_deg), el = radians(elevation_deg);
    return set(sin(az) * cos(el), sin(el), -cos(az) * cos(el));
}

// compass heading (deg) -> horizontal unit vector (x, 0, z)
vector gbx_heading(float deg)
{
    float a = radians(deg);
    return set(sin(a), 0.0, -cos(a));
}

// three.js / web-bake panorama convention: u = atan2(z, x)/2pi + 0.5, v = asin(y)/pi + 0.5 (v = 1 zenith)
vector gbx_latlong_dir(float u; float v)
{
    float phi = (u - 0.5) * 2.0 * GBX_PI;
    float theta = (v - 0.5) * GBX_PI;
    return set(cos(phi) * cos(theta), sin(theta), sin(phi) * cos(theta));
}

float gbx_remap(float v; float l0; float h0; float l1; float h1)
{
    return l1 + (v - l0) * (h1 - l1) / max(h0 - l0, 1e-5);
}

float gbx_altitude(vector p)
{
    return p.y + (p.x * p.x + p.z * p.z) / (2.0 * GBX_EARTH_R);
}

// roots of |o + t d| = R + alt with o = p + (0, R, 0); numerically stable. Returns (t0, t1) or (-1,-1).
vector2 gbx_shell_hit(vector p; vector rd; float alt)
{
    float b = p.x * rd.x + (GBX_EARTH_R + p.y) * rd.y + p.z * rd.z;
    float c = p.x * p.x + p.z * p.z + (p.y - alt) * (2.0 * GBX_EARTH_R + p.y + alt);
    float disc = b * b - c;
    if (disc < 0.0) return set(-1.0, -1.0);
    float s = sqrt(disc);
    float q = (b > 0.0) ? -(b + s) : (s - b);
    float t1 = (abs(q) > 1e-9) ? c / q : 0.0;
    return set(min(q, t1), max(q, t1));
}

// ------------------------------------------------------------------ noise (look-alike)
float gbx_gnoise(vector p)
{
    // VEX Perlin noise() is ~[0,1] around 0.5; rescale to ~[-1,1] like the GLSL gradient noise
    return (noise(p) - 0.5) * 2.4;
}

float gbx_worley(vector p)
{
    int seed;
    float f1, f2;
    wnoise(p, seed, f1, f2);
    return clamp(f1, 0.0, 1.0);
}

float gbx_fbm(vector p; int octaves)
{
    float s = 0.0, a = 0.5, n = 0.0;
    vector q = p;
    for (int i = 0; i < octaves; i++) {
        s += a * gbx_gnoise(q);
        n += a;
        q = q * 2.03 + set(17.1, 31.7, 5.3);
        a *= 0.5;
    }
    return s / n;
}

// ------------------------------------------------------------------ cloud model
float gbx_height_gradient(float h; float ctype)
{
    float st = smooth(0.0, 0.07, h) * (1.0 - smooth(0.14, 0.32, h));
    float sc = smooth(0.0, 0.12, h) * (1.0 - smooth(0.32, 0.62, h));
    float cu = smooth(0.0, 0.06, h) * (1.0 - smooth(0.18, 0.98, h));
    return ctype < 0.5 ? lerp(st, sc, ctype * 2.0) : lerp(sc, cu, ctype * 2.0 - 1.0);
}

// top-down coverage field, roughly uniform in [0,1]
float gbx_coverage_field(vector2 q; float ev)
{
    float f = gbx_fbm(set(q.x, q.y, ev * 0.5), 3) * 0.5 + 0.5;
    float c = 1.0 - gbx_worley(set(q.x * 1.7, q.y * 1.7, ev * 0.35) + 11.0);
    return smooth(0.24, 0.76, f * 0.6 + c * 0.4);
}

// wp: world position in metres with the wind offset already subtracted; hf: 0..1 height in the layer.
// lod 0 = full (detail erosion), 1 = shape only, 2 = cheap (for light marching / previews).
float gbx_cloud_density(vector wp; float hf; int lod;
                        float coverage, density, ctype, cscale, detail_scale, detail_strength, coverage_var, evolve;
                        vector seed)
{
    if (hf < 0.0 || hf > 1.0) return 0.0;
    float base_len = 5200.0 * max(cscale, 0.05);
    vector2 q = set(wp.x / base_len + seed.x, wp.z / base_len + seed.z);
    float weather = gbx_gnoise(set(wp.x / (base_len * 3.7) + seed.z, wp.z / (base_len * 3.7) + seed.x, evolve * 0.15)) * 0.5 + 0.5;
    float cov = clamp(coverage + (weather - 0.5) * 2.0 * coverage_var, 0.0, 1.0);
    if (cov < 0.003) return 0.0;
    float field = gbx_coverage_field(q, evolve);
    float hgrad = gbx_height_gradient(hf, ctype);
    float base = clamp((field - (1.0 - cov * hgrad)) * 3.5, 0.0, 1.0);
    if (base <= 0.0) return 0.0;

    vector pe = wp / (base_len * 0.18) + seed + set(0.0, evolve, 0.0);
    float perlin = gbx_fbm(pe, lod == 2 ? 2 : 3) * 0.5 + 0.5;
    float billow = lod == 2 ? perlin : 1.0 - gbx_worley(pe * 1.6);
    float raw = clamp((perlin * 0.45 + billow * 0.55 - 0.2) / 0.6, 0.0, 1.0);
    base = clamp(gbx_remap(base, (1.0 - raw) * 0.75, 1.0, 0.0, 1.0), 0.0, 1.0);
    if (base <= 0.0) return 0.0;

    if (lod == 0 && detail_strength > 0.0) {
        vector dp = wp / (280.0 * max(cscale, 0.05) * max(detail_scale, 0.05)) + seed * 3.1 + set(0.0, evolve * 3.0, 0.0);
        float dn = (1.0 - gbx_worley(dp)) * 0.65 + (1.0 - gbx_worley(dp * 2.3)) * 0.35;
        float dmod = lerp(1.0 - dn, dn, clamp(hf * 4.0, 0.0, 1.0));
        base = clamp(gbx_remap(base, dmod * detail_strength, 1.0, 0.0, 1.0), 0.0, 1.0);
    }
    return base * density;
}

// Convenience for a Volume Wrangle over world-space voxels (P in metres, y = altitude above ground).
// time = sky seconds (= $T * time_scale + time_offset). Returns density (0..density); multiply by
// extinction (1/m) to get the extinction coefficient for rendering.
float gbx_cloud_layer_density(vector P; float time;
                              float bottom, top, coverage, density, ctype, cscale, detail_scale, detail_strength,
                                    coverage_var, wind_direction, wind_speed, evolve_speed;
                              vector seed; int lod)
{
    float hb = bottom, ht = max(top, bottom + 10.0);
    float hf = (gbx_altitude(P) - hb) / (ht - hb);
    if (hf < 0.0 || hf > 1.0) return 0.0;
    vector wind = gbx_heading(wind_direction) * wind_speed * time;
    float evolve = time * evolve_speed / 600.0;
    return gbx_cloud_density(P - wind, hf, lod, coverage, density, ctype, cscale, detail_scale, detail_strength,
                             coverage_var, evolve, seed);
}

// ------------------------------------------------------------------ sky radiance
vector gbx_sky_gradient(vector rd; vector zenith; vector horizon; float falloff; float intensity)
{
    float t = pow(clamp(rd.y, 0.0, 1.0), max(falloff, 0.01));
    return lerp(horizon, zenith, t) * intensity;
}

float gbx_air_mass(float mu)
{
    float m = max(mu, -0.026);
    float z = degrees(acos(clamp(mu, -0.026, 1.0)));
    return 1.0 / (m + 0.50572 * pow(max(96.07995 - z, 0.5), -1.6364));
}

// single scattering Rayleigh + Mie + ozone, same constants as the GLSL. phys_sun = sun colour * intensity / 3.
vector gbx_physical_sky(vector rd; vector sun_dir; vector phys_sun; float rayleigh; float mie; float mie_g;
                        float cam_alt; int steps)
{
    vector ro = set(0.0, max(cam_alt, 1.0), 0.0);
    vector2 ha = gbx_shell_hit(ro, rd, 60000.0);
    float tmax = ha.y;
    vector2 hg = gbx_shell_hit(ro, rd, 0.0);
    if (hg.x > 0.0) tmax = min(tmax, hg.x);
    if (tmax <= 0.0) return {0, 0, 0};
    vector bR = set(5.802e-6, 13.558e-6, 33.1e-6) * rayleigh;
    float bM = 21e-6 * mie;
    vector bO = set(0.650e-6, 1.881e-6, 0.085e-6) * rayleigh;
    float odR = 0.0, odM = 0.0, odO = 0.0;
    vector sumR = {0, 0, 0}, sumM = {0, 0, 0};
    float n = float(steps);
    for (int i = 0; i < steps; i++) {
        float u = (float(i) + 0.5) / n;
        float t = tmax * u * u;
        float ds = tmax * 2.0 * u / n;
        vector p = ro + rd * t;
        float h = max(gbx_altitude(p), 0.0);
        float hr = exp(-h / 8000.0) * ds;
        float hm = exp(-h / 1200.0) * ds;
        odR += hr; odM += hm;
        odO += max(0.0, 1.0 - abs(h - 25000.0) / 15000.0) * ds;
        vector2 hs = gbx_shell_hit(p, sun_dir, 0.0);
        if (hs.x > 0.0) continue;
        vector up = normalize(set(p.x / GBX_EARTH_R, 1.0, p.z / GBX_EARTH_R));
        float mus = dot(up, sun_dir);
        float am = gbx_air_mass(mus);
        float rr = GBX_EARTH_R / (GBX_EARTH_R + 25000.0);
        float odOl = 15000.0 / sqrt(max(1.0 - rr * rr * (1.0 - mus * mus), 1e-4));
        vector tau = bR * (odR + 8000.0 * exp(-h / 8000.0) * am)
                   + bM * 1.1 * (odM + 1200.0 * exp(-h / 1200.0) * am)
                   + bO * (odO + odOl);
        vector att = set(exp(-tau.x), exp(-tau.y), exp(-tau.z));
        sumR += att * hr;
        sumM += att * hm;
    }
    float mu = dot(rd, sun_dir);
    float pR = 3.0 / (16.0 * GBX_PI) * (1.0 + mu * mu);
    float g = mie_g;
    float pM = 3.0 / (8.0 * GBX_PI) * ((1.0 - g * g) * (1.0 + mu * mu)) / ((2.0 + g * g) * pow(max(1.0 + g * g - 2.0 * g * mu, 1e-4), 1.5));
    return (sumR * bR * pR + sumM * bM * pM) * 20.0 * phys_sun;
}

// Background (no clouds): gradient/physical mix, sun glow and a ground hemisphere — the env-map base.
vector gbx_sky_background(vector rd; vector sun_dir; vector sun_light; vector phys_sun;
                          vector zenith; vector horizon; vector ground; vector tint;
                          float falloff; float sky_intensity; float phys_mix; float phys_intensity;
                          float rayleigh; float mie; float mie_g; float haze; float glow; float glow_sharpness;
                          float cam_alt; int steps)
{
    vector rdc = normalize(set(rd.x, max(rd.y, 0.0005), rd.z));
    vector c = gbx_sky_gradient(rdc, zenith, horizon, falloff, sky_intensity);
    if (phys_mix > 0.001)
        c = lerp(c, gbx_physical_sky(rdc, sun_dir, phys_sun, rayleigh, mie, mie_g, cam_alt, steps) * phys_intensity, phys_mix);
    c *= tint;
    float ang = acos(clamp(dot(rdc, sun_dir), -1.0, 1.0));
    c += sun_light * glow * (0.06 * exp(-ang * glow_sharpness) + 0.012 * exp(-ang * glow_sharpness * 0.2));
    if (rd.y < 0.0) {
        vector amb_top = gbx_sky_gradient(set(0, 1, 0), zenith, horizon, falloff, sky_intensity) * tint;
        vector g = ground * (sun_light * max(sun_dir.y, 0.0) / GBX_PI + amb_top * 0.5);
        c = lerp(c, g, smooth(0.0, 0.01 + haze * 0.15, -rd.y));
    }
    return c;
}

#endif
