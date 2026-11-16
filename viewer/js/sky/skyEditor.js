// GeoBox Sky — standalone editor page logic (viewer/sky.html).
//
// URL options:  ?project=<name>    load/save sky_params via the GeoBox server (/api/projects/<name>/sky)
//               &preset=<name>     start from a preset        &quality=low|medium|high
//               &time=<s>          initial sky time           &paused=1
//               &yaw=&pitch=&fov=  initial view               &ui=0   hide panels (screenshots)
// window.__sky exposes { sky, renderer, camera, applyPreset, analyze, refine } for automation.

import * as THREE from 'three';
import GUI from 'lil-gui';
import { SkySystem, DEFAULT_SKY_PARAMS, mergeParams } from './SkySystem.js';
import { SKY_PRESETS, PRESET_NAMES } from './presets.js';
import { analyzeImage, refine as refineFit, imageToPixels } from './refFit.js';
import { bakeEquirect, bakeSequence, frameFileName } from './hdr.js';

const qs = new URLSearchParams( location.search );
const project = qs.get( 'project' );
const API = project ? `/api/projects/${ encodeURIComponent( project ) }` : null;

const $ = ( id ) => document.getElementById( id );
const statusEl = $( 'status' );
function setStatus( msg, kind = '' ) { statusEl.textContent = msg; statusEl.className = 'panel ' + kind; }

// ------------------------------------------------------------------------------------------ setup
const canvas = $( 'view' );
const renderer = new THREE.WebGLRenderer( { canvas, antialias: true, preserveDrawingBuffer: qs.get( 'ui' ) === '0' } );
renderer.setPixelRatio( Math.min( window.devicePixelRatio, 2 ) );
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera( 60, 1, 0.1, 1e7 );
camera.rotation.order = 'YXZ';

const QUALITY = {
	low: { steps: 24, lightSteps: 3, atmosphereSteps: 6, renderScale: 0.35 },
	medium: { steps: 48, lightSteps: 6, atmosphereSteps: 10, renderScale: 0.5 },
	high: { steps: 96, lightSteps: 8, atmosphereSteps: 16, renderScale: 1.0 },
};

const sky = new SkySystem( renderer, { params: {} } );
scene.add( sky.object );

// ground hint: grid + a few distant blocks for scale
const grid = new THREE.GridHelper( 4000, 80, 0x4a5563, 0x2b323c );
grid.material.transparent = true;
grid.material.opacity = 0.55;
scene.add( grid );
const blockMat = new THREE.MeshStandardMaterial( { color: 0x4a4f57, roughness: 0.9 } );
for ( let i = 0; i < 18; i ++ ) {
	const a = ( i / 18 ) * Math.PI * 2 + 0.2 * Math.sin( i * 7.1 );
	const d = 600 + ( ( i * 37 ) % 7 ) * 120;
	const hgt = 30 + ( ( i * 53 ) % 9 ) * 22;
	const m = new THREE.Mesh( new THREE.BoxGeometry( 60 + ( i % 3 ) * 30, hgt, 60 ), blockMat );
	m.position.set( Math.sin( a ) * d, hgt / 2, - Math.cos( a ) * d );
	scene.add( m );
}
const sunLight = new THREE.DirectionalLight( 0xffffff, 3 );
const hemi = new THREE.HemisphereLight( 0x8899bb, 0x444444, 1 );
scene.add( sunLight, hemi );

function applyLights() {
	sunLight.position.copy( sky.sunDirection ).multiplyScalar( 1000 );
	sunLight.color.copy( sky.sunColor );
	sunLight.intensity = sky.sunIntensity;
	hemi.color.copy( sky.ambientColor );
	hemi.groundColor.copy( sky.groundColor );
	hemi.intensity = sky.ambientIntensity;
	sky.configureRenderer( renderer );
}

function updateCamera() {
	const v = sky.params.view;
	camera.fov = v.fov;
	camera.position.set( 0, v.height, 0 );
	camera.rotation.set( THREE.MathUtils.degToRad( v.pitch ), - THREE.MathUtils.degToRad( v.yaw ), 0 );
	camera.updateProjectionMatrix();
	camera.updateMatrixWorld();
}

function resize() {
	const w = window.innerWidth, h = window.innerHeight;
	renderer.setSize( w, h, false );
	camera.aspect = w / h;
	camera.updateProjectionMatrix();
}
window.addEventListener( 'resize', resize );
resize();

