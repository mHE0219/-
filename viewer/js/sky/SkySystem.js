// GeoBox Sky — SkySystem: reusable sky + cloud background for three.js scenes.
//
//   import { SkySystem, DEFAULT_SKY_PARAMS } from './sky/SkySystem.js';
//   const sky = new SkySystem( renderer, { params } );   // params = partial sky_params.json
//   scene.add( sky.object );                             // full-screen background, drawn first
//   // per frame:
//   sky.update( dt, camera );
//   dirLight.position.copy( sky.sunDirection ).multiplyScalar( 1000 );
//   dirLight.color.copy( sky.sunColor ); dirLight.intensity = sky.sunIntensity;
//   hemiLight.color.copy( sky.ambientColor ); hemiLight.intensity = sky.ambientIntensity;
//
// Tone mapping: the sky does its OWN exposure + tonemap + grading (params.post) and writes display
// values through the renderer's output colour space; it ignores renderer.toneMapping
// (material.toneMapped = false). To make city meshes match, call sky.configureRenderer( renderer ):
// it sets renderer.toneMapping / toneMappingExposure to the same operator (three.js' own ACES/AgX/
// Neutral/Reinhard implementations are used inside the sky shader, so they match exactly). Grading
// (saturation/contrast/grade tint) is sky-only.
//
// Performance: the heavy pass (atmosphere + raymarched clouds) renders into an HDR target at
// params.quality.renderScale of the drawing buffer and is upsampled; the sun disc is added at full
// resolution. The pass is skipped when camera, time and params are unchanged.
//
// Colours in params are sRGB hex strings ('#rrggbb'); they are converted to linear for shading.

import * as THREE from 'three';
import {
	createSkyPassMaterial, createCompositeMaterial, createFullscreenTriangle,
	SHARED_UNIFORMS, TONEMAP_MODES,
} from './skyShader.js';

export const SKY_PARAMS_VERSION = 1;

export const DEFAULT_SKY_PARAMS = deepFreeze( {
	version: SKY_PARAMS_VERSION,
	type: 'geobox.sky',
	name: 'default',
	sun: {
		azimuth: 150,          // deg, clockwise from north
		elevation: 38,         // deg above horizon
		color: '#fff3e2',
		intensity: 3.0,        // = DirectionalLight intensity in the city viewer
		discSize: 1.0,         // x real angular size (0.53 deg)
		discIntensity: 40,
		glow: 1.0,
		glowSharpness: 12,
	},
	atmosphere: {
		physicalMix: 0.6,      // 0 = artist gradient, 1 = physical single scattering
		physicalIntensity: 1.0,
		zenithColor: '#3b78d8',
		horizonColor: '#b9d0ea',
		groundColor: '#6b665e',
		horizonFalloff: 0.45,  // gradient exponent (smaller = zenith colour reaches lower)
		skyIntensity: 0.6,
		tint: '#ffffff',
		rayleigh: 1.0,
		mie: 1.0,              // turbidity / aerosols
		mieG: 0.76,
		haze: 0.2,             // horizon/ground blend band
		sunReddening: 1.0,     // 0 = sun colour as-is, 1 = physically reddened at low elevation
	},
	clouds: {
		enabled: true,
		coverage: 0.42,
		density: 1.0,
		type: 0.75,            // 0 stratus, 0.5 stratocumulus, 1 cumulus
		bottom: 1500,          // m
		top: 4200,             // m
		scale: 1.0,            // feature size multiplier (base ~5.2 km)
		detailScale: 1.0,
		detailStrength: 0.35,
		coverageVariation: 0.3,
		seed: 1,
		litColor: '#ffffff',
		litIntensity: 1.0,
		shadowColor: '#76859e',
		ambient: 1.0,
		silverIntensity: 0.6,
		silverSpread: 0.25,
		forwardScatter: 0.6,
		backScatter: -0.3,
		powder: 0.5,
		multiScatter: 0.5,
		extinction: 0.04,      // 1/m at density 1
		fadeDistance: 35000,   // m, aerial-perspective fade
		maxDistance: 80000,    // m, raymarch cut-off
	},
	cirrus: {
		enabled: true,
		coverage: 0.25,
		opacity: 0.5,
		altitude: 8500,
		scale: 1.0,
		stretch: 3.0,
		direction: 60,         // deg compass heading of the streaks / their drift
		speed: 25,             // m/s
		color: '#ffffff',
	},
	animation: {
		timeScale: 8,          // sky seconds per real second
		paused: false,
		windDirection: 70,     // deg compass heading the clouds move TOWARD
		windSpeed: 12,         // m/s
		evolveSpeed: 1.0,      // shape morphing rate (1 = one noise period per 10 sky-minutes)
	},
	post: {
		exposure: 1.0,
		tonemap: 'aces',       // none | aces | agx | neutral | reinhard
		saturation: 1.0,
		contrast: 1.0,
		grade: '#ffffff',      // white-balance / grade tint
	},
	quality: {
		steps: 48,
		lightSteps: 6,
		atmosphereSteps: 10,
		renderScale: 0.5,
		jitter: true,
	},
	view: {                    // sky.html editor camera (ignored by SkySystem)
		fov: 60,
		yaw: 0,
		pitch: 12,
		height: 2,
	},
} );

