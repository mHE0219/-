// GeoBox Sky — reference image → sky parameters.
//
// Pure functions (computeSkyStats / statsToParams / compareStats) work on { width, height, data } RGBA
// 8-bit sRGB pixel arrays with rows top→bottom, so they run in Node for tests. analyzeImage() needs a DOM
// (canvas) to read an <img>/<canvas>/ImageBitmap; refine() needs a SkySystem (uses sky.renderView()).
//
// Algorithm summary (details in docs/sky.md):
//   1. horizon: strongest darker-below row transition in the lower 70 % of the image (or user value)
//   2. cloud mask: per-pixel feature F = (B-R)/(B+R) + 0.5·S (blue & saturated = clear sky), Otsu
//      threshold on F, with clamps for "all clear" / "overcast" cases (or user threshold)
//   3. coverage = cloud fraction of sky pixels
//   4. clear-sky colours: per-channel linear fit c(el) = H + (Z - H)·sin(el)^k over clear pixels
//      (elevation from a pinhole camera with the given vertical FOV and the horizon row)
//   5. cloud lit / shadow colours: mean of brightest / darkest 15 % cloud pixels
//   6. sun: centroid of the largest near-saturated blob, else brightness-gradient fallback
//   7. cloud size from mean horizontal run length → clouds.scale; anisotropy, edge softness and
//      intra-cloud contrast → clouds.type / cirrus / density
//   8. colours are pulled back through an approximate inverse tonemap (ACES, exposure 1)
//   refine(): renders the sky with the matched camera, measures the same stats (same threshold),
//   then corrects coverage (bracketed secant), scale, sky & cloud colours (damped ratios).

const DEG = Math.PI / 180;

export const srgbToLin = ( c ) => ( c <= 0.04045 ? c / 12.92 : Math.pow( ( c + 0.055 ) / 1.055, 2.4 ) );
export const linToSrgb = ( c ) => { c = Math.max( 0, Math.min( 1, c ) ); return c <= 0.0031308 ? c * 12.92 : 1.055 * Math.pow( c, 1 / 2.4 ) - 0.055; };
const lum = ( r, g, b ) => 0.2126 * r + 0.7152 * g + 0.0722 * b;
const clamp = ( v, a, b ) => Math.max( a, Math.min( b, v ) );

export function linToHex( rgb ) {
	return '#' + rgb.map( ( v ) => Math.round( linToSrgb( v ) * 255 ).toString( 16 ).padStart( 2, '0' ) ).join( '' );
}
export function hexToLin( hex ) {
	const n = parseInt( String( hex ).replace( '#', '' ), 16 );
	return [ ( n >> 16 ) & 255, ( n >> 8 ) & 255, n & 255 ].map( ( v ) => srgbToLin( v / 255 ) );
}

// --------------------------------------------------------------------------------------------- tonemap
// three.js ACES filmic (RRT+ODT fit with input/output matrices), exposure 1
function acesFilmic( rgb ) {
	const x = rgb.map( ( v ) => v / 0.6 );
	const inM = [ [ 0.59719, 0.35458, 0.04823 ], [ 0.07600, 0.90834, 0.01566 ], [ 0.02840, 0.13383, 0.83777 ] ];
	const outM = [ [ 1.60475, - 0.53108, - 0.07367 ], [ - 0.10208, 1.10813, - 0.00605 ], [ - 0.00327, - 0.07276, 1.07602 ] ];
	const mul = ( m, v ) => m.map( ( r ) => r[ 0 ] * v[ 0 ] + r[ 1 ] * v[ 1 ] + r[ 2 ] * v[ 2 ] );
	const v = mul( inM, x ).map( ( c ) => ( c * ( c + 0.0245786 ) - 0.000090537 ) / ( c * ( 0.983729 * c + 0.4329510 ) + 0.238081 ) );
	return mul( outM, v ).map( ( c ) => clamp( c, 0, 1 ) );
}
export function tonemapForward( rgb, mode = 'aces', exposure = 1 ) {
	const x = rgb.map( ( v ) => Math.max( 0, v * exposure ) );
	if ( mode === 'aces' ) return acesFilmic( x );
	if ( mode === 'reinhard' ) return x.map( ( v ) => v / ( 1 + v ) );
	return x.map( ( v ) => clamp( v, 0, 1 ) );
}
/** Approximate inverse: grey-axis bisection on luminance, then keep chromaticity ratios. */
export function tonemapInverse( rgb, mode = 'aces', exposure = 1 ) {
	if ( mode === 'none' ) return rgb.map( ( v ) => v / exposure );
	const target = lum( ...rgb );
	if ( target <= 1e-5 ) return [ 0, 0, 0 ];
	let lo = 0, hi = 64;
	for ( let i = 0; i < 40; i ++ ) {
		const m = 0.5 * ( lo + hi );
		if ( lum( ...tonemapForward( [ m, m, m ], mode, exposure ) ) < target ) lo = m; else hi = m;
	}
	const g = 0.5 * ( lo + hi );
	return rgb.map( ( v ) => ( v / target ) * g );
}