// ------------------------------------------------------------------------------------------ input
let drag = null;
canvas.addEventListener( 'pointerdown', ( e ) => { drag = { x: e.clientX, y: e.clientY, yaw: sky.params.view.yaw, pitch: sky.params.view.pitch }; canvas.setPointerCapture( e.pointerId ); } );
canvas.addEventListener( 'pointermove', ( e ) => {
	if ( ! drag ) return;
	const k = sky.params.view.fov / window.innerHeight;
	sky.params.view.yaw = ( ( drag.yaw - ( e.clientX - drag.x ) * k ) % 360 + 360 ) % 360;
	sky.params.view.pitch = THREE.MathUtils.clamp( drag.pitch + ( e.clientY - drag.y ) * k, - 89, 89 );
	refreshGui();
} );
canvas.addEventListener( 'pointerup', () => { drag = null; } );
canvas.addEventListener( 'wheel', ( e ) => {
	e.preventDefault();
	sky.params.view.fov = THREE.MathUtils.clamp( sky.params.view.fov * Math.exp( e.deltaY * 0.001 ), 10, 120 );
	refreshGui();
}, { passive: false } );
window.addEventListener( 'keydown', ( e ) => {
	if ( e.code === 'Space' && e.target === document.body ) { e.preventDefault(); togglePlay(); }
} );

// ------------------------------------------------------------------------------------------ io
function download( name, data, type ) {
	const blob = data instanceof Blob ? data : new Blob( [ data ], { type } );
	const a = document.createElement( 'a' );
	a.href = URL.createObjectURL( blob );
	a.download = name;
	document.body.appendChild( a );
	a.click();
	setTimeout( () => { URL.revokeObjectURL( a.href ); a.remove(); }, 1000 );
}

async function loadFromProject() {
	if ( ! API ) return false;
	try {
		const res = await fetch( `${ API }/sky` );
		if ( ! res.ok ) throw new Error( res.status );
		const json = await res.json();
		if ( json && typeof json === 'object' && Object.keys( json ).length ) sky.resetParams( json );
		setStatus( `已載入專案天空參數：${ project }`, 'ok' );
		return true;
	} catch ( e ) {
		setStatus( `專案 ${ project } 沒有 sky_params（或 server 不可用），使用預設值。`, 'warn' );
		return false;
	}
}

async function saveParams() {
	const json = JSON.stringify( sky.getParams(), null, 2 );
	if ( API ) {
		try {
			const res = await fetch( `${ API }/sky`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: json } );
			if ( ! res.ok ) throw new Error( res.status );
			setStatus( `已儲存 sky_params.json → projects/${ project }/`, 'ok' );
			return;
		} catch ( e ) {
			setStatus( `儲存到 server 失敗 (${ e.message })，改為下載檔案。`, 'warn' );
		}
	}
	download( 'sky_params.json', json, 'application/json' );
}

async function writeProjectFile( relPath, bytes ) {
	if ( API ) {
		try {
			const res = await fetch( `${ API }/files/${ relPath }`, { method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: bytes } );
			if ( res.ok ) return 'server';
			throw new Error( res.status );
		} catch ( e ) {
			console.warn( 'file PUT failed, downloading instead', e );
		}
	}
	download( relPath.split( '/' ).pop(), bytes, 'application/octet-stream' );
	return 'download';
}

function loadJsonFile() {
	const inp = document.createElement( 'input' );
	inp.type = 'file';
	inp.accept = '.json,application/json';
	inp.onchange = async () => {
		const f = inp.files[ 0 ];
		if ( ! f ) return;
		try {
			sky.resetParams( JSON.parse( await f.text() ) );
			refreshGui();
			setStatus( `已載入 ${ f.name }`, 'ok' );
		} catch ( e ) { setStatus( `JSON 讀取失敗：${ e.message }`, 'warn' ); }
	};
	inp.click();
}

// ------------------------------------------------------------------------------------------ gui
let gui;
const ui = {
	preset: 'fair-cumulus',
	quality: 'medium',
	bake: { width: 2048, includeSun: true, applyGrade: true, start: 1, end: 48, fps: 24, startTime: 0, busy: false, cancel: false },
};