// ------------------------------------------------------------------------------------------------
// helpers (exported for tests / other modules)
// ------------------------------------------------------------------------------------------------
export function deepClone( o ) { return JSON.parse( JSON.stringify( o ) ); }

function deepFreeze( o ) {
	Object.values( o ).forEach( ( v ) => { if ( v && typeof v === 'object' ) deepFreeze( v ); } );
	return Object.freeze( o );
}

/** Deep-assign `src` into `dst` in place (plain objects recurse, everything else is copied). */
export function deepAssign( dst, src ) {
	if ( ! src || typeof src !== 'object' ) return dst;
	for ( const k of Object.keys( src ) ) {
		const v = src[ k ];
		if ( v && typeof v === 'object' && ! Array.isArray( v ) ) {
			if ( ! dst[ k ] || typeof dst[ k ] !== 'object' ) dst[ k ] = {};
			deepAssign( dst[ k ], v );
		} else if ( v !== undefined ) dst[ k ] = Array.isArray( v ) ? v.slice() : v;
	}
	return dst;
}

/** Deep-merge `partial` into a copy of `base` (only keys of plain objects are merged). */
export function mergeParams( base, partial ) {
	const out = deepClone( base );
	const rec = ( dst, src ) => {
		if ( ! src || typeof src !== 'object' ) return;
		for ( const k of Object.keys( src ) ) {
			const v = src[ k ];
			if ( v && typeof v === 'object' && ! Array.isArray( v ) && dst[ k ] && typeof dst[ k ] === 'object' ) rec( dst[ k ], v );
			else if ( v !== undefined ) dst[ k ] = Array.isArray( v ) ? v.slice() : v;
		}
	};
	rec( out, partial );
	return out;
}

export function srgbToLinear( c ) { return c <= 0.04045 ? c / 12.92 : Math.pow( ( c + 0.055 ) / 1.055, 2.4 ); }
export function linearToSrgb( c ) { c = Math.max( 0, Math.min( 1, c ) ); return c <= 0.0031308 ? c * 12.92 : 1.055 * Math.pow( c, 1 / 2.4 ) - 0.055; }

/** '#rrggbb' (sRGB) -> [r,g,b] linear */
export function hexToLinear( hex ) {
	let h = String( hex || '#ffffff' ).trim().replace( '#', '' );
	if ( h.length === 3 ) h = h.split( '' ).map( ( c ) => c + c ).join( '' );
	const n = parseInt( h.slice( 0, 6 ), 16 );
	if ( Number.isNaN( n ) ) return [ 1, 1, 1 ];
	return [ ( n >> 16 ) & 255, ( n >> 8 ) & 255, n & 255 ].map( ( v ) => srgbToLinear( v / 255 ) );
}