// --------------------------------------------------------------------------------------------- stats
function otsu( values, bins = 64, lo = - 1, hi = 1.5 ) {
	const hist = new Float64Array( bins );
	for ( const v of values ) hist[ clamp( Math.floor( ( v - lo ) / ( hi - lo ) * bins ), 0, bins - 1 ) ] ++;
	const total = values.length;
	let sum = 0;
	for ( let i = 0; i < bins; i ++ ) sum += i * hist[ i ];
	let sumB = 0, wB = 0, best = 0, bestI = bins / 2, varT = 0;
	const mean = sum / total;
	for ( let i = 0; i < bins; i ++ ) varT += hist[ i ] * ( i - mean ) ** 2;
	varT /= total;
	for ( let i = 0; i < bins; i ++ ) {
		wB += hist[ i ];
		if ( wB === 0 ) continue;
		const wF = total - wB;
		if ( wF === 0 ) break;
		sumB += i * hist[ i ];
		const mB = sumB / wB, mF = ( sum - sumB ) / wF;
		const between = wB * wF * ( mB - mF ) ** 2 / ( total * total );
		if ( between > best ) { best = between; bestI = i; }
	}
	return { threshold: lo + ( bestI + 1 ) / bins * ( hi - lo ), separability: varT > 0 ? best / varT : 0 };
}

function featureF( data, i ) {
	const r = data[ i ] / 255, g = data[ i + 1 ] / 255, b = data[ i + 2 ] / 255;
	const mx = Math.max( r, g, b ), mn = Math.min( r, g, b );
	return ( b - r ) / ( b + r + 0.02 ) + 0.5 * ( mx > 0 ? ( mx - mn ) / mx : 0 );
}

/** Horizon row fraction (0 = top). Returns 1 when no ground is detected. */
export function detectHorizon( img ) {
	const { width: w, height: h, data } = img;
	const rowL = new Float64Array( h ), rowB = new Float64Array( h );
	for ( let y = 0; y < h; y ++ ) {
		let sl = 0, sb = 0;
		for ( let x = 0; x < w; x ++ ) {
			const i = ( y * w + x ) * 4;
			const r = data[ i ] / 255, g = data[ i + 1 ] / 255, b = data[ i + 2 ] / 255;
			sl += lum( r, g, b );
			sb += ( b - r ) / ( b + r + 0.02 );
		}
		rowL[ y ] = sl / w; rowB[ y ] = sb / w;
	}
	const win = Math.max( 2, Math.round( h * 0.04 ) );
	let best = 0, bestY = h;
	for ( let y = Math.round( h * 0.3 ); y < h - win; y ++ ) {
		let a = 0, b = 0, ab = 0, bb = 0;
		for ( let k = 1; k <= win; k ++ ) { a += rowL[ y - k ]; b += rowL[ y + k - 1 ]; ab += rowB[ y - k ]; bb += rowB[ y + k - 1 ]; }
		a /= win; b /= win; ab /= win; bb /= win;
		const score = ( a - b ) / ( a + 0.05 ) + 0.5 * ( ab - bb );
		if ( score > best ) { best = score; bestY = y; }
	}
	return best > 0.35 ? bestY / h : 1;
}

/**
 * Compute image statistics. opts: { horizon (0..1 row fraction | null=auto), threshold (null=auto),
 * fovY (deg, default 60), tonemap ('aces'), exposure (1) }
 */