function applyPreset( name ) {
	const keep = { view: sky.getParams().view, quality: sky.getParams().quality, animation: { paused: sky.params.animation.paused } };
	sky.resetParams( mergeParams( mergeParams( DEFAULT_SKY_PARAMS, SKY_PRESETS[ name ] || {} ), keep ) );
	ui.preset = name;
	refreshGui();
}

function applyQuality( q ) {
	sky.setParams( { quality: QUALITY[ q ] || QUALITY.medium } );
	ui.quality = q;
	refreshGui();
}

function refreshGui() {
	if ( gui ) gui.controllersRecursive().forEach( ( c ) => c.updateDisplay() );
	sky.syncUniforms();
}

function buildGui() {
	const P = sky.params;
	gui = new GUI( { title: '天空參數 Sky Params' } );
	gui.add( ui, 'preset', PRESET_NAMES ).name( '預設 Preset' ).onChange( applyPreset );
	gui.add( ui, 'quality', Object.keys( QUALITY ) ).name( '品質 Quality' ).onChange( applyQuality );

	const fs = gui.addFolder( '太陽 Sun' );
	fs.add( P.sun, 'azimuth', 0, 360, 0.5 ).name( '方位 Azimuth°' );
	fs.add( P.sun, 'elevation', - 5, 90, 0.1 ).name( '仰角 Elevation°' );
	fs.addColor( P.sun, 'color' ).name( '顏色 Color' );
	fs.add( P.sun, 'intensity', 0, 10, 0.05 ).name( '強度 Intensity' );
	fs.add( P.sun, 'discSize', 0, 10, 0.05 ).name( '日盤大小 Disc size' );
	fs.add( P.sun, 'discIntensity', 0, 200, 1 ).name( '日盤亮度 Disc int.' );
	fs.add( P.sun, 'glow', 0, 5, 0.01 ).name( '光暈 Glow' );
	fs.add( P.sun, 'glowSharpness', 1, 60, 0.5 ).name( '光暈銳利 Glow sharp' );

	const fa = gui.addFolder( '大氣 Atmosphere' );
	fa.add( P.atmosphere, 'physicalMix', 0, 1, 0.01 ).name( '物理混合 Physical mix' );
	fa.add( P.atmosphere, 'physicalIntensity', 0, 5, 0.01 ).name( '物理亮度 Phys int.' );
	fa.addColor( P.atmosphere, 'zenithColor' ).name( '天頂色 Zenith' );
	fa.addColor( P.atmosphere, 'horizonColor' ).name( '地平色 Horizon' );
	fa.addColor( P.atmosphere, 'groundColor' ).name( '地面色 Ground' );
	fa.add( P.atmosphere, 'horizonFalloff', 0.05, 3, 0.01 ).name( '漸層衰減 Falloff' );
	fa.add( P.atmosphere, 'skyIntensity', 0, 5, 0.01 ).name( '漸層亮度 Grad int.' );
	fa.addColor( P.atmosphere, 'tint' ).name( '天空色調 Tint' );
	fa.add( P.atmosphere, 'rayleigh', 0, 5, 0.01 ).name( 'Rayleigh' );
	fa.add( P.atmosphere, 'mie', 0, 10, 0.01 ).name( 'Mie / 霾 Turbidity' );
	fa.add( P.atmosphere, 'mieG', 0, 0.99, 0.01 ).name( 'Mie g' );
	fa.add( P.atmosphere, 'haze', 0, 1, 0.01 ).name( '地平霧帶 Haze' );
	fa.add( P.atmosphere, 'sunReddening', 0, 1, 0.01 ).name( '夕陽紅化 Reddening' );
	fa.close();

	const fc = gui.addFolder( '積雲層 Clouds' );
	fc.add( P.clouds, 'enabled' ).name( '啟用 Enabled' );
	fc.add( P.clouds, 'coverage', 0, 1, 0.005 ).name( '覆蓋率 Coverage' );
	fc.add( P.clouds, 'density', 0, 4, 0.01 ).name( '密度 Density' );
	fc.add( P.clouds, 'type', 0, 1, 0.01 ).name( '雲型 Type (層→積)' );
	fc.add( P.clouds, 'bottom', 100, 8000, 10 ).name( '雲底 Bottom m' );
	fc.add( P.clouds, 'top', 200, 14000, 10 ).name( '雲頂 Top m' );
	fc.add( P.clouds, 'scale', 0.1, 6, 0.01 ).name( '尺度 Scale' );
	fc.add( P.clouds, 'detailScale', 0.1, 6, 0.01 ).name( '細節尺度 Detail scale' );
	fc.add( P.clouds, 'detailStrength', 0, 1, 0.01 ).name( '細節侵蝕 Detail' );
	fc.add( P.clouds, 'coverageVariation', 0, 1, 0.01 ).name( '覆蓋變化 Cov. var' );
	fc.add( P.clouds, 'seed', 0, 999, 1 ).name( '種子 Seed' );
	fc.addColor( P.clouds, 'litColor' ).name( '受光色 Lit colour' );
	fc.add( P.clouds, 'litIntensity', 0, 5, 0.01 ).name( '受光強度 Lit int.' );
	fc.addColor( P.clouds, 'shadowColor' ).name( '陰影色 Shadow colour' );
	fc.add( P.clouds, 'ambient', 0, 5, 0.01 ).name( '環境光 Ambient' );
	fc.add( P.clouds, 'silverIntensity', 0, 5, 0.01 ).name( '銀邊 Silver lining' );
	fc.add( P.clouds, 'silverSpread', 0, 0.9, 0.01 ).name( '銀邊擴散 Silver spread' );
	fc.add( P.clouds, 'forwardScatter', 0, 0.95, 0.01 ).name( '前向散射 g+' );
	fc.add( P.clouds, 'backScatter', - 0.9, 0, 0.01 ).name( '背向散射 g-' );
	fc.add( P.clouds, 'powder', 0, 1, 0.01 ).name( 'Powder 暗邊' );
	fc.add( P.clouds, 'multiScatter', 0, 0.95, 0.01 ).name( '多重散射 Multi-scat' );
	fc.add( P.clouds, 'extinction', 0.002, 0.2, 0.001 ).name( '消光 Extinction' );
	fc.add( P.clouds, 'fadeDistance', 2000, 150000, 500 ).name( '遠景淡出 Fade m' );
	fc.add( P.clouds, 'maxDistance', 5000, 300000, 1000 ).name( '最大距離 Max m' );

	const fci = gui.addFolder( '卷雲 Cirrus' );
	fci.add( P.cirrus, 'enabled' ).name( '啟用 Enabled' );
	fci.add( P.cirrus, 'coverage', 0, 1, 0.01 ).name( '覆蓋率 Coverage' );
	fci.add( P.cirrus, 'opacity', 0, 1, 0.01 ).name( '不透明 Opacity' );
	fci.add( P.cirrus, 'altitude', 3000, 15000, 50 ).name( '高度 Altitude m' );
	fci.add( P.cirrus, 'scale', 0.1, 6, 0.01 ).name( '尺度 Scale' );
	fci.add( P.cirrus, 'stretch', 1, 20, 0.1 ).name( '拉伸 Stretch' );
	fci.add( P.cirrus, 'direction', 0, 360, 1 ).name( '方向 Direction°' );
	fci.add( P.cirrus, 'speed', 0, 120, 0.5 ).name( '速度 Speed m/s' );
	fci.addColor( P.cirrus, 'color' ).name( '顏色 Colour' );
	fci.close();

	const fan = gui.addFolder( '動畫 Animation' );
	fan.add( P.animation, 'paused' ).name( '暫停 Paused' ).onChange( updatePlayButton );
	fan.add( P.animation, 'timeScale', 0, 200, 0.5 ).name( '時間倍率 Time scale' );
	fan.add( P.animation, 'windDirection', 0, 360, 1 ).name( '風向(去向) Wind to°' );
	fan.add( P.animation, 'windSpeed', 0, 80, 0.5 ).name( '風速 Wind m/s' );
	fan.add( P.animation, 'evolveSpeed', 0, 20, 0.05 ).name( '形變 Evolve' );
	fan.add( sky, 'time', 0, 36000, 1 ).name( '時間 Time s' ).listen().onChange( ( t ) => sky.setTime( t ) );

	const fp = gui.addFolder( '後製 Post' );
	fp.add( P.post, 'exposure', 0, 8, 0.01 ).name( '曝光 Exposure' );
	fp.add( P.post, 'tonemap', [ 'none', 'aces', 'agx', 'neutral', 'reinhard' ] ).name( 'Tonemap' );
	fp.add( P.post, 'saturation', 0, 2, 0.01 ).name( '飽和 Saturation' );
	fp.add( P.post, 'contrast', 0.5, 2, 0.01 ).name( '對比 Contrast' );
	fp.addColor( P.post, 'grade' ).name( '色調 Grade' );
	fp.close();

	const fq = gui.addFolder( '品質 Quality' );
	fq.add( P.quality, 'steps', 4, 256, 1 ).name( '步數 Steps' );
	fq.add( P.quality, 'lightSteps', 1, 16, 1 ).name( '光線步數 Light steps' );
	fq.add( P.quality, 'atmosphereSteps', 2, 32, 1 ).name( '大氣步數 Atmo steps' );
	fq.add( P.quality, 'renderScale', 0.1, 1, 0.05 ).name( '解析度 Render scale' );
	fq.add( P.quality, 'jitter' ).name( 'Jitter' );
	fq.close();

	const fv = gui.addFolder( '視角 View' );
	fv.add( P.view, 'fov', 10, 120, 0.5 ).name( 'FOV°' );
	fv.add( P.view, 'yaw', 0, 360, 0.5 ).name( '方位 Yaw°' );
	fv.add( P.view, 'pitch', - 89, 89, 0.5 ).name( '俯仰 Pitch°' );
	fv.add( P.view, 'height', 1, 6000, 1 ).name( '高度 Height m' );
	fv.close();

	const fb = gui.addFolder( 'HDR 烘焙 Bake (→ Houdini)' );
	fb.add( ui.bake, 'width', [ 512, 1024, 2048, 4096 ] ).name( '寬度 Width' );
	fb.add( ui.bake, 'includeSun' ).name( '含日盤 Sun disc' );
	fb.add( ui.bake, 'applyGrade' ).name( '套用色調 Grade' );
	fb.add( ui.bake, 'start', 1, 100000, 1 ).name( '起始幀 Start' );
	fb.add( ui.bake, 'end', 1, 100000, 1 ).name( '結束幀 End' );
	fb.add( ui.bake, 'fps', 1, 120, 1 ).name( 'FPS' );
	fb.add( ui.bake, 'startTime', 0, 36000, 1 ).name( '起始時間 t0 s' );
	fb.add( { f: bakeCurrent }, 'f' ).name( '烘焙目前畫面 Bake frame' );
	fb.add( { f: bakeRange }, 'f' ).name( '烘焙序列 Bake sequence' );
	fb.add( { f: () => { ui.bake.cancel = true; } }, 'f' ).name( '取消 Cancel' );
	fb.close();

	const ff = gui.addFolder( '檔案 File' );
	ff.add( { f: saveParams }, 'f' ).name( project ? '💾 儲存到專案 Save' : '💾 下載 sky_params.json' );
	ff.add( { f: () => download( 'sky_params.json', JSON.stringify( sky.getParams(), null, 2 ), 'application/json' ) }, 'f' ).name( '下載 JSON' );
	ff.add( { f: loadJsonFile }, 'f' ).name( '載入 JSON' );
	ff.add( { f: () => { sky.resetParams( {} ); refreshGui(); } }, 'f' ).name( '重設預設 Reset' );

	gui.onChange( () => sky.syncUniforms() );
}

