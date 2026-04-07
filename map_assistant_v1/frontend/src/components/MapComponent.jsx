import React, { useEffect, useRef, useCallback } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import * as EL from 'esri-leaflet';

// ✅ 修复 Leaflet 默认 marker 图标路径（移除多余空格）
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon-2x.png',
  iconUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon.png',
  shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-shadow.png',
});

class MapManager {
  constructor(containerId, options = {}) {
    this.containerId = containerId;
    this.map = null;
    this.markers = [];
    this.layers = {};
    this.overlays = {};
    this.currentBaseLayer = null;
    this.options = {
      center: [34.7, 113.6],
      zoom: 7,
      ...options
    };
    this.visibilityThresholds = {
      caiqu: 11,
      henanBaseMap: 12
    };
    this.markingMode = false;
    this.manualMarkers = L.layerGroup();
    this.onLayerStatus = typeof this.options.onLayerStatus === 'function' ? this.options.onLayerStatus : null;
  }

  initMap() {
    if (this.map) {
      this.map.remove();
    }

    this.map = L.map(this.containerId, {
      center: this.options.center,
      zoom: this.options.zoom,
      zoomControl: true,
      zoomAnimation: true,
      markerZoomAnimation: true,
      updateWhenZooming: true,
      preferCanvas: true
    });

    this.layers = {};
    this.overlays = {};

    // 初始化图层组
    this.manualMarkers.addTo(this.map);
    
    // 添加底图
    this.addSatelliteLayer();
    this.addOSMLayer();
    this.addArcGis3857Layer();

    // 初始化业务图层组
    this.overlays.caiqu = L.layerGroup();
    this.overlays.henanBaseMap = L.layerGroup();
    
    this.addLocalGeoJSONLayer();
    this.addHXLayer();

    // 1. 先设置并添加底图（最底层）
    this.currentBaseLayer = this.layers.satellite;
    this.currentBaseLayer.addTo(this.map);

    // 添加交互工具
    this.addTaggingControl();

    // 添加点击事件
    this.map.on('click', (e) => {
      if (this.markingMode) {
        const { lat, lng } = e.latlng;
        const marker = L.marker([lat, lng]).addTo(this.manualMarkers);
        const popupContent = `
          <div style="font-family: sans-serif;">
            <b style="color: #2c3e50;">手动标记点</b><br/>
            <span style="color: #7f8c8d;">纬度:</span> ${lat.toFixed(6)}<br/>
            <span style="color: #7f8c8d;">经度:</span> ${lng.toFixed(6)}
          </div>
        `;
        marker.bindPopup(popupContent).openPopup();
      }
    });

    // 添加缩放/拖拽事件的轻量防抖示例（可用于更新轻量 UI）
    const debounce = (fn, wait = 150) => {
      let t = null;
      return (...args) => {
        if (t) clearTimeout(t);
        t = setTimeout(() => fn(...args), wait);
      };
    };
    const onMoveEnd = debounce(() => {
      // 预留位置：此处仅作为节流示例，不做重逻辑
    }, 150);
    const onZoomEnd = debounce(() => {
      // 预留位置：此处仅作为节流示例，不做重逻辑
    }, 150);
    this.map.on('moveend', onMoveEnd);
    this.map.on('zoomend', onZoomEnd);

    // 2. 添加图层组到地图
    this.overlays.henanBaseMap.addTo(this.map);
    this.overlays.caiqu.addTo(this.map);

    const baseLayers = {
      '卫星影像': this.layers.satellite,
      '高分影像(ArcGIS)': this.layers.arcgis
    };

    const overlays = {
      '采区边界': this.overlays.caiqu,
      '红线底图': this.overlays.henanBaseMap,
    };

    L.control.layers(baseLayers, overlays, { position: 'topleft' }).addTo(this.map);

    const xinyangBounds = L.latLngBounds([31.5, 113.5], [32.7, 115.2]);
    this.map.fitBounds(xinyangBounds.pad(0.05));

    if (!L.vectorGrid) {
      const s = document.createElement('script');
      s.src = 'https://unpkg.com/leaflet.vectorgrid/dist/Leaflet.VectorGrid.bundled.js';
      s.defer = true;
      document.head.appendChild(s);
    }

    return this.map;
  }

