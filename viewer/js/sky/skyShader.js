// GeoBox Sky — GLSL (WebGL2 / GLSL ES 3.0) sky + volumetric cloud model.
//
// Two materials:
//   * createSkyPassMaterial()  — the expensive pass: atmosphere + cirrus + raymarched cumulus layer.
//       Rendered into a (low-res) HDR render target by SkySystem, or directly for bakes/refits.
//       Output (uApplyPost == 0): rgb = linear HDR radiance (no sun disc), a = transmittance toward camera
//       (used by the composite pass to occlude the full-res sun disc).
//       Equirect mode (uEquirect == 1) maps the viewport to a lat-long panorama (three.js convention,
//       u = atan(z, x) / 2PI + 0.5, v = asin(y) / PI + 0.5, v = 1 at zenith).
//   * createCompositeMaterial() — the cheap full-res pass that lives in the user's scene: upsamples the
//       sky target, adds the sun disc at full resolution, applies exposure / tonemap / grading and the
//       renderer's output colour space.
//
// Coordinate frame (docs/conventions.md): metres, +Y up, +X east, -Z north; ground at y = 0.
// The planet is a sphere of radius EARTH_R centred at (0, -EARTH_R, 0); all sphere intersections use a
// numerically stable formulation so float32 is enough even with 6.36e6 m radii.
//
// Noise is hash-based (pcg3d) gradient + Worley noise — no textures — so the model can be ported to VEX
// (see houdini/vex/include/geobox_sky.h, a look-alike port).

import * as THREE from 'three';

// three.js' own tonemappers, with exposure handled by us (uExposure) instead of the renderer uniform.
const TONEMAP_CHUNK = THREE.ShaderChunk.tonemapping_pars_fragment.replace(
	'uniform float toneMappingExposure;',
	'const float toneMappingExposure = 1.0;'
);

export const TONEMAP_MODES = { none: 0, aces: 1, agx: 2, neutral: 3, reinhard: 4 };

const VERTEX = /* glsl */`
varying vec2 vNdc;
void main() {
	vNdc = position.xy;
	gl_Position = vec4( position.xy, 0.9999, 1.0 );
}
`;

// ---------------------------------------------------------------------------------------------
// Shared: ray generation, sun disc, post
// ---------------------------------------------------------------------------------------------
const COMMON = /* glsl */`
#define PI 3.14159265359
#define EARTH_R 6360000.0

varying vec2 vNdc;

uniform mat4 uInvProj;
uniform mat4 uCamToWorld;
uniform int uEquirect;
uniform vec2 uTileOffset;
uniform vec2 uTileScale;

uniform vec3 uSunDir;
uniform vec3 uSunLight;
uniform float uSunDiscSize;
uniform float uSunDiscIntensity;

uniform float uExposure;
uniform int uTonemap;
uniform float uSaturation;
uniform float uContrast;
uniform vec3 uGrade;

${ TONEMAP_CHUNK }

vec3 getRayDir() {
	if ( uEquirect == 1 ) {
		vec2 uv = uTileOffset + ( vNdc * 0.5 + 0.5 ) * uTileScale;
		float phi = ( uv.x - 0.5 ) * 2.0 * PI;
		float theta = ( uv.y - 0.5 ) * PI;
		return vec3( cos( phi ) * cos( theta ), sin( theta ), sin( phi ) * cos( theta ) );
	}
	vec4 v = uInvProj * vec4( vNdc, -1.0, 1.0 ); // near plane: far-plane w underflows in float32 for large far/near
	vec3 dv = normalize( v.xyz / v.w );
	return normalize( ( uCamToWorld * vec4( dv, 0.0 ) ).xyz );
}

float luma( vec3 c ) { return dot( c, vec3( 0.2126, 0.7152, 0.0722 ) ); }

vec3 sunDisc( vec3 rd ) {
	float s = length( cross( rd, uSunDir ) );
	float c = dot( rd, uSunDir );
	float ang = atan( s, c );
	float r = uSunDiscSize;
	if ( c < 0.0 || ang > r * 1.3 ) return vec3( 0.0 );
	float x = clamp( ang / r, 0.0, 1.0 );
	float limb = 1.0 - 0.55 * ( 1.0 - sqrt( max( 1.0 - x * x, 0.0 ) ) );
	float edge = 1.0 - smoothstep( 0.9, 1.15, ang / r );
	float horizon = smoothstep( -0.002, 0.002, rd.y );
	return uSunLight * uSunDiscIntensity * limb * edge * horizon;
}

vec3 gbxTonemap( vec3 c ) {
	if ( uTonemap == 1 ) return ACESFilmicToneMapping( c );
	if ( uTonemap == 2 ) return AgXToneMapping( c );
	if ( uTonemap == 3 ) return NeutralToneMapping( c );
	if ( uTonemap == 4 ) return ReinhardToneMapping( c );
	return clamp( c, 0.0, 1.0 );
}

// display grading: exposure -> tonemap -> saturation -> contrast (in gamma space). Linear in / linear out.
vec3 gbxPost( vec3 c ) {
	c = max( c, 0.0 ) * uExposure * uGrade;
	c = gbxTonemap( c );
	float l = luma( c );
	c = max( mix( vec3( l ), c, uSaturation ), 0.0 );
	vec3 g = pow( c, vec3( 1.0 / 2.2 ) );
	g = ( g - 0.45 ) * uContrast + 0.45;
	return pow( clamp( g, 0.0, 1.0 ), vec3( 2.2 ) );
}

vec3 gbxLinearToSRGB( vec3 c ) {
	return mix( c * 12.92, 1.055 * pow( c, vec3( 1.0 / 2.4 ) ) - 0.055, step( 0.0031308, c ) );
}
`;