// ------------------------------------------------------------------------------------------ bake
async function bakeCurrent() {
	if ( ui.bake.busy ) return;
	ui.bake.busy = true;
	try {
		const w = + ui.bake.width;
		setStatus( `烘焙中 ${ w }×${ w / 2 } …` );
		await new Promise( ( r ) => setTimeout( r, 30 ) );
		const t0 = performance.now();
		const { bytes } = bakeEquirect( sky, { width: w, height: w / 2, includeSun: ui.bake.includeSun, applyGrade: ui.bake.applyGrade } );
		const name = frameFileName( Math.round( ui.bake.start ) );
		const where = await writeProjectFile( `sky/${ name }`, bytes );
		setStatus( `完成 ${ name } (${ ( bytes.length / 1048576 ).toFixed( 1 ) } MB, ${ ( ( performance.now() - t0 ) / 1000 ).toFixed( 1 ) } s) → ${ where === 'server' ? `projects/${ project }/sky/` : '下載' }`, 'ok' );
	} catch ( e ) {
		console.error( e );
		setStatus( `烘焙失敗：${ e.message }`, 'warn' );
	} finally { ui.bake.busy = false; }
}

async function bakeRange() {
	if ( ui.bake.busy ) return;
	if ( ! API && ui.bake.end - ui.bake.start > 10 && ! confirm( '沒有專案 (?project=)，每一幀都會個別下載。繼續？' ) ) return;
	ui.bake.busy = true;
	ui.bake.cancel = false;
	try {
		const w = + ui.bake.width;
		const res = await bakeSequence( sky, {
			start: Math.round( ui.bake.start ), end: Math.round( ui.bake.end ), fps: ui.bake.fps, startTime: ui.bake.startTime,
			width: w, height: w / 2, includeSun: ui.bake.includeSun, applyGrade: ui.bake.applyGrade,
			writer: ( f, bytes, name ) => writeProjectFile( `sky/${ name }`, bytes ),
			onProgress: ( i, n ) => setStatus( `烘焙序列 ${ i } / ${ n } …` ),
			shouldCancel: () => ui.bake.cancel,
		} );
		// also store the params next to the frames so Houdini knows the mapping
		if ( API ) await saveParams();
		setStatus( `序列完成：${ res.length } 幀 (sky.$F4.hdr)`, 'ok' );
	} catch ( e ) {
		console.error( e );
		setStatus( `序列烘焙失敗：${ e.message }`, 'warn' );
	} finally { ui.bake.busy = false; }
}