  emitLayerStatus(status, detail = {}) {
    if (this.onLayerStatus) {
      this.onLayerStatus({ status, ...detail });
    }
  }

  shouldShowLayer(layerKey, zoomLevel) {
    const threshold = this.visibilityThresholds?.[layerKey];
    if (typeof threshold !== 'number') {
      return true;
    }
    return zoomLevel >= threshold;
  }

  addSatelliteLayer() {
    const satelliteLayer = L.tileLayer(
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      {
        attribution: '&copy; <a href="https://www.esri.com/">Esri</a>',
        maxZoom: 18,
        maxNativeZoom: 18,
        updateWhenIdle: false,
        keepBuffer: 2,
        crossOrigin: 'anonymous'
      }
    );
    this.layers.satellite = satelliteLayer;
  }


  addOSMLayer() {
    if (this.layers.osm) return;
    const osmLayer = L.tileLayer(
      'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 18,
        maxNativeZoom: 18,
        updateWhenIdle: false,
        keepBuffer: 2,
        crossOrigin: 'anonymous'
      }
    );
    this.layers.osm = osmLayer;
  }

  addArcGis3857Layer() {
    const base = this.options.arcgisBaseUrl || 'http://123.149.20.94:60805/arcgis/rest/services/%E9%AB%98%E5%88%86%E5%BD%B1%E5%83%8F/GF_2024_YM';
    const tpl = base.replace(/\/+$/, '') + '/MapServer/tile/{z}/{y}/{x}';
    const layer = L.tileLayer(tpl, {
      attribution: 'ArcGIS',
      maxZoom: 23,
      maxNativeZoom: 23,
      updateWhenIdle: false,
      keepBuffer: 2,
      crossOrigin: 'anonymous'
    });
    layer.on('tileerror', (e) => {
      console.warn('ArcGIS瓦片加载失败:', e.url);
    });
    this.layers.arcgis = layer;
    try {
      const infoUrl = base.replace(/\/+$/, '') + '/MapServer?f=json';
      fetch(infoUrl)
        .then(r => r.ok ? r.json() : null)
        .then(j => {
          if (!j) return;
          const lods = (j.tileInfo && j.tileInfo.lods) || [];
          if (Array.isArray(lods) && lods.length > 0) {
            const mz = lods.length - 1;
            layer.options.maxZoom = mz;
            layer.options.maxNativeZoom = mz;
          }
          const fe = j.fullExtent;
          if (fe && this.map && L && L.CRS && L.CRS.EPSG3857) {
            const sw = L.CRS.EPSG3857.unproject(L.point(fe.xmin, fe.ymin));
            const ne = L.CRS.EPSG3857.unproject(L.point(fe.xmax, fe.ymax));
            const b = L.latLngBounds(sw, ne);
            this.map.fitBounds(b.pad(0.05));
          }
        })
        .catch(() => {});
    } catch (_) {}
  }

  addLocalGeoJSONLayer() {
    const url = '/data/caiqu.geojson';
    if (this.overlays.caiqu) {
      this.overlays.caiqu.clearLayers();
    }

    fetch(url)
      .then(res => {
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        return res.json();
      })
      .then(geojson => {
        const geoJsonLayer = L.geoJSON(geojson, {
          style: {
            color: '#2773d7ff',
            weight: 2,
            fillColor: '#fee8c8',
            fillOpacity: 0.6,
          },
          onEachFeature: (feature, layer) => {
            const name = feature.properties?.name || '采区';
            layer.bindPopup(`<b>${name}</b>`);
          }
        });
        this.overlays.caiqu.addLayer(geoJsonLayer);
      })
      .catch(err => {
        console.error('加载采区 GeoJSON 失败:', err);
        alert(`加载“采区边界”图层失败：${err.message}`);
      });
  }

  addHXLayer() {
    const url = '/data/hx.geojson';
    if (this.overlays.henanBaseMap) {
      this.overlays.henanBaseMap.clearLayers();
    }

    fetch(url)
      .then(res => {
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        return res.json();
      })
      .then(geojson => {
        const geoJsonLayer = L.geoJSON(geojson, {
          style: {
            color: '#ff0000', // 渲染成红色
            weight: 2,
            fillColor: '#ff0000',
            fillOpacity: 0.3,
          },
          onEachFeature: (feature, layer) => {
            const name = feature.properties?.name || '红线区域';
            layer.bindPopup(`<b>${name}</b>`);
          }
        });
        this.overlays.henanBaseMap.addLayer(geoJsonLayer);
      })
      .catch(err => {
        console.error('加载红线 GeoJSON 失败:', err);
      });
  }

  addTaggingControl() {
    const TaggingControl = L.Control.extend({
      options: {
        position: 'topleft'
      },
      onAdd: (map) => {
        const container = L.DomUtil.create('div', 'leaflet-bar leaflet-control leaflet-tagging-control');
        container.style.backgroundColor = 'white';
        container.style.display = 'flex';
        container.style.flexDirection = 'column';

        const toggleBtn = L.DomUtil.create('a', '', container);
        toggleBtn.innerHTML = '📍';
        toggleBtn.href = '#';
        toggleBtn.title = '打点标记工具';
        toggleBtn.style.fontSize = '18px';
        toggleBtn.style.display = 'flex';
        toggleBtn.style.alignItems = 'center';
        toggleBtn.style.justifyContent = 'center';

        const clearBtn = L.DomUtil.create('a', '', container);
        clearBtn.innerHTML = '🗑️';
        clearBtn.href = '#';
        clearBtn.title = '清除所有标记';
        clearBtn.style.fontSize = '16px';
        clearBtn.style.display = 'flex';
        clearBtn.style.alignItems = 'center';
        clearBtn.style.justifyContent = 'center';

        L.DomEvent.disableClickPropagation(container);

        L.DomEvent.on(toggleBtn, 'click', (e) => {
          L.DomEvent.stop(e);
          this.markingMode = !this.markingMode;
          toggleBtn.style.backgroundColor = this.markingMode ? '#e1f5fe' : 'white';
          toggleBtn.style.color = this.markingMode ? '#03a9f4' : 'black';
          map.getContainer().style.cursor = this.markingMode ? 'crosshair' : '';
        });

        L.DomEvent.on(clearBtn, 'click', (e) => {
          L.DomEvent.stop(e);
          this.manualMarkers.clearLayers();
        });

        return container;
      }
    });

    new TaggingControl().addTo(this.map);
  }

  executeMapCommand(command) {
    if (!this.map || !command) return;

    const cmdType = command.type || command.command;

    switch (cmdType) {
      case 'load_vector_layer':
        this.addVectorLayerFromAPI(command.url, command.name);
        break;
      case 'load_vector_tile_layer':
        this.addVectorTileLayerFromAPI(command.url, command.name, command.style);
        break;
      case 'add_marker':
        this.addMarkerWithPopup(
          command.lat,
          command.lng,
          command.popup || command.title || '标记点'
        );
        break;
      case 'clear_markers':
        this.clearMarkers();
        break;
      case 'set_view':
        this.setView(command.lat, command.lng, command.zoom || this.options.zoom);
        break;
      case 'fit_markers':
        this.fitMarkers();
        break;
      case 'switch_layer':
        if (this.currentBaseLayer) {
          this.map.removeLayer(this.currentBaseLayer);
        }
        let newLayer;
        if (command.layer === 'satellite') {
          newLayer = this.layers.satellite;
        } else if (command.layer === 'arcgis') {
          newLayer = this.layers.arcgis;
        } else {
          newLayer = this.layers.osm;
        }

        if (newLayer) {
          newLayer.addTo(this.map);
          this.currentBaseLayer = newLayer;
        }
        break;
      default:
        console.warn('未知的地图命令:', cmdType);
    }
  }

  addVectorLayerFromAPI(url, layerName = '矢量数据') {
    if (this.overlays[layerName]) {
      this.map.removeLayer(this.overlays[layerName]);
    }

    let fullUrl = url;
    if (url.startsWith('/')) {
      fullUrl = url;
    } else if (!/^https?:\/\//.test(url)) {
      const origin = typeof window !== 'undefined' ? window.location.origin : '';
      fullUrl = origin ? origin.replace(/\/+$/, '') + '/' + url.replace(/^\/+/, '') : '/' + url.replace(/^\/+/, '');
    }

    console.log('【调试】加载矢量图层 URL:', fullUrl);
    this.emitLayerStatus('loading', { layerName, url: fullUrl });

    fetch(fullUrl)
      .then(res => {
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
        return res.json();
      })
      .then(geojson => {
        let normalized = geojson;
        if (typeof normalized === 'string') {
          try {
            normalized = JSON.parse(normalized);
          } catch (e) {
            normalized = null;
          }
        }
        if (!normalized || typeof normalized !== 'object') {
          this.emitLayerStatus('error', { layerName, url: fullUrl, message: '返回数据格式异常' });
          throw new Error('返回数据格式异常');
        }
        if (!Array.isArray(normalized.features)) {
          normalized.features = [];
        }
        if (geojson && (geojson._error || geojson._debug)) {
          const dbg = geojson._debug || {};
          const err = geojson._error ? `错误: ${geojson._error}` : '';
          const stats = (dbg.matched_total !== undefined || dbg.geom_valid_total !== undefined || dbg.lonlat_total !== undefined)
            ? `匹配数=${dbg.matched_total ?? 'N/A'}，有效几何数=${dbg.geom_valid_total ?? 'N/A'}，经纬度数=${dbg.lonlat_total ?? 'N/A'}`
            : '';
          const applied = dbg.applied_filter ? `过滤条件: ${dbg.applied_filter}` : '';
          const msg = [err, stats, applied].filter(Boolean).join('\n');
          if (msg) {
            console.warn(msg);
          }
        }
        const featureCount = normalized.features.length;
        if (normalized._error) {
          this.emitLayerStatus('error', { layerName, url: fullUrl, message: normalized._error });
        } else if (featureCount === 0) {
          try {
            const hasRetried = /[?&]retry=1(&|$)/.test(fullUrl);
            if (!hasRetried) {
              const u = new URL(fullUrl, window.location.origin);
              const f = u.searchParams.get('filter') || '';
              const eqMatch = f.match(/\"Mineable_Area_Name\"\\s*=\\s*'([^']+)'/);
              const simpleName = eqMatch ? eqMatch[1] : layerName;
              u.searchParams.set('filter', `"Mineable_Area_Name"='${simpleName}'`);
              u.searchParams.set('retry', '1');
              const retryUrl = u.pathname + '?' + u.searchParams.toString();
              this.addVectorLayerFromAPI(retryUrl, layerName);
              return;
            }
          } catch (_) {}
          const meta = normalized.meta || {};
          this.emitLayerStatus('empty', {
            layerName,
            url: fullUrl,
            featureCount,
            message: meta.message || `图层 ${layerName} 无可用要素`
          });
        } else {
          this.emitLayerStatus('success', { layerName, url: fullUrl, featureCount });
        }
        const layer = L.geoJSON(normalized, {
          pointToLayer: (feature, latlng) => {
            const props = feature.properties || {};
            let fillColor = props._style_color;
            
            // 如果后端没有提供颜色，前端根据逻辑计算
            if (!fillColor) {
              const depth = props.Measured_Depth || props.measured_depth || props.h || props.depth || props.value;
              const control = props.Control_Elevation || props.control_elevation || props.control;
              if (depth !== undefined && control !== undefined) {
                // 业务逻辑：测量值（高程） < 控制高程 -> 已超深(红色)；测量值 >= 控制高程 -> 未超深(绿色)
                fillColor = Number(depth) < Number(control) ? "red" : "green";
              } else {
                fillColor = "#cc0000ff"; // 默认红色
              }
            }
            
            return L.circleMarker(latlng, {
              radius: 6,
              fillColor: fillColor,
              color: "#fff",
              weight: 1,
              fillOpacity: 0.8
            });
          },
          onEachFeature: (feature, layer) => {
            const props = feature.properties || {};
            console.log('【调试】要素属性:', props);
            
            // 动态识别坐标字段（兼容大小写）
            const lon = props.Lon_4326 || props.lon_4326 || props.lon || props.x || 0;
            const lat = props.Lat_4326 || props.lat_4326 || props.lat || props.y || 0;
            const coordText = `(${Number(lon).toFixed(6)}, ${Number(lat).toFixed(6)})`;
            
            // 动态识别深度/高程字段（兼容大小写）
            const depth = props.Measured_Depth || props.measured_depth || props.h || props.depth || props.value;
            const control = props.Control_Elevation || props.control_elevation || props.control;
            
            let depthText = '';
            if (depth !== undefined) {
              // 业务逻辑：测量值（高程） < 控制高程 -> 已超深(红色)；测量值 >= 控制高程 -> 未超深(绿色)
              const isOverDeep = control !== undefined && Number(depth) < Number(control);
              const status = control !== undefined 
                ? (isOverDeep ? ' <span style="color:red; font-weight:bold;">(已超深)</span>' : ' <span style="color:green; font-weight:bold;">(未超深)</span>')
                : '';
              depthText = `<br><b>实测高程:</b> ${Number(depth).toFixed(3)} m${status}`;
            }
            
            if (control !== undefined) {
              depthText += `<br><b>红线标准(控制高程):</b> ${Number(control).toFixed(3)} m`;
            }
            
            // 其他常用字段
            const areaName = props.Mineable_Area_Name || props.mineable_area_name || props.name || layerName;
            const areaId = props.Mineable_Area_ID || props.mineable_area_id || '';
            const county = props.County_District || props.county_district || '';
            const year = (props.Year || props.year) ? `<br><b>年份:</b> ${props.Year || props.year}` : '';
            
            const idText = areaId ? `<br><b>许可证号:</b> ${areaId}` : '';
            const countyText = county ? `<br><b>所属县区:</b> ${county}` : '';
            
            const popupContent = `<b>${areaName}</b>${idText}${countyText}<br><b>坐标:</b> ${coordText}${depthText}${year}`;
            layer.bindPopup(popupContent);
            
            // 移除或优化注记 (Tooltip)，不再始终显示
            layer.bindTooltip(`${areaName}`, {
              permanent: false, // 改为仅在悬停时显示
              direction: 'top',
              offset: [0, -5],
              className: 'map-annotation'
            });
          }
        });

        layer.addTo(this.map);
        this.overlays[layerName] = layer;

        // 自动缩放
        if (normalized.features?.length > 0) {
          const bounds = L.geoJSON(normalized).getBounds();
          this.map.fitBounds(bounds.pad(0.1));
        } else {
          console.warn(`图层 ${layerName} 无可用要素，保持当前视图。`);
        }
      })
      .catch(err => {
        console.error('加载矢量数据失败:', err);
        this.emitLayerStatus('error', { layerName, url: fullUrl, message: err.message });
      });
  }

  addVectorTileLayerFromAPI(urlTemplate, layerName = 'mvt', style = {}) {
    if (!L.vectorGrid || !L.vectorGrid.protobuf) return;
    if (this.overlays[layerName]) {
      this.map.removeLayer(this.overlays[layerName]);
    }
    const BACKEND_BASE = 'http://172.136.16.14:8006';
    let tpl = urlTemplate;
    if (urlTemplate.startsWith('/')) {
      tpl = BACKEND_BASE + urlTemplate;
    } else if (!urlTemplate.startsWith('http')) {
      tpl = BACKEND_BASE + '/' + urlTemplate;
    }
    const defaultStyle = {
      weight: 1,
      color: '#2773d7',
      fillColor: '#fee8c8',
      fillOpacity: 0.6
    };
    const layer = L.vectorGrid.protobuf(tpl, {
      maxNativeZoom: 18,
      rendererFactory: L.canvas.tile,
      vectorTileLayerStyles: {
        [layerName]: Object.assign({}, defaultStyle, style),
        '*': Object.assign({}, defaultStyle, style)
      },
      interactive: true,
    });
    layer.addTo(this.map);
    this.overlays[layerName] = layer;
  }

  clearMarkers() {
    this.markers.forEach(marker => this.map.removeLayer(marker));
    this.markers = [];
  }

  clearAllLayers() {
    this.markers.forEach(marker => this.map.removeLayer(marker));
    this.markers = [];
    Object.keys(this.overlays).forEach(key => {
      if (this.overlays[key]) {
        this.overlays[key].clearLayers();
        if (this.map.hasLayer(this.overlays[key])) {
          this.map.removeLayer(this.overlays[key]);
        }
      }
    });
  }

  addMarker(lat, lng, options = {}) {
    const marker = L.marker([lat, lng], options).addTo(this.map);
    this.markers.push(marker);
    return marker;
  }

  addMarkerWithPopup(lat, lng, popupContent, options = {}) {
    const marker = this.addMarker(lat, lng, options);
    marker.bindPopup(popupContent);
    return marker;
  }

  setView(lat, lng, zoom = this.options.zoom) {
    if (this.map) this.map.setView([lat, lng], zoom);
  }

  fitMarkers() {
    if (this.markers.length > 0) {
      const group = L.featureGroup(this.markers);
      this.map.fitBounds(group.getBounds().pad(0.1));
    }
  }

  getMap() {
    return this.map;
  }

  destroy() {
    if (this.map) {
      this.map.remove();
      this.map = null;
    }
  }
}