export function computeSkyStats( img, opts = {} ) {
	const { width: w, height: h, data } = img;
	const fovY = opts.fovY ?? 60;
	const horizon = opts.horizon ?? detectHorizon( img );
	const skyRows = clamp( Math.round( horizon * h ), 1, h );

	// camera model: elevation of a row
	const tanHalf = Math.tan( fovY * DEG / 2 );
	const yNdc = ( y ) => ( 0.5 - ( y + 0.5 ) / h ) * 2;
	const pitch = horizon < 1 ? - Math.atan( yNdc( horizon * h - 0.5 ) * tanHalf ) : ( fovY / 2 ) * DEG;
	const elevOf = ( y ) => pitch + Math.atan( yNdc( y ) * tanHalf );

	// features
	const n = w * skyRows;
	const F = new Float32Array( n );
	const L = new Float32Array( n );
	for ( let y = 0; y < skyRows; y ++ ) {
		for ( let x = 0; x < w; x ++ ) {
			const i = ( y * w + x ) * 4, j = y * w + x;
			const r = data[ i ] / 255, g = data[ i + 1 ] / 255, b = data[ i + 2 ] / 255;
			const mx = Math.max( r, g, b ), mn = Math.min( r, g, b );
			const s = mx > 0 ? ( mx - mn ) / mx : 0;
			F[ j ] = ( b - r ) / ( b + r + 0.02 ) + 0.5 * s;
			L[ j ] = lum( r, g, b );
		}
	}
	// statistics ignore the hazy band just above the horizon (elevation < minElevation)
	const minEl = ( opts.minElevation ?? 3 ) * DEG;
	let statRows = skyRows;
	if ( horizon < 1 ) while ( statRows > 2 && elevOf( statRows - 1 ) < minEl ) statRows --;
	const sN = w * statRows;

	// Blueness relative to the clear sky *of the same row*: the clear sky gets paler toward the horizon,
	// so a global threshold confuses horizon haze with cloud. refF(y) = 85th percentile of F per row,
	// limited in how fast it may drop row-to-row (rows mostly covered by cloud borrow from above).
	const refF = new Float32Array( skyRows );
	const rowVals = new Float32Array( w );
	for ( let y = 0; y < skyRows; y ++ ) {
		for ( let x = 0; x < w; x ++ ) rowVals[ x ] = F[ y * w + x ];
		rowVals.sort();
		refF[ y ] = rowVals[ Math.min( w - 1, Math.floor( w * 0.85 ) ) ];
	}
	const maxDrop = 1.2 / Math.max( skyRows, 1 );
	for ( let y = 1; y < skyRows; y ++ ) refF[ y ] = Math.max( refF[ y ], refF[ y - 1 ] - maxDrop );
	// exact clear-sky reference supplied by the caller (refine() renders the sky without clouds)
	if ( opts.refF ) for ( let y = 0; y < skyRows; y ++ ) refF[ y ] = opts.refF[ Math.min( y, opts.refF.length - 1 ) ];
	const G = new Float32Array( n );
	const clearImg = opts.clearImage && opts.clearImage.width === w && opts.clearImage.height === h ? opts.clearImage : null;
	if ( clearImg ) for ( let j = 0; j < n; j ++ ) G[ j ] = F[ j ] - featureF( clearImg.data, j * 4 ); // pixel-exact reference
	else for ( let j = 0; j < n; j ++ ) G[ j ] = F[ j ] - refF[ Math.floor( j / w ) ];
	// sky colours are measured on the clear image when one is given (every pixel is clear sky there)
	const colData = clearImg ? clearImg.data : data;

	let threshold = opts.threshold;
	let mode = 'manual';
	let useG = true;
	if ( threshold === null || threshold === undefined ) {
		mode = 'otsu';
		// global check first: entirely grey (overcast) or entirely blue (clear)
		const o = otsu( F.subarray( 0, sN ) );
		let cm = 0, cn = 0, sm = 0, sn = 0;
		for ( let j = 0; j < sN; j ++ ) { if ( F[ j ] < o.threshold ) { cm += F[ j ]; cn ++; } else { sm += F[ j ]; sn ++; } }
		cm = cn ? cm / cn : 0; sm = sn ? sm / sn : 1;
		if ( sm < 0.16 ) { threshold = 1.5; mode = 'overcast'; useG = false; }
		else {
			const og = otsu( G.subarray( 0, sN ), 64, - 1.2, 0.3 );
			threshold = clamp( og.threshold, - 0.32, - 0.06 );
			if ( cm > 0.32 && og.separability < 0.5 ) { threshold = - 0.32; mode = 'clear'; }
		}
	} else if ( opts.thresholdSpace === 'F' ) useG = false;
	const mask = new Uint8Array( n );
	let cloudCount = 0;
	const feat = useG ? G : F;
	for ( let j = 0; j < n; j ++ ) if ( feat[ j ] < threshold ) { mask[ j ] = 1; if ( j < sN ) cloudCount ++; }
	const coverage = cloudCount / sN;

	// clear-sky colour fit (linear): c(el) = H + (Z - H)·sin(el)^k. k (= atmosphere.horizonFalloff) is
	// searched over a small grid unless the caller fixes it (refine() measures with the target's k).
	const step = Math.max( 1, Math.floor( sN / 20000 ) );
	const samples = [];
	for ( let j = 0; j < sN; j += step ) {
		if ( mask[ j ] && ! clearImg ) continue;
		const y = Math.floor( j / w ), x = j % w;
		const el = elevOf( y );
		if ( el <= 0 ) continue;
		const i = ( y * w + x ) * 4;
		samples.push( { s: Math.sin( el ), c: tonemapInverse( [ srgbToLin( colData[ i ] / 255 ), srgbToLin( colData[ i + 1 ] / 255 ), srgbToLin( colData[ i + 2 ] / 255 ) ], opts.tonemap ?? 'aces', opts.exposure ?? 1 ) } );
	}
	const fitK = ( k ) => {
		const sx = [ 0, 0, 0 ], sxt = [ 0, 0, 0 ];
		let st = 0, stt = 0;
		for ( const sm of samples ) {
			const t = Math.pow( sm.s, k );
			for ( let c = 0; c < 3; c ++ ) { sx[ c ] += sm.c[ c ]; sxt[ c ] += sm.c[ c ] * t; }
			st += t; stt += t * t;
		}
		const cnt = samples.length;
		const den = cnt * stt - st * st;
		const slope = [ 0, 1, 2 ].map( ( c ) => ( Math.abs( den ) > 1e-9 ? ( cnt * sxt[ c ] - st * sx[ c ] ) / den : 0 ) );
		const icpt = [ 0, 1, 2 ].map( ( c ) => ( sx[ c ] - slope[ c ] * st ) / cnt );
		let sse = 0;
		for ( const sm of samples ) {
			const t = Math.pow( sm.s, k );
			for ( let c = 0; c < 3; c ++ ) sse += ( icpt[ c ] + slope[ c ] * t - sm.c[ c ] ) ** 2;
		}
		return { k, icpt, slope, sse };
	};
	let zenith = null, horizonCol = null, falloff = opts.horizonFalloff ?? null;
	if ( samples.length > 10 ) {
		let best;
		if ( falloff !== null ) best = fitK( falloff );
		else for ( const k of [ 0.15, 0.22, 0.3, 0.4, 0.5, 0.65, 0.8, 1.0, 1.3, 1.7 ] ) {
			const f = fitK( k );
			if ( ! best || f.sse < best.sse ) best = f;
		}
		falloff = best.k;
		horizonCol = best.icpt.map( ( v ) => Math.max( 0.002, v ) );
		zenith = best.icpt.map( ( v, c ) => Math.max( 0.002, v + best.slope[ c ] ) );
		// a photo that never looks high up can't pin the zenith: blend toward the highest observed colour
		const maxS = samples.reduce( ( m, sm ) => Math.max( m, sm.s ), 0 );
		if ( maxS < 0.5 ) {
			const top = samples.filter( ( sm ) => sm.s > maxS * 0.85 );
			if ( top.length ) {
				const mean = [ 0, 1, 2 ].map( ( c ) => top.reduce( ( a, sm ) => a + sm.c[ c ], 0 ) / top.length );
				const wf = maxS / 0.5;
				zenith = zenith.map( ( v, c ) => wf * v + ( 1 - wf ) * mean[ c ] );
			}
		}
		if ( ! zenith.every( Number.isFinite ) || ! horizonCol.every( Number.isFinite ) ) zenith = horizonCol = null;
	}

	// cloud colours
	const cloudL = [];
	for ( let j = 0; j < sN; j += step ) if ( mask[ j ] ) cloudL.push( j );
	cloudL.sort( ( a, b ) => L[ a ] - L[ b ] );
	const meanCol = ( idx ) => {
		const s = [ 0, 0, 0 ];
		for ( const j of idx ) {
			const y = Math.floor( j / w ), x = j % w, i = ( y * w + x ) * 4;
			const lin = tonemapInverse( [ srgbToLin( data[ i ] / 255 ), srgbToLin( data[ i + 1 ] / 255 ), srgbToLin( data[ i + 2 ] / 255 ) ], opts.tonemap ?? 'aces', opts.exposure ?? 1 );
			for ( let c = 0; c < 3; c ++ ) s[ c ] += lin[ c ];
		}
		return idx.length ? s.map( ( v ) => v / idx.length ) : null;
	};
	const q = Math.max( 1, Math.floor( cloudL.length * 0.15 ) );
	const cloudShadow = cloudL.length > 20 ? meanCol( cloudL.slice( 0, q ) ) : null;
	const cloudLit = cloudL.length > 20 ? meanCol( cloudL.slice( - q ) ) : null;
	let cloudMeanL = 0, cloudVarL = 0;
	for ( const j of cloudL ) cloudMeanL += L[ j ];
	cloudMeanL = cloudL.length ? cloudMeanL / cloudL.length : 0;
	for ( const j of cloudL ) cloudVarL += ( L[ j ] - cloudMeanL ) ** 2;
	const cloudContrast = cloudL.length ? Math.sqrt( cloudVarL / cloudL.length ) / Math.max( cloudMeanL, 0.02 ) : 0;

	// run lengths & edges
	let hRuns = 0, hSum = 0, vRuns = 0, vSum = 0, edges = 0, softEdges = 0;
	for ( let y = 0; y < statRows; y ++ ) {
		let run = 0;
		for ( let x = 0; x <= w; x ++ ) {
			const m = x < w ? mask[ y * w + x ] : 0;
			if ( m ) run ++; else if ( run ) { hSum += run; hRuns ++; run = 0; }
		}
	}
	for ( let x = 0; x < w; x ++ ) {
		let run = 0;
		for ( let y = 0; y <= statRows; y ++ ) {
			const m = y < statRows ? mask[ y * w + x ] : 0;
			if ( m ) run ++; else if ( run ) { vSum += run; vRuns ++; run = 0; }
		}
	}
	for ( let y = 1; y < statRows - 1; y ++ ) {
		for ( let x = 1; x < w - 1; x ++ ) {
			const j = y * w + x;
			if ( mask[ j ] === mask[ j + 1 ] && mask[ j ] === mask[ j + w ] ) continue;
			edges ++;
			const gx = F[ j + 1 ] - F[ j - 1 ], gy = F[ j + w ] - F[ j - w ];
			if ( Math.hypot( gx, gy ) < 0.12 ) softEdges ++;
		}
	}
	const meanRunH = hRuns ? hSum / hRuns / w : 0; // fraction of image width
	const meanRunV = vRuns ? vSum / vRuns / h : 0;
	void meanRunV;
	const anisotropy = vRuns && hRuns ? ( hSum / hRuns ) / Math.max( vSum / vRuns, 1 ) : 1;
	const edgeSoftness = edges ? softEdges / edges : 0;

	// sun
	let sun = { found: false, x: 0.5, y: 0.5 };
	{
		// the sun = a small, compact, fully saturated blob
		let sxx = 0, syy = 0, sxx2 = 0, syy2 = 0, sc = 0;
		for ( let y = 0; y < skyRows; y ++ ) for ( let x = 0; x < w; x ++ ) {
			const i = ( y * w + x ) * 4;
			if ( data[ i ] >= 252 && data[ i + 1 ] >= 250 && data[ i + 2 ] >= 240 ) { sxx += x; syy += y; sxx2 += x * x; syy2 += y * y; sc ++; }
		}
		const spread = sc ? Math.sqrt( Math.max( sxx2 / sc - ( sxx / sc ) ** 2, 0 ) + Math.max( syy2 / sc - ( syy / sc ) ** 2, 0 ) ) : 1e9;
		if ( sc > 0 && sc < n * 0.03 && spread < 0.06 * Math.max( w, h ) ) sun = { found: true, x: ( sxx / sc + 0.5 ) / w, y: ( syy / sc + 0.5 ) / h };
		else {
			let left = 0, right = 0, top = 0, bottom = 0;
			for ( let y = 0; y < skyRows; y ++ ) for ( let x = 0; x < w; x ++ ) {
				const v = L[ y * w + x ];
				if ( x < w / 2 ) left += v; else right += v;
				if ( y < skyRows / 2 ) top += v; else bottom += v;
			}
			const gx = ( right - left ) / ( right + left + 1e-6 );
			sun = { found: false, x: 0.5 + clamp( gx * 4, - 1, 1 ) * 0.9, y: null, gradient: gx };
		}
	}
	const aspect = w / h;
	const tanHalfX = tanHalf * aspect;
	const sunAzimuthOffset = Math.atan( ( sun.x - 0.5 ) * 2 * tanHalfX ) / DEG;
	let sunElevation;
	if ( sun.found ) sunElevation = elevOf( sun.y * h - 0.5 ) / DEG;
	else {
		// warm horizon → low sun
		const hc = horizonCol || [ 0.5, 0.5, 0.5 ];
		const warmth = hc[ 0 ] / Math.max( hc[ 2 ], 1e-3 );
		sunElevation = clamp( 55 - ( warmth - 0.55 ) * 60, 3, 70 );
	}

	return {
		width: w, height: h, fovY, horizon, pitch: pitch / DEG, threshold, maskMode: mode,
		coverage, zenith, horizonColor: horizonCol, horizonFalloff: falloff, cloudLit, cloudShadow, cloudContrast,
		meanRunH, meanRunV, anisotropy, edgeSoftness,
		sun: { ...sun, azimuthOffset: sunAzimuthOffset, elevation: sunElevation },
		mask, skyRows, statRows, thresholdSpace: useG ? 'G' : 'F',
	};
}