// ------------------------------------------------------------------------------------------ reference fit
const ref = { img: null, pixels: null, result: null, showMask: true };
const refCanvas = $( 'refCanvas' ), renderCanvas = $( 'renderCanvas' );
const statsEl = $( 'stats' );

function fitOpts() {
	return {
		fovY: + $( 'fov' ).value,
		horizon: $( 'horizonAuto' ).checked ? null : + $( 'horizon' ).value,
		threshold: $( 'thresholdAuto' ).checked ? null : + $( 'threshold' ).value,
		cameraYaw: + $( 'camYaw' ).value,
		tonemap: sky.params.post.tonemap,
		exposure: sky.params.post.exposure,
		horizonFalloff: sky.params.atmosphere.horizonFalloff,
	};
}

function drawPixels( cv, img, mask = null, skyRows = 0, horizon = null ) {
	cv.width = img.width; cv.height = img.height;
	const ctx = cv.getContext( '2d' );
	const id = new ImageData( new Uint8ClampedArray( img.data ), img.width, img.height );
	if ( mask ) {
		for ( let j = 0; j < mask.length; j ++ ) if ( mask[ j ] ) {
			const i = j * 4;
			id.data[ i ] = id.data[ i ] * 0.55 + 255 * 0.45;
			id.data[ i + 1 ] *= 0.55; id.data[ i + 2 ] *= 0.55;
		}
	}
	ctx.putImageData( id, 0, 0 );
	if ( horizon !== null && horizon < 1 ) {
		ctx.strokeStyle = '#4da3ff'; ctx.lineWidth = 1;
		ctx.beginPath(); ctx.moveTo( 0, horizon * img.height ); ctx.lineTo( img.width, horizon * img.height ); ctx.stroke();
	}
}