/** [r,g,b] linear -> '#rrggbb' (clamped) */
export function linearToHex( rgb ) {
	return '#' + rgb.map( ( v ) => Math.round( linearToSrgb( v ) * 255 ).toString( 16 ).padStart( 2, '0' ) ).join( '' );
}

/** Unit vector toward the sun. azimuth: deg clockwise from north (-Z), elevation: deg. */
export function sunDirectionFromAngles( azimuthDeg, elevationDeg, target = new THREE.Vector3() ) {
	const az = THREE.MathUtils.degToRad( azimuthDeg );
	const el = THREE.MathUtils.degToRad( elevationDeg );
	return target.set( Math.sin( az ) * Math.cos( el ), Math.sin( el ), - Math.cos( az ) * Math.cos( el ) );
}

/** Compass heading (deg) -> horizontal unit vector [x, z]. */
export function headingToXZ( deg ) {
	const a = THREE.MathUtils.degToRad( deg );
	return [ Math.sin( a ), - Math.cos( a ) ];
}

/** Relative sun transmittance (1 at zenith) for an elevation, Kasten–Young air mass. */
export function sunTransmittance( elevationDeg, rayleigh = 1, mie = 1 ) {
	const z = Math.min( 90 - Math.max( elevationDeg, - 2 ), 91.5 );
	const zr = THREE.MathUtils.degToRad( z );
	const airmass = 1 / Math.max( Math.cos( zr ) + 0.50572 * Math.pow( 96.07995 - z, - 1.6364 ), 1e-3 );
	const tauR = [ 5.802e-6, 13.558e-6, 33.1e-6 ].map( ( b ) => b * 8000 * rayleigh );
	const tauM = 21e-6 * 1.1 * 1200 * mie;
	return tauR.map( ( t ) => Math.exp( - ( t + tauM ) * ( airmass - 1 ) ) );
}

function seedVector( seed ) {
	const f = ( k ) => { const x = Math.sin( ( seed + 1 ) * k ) * 43758.5453; return ( x - Math.floor( x ) ) * 97.0; };
	return new THREE.Vector3( f( 12.9898 ), f( 78.233 ), f( 37.719 ) );
}

const _v2 = new THREE.Vector2();

// ------------------------------------------------------------------------------------------------
export class SkySystem {

	constructor( renderer, { params = {} } = {} ) {
		this.renderer = renderer;
		this.params = mergeParams( DEFAULT_SKY_PARAMS, params );
		this.time = 0;

		this.sunDirection = new THREE.Vector3( 0, 1, 0 );
		this.sunColor = new THREE.Color( 1, 1, 1 );
		this.sunIntensity = 3;
		this.ambientColor = new THREE.Color( 0.5, 0.6, 0.8 );
		this.ambientIntensity = 1;
		this.groundColor = new THREE.Color( 0.3, 0.3, 0.3 );

		this._passMaterial = createSkyPassMaterial();
		this._passMesh = new THREE.Mesh( createFullscreenTriangle(), this._passMaterial );
		this._passMesh.frustumCulled = false;
		this._passScene = new THREE.Scene();
		this._passScene.add( this._passMesh );
		this._passCamera = new THREE.OrthographicCamera( - 1, 1, 1, - 1, 0, 1 );

		this._rtType = this._pickHdrType();
		this._rt = new THREE.WebGLRenderTarget( 16, 16, {
			type: this._rtType, format: THREE.RGBAFormat, depthBuffer: false,
			minFilter: THREE.LinearFilter, magFilter: THREE.LinearFilter, generateMipmaps: false,
		} );

		this._compositeMaterial = createCompositeMaterial( this._rt.texture );
		this.object = new THREE.Mesh( createFullscreenTriangle(), this._compositeMaterial );
		this.object.name = 'GeoBoxSky';
		this.object.frustumCulled = false;
		this.object.renderOrder = - 1;
		this.object.onBeforeRender = ( r, scene, camera ) => this._renderPass( r, camera );

		this._dirty = true;
		this._lastKey = null;
		this.syncUniforms();
	}

