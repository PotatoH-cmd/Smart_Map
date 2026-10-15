// ============================================================
// Cesium 底图/叠加层/倾斜摄影预设与底图加载（自 CesiumComponent.jsx 拆分）
// ============================================================
import * as Cesium from 'cesium';

// ==================== 底图加载（Esri World Imagery）====================

const BASEMAP_PRESETS = {
  satellite: {
    label: '卫星影像',
    providers: [{
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      credit: 'Esri World Imagery',
    }],
  },
  street: {
    label: '街道地图',
    providers: [{
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
      credit: 'Esri World Street Map',
    }],
  },
  hybrid: {
    label: '影像注记',
    providers: [
      {
        url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        credit: 'Esri World Imagery',
      },
      {
        url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
        credit: 'Esri Reference',
      },
    ],
  },
  topo: {
    label: '地形图',
    providers: [{
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
      credit: 'Esri World Topo',
    }],
  },
  jcdt: {
    label: '基础底图',
    type: 'arcgisMapServer',
    url: 'http://gis95.yskc.com/server/rest/services/河南省基础底图/HNS_JCDT_SL/MapServer',
    credit: '基础底图',
  },
  gf2024: {
    label: '2024年高分影像',
    providers: [{
      url: '/proxy/gf-tiles/{z}/{y}/{x}',
      credit: '2024年高分影像',
    }],
  },
  gf2025: {
    label: '2025年高分影像',
    providers: [{
      url: '/proxy/gf2025-tiles/{z}/{y}/{x}',
      credit: '2025年高分影像',
    }],
  },
};

const OVERLAY_PRESETS = {
  hx: {
    label: '河道红线',
    name: 'overlay-hx',
    url: '/api/overlay_tile/hx/{z}/{x}/{y}.png',
    swatch: '#ef4444',
    minLevel: 0,
    maxLevel: 18,
  },
  caiqu: {
    label: '2025年采区边界',
    name: 'overlay-caiqu',
    url: '/api/overlay_tile/caiqu/{z}/{x}/{y}.png',
    swatch: '#38bdf8',
    minLevel: 0,
    maxLevel: 18,
  },
};

const OBLIQUE_TILESETS = {
  'qx-dyt': {
    label: '大云台倾斜影像',
    url: 'http://data.mars3d.cn/3dtiles/qx-dyt/tileset.json',
    name: '大云台倾斜影像',
    autoGroundClamp: false,
  },
};

function loadBasemap(viewer, type) {
  if (!viewer || viewer.isDestroyed()) return;
  const preset = BASEMAP_PRESETS[type] || BASEMAP_PRESETS.satellite;
  viewer.imageryLayers.removeAll();

  if (preset.type === 'arcgisMapServer') {
    Cesium.ArcGisMapServerImageryProvider.fromUrl(preset.url, {
      credit: preset.credit || '',
    }).then(provider => {
      if (!viewer.isDestroyed()) {
        viewer.imageryLayers.addImageryProvider(provider);
      }
    }).catch(err => {
      console.error('[Cesium] ArcGIS MapServer 底图加载失败:', preset.url, err);
    });
  } else {
    preset.providers.forEach(p => {
      viewer.imageryLayers.addImageryProvider(
        new Cesium.UrlTemplateImageryProvider({
          url: p.url,
          tilingScheme: new Cesium.WebMercatorTilingScheme(),
          maximumLevel: p.maxLevel || 19,
          credit: p.credit,
        })
      );
    });
  }
  console.log('[Cesium] 底图已加载:', type || 'satellite');
}

export { BASEMAP_PRESETS, OVERLAY_PRESETS, OBLIQUE_TILESETS, loadBasemap };