function fmtCol( c ) { return c ? c.map( ( v ) => v.toFixed( 3 ) ).join( ', ' ) : '—'; }

function analyze() {
	if ( ! ref.pixels ) return null;
	const r = analyzeImage( ref.pixels, fitOpts() );
	ref.result = r;
	const s = r.stats;
	drawPixels( refCanvas, ref.pixels, ref.showMask ? s.mask : null, s.skyRows, s.horizon );
	if ( $( 'horizonAuto' ).checked ) { $( 'horizon' ).value = s.horizon; $( 'horizonVal' ).textContent = 'auto ' + s.horizon.toFixed( 2 ); }
	if ( $( 'thresholdAuto' ).checked ) { $( 'threshold' ).value = s.threshold; $( 'thresholdVal' ).textContent = 'auto'; }
	statsEl.textContent = [
		`遮罩模式 mask: ${ s.maskMode }  threshold ${ s.threshold.toFixed( 3 ) }`,
		`雲覆蓋 coverage (image): ${ ( s.coverage * 100 ).toFixed( 1 )}%`,
		`地平線 horizon: ${ s.horizon.toFixed( 3 ) }  camera pitch ${ s.pitch.toFixed( 1 ) }°`,
		`天頂色 zenith (lin): ${ fmtCol( s.zenith ) }`,
		`地平色 horizon (lin): ${ fmtCol( s.horizonColor ) }`,
		`雲亮部 lit: ${ fmtCol( s.cloudLit ) }`,
		`雲暗部 shadow: ${ fmtCol( s.cloudShadow ) }`,
		`雲對比 contrast ${ s.cloudContrast.toFixed( 2 ) }  邊緣柔 soft ${ s.edgeSoftness.toFixed( 2 ) }  異向 aniso ${ s.anisotropy.toFixed( 2 ) }`,
		`太陽 sun: ${ s.sun.found ? 'found' : 'estimated' } az+${ s.sun.azimuthOffset.toFixed( 1 ) }° el ${ s.sun.elevation.toFixed( 1 ) }°`,
		'',
		'→ 建議參數 params: ' + JSON.stringify( { clouds: { coverage: + r.params.clouds.coverage.toFixed( 3 ), type: + r.params.clouds.type.toFixed( 2 ), scale: + r.params.clouds.scale.toFixed( 2 ) }, sun: { azimuth: + r.params.sun.azimuth.toFixed( 1 ), elevation: + r.params.sun.elevation.toFixed( 1 ) } } ),
	].join( '\n' );
	return r;
}