// ---------------------------------------------------------------------------------------------
// Sky pass
// ---------------------------------------------------------------------------------------------
const SKY_FRAGMENT = /* glsl */`
precision highp float;
precision highp int;
${ COMMON }

uniform vec3 uCamPos;
uniform float uTime;

// atmosphere
uniform float uPhysMix;
uniform vec3 uZenith;
uniform vec3 uHorizon;
uniform vec3 uGround;
uniform float uHorizonFalloff;
uniform float uSkyIntensity;
uniform float uPhysIntensity;
uniform vec3 uSkyTint;
uniform float uRayleigh;
uniform float uMie;
uniform float uMieG;
uniform float uHaze;
uniform vec3 uPhysSun;
uniform float uSunGlow;
uniform float uSunGlowSharpness;
uniform int uAtmoSteps;

// cumulus layer
uniform int uCloudsOn;
uniform float uCoverage;
uniform float uCloudDensity;
uniform float uCloudType;
uniform float uCloudBottom;
uniform float uCloudTop;
uniform float uCloudScale;
uniform float uDetailScale;
uniform float uDetailStrength;
uniform float uCoverageVar;
uniform vec3 uSeed;
uniform vec3 uCloudLit;
uniform vec3 uCloudShadow;
uniform float uCloudAmbient;
uniform float uSilver;
uniform float uSilverSpread;
uniform float uForwardG;
uniform float uBackG;
uniform float uPowder;
uniform float uMultiScatter;
uniform float uExtinction;
uniform float uFadeDist;
uniform float uMaxDist;
uniform vec3 uWindOffset;
uniform float uEvolve;
uniform int uSteps;
uniform int uLightSteps;
uniform int uJitter;

// cirrus
uniform int uCirrusOn;
uniform float uCirrusCoverage;
uniform float uCirrusOpacity;
uniform float uCirrusAlt;
uniform float uCirrusScale;
uniform float uCirrusStretch;
uniform vec2 uCirrusDir;
uniform vec2 uCirrusOffset;
uniform vec3 uCirrusColor;

// output control
uniform int uSunDiscInPass;
uniform int uApplyPost;
uniform int uOutSRGB;
uniform int uLinearGrade;
uniform float uBakeScale;
uniform int uOpacityOut;

// ------------------------------------------------------------------ noise
uvec3 pcg3d( uvec3 v ) {
	v = v * 1664525u + 1013904223u;
	v.x += v.y * v.z; v.y += v.z * v.x; v.z += v.x * v.y;
	v ^= v >> 16u;
	v.x += v.y * v.z; v.y += v.z * v.x; v.z += v.x * v.y;
	return v;
}
vec3 hash33( vec3 cell ) {
	uvec3 u = uvec3( ivec3( cell ) + ivec3( 1048576 ) );
	return vec3( pcg3d( u ) ) * ( 1.0 / 4294967295.0 );
}
float ign( vec2 px ) { return fract( 52.9829189 * fract( dot( px, vec2( 0.06711056, 0.00583715 ) ) ) ); }

// gradient noise, ~[-1, 1]
float gnoise( vec3 p ) {
	vec3 i = floor( p );
	vec3 f = p - i;
	vec3 u = f * f * f * ( f * ( f * 6.0 - 15.0 ) + 10.0 );
	float n000 = dot( hash33( i ) * 2.0 - 1.0, f );
	float n100 = dot( hash33( i + vec3( 1, 0, 0 ) ) * 2.0 - 1.0, f - vec3( 1, 0, 0 ) );
	float n010 = dot( hash33( i + vec3( 0, 1, 0 ) ) * 2.0 - 1.0, f - vec3( 0, 1, 0 ) );
	float n110 = dot( hash33( i + vec3( 1, 1, 0 ) ) * 2.0 - 1.0, f - vec3( 1, 1, 0 ) );
	float n001 = dot( hash33( i + vec3( 0, 0, 1 ) ) * 2.0 - 1.0, f - vec3( 0, 0, 1 ) );
	float n101 = dot( hash33( i + vec3( 1, 0, 1 ) ) * 2.0 - 1.0, f - vec3( 1, 0, 1 ) );
	float n011 = dot( hash33( i + vec3( 0, 1, 1 ) ) * 2.0 - 1.0, f - vec3( 0, 1, 1 ) );
	float n111 = dot( hash33( i + vec3( 1, 1, 1 ) ) * 2.0 - 1.0, f - vec3( 1, 1, 1 ) );
	return 1.6 * mix( mix( mix( n000, n100, u.x ), mix( n010, n110, u.x ), u.y ),
	                  mix( mix( n001, n101, u.x ), mix( n011, n111, u.x ), u.y ), u.z );
}

// Worley F1 distance, ~[0, 1]
float worley( vec3 p ) {
	vec3 i = floor( p );
	vec3 f = p - i;
	float d = 1e9;
	for ( int z = -1; z <= 1; z++ )
	for ( int y = -1; y <= 1; y++ )
	for ( int x = -1; x <= 1; x++ ) {
		vec3 o = vec3( float( x ), float( y ), float( z ) );
		vec3 r = o + hash33( i + o ) - f;
		d = min( d, dot( r, r ) );
	}
	return sqrt( d );
}

float fbmP( vec3 p, int oct ) {
	float s = 0.0, a = 0.5, n = 0.0;
	for ( int i = 0; i < 6; i++ ) {
		if ( i >= oct ) break;
		s += a * gnoise( p );
		n += a;
		p = p * 2.03 + vec3( 17.1, 31.7, 5.3 );
		a *= 0.5;
	}
	return s / n;
}

float remap( float v, float l0, float h0, float l1, float h1 ) {
	return l1 + ( v - l0 ) * ( h1 - l1 ) / max( h0 - l0, 1e-5 );
}

// ------------------------------------------------------------------ geometry
// roots of |o + t d| = EARTH_R + alt, o = p + (0, EARTH_R, 0). Stable for float32.
vec2 shellHit( vec3 p, vec3 rd, float alt ) {
	float b = p.x * rd.x + ( EARTH_R + p.y ) * rd.y + p.z * rd.z;
	float c = p.x * p.x + p.z * p.z + ( p.y - alt ) * ( 2.0 * EARTH_R + p.y + alt );
	float disc = b * b - c;
	if ( disc < 0.0 ) return vec2( -1.0 );
	float s = sqrt( disc );
	float q = ( b > 0.0 ) ? -( b + s ) : ( s - b );
	float t0 = q;
	float t1 = ( abs( q ) > 1e-9 ) ? c / q : 0.0;
	return vec2( min( t0, t1 ), max( t0, t1 ) );
}
float altitudeOf( vec3 p ) { return p.y + dot( p.xz, p.xz ) / ( 2.0 * EARTH_R ); }

// ------------------------------------------------------------------ atmosphere
const vec3 BETA_R = vec3( 5.802e-6, 13.558e-6, 33.1e-6 );
const float BETA_M = 21e-6;
const vec3 BETA_O = vec3( 0.650e-6, 1.881e-6, 0.085e-6 ); // ozone absorption (keeps twilight blue)

// Kasten–Young relative air mass for a zenith cosine (clamped just below the horizon)
float airMass( float mu ) {
	float z = degrees( acos( clamp( mu, -0.026, 1.0 ) ) );
	return 1.0 / ( max( mu, -0.026 ) + 0.50572 * pow( max( 96.07995 - z, 0.5 ), -1.6364 ) );
}

vec3 physicalSky( vec3 rd ) {
	vec3 ro = vec3( 0.0, max( uCamPos.y, 1.0 ), 0.0 );
	float tMax = shellHit( ro, rd, 60000.0 ).y;
	vec2 hg = shellHit( ro, rd, 0.0 );
	if ( hg.x > 0.0 ) tMax = min( tMax, hg.x );
	if ( tMax <= 0.0 ) return vec3( 0.0 );
	vec3 bR = BETA_R * uRayleigh;
	float bM = BETA_M * uMie;
	float odR = 0.0, odM = 0.0, odO = 0.0;
	vec3 sumR = vec3( 0.0 ), sumM = vec3( 0.0 );
	float n = float( uAtmoSteps );
	for ( int i = 0; i < 32; i++ ) {
		if ( i >= uAtmoSteps ) break;
		// quadratic sample distribution: dense near the camera where the air is dense
		float u = ( float( i ) + 0.5 ) / n;
		float t = tMax * u * u;
		float ds = tMax * 2.0 * u / n;
		vec3 p = ro + rd * t;
		float h = max( altitudeOf( p ), 0.0 );
		float hr = exp( -h / 8000.0 ) * ds;
		float hm = exp( -h / 1200.0 ) * ds;
		odR += hr; odM += hm;
		odO += max( 0.0, 1.0 - abs( h - 25000.0 ) / 15000.0 ) * ds;
		vec3 up = normalize( vec3( p.x / EARTH_R, 1.0, p.z / EARTH_R ) );
		float muS = dot( up, uSunDir );
		if ( shellHit( p, uSunDir, 0.0 ).x > 0.0 ) continue; // earth shadow
		float am = airMass( muS );
		float odRl = 8000.0 * exp( -h / 8000.0 ) * am;
		float odMl = 1200.0 * exp( -h / 1200.0 ) * am;
		float rr = EARTH_R / ( EARTH_R + 25000.0 );
		float odOl = 15000.0 / sqrt( max( 1.0 - rr * rr * ( 1.0 - muS * muS ), 1e-4 ) );
		vec3 att = exp( -( bR * ( odR + odRl ) + bM * 1.1 * ( odM + odMl ) + BETA_O * uRayleigh * ( odO + odOl ) ) );
		sumR += att * hr;
		sumM += att * hm;
	}
	float mu = dot( rd, uSunDir );
	float pR = 3.0 / ( 16.0 * PI ) * ( 1.0 + mu * mu );
	float g = uMieG;
	float pM = 3.0 / ( 8.0 * PI ) * ( ( 1.0 - g * g ) * ( 1.0 + mu * mu ) ) /
	           ( ( 2.0 + g * g ) * pow( max( 1.0 + g * g - 2.0 * g * mu, 1e-4 ), 1.5 ) );
	return ( sumR * bR * pR + sumM * bM * pM ) * 20.0 * uPhysSun;
}

vec3 gradientSky( vec3 rd ) {
	float t = pow( clamp( rd.y, 0.0, 1.0 ), uHorizonFalloff );
	return mix( uHorizon, uZenith, t ) * uSkyIntensity;
}

// sky radiance above the horizon (no sun glow), shared by background and cloud ambient
vec3 skyDome( vec3 rd ) {
	vec3 c = gradientSky( rd );
	if ( uPhysMix > 0.001 ) c = mix( c, physicalSky( rd ) * uPhysIntensity, uPhysMix );
	return c * uSkyTint;
}

vec3 skyBackground( vec3 rd, vec3 ambTop ) {
	vec3 rdc = normalize( vec3( rd.x, max( rd.y, 0.0005 ), rd.z ) );
	vec3 c = skyDome( rdc );
	// atmospheric glow around the sun (forward scattering), occluded by clouds later
	float ang = acos( clamp( dot( rdc, uSunDir ), -1.0, 1.0 ) );
	c += uSunLight * uSunGlow * ( 0.06 * exp( -ang * uSunGlowSharpness ) + 0.012 * exp( -ang * uSunGlowSharpness * 0.2 ) );
	// below the horizon: ground (lit by sun + sky), blended through a haze band
	if ( rd.y < 0.0 ) {
		vec3 ground = uGround * ( uSunLight * max( uSunDir.y, 0.0 ) / PI + ambTop * 0.5 );
		float g = smoothstep( 0.0, 0.01 + uHaze * 0.15, -rd.y );
		c = mix( c, ground, g );
	}
	return c;
}

// ------------------------------------------------------------------ clouds
float heightGradient( float h, float type ) {
	float st = smoothstep( 0.0, 0.07, h ) * ( 1.0 - smoothstep( 0.14, 0.32, h ) );
	float sc = smoothstep( 0.0, 0.12, h ) * ( 1.0 - smoothstep( 0.32, 0.62, h ) );
	float cu = smoothstep( 0.0, 0.06, h ) * ( 1.0 - smoothstep( 0.18, 0.98, h ) );
	return type < 0.5 ? mix( st, sc, type * 2.0 ) : mix( sc, cu, type * 2.0 - 1.0 );
}

// Top-down coverage field, roughly uniform in [0, 1]: decides WHERE clouds are, so "coverage" reads as
// "fraction of the sky covered" and stays (mostly) independent of cloud type / vertical shape.
float coverageField( vec2 q, float ev ) {
	float f = fbmP( vec3( q, ev * 0.5 ), 3 ) * 0.5 + 0.5;
	float c = 1.0 - worley( vec3( q * 1.7, ev * 0.35 ) + 11.0 );
	return smoothstep( 0.24, 0.76, f * 0.6 + c * 0.4 );
}

// lod 0: shape + detail erosion, 1: shape only, 2: cheap shape (light march)
float cloudDensity( vec3 wp, float hf, int lod ) {
	float baseLen = 5200.0 * uCloudScale;
	vec2 q = wp.xz / baseLen + uSeed.xz;
	float weather = gnoise( vec3( wp.xz / ( baseLen * 3.7 ) + uSeed.zx, uEvolve * 0.15 ) ) * 0.5 + 0.5;
	float cov = clamp( uCoverage + ( weather - 0.5 ) * 2.0 * uCoverageVar, 0.0, 1.0 );
	if ( cov < 0.003 ) return 0.0;
	float field = coverageField( q, uEvolve );
	// the coverage threshold rises with the height profile → flat bases and domed tops
	float hgrad = heightGradient( hf, uCloudType );
	float base = clamp( ( field - ( 1.0 - cov * hgrad ) ) * 3.5, 0.0, 1.0 );
	if ( base <= 0.0 ) return 0.0;

	// billowy 3D erosion (cauliflower edges, ~1 km and ~0.6 km features at scale 1)
	vec3 pe = wp / ( baseLen * 0.18 ) + uSeed + vec3( 0.0, uEvolve, 0.0 );
	float perlin = fbmP( pe, lod == 2 ? 2 : 3 ) * 0.5 + 0.5;
	float billow = lod == 2 ? perlin : 1.0 - worley( pe * 1.6 );
	float raw = clamp( ( perlin * 0.45 + billow * 0.55 - 0.2 ) / 0.6, 0.0, 1.0 );
	base = clamp( remap( base, ( 1.0 - raw ) * 0.75, 1.0, 0.0, 1.0 ), 0.0, 1.0 );
	if ( base <= 0.0 ) return 0.0;

	if ( lod == 0 && uDetailStrength > 0.0 ) {
		vec3 dp = wp / ( 280.0 * uCloudScale * uDetailScale ) + uSeed * 3.1 + vec3( 0.0, uEvolve * 3.0, 0.0 );
		float dn = ( 1.0 - worley( dp ) ) * 0.65 + ( 1.0 - worley( dp * 2.3 ) ) * 0.35;
		float dmod = mix( 1.0 - dn, dn, clamp( hf * 4.0, 0.0, 1.0 ) );
		base = clamp( remap( base, dmod * uDetailStrength, 1.0, 0.0, 1.0 ), 0.0, 1.0 );
	}
	return base * uCloudDensity;
}

float hgN( float c, float g ) {
	float g2 = g * g;
	return ( 1.0 - g2 ) / pow( max( 1.0 + g2 - 2.0 * g * c, 1e-4 ), 1.5 );
}
float phaseK( float mu, float s ) {
	float p = mix( hgN( mu, uForwardG * s ), hgN( mu, uBackG * s ), 0.3 );
	p += uSilver * 0.15 * hgN( mu, clamp( 0.99 - uSilverSpread, 0.0, 0.99 ) * s );
	return p;
}

float lightOD( vec3 p ) {
	float Hb = uCloudBottom;
	float thick = max( uCloudTop - uCloudBottom, 10.0 );
	float od = 0.0, t = 0.0;
	float base = thick * 0.035;
	for ( int j = 0; j < 16; j++ ) {
		if ( j >= uLightSteps ) break;
		float sl = base * pow( 1.75, float( j ) );
		vec3 q = p + uSunDir * ( t + sl * 0.5 );
		t += sl;
		float qf = ( altitudeOf( q ) - Hb ) / thick;
		if ( qf > 1.0 || qf < 0.0 ) break;
		od += cloudDensity( q - uWindOffset, qf, 2 ) * sl;
	}
	return od * uExtinction;
}

vec4 marchClouds( vec3 ro, vec3 rd, vec3 ambTop ) {
	float Hb = uCloudBottom;
	float Ht = max( uCloudTop, uCloudBottom + 10.0 );
	float alt0 = ro.y;
	vec2 hb = shellHit( ro, rd, Hb );
	vec2 ht = shellHit( ro, rd, Ht );
	float t0, t1;
	if ( alt0 < Hb ) {
		if ( shellHit( ro, rd, 0.0 ).x > 0.0 || hb.y <= 0.0 ) return vec4( 0.0, 0.0, 0.0, 1.0 );
		t0 = hb.y; t1 = ht.y;
	} else if ( alt0 < Ht ) {
		t0 = 0.0; t1 = ( hb.x > 0.0 ) ? hb.x : ht.y;
	} else {
		if ( ht.x <= 0.0 ) return vec4( 0.0, 0.0, 0.0, 1.0 );
		t0 = ht.x; t1 = ( hb.x > 0.0 ) ? hb.x : ht.y;
	}
	t1 = min( t1, uMaxDist );
	if ( t1 <= t0 ) return vec4( 0.0, 0.0, 0.0, 1.0 );

	float len = t1 - t0;
	// white-noise jitter per pixel (smoothed by the composite tent filter); IGN gave visible combing
	float jit = ( uJitter == 1 ) ? hash33( vec3( floor( gl_FragCoord.xy ), 7.0 ) ).x : 0.5;
	float fn = float( uSteps );
	float T = 1.0;
	vec3 L = vec3( 0.0 );
	float mu = dot( rd, uSunDir );
	float sumW = 0.0, sumTW = 0.0;
	float thick = Ht - Hb;

	for ( int i = 0; i < 256; i++ ) {
		if ( i >= uSteps ) break;
		// samples spaced as u^1.6: fine near the entry (where detail is visible), coarse far away
		float u0 = float( i ) / fn, u1 = ( float( i ) + 1.0 ) / fn;
		float ta = t0 + len * pow( u0, 1.6 ), tb = t0 + len * pow( u1, 1.6 );
		float ds = tb - ta;
		float t = ta + ds * jit;
		vec3 p = ro + rd * t;
		float hf = ( altitudeOf( p ) - Hb ) / thick;
		if ( hf >= 0.0 && hf <= 1.0 ) {
			float d = cloudDensity( p - uWindOffset, hf, 0 );
			if ( d > 0.002 ) {
				float sig = d * uExtinction;
				float odl = lightOD( p );
				float ms = 0.0, a = 1.0, b = 1.0, s = 1.0;
				for ( int k = 0; k < 3; k++ ) {
					ms += a * exp( -odl * b ) * phaseK( mu, s );
					a *= uMultiScatter; b *= 0.3; s *= 0.5;
				}
				float powder = mix( 1.0, 1.0 - exp( -sig * 180.0 ), uPowder * ( 0.75 - 0.25 * mu ) );
				vec3 amb = mix( uCloudShadow, ambTop, clamp( hf * 1.2, 0.0, 1.0 ) ) * uCloudAmbient
				           * mix( 0.55, 1.0, exp( -d * 1.5 ) );
				// sunlight diffused through the layer (keeps thick decks grey-white instead of sky-blue)
				vec3 diffuseSun = uSunLight * uMultiScatter * 0.07 * ( 0.3 + 0.7 * hf ) * clamp( uSunDir.y * 2.0 + 0.2, 0.0, 1.0 );
				vec3 light = uSunLight * uCloudLit * ms * powder + ( amb + diffuseSun * uCloudLit );
				float Ts = exp( -sig * ds );
				float w = T * ( 1.0 - Ts );
				L += light * w;
				sumTW += t * w; sumW += w;
				T *= Ts;
				if ( T < 0.008 ) break;
			}
		}
	}
	if ( sumW > 0.0 ) {
		float dist = sumTW / sumW;
		float f = exp( -dist / max( uFadeDist, 1.0 ) );
		L *= f;
		T = mix( 1.0, T, f );
	}
	return vec4( L, T );
}

vec4 cirrus( vec3 ro, vec3 rd, vec3 ambTop ) {
	if ( rd.y < 0.0 ) return vec4( 0.0 );
	float t = shellHit( ro, rd, uCirrusAlt ).y;
	if ( t <= 0.0 ) return vec4( 0.0 );
	vec3 hp = ro + rd * t;
	vec2 xz = hp.xz - uCirrusOffset;
	vec2 d = uCirrusDir;
	vec2 q = vec2( dot( xz, d ), dot( xz, vec2( -d.y, d.x ) ) ) / ( 9000.0 * uCirrusScale );
	q.x /= max( uCirrusStretch, 0.1 );
	vec3 sd = uSeed * 0.71;
	float ev = uEvolve * 0.3;
	// large-scale patches decide where cirrus exists at all
	float patchN = fbmP( vec3( q * 0.45, ev ) + sd, 3 ) * 0.5 + 0.5;
	float cov = smoothstep( 0.75 - uCirrusCoverage * 0.6, 0.95 - uCirrusCoverage * 0.6, patchN );
	if ( cov <= 0.0 ) return vec4( 0.0 );
	// warped fibres: much higher frequency across the streak direction than along it
	vec2 w = vec2( gnoise( vec3( q * 0.9, ev ) + sd.yzx ), gnoise( vec3( q * 0.9, ev ) + sd.zxy + 5.2 ) );
	vec2 qq = q + w * 0.7;
	float fib = fbmP( vec3( qq.x * 1.6, qq.y * 4.0, ev ) + sd.zyx, 4 ) * 0.5 + 0.5;
	float wisp = fbmP( vec3( qq * 3.1, ev + 3.0 ) + sd, 3 ) * 0.5 + 0.5;
	// break long streaks into tufts / hooks
	float seg = smoothstep( 0.35, 0.7, gnoise( vec3( qq.x * 2.6, qq.y * 1.3, ev + 7.0 ) + sd.yxz ) * 0.5 + 0.5 );
	float n = ( fib * 0.65 + wisp * 0.35 ) * mix( 0.55, 1.0, seg );
	float c = cov * smoothstep( 0.38, 0.72, n );
	c *= c * ( 3.0 - 2.0 * c );
	float fade = smoothstep( 0.0, 0.12, rd.y ) * exp( -t / ( uFadeDist * 3.0 ) );
	float alpha = clamp( c * uCirrusOpacity * fade, 0.0, 1.0 );
	float mu = dot( rd, uSunDir );
	vec3 col = uCirrusColor * ( uSunLight * ( 0.35 + 0.65 * hgN( mu, 0.55 ) * 0.5 ) + ambTop * 0.9 );
	return vec4( col * alpha, alpha );
}

void main() {
	vec3 rd = getRayDir();
	vec3 ro = vec3( uCamPos.x, max( uCamPos.y, 1.0 ), uCamPos.z );

	vec3 ambTop = skyDome( vec3( 0.0, 1.0, 0.0 ) );
	vec3 col = skyBackground( rd, ambTop );
	float T = 1.0;

	if ( uCirrusOn == 1 && uCirrusOpacity > 0.0 && uCirrusCoverage > 0.0 ) {
		vec4 ci = cirrus( ro, rd, ambTop );
		col = col * ( 1.0 - ci.a ) + ci.rgb;
		T *= 1.0 - ci.a;
	}
	if ( uCloudsOn == 1 && uCoverage > 0.0 && rd.y > -0.3 ) {
		vec4 cl = marchClouds( ro, rd, ambTop );
		col = col * cl.a + cl.rgb;
		T *= cl.a;
	}
	if ( uOpacityOut == 1 ) { gl_FragColor = vec4( vec3( 1.0 - T ), 1.0 ); return; } // cloud opacity (fitting)
	if ( uSunDiscInPass == 1 ) col += sunDisc( rd ) * T * smoothstep( 0.01, 0.2, T );

	if ( uApplyPost == 1 ) {
		col = gbxPost( col );
		if ( uOutSRGB == 1 ) col = gbxLinearToSRGB( col );
		gl_FragColor = vec4( col, 1.0 );
	} else {
		if ( uLinearGrade == 1 ) {
			col *= uGrade;
			col = max( mix( vec3( luma( col ) ), col, uSaturation ), 0.0 );
		}
		gl_FragColor = vec4( col * uBakeScale, uSunDiscInPass == 1 ? 1.0 : T );
	}
}
`;

