// Optional reference layer: Google Photorealistic 3D Tiles, clipped to the box.
// Streams with YOUR Map Tiles API key; Google's terms forbid exporting/caching this
// geometry, so it is display-only (attribution shown). Geometry for Houdini comes from OSM.
import * as THREE from 'three';

export class Google3DTiles {
  constructor(renderer, camera, parent) {
    this.renderer = renderer;
    this.camera = camera;
    this.parent = parent;
    this.tiles = null;
    this.offsetY = 0;
    this.opacity = 1;
    this.clipPlanes = [];
    // ReorientationPlugin yields X=west, Z=north at the origin; rotate 180° to X=east, -Z=north.
    this.wrapper = new THREE.Group();
    this.wrapper.rotation.y = Math.PI;
    this.wrapper.name = 'google3dtiles';
    parent.add(this.wrapper);
  }

  setBox(rect) {
    const [x0, z0, x1, z1] = rect;
    this.clipPlanes = [
      new THREE.Plane(new THREE.Vector3(1, 0, 0), -x0), new THREE.Plane(new THREE.Vector3(-1, 0, 0), x1),
      new THREE.Plane(new THREE.Vector3(0, 0, 1), -z0), new THREE.Plane(new THREE.Vector3(0, 0, -1), z1),
    ];
    this._applyMaterials();
  }

  async enable({ apiKey, lat, lon, height = 0 }) {
    this.disable();
    const { TilesRenderer } = await import('3d-tiles-renderer');
    const { GoogleCloudAuthPlugin, ReorientationPlugin, GLTFExtensionsPlugin, TileCompressionPlugin } = await import('3d-tiles-renderer/plugins');
    const { DRACOLoader } = await import('three/addons/loaders/DRACOLoader.js');
    const draco = new DRACOLoader().setDecoderPath('./vendor/three/examples/jsm/libs/draco/gltf/');
    const tiles = new TilesRenderer();
    tiles.registerPlugin(new GoogleCloudAuthPlugin({ apiToken: apiKey, autoRefreshToken: true }));
    tiles.registerPlugin(new GLTFExtensionsPlugin({ dracoLoader: draco }));
    tiles.registerPlugin(new TileCompressionPlugin());
    tiles.registerPlugin(new ReorientationPlugin({ lat: THREE.MathUtils.degToRad(lat), lon: THREE.MathUtils.degToRad(lon), height, recenter: true }));
    tiles.errorTarget = 6;
    tiles.setCamera(this.camera);
    tiles.setResolutionFromRenderer(this.camera, this.renderer);
    tiles.addEventListener('load-model', ({ scene }) => this._prepare(scene));
    this.renderer.localClippingEnabled = true;
    this.wrapper.add(tiles.group);
    this.tiles = tiles;
    this.setOffset(this.offsetY);
  }

  _prepare(root) {
    root.traverse((o) => {
      if (!o.material) return;
      o.material.clippingPlanes = this.clipPlanes;
      o.material.transparent = this.opacity < 1;
      o.material.opacity = this.opacity;
      o.material.depthWrite = this.opacity >= 1;
      o.receiveShadow = true;
    });
  }

  _applyMaterials() {
    if (this.tiles) this._prepare(this.tiles.group);
  }

  setOffset(y) { this.offsetY = y; this.wrapper.position.y = y; }
  setOpacity(a) { this.opacity = a; this._applyMaterials(); }

  attributions() {
    if (!this.tiles || !this.tiles.getAttributions) return '';
    try { return this.tiles.getAttributions().map((a) => (a.type === 'string' ? a.value : '')).filter(Boolean).join(' · '); } catch { return ''; }
  }

  update() {
    if (!this.tiles) return;
    this.camera.updateMatrixWorld();
    this.tiles.setResolutionFromRenderer(this.camera, this.renderer);
    this.tiles.update();
  }

  disable() {
    if (!this.tiles) return;
    this.wrapper.remove(this.tiles.group);
    this.tiles.dispose();
    this.tiles = null;
  }
}