/** Map stats → partial sky params. opts: { cameraYaw (deg compass, default 0) } */
export function statsToParams( stats, opts = {} ) {
	const yaw = opts.cameraYaw ?? 0;
	const p = { atmosphere: {}, clouds: {}, cirrus: {}, sun: {}, view: {} };
	p.view = { fov: stats.fovY, yaw, pitch: stats.pitch, height: 2 };
	p.sun.azimuth = ( ( yaw + stats.sun.azimuthOffset ) % 360 + 360 ) % 360;
	p.sun.elevation = clamp( stats.sun.elevation, - 2, 89 );

	p.atmosphere.physicalMix = 0.0;
	p.atmosphere.skyIntensity = 1.0;
	if ( stats.horizonFalloff ) p.atmosphere.horizonFalloff = stats.horizonFalloff;
	if ( stats.zenith ) p.atmosphere.zenithColor = linToHex( normalizeMax( stats.zenith ).col );
	if ( stats.horizonColor ) p.atmosphere.horizonColor = linToHex( normalizeMax( stats.horizonColor ).col );
	if ( stats.zenith && stats.horizonColor ) {
		const m = Math.max( ...stats.zenith, ...stats.horizonColor );
		p.atmosphere.skyIntensity = clamp( m, 0.05, 8 );
		p.atmosphere.zenithColor = linToHex( stats.zenith.map( ( v ) => v / m ) );
		p.atmosphere.horizonColor = linToHex( stats.horizonColor.map( ( v ) => v / m ) );
	}

	const cov = stats.coverage;
	// perspective compresses distant clouds: image coverage over-estimates 3D coverage
	p.clouds.enabled = cov > 0.01;
	p.clouds.coverage = clamp( 0.08 + cov * 0.62, 0, 1 );
	if ( stats.maskMode === 'overcast' ) p.clouds.coverage = 0.97;
	const typeScore = 0.25 + stats.cloudContrast * 1.6 + ( 1 - stats.edgeSoftness ) * 0.35 - Math.max( 0, cov - 0.75 ) * 1.5
		- Math.max( 0, Math.log2( Math.max( stats.anisotropy, 1 ) ) - 1.5 ) * 0.2;
	p.clouds.type = clamp( typeScore, 0, 1 );
	p.clouds.density = clamp( 0.7 + ( stats.cloudLit && stats.cloudShadow ? 1 - lum( ...stats.cloudShadow ) / Math.max( lum( ...stats.cloudLit ), 1e-3 ) : 0.4 ) * 0.9, 0.3, 2.5 );
	// angular run length → size: base reference 0.12 image widths ≈ scale 1 at 60° FOV
	const ang = stats.meanRunH * ( 2 * Math.atan( Math.tan( stats.fovY * DEG / 2 ) * stats.width / stats.height ) ) / DEG;
	p.clouds.scale = clamp( ang / 9.0, 0.3, 4 );
	p.clouds.detailStrength = clamp( 0.2 + stats.edgeSoftness * 0.5, 0.1, 0.7 );
	if ( stats.cloudLit ) {
		const n = normalizeMax( stats.cloudLit );
		p.clouds.litColor = linToHex( n.col );
	}
	if ( stats.cloudShadow ) p.clouds.shadowColor = linToHex( stats.cloudShadow.map( ( v ) => clamp( v * 1.4, 0, 1 ) ) );

	// thin, soft, streaky clouds → cirrus
	const cirrusLike = stats.edgeSoftness > 0.55 && stats.anisotropy > 2.5 && cov < 0.6;
	p.cirrus.enabled = true;
	p.cirrus.coverage = cirrusLike ? clamp( cov * 1.3, 0.2, 0.8 ) : 0.15;
	p.cirrus.opacity = cirrusLike ? 0.7 : 0.35;
	if ( cirrusLike ) { p.clouds.coverage *= 0.4; }
	return p;
}

