// GeoBox Sky — presets (partial sky_params, merged over DEFAULT_SKY_PARAMS).

export const SKY_PRESETS = {
	'clear-noon': {
		name: 'clear-noon',
		sun: { azimuth: 170, elevation: 68, intensity: 3.2, glow: 0.6 },
		atmosphere: { physicalMix: 0.75, zenithColor: '#2f6fd6', horizonColor: '#bcd3ee', mie: 0.8 },
		clouds: { enabled: false, coverage: 0.0 },
		cirrus: { enabled: true, coverage: 0.12, opacity: 0.35 },
	},
	'fair-cumulus': {
		name: 'fair-cumulus',
		sun: { azimuth: 140, elevation: 42, intensity: 3.0 },
		atmosphere: { physicalMix: 0.6 },
		clouds: { enabled: true, coverage: 0.42, type: 0.9, density: 1.1, bottom: 1400, top: 4000, scale: 1.0, detailStrength: 0.35 },
		cirrus: { enabled: true, coverage: 0.18, opacity: 0.35 },
	},
	'overcast-stratus': {
		name: 'overcast-stratus',
		sun: { azimuth: 160, elevation: 35, intensity: 2.2, glow: 0.2 },
		atmosphere: { physicalMix: 0.3, zenithColor: '#8c98a8', horizonColor: '#b8bec6', skyIntensity: 0.55 },
		clouds: {
			enabled: true, coverage: 0.93, type: 0.08, density: 0.7, bottom: 900, top: 2200, scale: 1.6,
			detailStrength: 0.25, coverageVariation: 0.15, shadowColor: '#8e97a3', ambient: 1.3, powder: 0.2,
		},
		cirrus: { enabled: false },
		post: { saturation: 0.85 },
	},
	'golden-hour': {
		name: 'golden-hour',
		sun: { azimuth: 255, elevation: 4.5, color: '#fff4ea', intensity: 4.0, glow: 2.2, glowSharpness: 7 },
		atmosphere: { physicalMix: 0.85, physicalIntensity: 1.2, mie: 1.6, zenithColor: '#4467a8', horizonColor: '#f2b27a', sunReddening: 1.0 },
		clouds: {
			enabled: true, coverage: 0.38, type: 0.55, density: 0.9, bottom: 1800, top: 3800, scale: 1.3,
			shadowColor: '#5b5878', silverIntensity: 1.0,
		},
		cirrus: { enabled: true, coverage: 0.35, opacity: 0.7, color: '#ffe9d6' },
		post: { exposure: 1.15, saturation: 1.1 },
	},
	'cirrus-high': {
		name: 'cirrus-high',
		sun: { azimuth: 120, elevation: 50 },
		atmosphere: { physicalMix: 0.7, mie: 0.7 },
		clouds: { enabled: true, coverage: 0.12, type: 0.8, scale: 0.8 },
		cirrus: { enabled: true, coverage: 0.7, opacity: 0.8, stretch: 3.5, scale: 1.2, altitude: 9500 },
	},
	stormy: {
		name: 'stormy',
		sun: { azimuth: 200, elevation: 25, intensity: 2.0, glow: 0.3 },
		atmosphere: { physicalMix: 0.2, zenithColor: '#66717e', horizonColor: '#a3abb4', skyIntensity: 1.3, mie: 2.5 },
		clouds: {
			enabled: true, coverage: 0.72, type: 0.95, density: 1.8, bottom: 900, top: 6500, scale: 1.4,
			detailStrength: 0.45, coverageVariation: 0.45, shadowColor: '#4a5562', ambient: 1.1, litIntensity: 1.0,
			extinction: 0.05,
		},
		cirrus: { enabled: false },
		post: { exposure: 1.15, saturation: 0.8, contrast: 1.1 },
	},
};

export const PRESET_NAMES = Object.keys( SKY_PRESETS );
