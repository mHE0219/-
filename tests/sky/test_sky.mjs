// Node tests for the pure parts of the sky system (no browser / WebGL needed).
//   node tests/sky/test_sky.mjs
import assert from 'node:assert/strict';
import { encodeRGBE, decodeRGBE, floatToRGBE, rgbeToFloat, frameFileName } from '../../viewer/js/sky/hdr.js';
import {
	computeSkyStats, statsToParams, detectHorizon, tonemapForward, tonemapInverse, compareStats, linToHex, hexToLin,
} from '../../viewer/js/sky/refFit.js';

let passed = 0;
const test = ( name, fn ) => {
	try { fn(); passed ++; console.log( '  ok  ', name ); } catch ( e ) { console.error( '  FAIL', name, '\n', e ); process.exitCode = 1; }
};

// ------------------------------------------------------------------------------------------- RGBE
test( 'floatToRGBE / rgbeToFloat round trip within 1%', () => {
	for ( const v of [ [ 1, 1, 1 ], [ 0.5, 0.25, 0.125 ], [ 120, 80, 3 ], [ 1e-3, 2e-3, 5e-4 ], [ 0.999, 0.5, 0.1 ] ] ) {
		const b = floatToRGBE( ...v );
		const back = rgbeToFloat( b[ 0 ], b[ 1 ], b[ 2 ], b[ 3 ] );
		const m = Math.max( ...v );
		v.forEach( ( x, i ) => assert.ok( Math.abs( back[ i ] - x ) <= m * 0.01 + 1e-6, `${ v } -> ${ back }` ) );
	}
	assert.deepEqual( Array.from( floatToRGBE( 0, 0, 0 ) ), [ 0, 0, 0, 0 ] );
	// exact powers of two must not overflow the mantissa
	const p = floatToRGBE( 2, 1, 0.5 );
	assert.ok( p[ 0 ] <= 255 && p[ 0 ] >= 128 );
} );

test( 'encodeRGBE header + RLE decode round trip (incl. runs and literals)', () => {
	const w = 64, h = 5;
	const rgba = new Float32Array( w * h * 4 );
	for ( let y = 0; y < h; y ++ ) for ( let x = 0; x < w; x ++ ) {
		const i = ( y * w + x ) * 4;
		const flat = x < 20; // long runs
		rgba[ i ] = flat ? 0.5 : ( x * 0.37 + y ) % 3;
		rgba[ i + 1 ] = flat ? 0.5 : Math.sin( x ) + 1.2;
		rgba[ i + 2 ] = flat ? 0.5 : 0.01 * x;
		rgba[ i + 3 ] = 1;
	}
	const bytes = encodeRGBE( w, h, rgba );
	const head = new TextDecoder().decode( bytes.slice( 0, 120 ) );
	assert.ok( head.startsWith( '#?RADIANCE\n' ) );
	assert.ok( head.includes( 'FORMAT=32-bit_rle_rgbe' ) );
	assert.ok( head.includes( `-Y ${ h } +X ${ w }` ) );
	const dec = decodeRGBE( bytes );
	assert.equal( dec.width, w ); assert.equal( dec.height, h );
	for ( let j = 0; j < w * h; j ++ ) for ( let c = 0; c < 3; c ++ ) {
		const a = rgba[ j * 4 + c ], b = dec.data[ j * 3 + c ];
		const m = Math.max( rgba[ j * 4 ], rgba[ j * 4 + 1 ], rgba[ j * 4 + 2 ] );
		assert.ok( Math.abs( a - b ) <= m * 0.01 + 1e-5, `px ${ j } ch ${ c }: ${ a } vs ${ b }` );
	}
	// RLE must actually compress the flat part
	assert.ok( bytes.length < w * h * 4 + 120 );
} );

test( 'encodeRGBE narrow image uses flat scanlines', () => {
	const rgba = new Float32Array( 4 * 2 * 4 ).fill( 0.25 );
	const dec = decodeRGBE( encodeRGBE( 4, 2, rgba ) );
	assert.equal( dec.width, 4 );
	assert.ok( Math.abs( dec.data[ 0 ] - 0.25 ) < 0.01 );
} );

test( 'frameFileName is Houdini $F4 style', () => {
	assert.equal( frameFileName( 7 ), 'sky.0007.hdr' );
	assert.equal( frameFileName( 1234, 'env' ), 'env.1234.hdr' );
} );

// ------------------------------------------------------------------------------------------- stats
const srgb8 = ( lin ) => Math.round( 255 * ( lin <= 0.0031308 ? lin * 12.92 : 1.055 * Math.pow( lin, 1 / 2.4 ) - 0.055 ) );