	// ------------------------------------------------------------------ params
	getParams() { return deepClone( this.params ); }

	/** Deep-merge a partial params object (in place: this.params keeps its identity for UI bindings). */
	setParams( partial ) {
		deepAssign( this.params, partial || {} );
		this.syncUniforms();
	}

	/** Replace all params (missing keys fall back to defaults); this.params keeps its identity. */
	resetParams( full = {} ) {
		deepAssign( this.params, mergeParams( DEFAULT_SKY_PARAMS, full ) );
		this.syncUniforms();
	}

	/** Call after mutating this.params in place (e.g. from lil-gui). */
	syncUniforms() {
		const p = this.params;
		const u = this._passMaterial.uniforms;
		const lin = ( hex ) => new THREE.Vector3( ...hexToLinear( hex ) );

		// sun
		sunDirectionFromAngles( p.sun.azimuth, p.sun.elevation, this.sunDirection );
		const sunLin = hexToLinear( p.sun.color );
		const tr = sunTransmittance( p.sun.elevation, p.atmosphere.rayleigh, p.atmosphere.mie );
		const fade = THREE.MathUtils.smoothstep( p.sun.elevation, - 4, 1 );
		const red = p.atmosphere.sunReddening;
		const sunLight = sunLin.map( ( c, i ) => c * p.sun.intensity * ( 1 + ( tr[ i ] - 1 ) * red ) * fade );
		const peak = Math.max( ...sunLight, 1e-6 );
		this.sunColor.setRGB( sunLight[ 0 ] / peak, sunLight[ 1 ] / peak, sunLight[ 2 ] / peak, THREE.LinearSRGBColorSpace );
		this.sunIntensity = peak;

		u.uSunDir.value.copy( this.sunDirection );
		u.uSunLight.value.set( ...sunLight );
		u.uSunDiscSize.value = 0.004625 * p.sun.discSize;
		u.uSunDiscIntensity.value = p.sun.discIntensity;
		u.uSunGlow.value = p.sun.glow;
		u.uSunGlowSharpness.value = p.sun.glowSharpness;
		u.uPhysSun.value.set( ...sunLin.map( ( c ) => c * p.sun.intensity / 3 ) );

		// atmosphere
		const a = p.atmosphere;
		u.uPhysMix.value = a.physicalMix;
		u.uPhysIntensity.value = a.physicalIntensity;
		u.uZenith.value.copy( lin( a.zenithColor ) );
		u.uHorizon.value.copy( lin( a.horizonColor ) );
		u.uGround.value.copy( lin( a.groundColor ) );
		u.uHorizonFalloff.value = Math.max( a.horizonFalloff, 0.01 );
		u.uSkyIntensity.value = a.skyIntensity;
		u.uSkyTint.value.copy( lin( a.tint ) );
		u.uRayleigh.value = a.rayleigh;
		u.uMie.value = a.mie;
		u.uMieG.value = a.mieG;
		u.uHaze.value = a.haze;

		// clouds
		const c = p.clouds;
		u.uCloudsOn.value = c.enabled ? 1 : 0;
		u.uCoverage.value = c.coverage;
		u.uCloudDensity.value = c.density;
		u.uCloudType.value = c.type;
		u.uCloudBottom.value = c.bottom;
		u.uCloudTop.value = Math.max( c.top, c.bottom + 10 );
		u.uCloudScale.value = Math.max( c.scale, 0.05 );
		u.uDetailScale.value = Math.max( c.detailScale, 0.05 );
		u.uDetailStrength.value = c.detailStrength;
		u.uCoverageVar.value = c.coverageVariation;
		u.uSeed.value.copy( seedVector( c.seed ) );
		u.uCloudLit.value.copy( lin( c.litColor ).multiplyScalar( c.litIntensity ) );
		u.uCloudShadow.value.copy( lin( c.shadowColor ) );
		u.uCloudAmbient.value = c.ambient;
		u.uSilver.value = c.silverIntensity;
		u.uSilverSpread.value = c.silverSpread;
		u.uForwardG.value = c.forwardScatter;
		u.uBackG.value = c.backScatter;
		u.uPowder.value = c.powder;
		u.uMultiScatter.value = c.multiScatter;
		u.uExtinction.value = c.extinction;
		u.uFadeDist.value = c.fadeDistance;
		u.uMaxDist.value = c.maxDistance;

		// cirrus
		const ci = p.cirrus;
		u.uCirrusOn.value = ci.enabled ? 1 : 0;
		u.uCirrusCoverage.value = ci.coverage;
		u.uCirrusOpacity.value = ci.opacity;
		u.uCirrusAlt.value = ci.altitude;
		u.uCirrusScale.value = Math.max( ci.scale, 0.05 );
		u.uCirrusStretch.value = ci.stretch;
		u.uCirrusDir.value.set( ...headingToXZ( ci.direction ) );
		u.uCirrusColor.value.copy( lin( ci.color ) );

		// quality
		const q = p.quality;
		u.uSteps.value = Math.round( THREE.MathUtils.clamp( q.steps, 4, 256 ) );
		u.uLightSteps.value = Math.round( THREE.MathUtils.clamp( q.lightSteps, 1, 16 ) );
		u.uAtmoSteps.value = Math.round( THREE.MathUtils.clamp( q.atmosphereSteps, 2, 32 ) );
		u.uJitter.value = q.jitter ? 1 : 0;

		// post
		const po = p.post;
		u.uExposure.value = po.exposure;
		u.uTonemap.value = TONEMAP_MODES[ po.tonemap ] ?? 1;
		u.uSaturation.value = po.saturation;
		u.uContrast.value = po.contrast;
		u.uGrade.value.copy( lin( po.grade ) );

		// ambient estimate (for HemisphereLight): zenith radiance of the dome
		const zen = hexToLinear( a.zenithColor ).map( ( v ) => v * a.skyIntensity );
		const sunUp = THREE.MathUtils.clamp( Math.sin( THREE.MathUtils.degToRad( p.sun.elevation ) ) * 1.4 + 0.08, 0.02, 1 );
		const phys = [ 0.075, 0.15, 0.34 ].map( ( v, i ) => v * sunLin[ i ] * p.sun.intensity / 3 * a.physicalIntensity * sunUp );
		const tint = hexToLinear( a.tint );
		const amb = zen.map( ( v, i ) => ( v * ( 1 - a.physicalMix ) + phys[ i ] * a.physicalMix ) * tint[ i ] );
		const ap = Math.max( ...amb, 1e-6 );
		this.ambientColor.setRGB( amb[ 0 ] / ap, amb[ 1 ] / ap, amb[ 2 ] / ap, THREE.LinearSRGBColorSpace );
		this.ambientIntensity = ap * Math.PI;
		const gr = hexToLinear( a.groundColor );
		this.groundColor.setRGB( gr[ 0 ], gr[ 1 ], gr[ 2 ], THREE.LinearSRGBColorSpace );

		this._applyTime();
		this._copyShared();
		this._dirty = true;
	}