function applyFit() {
	const r = analyze();
	if ( ! r ) return;
	sky.setParams( r.params );
	refreshGui();
	renderCompare();
	$( 'btnRefine' ).disabled = false;
	setStatus( '已套用參考圖估計參數。可按「精修 Refine」閉環修正。', 'ok' );
}

function renderCompare() {
	if ( ! ref.result ) return;
	const s = ref.result.stats;
	const w = 192, h = Math.max( 24, Math.round( w * s.height / s.width ) );
	const img = sky.renderView( { width: w, height: h, fov: s.fovY, yaw: sky.params.view.yaw, pitch: s.pitch, camHeight: 2 } );
	drawPixels( renderCanvas, img );
}

async function doRefine() {
	if ( ! ref.result ) return;
	$( 'btnRefine' ).disabled = true;
	setStatus( '精修中 Refining…' );
	try {
		const out = await refineFit( sky, ref.result.stats, {
			iterations: + $( 'iters' ).value || 6,
			width: 128,
			onProgress: ( i, n, err, img ) => { drawPixels( renderCanvas, img ); setStatus( `精修 ${ i }/${ n }  score ${ err.score.toFixed( 3 ) }  Δcoverage ${ ( err.coverage * 100 ).toFixed( 1 ) }%` ); },
		} );
		refreshGui();
		renderCompare();
		const first = out.log[ 0 ], last = out.log[ out.log.length - 1 ];
		statsEl.textContent += `\n\n精修 refine: score ${ first.score.toFixed( 3 ) } → ${ last.score.toFixed( 3 ) }\n` +
			out.log.map( ( l ) => `#${ l.iteration } cov ${ ( l.renderedCoverage * 100 ).toFixed( 1 ) }% (param ${ l.coverageParam.toFixed( 3 ) }) score ${ l.score.toFixed( 3 ) }` ).join( '\n' );
		setStatus( `精修完成 score ${ first.score.toFixed( 3 ) } → ${ last.score.toFixed( 3 ) }`, 'ok' );
	} catch ( e ) {
		console.error( e );
		setStatus( `精修失敗：${ e.message }`, 'warn' );
	} finally { $( 'btnRefine' ).disabled = false; }
}

function loadRefFile( file ) {
	const url = URL.createObjectURL( file );
	const img = new Image();
	img.onload = () => {
		ref.img = img;
		ref.pixels = imageToPixels( img, 256 );
		$( 'btnAnalyze' ).disabled = false;
		analyze();
		setStatus( `參考圖：${ file.name } (${ img.naturalWidth }×${ img.naturalHeight })`, 'ok' );
	};
	img.onerror = () => setStatus( '無法讀取圖片', 'warn' );
	img.src = url;
}

