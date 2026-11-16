// GeoBox Sky — HDR (Radiance .hdr / RGBE) baking helpers.
// Pure functions except bakeEquirect/bakeSequence, which take a SkySystem instance (no direct three import,
// so encodeRGBE / decodeRGBE can be unit-tested in Node).
//
// Panorama convention (same as three.js equirect / HDRLoader): image row 0 = zenith,
// column u: direction = (cos φ cos θ, sin θ, sin φ cos θ), φ = (u - 0.5)·2π, θ = latitude.
//   u = 0.5 → +X (east), u = 0.75 → +Z (south), u = 0.25 → -Z (north), u = 0 / 1 → -X (west).

/** Encode one float RGB triple to RGBE bytes. */
export function floatToRGBE( r, g, b, out = new Uint8Array( 4 ), o = 0 ) {
	const v = Math.max( r, g, b );
	if ( ! ( v > 1e-32 ) ) { out[ o ] = out[ o + 1 ] = out[ o + 2 ] = out[ o + 3 ] = 0; return out; }
	let e = Math.ceil( Math.log2( v ) );
	let scale = Math.pow( 2, - e ) * 256;
	// guard against rounding up to 256
	if ( v * scale >= 255.5 ) { e += 1; scale *= 0.5; }
	out[ o ] = Math.min( 255, Math.max( 0, Math.floor( r * scale ) ) );
	out[ o + 1 ] = Math.min( 255, Math.max( 0, Math.floor( g * scale ) ) );
	out[ o + 2 ] = Math.min( 255, Math.max( 0, Math.floor( b * scale ) ) );
	out[ o + 3 ] = e + 128;
	return out;
}

/** Decode RGBE bytes to float (center-of-bin reconstruction, like stb_image / three HDRLoader). */
export function rgbeToFloat( r, g, b, e ) {
	if ( e === 0 ) return [ 0, 0, 0 ];
	const f = Math.pow( 2, e - 136 );
	return [ ( r + 0.5 ) * f, ( g + 0.5 ) * f, ( b + 0.5 ) * f ];
}

function rleChannel( src, out ) {
	// standard Radiance RLE for one channel scanline; out = array of bytes (pushed)
	const n = src.length;
	let i = 0;
	while ( i < n ) {
		// find run
		let runStart = i;
		let runLen = 1;
		while ( runStart < n ) {
			runLen = 1;
			while ( runStart + runLen < n && runLen < 127 && src[ runStart + runLen ] === src[ runStart ] ) runLen ++;
			if ( runLen >= 4 ) break;
			runStart += runLen;
		}
		if ( runLen < 4 ) runStart = n;
		// literal bytes before the run
		while ( i < runStart ) {
			const cnt = Math.min( 128, runStart - i );
			out.push( cnt );
			for ( let k = 0; k < cnt; k ++ ) out.push( src[ i + k ] );
			i += cnt;
		}
		if ( runStart < n ) {
			out.push( 128 + runLen, src[ runStart ] );
			i = runStart + runLen;
		}
	}
}

/**
 * Encode an RGBA float image (rows top→bottom) as a Radiance .hdr file.
 * Uses new-style RLE scanlines when 8 <= width < 32768, flat otherwise.
 */
export function encodeRGBE( width, height, rgba, { exposure = 1, comment = 'GeoBox sky bake' } = {} ) {
	const header = `#?RADIANCE\n# ${ comment }\nFORMAT=32-bit_rle_rgbe\nEXPOSURE=1.0\n\n-Y ${ height } +X ${ width }\n`;
	const head = new TextEncoder().encode( header );
	const useRle = width >= 8 && width < 32768;
	const chunks = [ head ];
	const px = new Uint8Array( 4 );
	const line = new Uint8Array( width * 4 );
	for ( let y = 0; y < height; y ++ ) {
		for ( let x = 0; x < width; x ++ ) {
			const i = ( y * width + x ) * 4;
			floatToRGBE( rgba[ i ] * exposure, rgba[ i + 1 ] * exposure, rgba[ i + 2 ] * exposure, px );
			line.set( px, x * 4 );
		}
		if ( ! useRle ) { chunks.push( line.slice() ); continue; }
		const out = [ 2, 2, ( width >> 8 ) & 255, width & 255 ];
		const ch = new Uint8Array( width );
		for ( let c = 0; c < 4; c ++ ) {
			for ( let x = 0; x < width; x ++ ) ch[ x ] = line[ x * 4 + c ];
			rleChannel( ch, out );
		}
		chunks.push( Uint8Array.from( out ) );
	}
	let total = 0;
	for ( const c of chunks ) total += c.length;
	const file = new Uint8Array( total );
	let o = 0;
	for ( const c of chunks ) { file.set( c, o ); o += c.length; }
	return file;
}