	_applyTime() {
		const p = this.params;
		const u = this._passMaterial.uniforms;
		const t = this.time;
		const [ wx, wz ] = headingToXZ( p.animation.windDirection );
		u.uWindOffset.value.set( wx * p.animation.windSpeed * t, 0, wz * p.animation.windSpeed * t );
		u.uEvolve.value = t * p.animation.evolveSpeed / 600;
		const [ cx, cz ] = headingToXZ( p.cirrus.direction );
		u.uCirrusOffset.value.set( cx * p.cirrus.speed * t, cz * p.cirrus.speed * t );
		u.uTime.value = t;
	}

	_copyShared() {
		const src = this._passMaterial.uniforms;
		const dst = this._compositeMaterial.uniforms;
		for ( const k of SHARED_UNIFORMS ) {
			if ( k === 'uInvProj' || k === 'uCamToWorld' || k === 'uEquirect' || k === 'uTileOffset' || k === 'uTileScale' ) continue;
			const v = src[ k ].value;
			if ( v && v.isVector3 ) dst[ k ].value.copy( v ); else dst[ k ].value = v;
		}
	}

	// ------------------------------------------------------------------ animation
	/** Advance animation by dt real seconds (scaled by animation.timeScale unless paused). */
	update( dt = 0, camera = null ) {
		const a = this.params.animation;
		if ( ! a.paused && dt ) this.setTime( this.time + dt * a.timeScale );
		if ( camera ) camera.updateMatrixWorld();
	}