function setupRefPanel() {
	const drop = $( 'drop' ), file = $( 'file' );
	drop.addEventListener( 'click', () => file.click() );
	file.addEventListener( 'change', () => file.files[ 0 ] && loadRefFile( file.files[ 0 ] ) );
	[ 'dragenter', 'dragover' ].forEach( ( ev ) => drop.addEventListener( ev, ( e ) => { e.preventDefault(); drop.classList.add( 'over' ); } ) );
	[ 'dragleave', 'drop' ].forEach( ( ev ) => drop.addEventListener( ev, ( e ) => { e.preventDefault(); drop.classList.remove( 'over' ); } ) );
	drop.addEventListener( 'drop', ( e ) => { const f = e.dataTransfer.files[ 0 ]; if ( f ) loadRefFile( f ); } );
	window.addEventListener( 'dragover', ( e ) => e.preventDefault() );
	window.addEventListener( 'drop', ( e ) => { e.preventDefault(); const f = e.dataTransfer?.files?.[ 0 ]; if ( f && f.type.startsWith( 'image/' ) ) loadRefFile( f ); } );

	const bindRange = ( id, valId, autoId ) => {
		const el = $( id );
		el.addEventListener( 'input', () => { $( valId ).textContent = ( + el.value ).toFixed( 2 ); analyze(); } );
		if ( autoId ) $( autoId ).addEventListener( 'change', () => { el.disabled = $( autoId ).checked; analyze(); } );
	};
	bindRange( 'fov', 'fovVal' );
	$( 'fov' ).addEventListener( 'input', () => { $( 'fovVal' ).textContent = $( 'fov' ).value; } );
	bindRange( 'horizon', 'horizonVal', 'horizonAuto' );
	bindRange( 'threshold', 'thresholdVal', 'thresholdAuto' );
	$( 'camYaw' ).addEventListener( 'change', analyze );
	$( 'btnAnalyze' ).addEventListener( 'click', applyFit );
	$( 'btnRefine' ).addEventListener( 'click', doRefine );
	$( 'btnMask' ).addEventListener( 'click', () => { ref.showMask = ! ref.showMask; analyze(); } );
	$( 'btnRefToggle' ).addEventListener( 'click', () => { const p = $( 'refPanel' ); p.style.display = p.style.display === 'none' ? '' : 'none'; } );
}

// ------------------------------------------------------------------------------------------ play
function updatePlayButton() { $( 'btnPlay' ).textContent = sky.params.animation.paused ? '▶ 播放' : '⏸ 暫停'; }
function togglePlay() { sky.params.animation.paused = ! sky.params.animation.paused; updatePlayButton(); refreshGui(); }
$( 'btnPlay' ).addEventListener( 'click', togglePlay );

// ------------------------------------------------------------------------------------------ boot
async function boot() {
	$( 'projectLabel' ).textContent = project ? `專案 project: ${ project }` : '（無專案 / standalone）';
	const loaded = await loadFromProject();
	if ( ! loaded ) applyPreset( qs.get( 'preset' ) || 'fair-cumulus' );
	else if ( qs.get( 'preset' ) ) applyPreset( qs.get( 'preset' ) );
	if ( qs.get( 'quality' ) ) applyQuality( qs.get( 'quality' ) );
	if ( qs.get( 'paused' ) === '1' ) sky.params.animation.paused = true;
	if ( qs.get( 'time' ) ) sky.setTime( + qs.get( 'time' ) );
	for ( const k of [ 'yaw', 'pitch', 'fov' ] ) if ( qs.get( k ) !== null ) sky.params.view[ k ] = + qs.get( k );
	sky.syncUniforms();
	buildGui();
	setupRefPanel();
	updatePlayButton();
	if ( qs.get( 'ui' ) === '0' ) {
		for ( const id of [ 'topbar', 'refPanel', 'status', 'hint' ] ) $( id ).style.display = 'none';
		gui.hide();
	}
	if ( ! project ) setStatus( '單機模式：儲存/烘焙會改為下載檔案。加上 ?project=<name> 可直接寫入專案。' );

	const timer = new THREE.Timer();
	let frames = 0;
	const loop = () => {
		requestAnimationFrame( loop );
		timer.update(); const dt = Math.min( timer.getDelta(), 0.1 );
		sky.update( dt, camera );
		updateCamera();
		applyLights();
		renderer.render( scene, camera );
		if ( ( frames ++ & 7 ) === 0 ) $( 'timeLabel' ).textContent = `t = ${ sky.time.toFixed( 1 ) } s`;
		window.__frames = frames;
	};
	loop();
}

window.__sky = {
	sky, renderer, camera, applyPreset, applyQuality,
	analyze: ( pixels, opts ) => analyzeImage( pixels, opts ),
	refine: ( stats, opts ) => refineFit( sky, stats, opts ),
	bake: ( opts ) => bakeEquirect( sky, opts ),
};

boot().catch( ( e ) => { console.error( e ); setStatus( '啟動失敗：' + e.message, 'warn' ); } );