function normalizeMax( c ) {
	const m = Math.max( ...c, 1e-6 );
	return { col: c.map( ( v ) => v / m ), max: m };
}

/** Compare two stat sets; returns per-metric errors and a scalar score. */
export function compareStats( a, b ) {
	const colErr = ( x, y ) => ( x && y ? Math.sqrt( x.reduce( ( s, v, i ) => s + ( linToSrgb( v ) - linToSrgb( y[ i ] ) ) ** 2, 0 ) / 3 ) : 0 );
	const e = {
		coverage: Math.abs( a.coverage - b.coverage ),
		zenith: colErr( a.zenith, b.zenith ),
		horizon: colErr( a.horizonColor, b.horizonColor ),
		cloudLit: colErr( a.cloudLit, b.cloudLit ),
		cloudShadow: colErr( a.cloudShadow, b.cloudShadow ),
		size: a.meanRunH && b.meanRunH ? Math.abs( Math.log( a.meanRunH / b.meanRunH ) ) : 0,
	};
	e.score = e.coverage * 2 + e.zenith + e.horizon + e.cloudLit + e.cloudShadow + e.size * 0.3;
	return e;
}

// --------------------------------------------------------------------------------------------- DOM
/** Read pixels from an <img>/<canvas>/ImageBitmap (downscaled to maxSize on the long edge). */
export function imageToPixels( source, maxSize = 256 ) {
	const sw = source.naturalWidth || source.videoWidth || source.width;
	const sh = source.naturalHeight || source.videoHeight || source.height;
	const s = Math.min( 1, maxSize / Math.max( sw, sh ) );
	const w = Math.max( 8, Math.round( sw * s ) ), h = Math.max( 8, Math.round( sh * s ) );
	const cv = document.createElement( 'canvas' );
	cv.width = w; cv.height = h;
	const ctx = cv.getContext( '2d', { willReadFrequently: true } );
	ctx.drawImage( source, 0, 0, w, h );
	const id = ctx.getImageData( 0, 0, w, h );
	return { width: w, height: h, data: id.data };
}