	setTime( t ) {
		this.time = t;
		this._applyTime();
		this._dirty = true;
	}

	/** Matches renderer tone mapping to the sky's so scene meshes use the same operator/exposure. */
	configureRenderer( renderer = this.renderer ) {
		const map = {
			none: THREE.NoToneMapping, aces: THREE.ACESFilmicToneMapping, agx: THREE.AgXToneMapping,
			neutral: THREE.NeutralToneMapping, reinhard: THREE.ReinhardToneMapping,
		};
		renderer.toneMapping = map[ this.params.post.tonemap ] ?? THREE.ACESFilmicToneMapping;
		renderer.toneMappingExposure = this.params.post.exposure;
	}

	// ------------------------------------------------------------------ rendering
	_pickHdrType() {
		const gl = this.renderer.getContext();
		const isWebGL2 = typeof WebGL2RenderingContext !== 'undefined' && gl instanceof WebGL2RenderingContext;
		if ( isWebGL2 && ( gl.getExtension( 'EXT_color_buffer_float' ) || gl.getExtension( 'EXT_color_buffer_half_float' ) ) ) return THREE.HalfFloatType;
		return THREE.UnsignedByteType;
	}

	_setCamera( material, camera ) {
		material.uniforms.uInvProj.value.copy( camera.projectionMatrixInverse );
		material.uniforms.uCamToWorld.value.copy( camera.matrixWorld );
	}

	_renderPass( renderer, camera ) {
		const current = renderer.getRenderTarget();
		let w, h;
		if ( current ) { w = current.width; h = current.height; } else { renderer.getDrawingBufferSize( _v2 ); w = _v2.x; h = _v2.y; }
		const s = THREE.MathUtils.clamp( this.params.quality.renderScale, 0.1, 1 );
		const rw = Math.max( 16, Math.round( w * s ) );
		const rh = Math.max( 16, Math.round( h * s ) );
		if ( this._rt.width !== rw || this._rt.height !== rh ) {
			this._rt.setSize( rw, rh );
			this._compositeMaterial.uniforms.uSkyTexel.value.set( 1 / rw, 1 / rh );
			this._dirty = true;
		}
		this._compositeMaterial.uniforms.uSmooth.value = this.params.quality.jitter ? 0.85 : 0.0;

		this._setCamera( this._compositeMaterial, camera );
		const key = camera.matrixWorld.elements.join( ',' ) + '|' + camera.projectionMatrix.elements.join( ',' );
		if ( ! this._dirty && key === this._lastKey ) return;
		this._lastKey = key;
		this._dirty = false;

		const u = this._passMaterial.uniforms;
		this._setCamera( this._passMaterial, camera );
		u.uCamPos.value.setFromMatrixPosition( camera.matrixWorld );
		u.uEquirect.value = 0;
		u.uTileOffset.value.set( 0, 0 );
		u.uTileScale.value.set( 1, 1 );
		u.uSunDiscInPass.value = 0;
		u.uApplyPost.value = 0;
		u.uOutSRGB.value = 0;
		u.uLinearGrade.value = 0;
		u.uBakeScale.value = 1;
		this._renderInto( renderer, this._rt );
		const viewport = camera.viewport;
		if ( viewport !== undefined ) renderer.state.viewport( viewport );
	}