// ---------------------------------------------------------------------------------------------
// Composite (in-scene) pass
// ---------------------------------------------------------------------------------------------
const COMPOSITE_FRAGMENT = /* glsl */`
precision highp float;
${ COMMON }
uniform sampler2D tSky;
uniform vec2 uSkyTexel;
uniform float uSmooth;
void main() {
	// tent-filtered upsample: hides raymarch jitter of the low-res pass
	vec2 uv = vNdc * 0.5 + 0.5;
	vec2 o = uSkyTexel * uSmooth;
	vec4 s = texture2D( tSky, uv ) * 0.4
		+ ( texture2D( tSky, uv + vec2( o.x, o.y ) ) + texture2D( tSky, uv + vec2( -o.x, o.y ) )
		  + texture2D( tSky, uv + vec2( o.x, -o.y ) ) + texture2D( tSky, uv + vec2( -o.x, -o.y ) ) ) * 0.15;
	vec3 rd = getRayDir();
	// disc through cloud: T, with extra suppression when nearly opaque (avoids a white dot through overcast)
	vec3 col = s.rgb + sunDisc( rd ) * s.a * smoothstep( 0.01, 0.2, s.a );
	gl_FragColor = vec4( gbxPost( col ), 1.0 );
	#include <colorspace_fragment>
}
`;