/** Analyse a reference image. Returns { stats, params, pixels }. */
export function analyzeImage( source, opts = {} ) {
	const pixels = source.data ? source : imageToPixels( source, opts.maxSize ?? 256 );
	const stats = computeSkyStats( pixels, opts );
	const params = statsToParams( stats, opts );
	return { stats, params, pixels };
}

/**
 * Closed-loop refinement. Renders the sky with the matched view, measures, corrects.
 * targetStats: from analyzeImage(...).stats. Returns { params, log }.
 */
export async function refine( sky, targetStats, { iterations = 6, width = 128, onProgress, tonemap } = {} ) {
	const aspect = targetStats.width / targetStats.height;
	const height = Math.max( 24, Math.round( width / aspect ) );
	const p = sky.getParams();
	const view = { fov: targetStats.fovY, yaw: p.view.yaw, pitch: targetStats.pitch, camHeight: 2 };
	const statOpts = { horizon: targetStats.horizon, threshold: targetStats.threshold, thresholdSpace: targetStats.thresholdSpace, fovY: targetStats.fovY,
		tonemap: tonemap ?? p.post.tonemap, exposure: p.post.exposure, horizonFalloff: targetStats.horizonFalloff ?? p.atmosphere.horizonFalloff };
	const log = [];
	let lo = 0, hi = 1, prev = null, best = null;
	// measure the current render with the target's method; the clear-sky row reference comes from an
	// exact cloud-free render of the same view so the mask doesn't depend on how cloudy the render is
	const measure = () => {
		const keep = { c: sky.params.clouds.enabled, ci: sky.params.cirrus.enabled };
		sky.setParams( { clouds: { enabled: false }, cirrus: { enabled: false } } );
		const clear = sky.renderView( { width, height, ...view, includeSun: false } );
		sky.setParams( { clouds: { enabled: keep.c }, cirrus: { enabled: keep.ci } } );
		const image = sky.renderView( { width, height, ...view } );
		const st = computeSkyStats( image, { ...statOpts, clearImage: statOpts.thresholdSpace === 'G' ? clear : null } );
		st.image = image;
		return st;
	};
	const damp = 0.75;
	for ( let it = 0; it < iterations; it ++ ) {
		const st = measure();
		const img = st.image;
		const err = compareStats( st, targetStats );
		log.push( { iteration: it, ...err, coverageParam: sky.params.clouds.coverage, renderedCoverage: st.coverage } );
		if ( ! best || err.score < best.score ) best = { score: err.score, params: sky.getParams() };
		if ( onProgress ) onProgress( it + 1, iterations, err, img );
		const cur = sky.getParams();
		const patch = { clouds: {}, atmosphere: {} };

		// coverage: bracketed secant on (param → rendered coverage)
		if ( cur.clouds.enabled ) {
			const c = cur.clouds.coverage;
			const d = st.coverage - targetStats.coverage;
			if ( d > 0 ) hi = Math.min( hi, c ); else lo = Math.max( lo, c );
			let next;
			if ( prev && Math.abs( st.coverage - prev.measured ) > 1e-3 ) {
				next = c - d * ( c - prev.param ) / ( st.coverage - prev.measured );
			} else next = c - d * 0.8;
			if ( ! ( next > lo && next < hi ) ) next = 0.5 * ( lo + hi );
			prev = { param: c, measured: st.coverage };
			patch.clouds.coverage = clamp( next, 0, 1 );
		}
		// size
		if ( st.meanRunH > 0 && targetStats.meanRunH > 0 ) {
			patch.clouds.scale = clamp( cur.clouds.scale * Math.pow( targetStats.meanRunH / st.meanRunH, 0.6 ), 0.2, 6 );
		}
		// sky colours: ratio correction in linear space (folded into colour + intensity)
		const fix = ( hex, intensity, measured, target ) => {
			if ( ! measured || ! target ) return null;
			const base = hexToLin( hex ).map( ( v ) => v * intensity );
			const next = base.map( ( v, i ) => v * Math.pow( clamp( target[ i ] / Math.max( measured[ i ], 1e-4 ), 0.5, 2 ), damp ) );
			return next;
		};
		const z = fix( cur.atmosphere.zenithColor, cur.atmosphere.skyIntensity, st.zenith, targetStats.zenith );
		const hz = fix( cur.atmosphere.horizonColor, cur.atmosphere.skyIntensity, st.horizonColor, targetStats.horizonColor );
		if ( z && hz ) {
			const m = Math.max( ...z, ...hz, 1e-4 );
			patch.atmosphere.skyIntensity = clamp( m, 0.02, 16 );
			patch.atmosphere.zenithColor = linToHex( z.map( ( v ) => v / m ) );
			patch.atmosphere.horizonColor = linToHex( hz.map( ( v ) => v / m ) );
		}
		const lit = fix( cur.clouds.litColor, cur.clouds.litIntensity, st.cloudLit, targetStats.cloudLit );
		if ( lit ) {
			const m = Math.max( ...lit, 1e-4 );
			patch.clouds.litIntensity = clamp( m, 0.05, 6 );
			patch.clouds.litColor = linToHex( lit.map( ( v ) => v / m ) );
		}
		const sh = fix( cur.clouds.shadowColor, cur.clouds.ambient, st.cloudShadow, targetStats.cloudShadow );
		if ( sh ) {
			const m = Math.max( ...sh, 1e-4 );
			patch.clouds.ambient = clamp( m, 0.05, 6 );
			patch.clouds.shadowColor = linToHex( sh.map( ( v ) => v / m ) );
		}
		sky.setParams( patch );
		await new Promise( ( r ) => setTimeout( r, 0 ) );
	}
	let st = measure();
	let err = compareStats( st, targetStats );
	log.push( { iteration: iterations, ...err, coverageParam: sky.params.clouds.coverage, renderedCoverage: st.coverage } );
	if ( best && best.score < err.score ) {
		// keep the best iterate, not the last one
		sky.resetParams( best.params );
		st = measure();
		err = compareStats( st, targetStats );
		log.push( { iteration: 'best', ...err, coverageParam: sky.params.clouds.coverage, renderedCoverage: st.coverage } );
	}
	if ( onProgress ) onProgress( iterations, iterations, err, st.image );
	return { params: sky.getParams(), log, image: st.image, stats: st };
}