	_renderInto( renderer, target ) {
		const prevTarget = renderer.getRenderTarget();
		const prevXr = renderer.xr.enabled;
		const prevShadow = renderer.shadowMap.autoUpdate;
		const prevAutoClear = renderer.autoClear;
		renderer.xr.enabled = false;
		renderer.shadowMap.autoUpdate = false;
		renderer.autoClear = true;
		renderer.setRenderTarget( target );
		renderer.render( this._passScene, this._passCamera );
		renderer.setRenderTarget( prevTarget );
		renderer.autoClear = prevAutoClear;
		renderer.xr.enabled = prevXr;
		renderer.shadowMap.autoUpdate = prevShadow;
	}

	/** Perspective camera matching the sky.html / refFit view description. */
	static makeViewCamera( { fov = 60, aspect = 16 / 9, yaw = 0, pitch = 0, height = 2 } = {} ) {
		const cam = new THREE.PerspectiveCamera( fov, aspect, 0.1, 1e7 );
		cam.position.set( 0, height, 0 );
		cam.rotation.order = 'YXZ';
		cam.rotation.set( THREE.MathUtils.degToRad( pitch ), - THREE.MathUtils.degToRad( yaw ), 0 );
		cam.updateMatrixWorld();
		cam.updateProjectionMatrix();
		return cam;
	}

	/**
	 * Render a display-referred view (post applied, sRGB 8-bit) and read it back.
	 * Returns { width, height, data: Uint8ClampedArray RGBA } with rows top→bottom (image order).
	 */
	renderView( { width = 160, height = 90, camera = null, fov = 60, yaw = 0, pitch = 0, camHeight = 2, includeSun = true, opacity = false } = {} ) {
		const r = this.renderer;
		const cam = camera || SkySystem.makeViewCamera( { fov, aspect: width / height, yaw, pitch, height: camHeight } );
		const rt = new THREE.WebGLRenderTarget( width, height, { type: THREE.UnsignedByteType, depthBuffer: false } );
		const u = this._passMaterial.uniforms;
		this._setCamera( this._passMaterial, cam );
		u.uCamPos.value.copy( cam.position );
		u.uEquirect.value = 0;
		u.uTileOffset.value.set( 0, 0 );
		u.uTileScale.value.set( 1, 1 );
		u.uSunDiscInPass.value = includeSun ? 1 : 0;
		u.uApplyPost.value = 1;
		u.uOutSRGB.value = 1;
		u.uOpacityOut.value = opacity ? 1 : 0;
		this._renderInto( r, rt );
		u.uOpacityOut.value = 0;
		const buf = new Uint8Array( width * height * 4 );
		r.readRenderTargetPixels( rt, 0, 0, width, height, buf );
		rt.dispose();
		u.uApplyPost.value = 0; u.uOutSRGB.value = 0; u.uSunDiscInPass.value = 0;
		this._dirty = true;
		return { width, height, data: flipRows( new Uint8ClampedArray( buf.buffer ), width, height, 4 ) };
	}