/** synthetic sky photo: blue gradient, optional ground below `horizon`, white/grey blobs. */
function synth( { w = 160, h = 90, horizon = 0.8, blobs = [], overcast = false } = {} ) {
	const data = new Uint8ClampedArray( w * h * 4 );
	for ( let y = 0; y < h; y ++ ) for ( let x = 0; x < w; x ++ ) {
		const i = ( y * w + x ) * 4;
		let c;
		if ( y >= horizon * h ) c = [ 0.06, 0.05, 0.04 ];
		else {
			const t = y / ( horizon * h );
			c = [ 0.08 + 0.45 * t, 0.2 + 0.4 * t, 0.62 + 0.15 * t ];
			if ( overcast ) c = [ 0.55 + 0.05 * t, 0.56 + 0.05 * t, 0.58 + 0.05 * t ];
			for ( const b of blobs ) {
				const d = Math.hypot( ( x - b.x * w ) / ( b.r * w ), ( y - b.y * h ) / ( b.r * h * 0.6 ) );
				if ( d < 1 ) {
					const shade = 0.55 + 0.4 * ( 1 - ( y - ( b.y - b.r * 0.6 ) * h ) / ( 1.2 * b.r * h ) );
					c = [ shade, shade, shade * 1.02 ];
				}
			}
		}
		data[ i ] = srgb8( c[ 0 ] ); data[ i + 1 ] = srgb8( c[ 1 ] ); data[ i + 2 ] = srgb8( c[ 2 ] ); data[ i + 3 ] = 255;
	}
	return { width: w, height: h, data };
}

test( 'detectHorizon finds the ground edge', () => {
	const img = synth( { horizon: 0.7 } );
	const hz = detectHorizon( img );
	assert.ok( Math.abs( hz - 0.7 ) < 0.05, `horizon ${ hz }` );
	assert.equal( detectHorizon( synth( { horizon: 1.01 } ) ), 1 );
} );

test( 'clear sky → ~0 coverage, blue zenith', () => {
	const st = computeSkyStats( synth( { horizon: 0.8 } ), { fovY: 60 } );
	assert.ok( st.coverage < 0.05, `coverage ${ st.coverage }` );
	assert.ok( st.zenith[ 2 ] > st.zenith[ 0 ], 'zenith should be blue' );
} );

test( 'overcast → coverage ~1', () => {
	const st = computeSkyStats( synth( { horizon: 0.8, overcast: true } ), { fovY: 60 } );
	assert.equal( st.maskMode, 'overcast' );
	assert.ok( st.coverage > 0.95 );
	const p = statsToParams( st );
	assert.ok( p.clouds.coverage > 0.9 );
} );

test( 'broken clouds → coverage in range, colours & params sane', () => {
	const blobs = [ { x: 0.2, y: 0.25, r: 0.12 }, { x: 0.55, y: 0.4, r: 0.15 }, { x: 0.85, y: 0.2, r: 0.1 }, { x: 0.4, y: 0.62, r: 0.08 } ];
	const img = synth( { horizon: 0.8, blobs } );
	const st = computeSkyStats( img, { fovY: 60 } );
	assert.ok( st.coverage > 0.12 && st.coverage < 0.5, `coverage ${ st.coverage }` );
	assert.ok( st.cloudLit && st.cloudShadow );
	assert.ok( st.cloudLit[ 0 ] > st.cloudShadow[ 0 ], 'lit brighter than shadow' );
	const p = statsToParams( st, { cameraYaw: 90 } );
	for ( const k of [ 'coverage', 'type', 'density', 'scale' ] ) assert.ok( Number.isFinite( p.clouds[ k ] ), k );
	assert.ok( p.clouds.coverage > 0 && p.clouds.coverage < 1 );
	assert.match( p.atmosphere.zenithColor, /^#[0-9a-f]{6}$/ );
	assert.equal( p.view.yaw, 90 );
	const e = compareStats( st, st );
	assert.equal( e.score, 0 );
} );

test( 'manual threshold overrides auto mask', () => {
	const img = synth( { horizon: 0.8, blobs: [ { x: 0.5, y: 0.4, r: 0.2 } ] } );
	const a = computeSkyStats( img, { fovY: 60, threshold: - 5 } ); // nothing passes
	assert.equal( a.coverage, 0 );
	const b = computeSkyStats( img, { fovY: 60, threshold: 5 } ); // everything passes
	assert.equal( b.coverage, 1 );
} );

test( 'tonemap inverse ≈ forward^-1 (ACES)', () => {
	for ( const v of [ [ 0.05, 0.1, 0.3 ], [ 0.5, 0.4, 0.3 ], [ 1.5, 1.2, 1.0 ] ] ) {
		const fwd = tonemapForward( v, 'aces' );
		const inv = tonemapInverse( fwd, 'aces' );
		const l = ( c ) => 0.2126 * c[ 0 ] + 0.7152 * c[ 1 ] + 0.0722 * c[ 2 ];
		assert.ok( Math.abs( l( inv ) - l( v ) ) / l( v ) < 0.2, `${ v } -> ${ inv }` );
	}
} );

test( 'hex <-> linear', () => {
	assert.equal( linToHex( hexToLin( '#3b78d8' ) ), '#3b78d8' );
} );

console.log( `\n${ passed } passed${ process.exitCode ? ', some FAILED' : '' }` );