const MapComponent = ({ onMapReady, onLayerStatus, className = '', options = {} }) => {
  const mapRef = useRef(null);
  const mapManagerRef = useRef(null);
  const isInitializedRef = useRef(false);

  const stableOnMapReady = useCallback(
    (mapManagerInstance) => {
      if (onMapReady && typeof onMapReady === 'function') {
        onMapReady(mapManagerInstance);
      }
    },
    [onMapReady]
  );

  useEffect(() => {
    if (mapRef.current && !isInitializedRef.current) {
      isInitializedRef.current = true;
      try {
        const manager = new MapManager(mapRef.current, { ...options, onLayerStatus });
        mapManagerRef.current = manager;
        manager.initMap();
        stableOnMapReady(manager);
      } catch (error) {
        console.error('地图初始化失败:', error);
        isInitializedRef.current = false;
      }
    }

    return () => {
      if (mapManagerRef.current) {
        try {
          mapManagerRef.current.destroy();
        } catch (error) {
          console.error('地图销毁出错:', error);
        }
        mapManagerRef.current = null;
        isInitializedRef.current = false;
      }
    };
  }, []);

  return (
    <div
      ref={mapRef}
      className={`map-container ${className}`}
      style={{ width: '100%', height: '100%' }}
    />
  );
};

export default MapComponent;
export { MapManager };