function commonUniforms() {
	return {
		uInvProj: { value: new THREE.Matrix4() },
		uCamToWorld: { value: new THREE.Matrix4() },
		uEquirect: { value: 0 },
		uTileOffset: { value: new THREE.Vector2( 0, 0 ) },
		uTileScale: { value: new THREE.Vector2( 1, 1 ) },
		uSunDir: { value: new THREE.Vector3( 0, 1, 0 ) },
		uSunLight: { value: new THREE.Vector3( 1, 1, 1 ) },
		uSunDiscSize: { value: 0.00465 },
		uSunDiscIntensity: { value: 40 },
		uExposure: { value: 1 },
		uTonemap: { value: 1 },
		uSaturation: { value: 1 },
		uContrast: { value: 1 },
		uGrade: { value: new THREE.Vector3( 1, 1, 1 ) },
	};
}

export function createSkyPassMaterial() {
	const uniforms = {
		...commonUniforms(),
		uCamPos: { value: new THREE.Vector3( 0, 2, 0 ) },
		uTime: { value: 0 },
		uPhysMix: { value: 0.5 },
		uZenith: { value: new THREE.Vector3() },
		uHorizon: { value: new THREE.Vector3() },
		uGround: { value: new THREE.Vector3() },
		uHorizonFalloff: { value: 0.4 },
		uSkyIntensity: { value: 1 },
		uPhysIntensity: { value: 1 },
		uSkyTint: { value: new THREE.Vector3( 1, 1, 1 ) },
		uRayleigh: { value: 1 },
		uMie: { value: 1 },
		uMieG: { value: 0.76 },
		uHaze: { value: 0.2 },
		uPhysSun: { value: new THREE.Vector3( 1, 1, 1 ) },
		uSunGlow: { value: 1 },
		uSunGlowSharpness: { value: 12 },
		uAtmoSteps: { value: 10 },
		uCloudsOn: { value: 1 },
		uCoverage: { value: 0.45 },
		uCloudDensity: { value: 1 },
		uCloudType: { value: 0.7 },
		uCloudBottom: { value: 1500 },
		uCloudTop: { value: 4000 },
		uCloudScale: { value: 1 },
		uDetailScale: { value: 1 },
		uDetailStrength: { value: 0.35 },
		uCoverageVar: { value: 0.3 },
		uSeed: { value: new THREE.Vector3() },
		uCloudLit: { value: new THREE.Vector3( 1, 1, 1 ) },
		uCloudShadow: { value: new THREE.Vector3( 0.2, 0.22, 0.27 ) },
		uCloudAmbient: { value: 1 },
		uSilver: { value: 0.6 },
		uSilverSpread: { value: 0.25 },
		uForwardG: { value: 0.6 },
		uBackG: { value: -0.3 },
		uPowder: { value: 0.5 },
		uMultiScatter: { value: 0.5 },
		uExtinction: { value: 0.04 },
		uFadeDist: { value: 35000 },
		uMaxDist: { value: 80000 },
		uWindOffset: { value: new THREE.Vector3() },
		uEvolve: { value: 0 },
		uSteps: { value: 48 },
		uLightSteps: { value: 6 },
		uJitter: { value: 1 },
		uCirrusOn: { value: 1 },
		uCirrusCoverage: { value: 0.3 },
		uCirrusOpacity: { value: 0.6 },
		uCirrusAlt: { value: 8000 },
		uCirrusScale: { value: 1 },
		uCirrusStretch: { value: 4 },
		uCirrusDir: { value: new THREE.Vector2( 1, 0 ) },
		uCirrusOffset: { value: new THREE.Vector2() },
		uCirrusColor: { value: new THREE.Vector3( 1, 1, 1 ) },
		uSunDiscInPass: { value: 0 },
		uApplyPost: { value: 0 },
		uOutSRGB: { value: 0 },
		uLinearGrade: { value: 0 },
		uBakeScale: { value: 1 },
		uOpacityOut: { value: 0 },
	};
	return new THREE.ShaderMaterial( {
		name: 'GeoBoxSkyPass',
		uniforms,
		vertexShader: VERTEX,
		fragmentShader: SKY_FRAGMENT,
		depthTest: false,
		depthWrite: false,
		toneMapped: false,
	} );
}

export function createCompositeMaterial( skyTexture ) {
	const uniforms = {
		...commonUniforms(),
		tSky: { value: skyTexture },
		uSkyTexel: { value: new THREE.Vector2( 1 / 16, 1 / 16 ) },
		uSmooth: { value: 0.75 },
	};
	return new THREE.ShaderMaterial( {
		name: 'GeoBoxSkyComposite',
		uniforms,
		vertexShader: VERTEX,
		fragmentShader: COMPOSITE_FRAGMENT,
		depthTest: false,
		depthWrite: false,
		toneMapped: false,
	} );
}

/** Names of uniforms shared by both materials (SkySystem copies these from pass → composite). */
export const SHARED_UNIFORMS = Object.keys( commonUniforms() );

export function createFullscreenTriangle() {
	const g = new THREE.BufferGeometry();
	g.setAttribute( 'position', new THREE.Float32BufferAttribute( [ -1, -1, 0, 3, -1, 0, -1, 3, 0 ], 3 ) );
	return g;
}