/** Minimal Radiance decoder (flat + new RLE) for tests / round trips. Returns { width, height, data: Float32Array RGB }. */
export function decodeRGBE( bytes ) {
	let p = 0;
	const readLine = () => {
		let s = '';
		while ( p < bytes.length && bytes[ p ] !== 10 ) s += String.fromCharCode( bytes[ p ++ ] );
		p ++;
		return s;
	};
	const first = readLine();
	if ( ! first.startsWith( '#?' ) ) throw new Error( 'not a radiance file' );
	let line;
	while ( ( line = readLine() ) !== '' ) { /* header lines */ }
	const res = readLine().match( /-Y (\d+) \+X (\d+)/ );
	if ( ! res ) throw new Error( 'unsupported resolution line' );
	const height = + res[ 1 ], width = + res[ 2 ];
	const data = new Float32Array( width * height * 3 );
	const scan = new Uint8Array( width * 4 );
	for ( let y = 0; y < height; y ++ ) {
		if ( bytes[ p ] === 2 && bytes[ p + 1 ] === 2 && ( ( bytes[ p + 2 ] << 8 ) | bytes[ p + 3 ] ) === width && width >= 8 ) {
			p += 4;
			for ( let c = 0; c < 4; c ++ ) {
				let x = 0;
				while ( x < width ) {
					let cnt = bytes[ p ++ ];
					if ( cnt > 128 ) { cnt -= 128; const v = bytes[ p ++ ]; for ( let k = 0; k < cnt; k ++ ) scan[ ( x ++ ) * 4 + c ] = v; }
					else { for ( let k = 0; k < cnt; k ++ ) scan[ ( x ++ ) * 4 + c ] = bytes[ p ++ ]; }
				}
			}
		} else {
			scan.set( bytes.subarray( p, p + width * 4 ) );
			p += width * 4;
		}
		for ( let x = 0; x < width; x ++ ) {
			const f = rgbeToFloat( scan[ x * 4 ], scan[ x * 4 + 1 ], scan[ x * 4 + 2 ], scan[ x * 4 + 3 ] );
			data.set( f, ( y * width + x ) * 3 );
		}
	}
	return { width, height, data };
}

/** Bake the current sky state to a Radiance file. Returns { bytes, width, height, rgba }. */
export function bakeEquirect( sky, { width = 2048, height = 1024, time = null, includeSun = true, applyGrade = true, scale = 1 } = {} ) {
	const prev = sky.time;
	if ( time !== null && time !== undefined ) sky.setTime( time );
	const rgba = sky.renderEquirect( { width, height, includeSun, applyGrade, scale } );
	if ( time !== null && time !== undefined ) sky.setTime( prev );
	const bytes = encodeRGBE( width, height, rgba, { comment: `GeoBox sky bake t=${ ( time ?? sky.time ).toFixed( 3 ) }s` } );
	return { bytes, width, height, rgba };
}

/** File name for a frame, Houdini style ($F4). */
export function frameFileName( frame, prefix = 'sky' ) {
	return `${ prefix }.${ String( frame ).padStart( 4, '0' ) }.hdr`;
}

/**
 * Bake a frame range. Sky time for frame f = startTime + (f - start) / fps * timeScale
 * (same mapping as Houdini: time = ($F - start)/$FPS * timeScale + startTime).
 * writer(frame, bytes, name) may be async. onProgress(i, total).
 */
export async function bakeSequence( sky, {
	start = 1, end = 24, fps = 24, startTime = 0, width = 1024, height = 512, includeSun = true,
	applyGrade = true, prefix = 'sky', writer, onProgress, shouldCancel,
} = {} ) {
	const timeScale = sky.params.animation.paused ? 0 : sky.params.animation.timeScale;
	const total = end - start + 1;
	const results = [];
	for ( let f = start; f <= end; f ++ ) {
		if ( shouldCancel && shouldCancel() ) break;
		const t = startTime + ( f - start ) / fps * timeScale;
		const { bytes } = bakeEquirect( sky, { width, height, time: t, includeSun, applyGrade } );
		const name = frameFileName( f, prefix );
		if ( writer ) await writer( f, bytes, name );
		results.push( { frame: f, time: t, name, size: bytes.length } );
		if ( onProgress ) onProgress( f - start + 1, total );
		await new Promise( ( r ) => setTimeout( r, 0 ) );
	}
	return results;
}