	/**
	 * Render a linear HDR lat-long panorama (three.js equirect convention: u=0.5 → +X ... see docs/sky.md).
	 * Rendered in horizontal strips to avoid GPU timeouts. Returns Float32Array RGBA, rows top→bottom.
	 */
	renderEquirect( { width = 2048, height = 1024, includeSun = true, applyGrade = true, scale = 1, strips = 0, stepMultiplier = 2 } = {} ) {
		const r = this.renderer;
		const gl = r.getContext();
		const type = gl.getExtension( 'EXT_color_buffer_float' ) ? THREE.FloatType
			: ( gl.getExtension( 'EXT_color_buffer_half_float' ) ? THREE.HalfFloatType : THREE.UnsignedByteType );
		if ( type === THREE.UnsignedByteType ) console.warn( 'SkySystem.renderEquirect: no float render targets, bake is LDR.' );
		const nStrips = strips || Math.max( 1, Math.ceil( ( width * height ) / ( 512 * 512 ) ) );
		const stripH = Math.ceil( height / nStrips );
		const out = new Float32Array( width * height * 4 );
		const rt = new THREE.WebGLRenderTarget( width, stripH, { type, depthBuffer: false } );
		const u = this._passMaterial.uniforms;
		u.uEquirect.value = 1;
		u.uCamPos.value.set( 0, Math.max( this.params.view?.height ?? 2, 1 ), 0 );
		u.uSunDiscInPass.value = includeSun ? 1 : 0;
		u.uApplyPost.value = 0;
		u.uLinearGrade.value = applyGrade ? 1 : 0;
		u.uBakeScale.value = scale;
		// bakes: more steps + jitter (grain integrates away in lighting, banding would not)
		const prevJitter = u.uJitter.value;
		const prevSteps = u.uSteps.value;
		u.uJitter.value = 1;
		u.uSteps.value = Math.min( 256, Math.round( prevSteps * stepMultiplier ) );
		const n = width * stripH * 4;
		const buf = type === THREE.FloatType ? new Float32Array( n ) : ( type === THREE.HalfFloatType ? new Uint16Array( n ) : new Uint8Array( n ) );
		const strip = new Float32Array( n );
		for ( let s = 0; s < nStrips; s ++ ) {
			const y0 = s * stripH;
			const hh = Math.min( stripH, height - y0 );
			u.uTileOffset.value.set( 0, y0 / height );
			u.uTileScale.value.set( 1, stripH / height );
			this._renderInto( r, rt );
			r.readRenderTargetPixels( rt, 0, 0, width, stripH, buf );
			if ( type === THREE.FloatType ) strip.set( buf );
			else if ( type === THREE.HalfFloatType ) for ( let i = 0; i < n; i ++ ) strip[ i ] = THREE.DataUtils.fromHalfFloat( buf[ i ] );
			else for ( let i = 0; i < n; i ++ ) strip[ i ] = buf[ i ] / 255;
			// strip row 0 = lowest latitude of the strip; output rows are image order (row 0 = zenith)
			for ( let row = 0; row < hh; row ++ ) {
				const dstRow = height - 1 - ( y0 + row );
				out.set( strip.subarray( row * width * 4, ( row + 1 ) * width * 4 ), dstRow * width * 4 );
			}
		}
		rt.dispose();
		u.uEquirect.value = 0;
		u.uTileOffset.value.set( 0, 0 );
		u.uTileScale.value.set( 1, 1 );
		u.uSunDiscInPass.value = 0;
		u.uLinearGrade.value = 0;
		u.uBakeScale.value = 1;
		u.uJitter.value = prevJitter;
		u.uSteps.value = prevSteps;
		this._dirty = true;
		return out;
	}

	dispose() {
		this._rt.dispose();
		this._passMaterial.dispose();
		this._compositeMaterial.dispose();
		this._passMesh.geometry.dispose();
		this.object.geometry.dispose();
		if ( this.object.parent ) this.object.parent.remove( this.object );
	}

}

function flipRows( data, w, h, c ) {
	const out = new data.constructor( data.length );
	const row = w * c;
	for ( let y = 0; y < h; y ++ ) out.set( data.subarray( y * row, ( y + 1 ) * row ), ( h - 1 - y ) * row );
	return out;
}

